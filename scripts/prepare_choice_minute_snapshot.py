#!/usr/bin/env python3
"""Collect Choice CMC data and publish a validated one-minute snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from contextlib import suppress
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from EmQuantAPI import c

from ashare_lab.adapters.market_data.choice_minute import (
    canonical_payload_sha256,
    classify_choice_error,
    compare_cmc_prefix,
    decode_cmc_batch,
    decode_csd_batch,
    infer_timestamp_semantics,
    reconcile_minute_daily,
    serialize_sdk_result,
)
from ashare_lab.adapters.market_data.choice_minute_snapshot import (
    ChoiceMinuteSnapshotSpec,
    build_choice_minute_snapshot,
)

EXECUTION_INDICATORS = ("OPEN", "HIGH", "LOW", "CLOSE", "VOLUME", "AMOUNT")
SIGNAL_INDICATORS = ("CLOSE",)


def main() -> int:
    args = _parse_args()
    login = c.start("ForceLogin=0,RecordLoginInfo=0", _quiet_log)
    if login.ErrorCode != 0:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "stage": "login",
                    "errorCode": login.ErrorCode,
                    "errorMessage": login.ErrorMsg,
                    "classification": classify_choice_error(login.ErrorCode),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    try:
        calendar_result = _require_success(
            c.tradedates(args.start.isoformat(), args.end.isoformat(), "Market=CNSESH"),
            "market_calendar",
        )
        market_calendar = _extract_dates(calendar_result.Data)
        if len(market_calendar) < 2:
            raise RuntimeError(
                "minute snapshot publication requires at least two trading sessions "
                "for endpoint stability validation"
            )

        execution_rows: list[dict[str, object]] = []
        signal_rows: list[dict[str, object]] = []
        response_audits: list[dict[str, object]] = []
        request_chunks: list[dict[str, object]] = []
        for chunk_start, chunk_end in _session_chunks(
            market_calendar,
            size=args.chunk_sessions,
        ):
            execution_result = _require_success(
                c.cmc(
                    args.symbol,
                    ",".join(EXECUTION_INDICATORS),
                    chunk_start.isoformat(),
                    chunk_end.isoformat(),
                    _cmc_options(adjust_flag=1),
                ),
                f"minute_execution_{chunk_start}_{chunk_end}",
            )
            signal_result = _require_success(
                c.cmc(
                    args.symbol,
                    ",".join(SIGNAL_INDICATORS),
                    chunk_start.isoformat(),
                    chunk_end.isoformat(),
                    _cmc_options(adjust_flag=2),
                ),
                f"minute_signal_{chunk_start}_{chunk_end}",
            )
            decoded_execution = decode_cmc_batch(
                execution_result,
                symbol=args.symbol,
                expected_indicators=EXECUTION_INDICATORS,
            )
            decoded_signal = decode_cmc_batch(
                signal_result,
                symbol=args.symbol,
                expected_indicators=SIGNAL_INDICATORS,
            )
            execution_rows.extend(decoded_execution)
            signal_rows.extend(decoded_signal)
            for lane, result in (
                ("execution_unadjusted", execution_result),
                ("signal_back_adjusted_close", signal_result),
            ):
                serialized = serialize_sdk_result(result)
                response_audits.append(
                    {
                        "lane": lane,
                        "start": chunk_start.isoformat(),
                        "end": chunk_end.isoformat(),
                        "decodedResponseSha256": canonical_payload_sha256(serialized),
                        "hashSemantics": "canonical_sdk_decoded_response_json",
                        "result": serialized,
                    }
                )
            request_chunks.append(
                {
                    "start": chunk_start.isoformat(),
                    "end": chunk_end.isoformat(),
                    "executionIndicators": list(EXECUTION_INDICATORS),
                    "signalIndicators": list(SIGNAL_INDICATORS),
                }
            )

        timestamp_contract = infer_timestamp_semantics(execution_rows)
        semantics = timestamp_contract.get("semantics")
        if timestamp_contract.get("status") != "verified" or semantics not in {
            "bar_start",
            "bar_end",
        }:
            raise RuntimeError(
                "Choice minute timestamp labels could not be proven as bar_start or bar_end"
            )

        daily_execution_result = _require_success(
            c.csd(
                args.symbol,
                ",".join(EXECUTION_INDICATORS),
                args.start.isoformat(),
                args.end.isoformat(),
                "Period=1,AdjustFlag=1,Order=1,Ispandas=0,RECVtimeout=30",
            ),
            "daily_execution_reconciliation",
        )
        daily_signal_result = _require_success(
            c.csd(
                args.symbol,
                ",".join(SIGNAL_INDICATORS),
                args.start.isoformat(),
                args.end.isoformat(),
                "Period=1,AdjustFlag=2,Order=1,Ispandas=0,RECVtimeout=30",
            ),
            "daily_signal_reconciliation",
        )
        daily_reconciliation = reconcile_minute_daily(
            execution_rows=execution_rows,
            signal_rows=signal_rows,
            daily_execution_rows=decode_csd_batch(
                daily_execution_result,
                symbol=args.symbol,
                expected_indicators=EXECUTION_INDICATORS,
            ),
            daily_signal_rows=decode_csd_batch(
                daily_signal_result,
                symbol=args.symbol,
                expected_indicators=SIGNAL_INDICATORS,
            ),
            timestamp_semantics=semantics,
        )

        prefix_end = market_calendar[0]
        prefix_execution_result = _require_success(
            c.cmc(
                args.symbol,
                ",".join(EXECUTION_INDICATORS),
                prefix_end.isoformat(),
                prefix_end.isoformat(),
                _cmc_options(adjust_flag=1),
            ),
            "minute_execution_prefix",
        )
        prefix_signal_result = _require_success(
            c.cmc(
                args.symbol,
                ",".join(SIGNAL_INDICATORS),
                prefix_end.isoformat(),
                prefix_end.isoformat(),
                _cmc_options(adjust_flag=2),
            ),
            "minute_signal_prefix",
        )
        raw_prefix = compare_cmc_prefix(
            execution_rows,
            decode_cmc_batch(
                prefix_execution_result,
                symbol=args.symbol,
                expected_indicators=EXECUTION_INDICATORS,
            ),
            fields=tuple(item.lower() for item in EXECUTION_INDICATORS),
            expected_rows=240,
        )
        signal_prefix = compare_cmc_prefix(
            signal_rows,
            decode_cmc_batch(
                prefix_signal_result,
                symbol=args.symbol,
                expected_indicators=SIGNAL_INDICATORS,
            ),
            fields=("close",),
            expected_rows=240,
        )
        prefix_stability = {
            "status": (
                "passed"
                if raw_prefix.get("status") == signal_prefix.get("status") == "passed"
                else "failed"
            ),
            "prefixEnd": prefix_end.isoformat(),
            "overlapRows": min(
                int(raw_prefix.get("overlapRows", 0)),
                int(signal_prefix.get("overlapRows", 0)),
            ),
            "execution": raw_prefix,
            "signal": signal_prefix,
        }

        for lane, result in (
            ("daily_execution_reconciliation", daily_execution_result),
            ("daily_signal_reconciliation", daily_signal_result),
            ("minute_execution_prefix", prefix_execution_result),
            ("minute_signal_prefix", prefix_signal_result),
            ("market_calendar", calendar_result),
        ):
            serialized = serialize_sdk_result(result)
            response_audits.append(
                {
                    "lane": lane,
                    "decodedResponseSha256": canonical_payload_sha256(serialized),
                    "hashSemantics": "canonical_sdk_decoded_response_json",
                    "result": serialized,
                }
            )

        request_audit: dict[str, object] = {
            "function": "cmc",
            "symbol": args.symbol,
            "requestedRange": [args.start.isoformat(), args.end.isoformat()],
            "periodMinutes": 1,
            "timestampCalibration": timestamp_contract,
            "chunks": request_chunks,
            "executionOptions": _cmc_options(adjust_flag=1),
            "signalOptions": _cmc_options(adjust_flag=2),
            "prefixEnd": prefix_end.isoformat(),
            "runtimeNetworkFetchAllowed": False,
        }
        result = build_choice_minute_snapshot(
            spec=ChoiceMinuteSnapshotSpec(
                symbol=args.symbol,
                start=args.start,
                end=args.end,
                timestamp_semantics=semantics,
            ),
            execution_rows=execution_rows,
            signal_rows=signal_rows,
            market_calendar=market_calendar,
            raw_response_chunks=response_audits,
            request_audit=request_audit,
            daily_reconciliation=daily_reconciliation,
            prefix_stability=prefix_stability,
            output_root=args.output_root,
            captured_at=datetime.now(UTC),
            sdk_archive_sha256=_optional_sha256(args.sdk_archive),
        )
        print(
            json.dumps(
                {
                    "status": "ok",
                    "snapshotId": result.snapshot_id,
                    "path": str(result.path),
                    "rows": result.manifest["rowCounts"],
                    "timestampContract": timestamp_contract,
                    "dailyReconciliation": daily_reconciliation,
                    "prefixStability": prefix_stability,
                    "runtimeProfile": "choice_minute_snapshot",
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0
    except Exception as error:
        print(
            json.dumps(
                {"status": "failed", "message": str(error)},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    finally:
        c.stop()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="300059.SZ")
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--chunk-sessions", type=int, default=5)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("var/snapshots/choice-minute"),
    )
    parser.add_argument(
        "--sdk-archive",
        type=Path,
        default=Path.home() / "Downloads" / "EMQuantAPI_Python.zip",
    )
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("--start must not be after --end")
    if not 1 <= args.chunk_sessions <= 20:
        parser.error("--chunk-sessions must be between 1 and 20")
    return args


def _session_chunks(
    sessions: Sequence[date],
    *,
    size: int,
) -> list[tuple[date, date]]:
    return [
        (sessions[index], sessions[min(index + size - 1, len(sessions) - 1)])
        for index in range(0, len(sessions), size)
    ]


def _cmc_options(*, adjust_flag: int) -> str:
    return (
        f"Period=1,Market=CNSESH,Order=1,AdjustFlag={adjust_flag},"
        "Curtype=1,Pricetype=1,Type=1,mode=batch,RECVtimeout=30,Ispandas=0"
    )


def _require_success(result: Any, operation: str) -> Any:
    if not isinstance(result, c.EmQuantData):
        raise RuntimeError(f"{operation} returned {type(result).__name__}")
    if result.ErrorCode != 0:
        classification = classify_choice_error(result.ErrorCode)
        raise RuntimeError(
            f"{operation} failed: {result.ErrorCode} {result.ErrorMsg} ({classification})"
        )
    return result


def _extract_dates(value: object) -> list[date]:
    found: list[date] = []

    def visit(item: object) -> None:
        if isinstance(item, Mapping):
            for nested in item.values():
                visit(nested)
        elif isinstance(item, Sequence) and not isinstance(item, str | bytes | bytearray):
            for nested in item:
                visit(nested)
        elif isinstance(item, datetime):
            found.append(item.date())
        elif isinstance(item, date):
            found.append(item)
        elif isinstance(item, str):
            with suppress(ValueError):
                found.append(date.fromisoformat(item[:10].replace("/", "-")))

    visit(value)
    dates = sorted(set(found))
    if not dates:
        raise RuntimeError("Choice market calendar did not contain any dates")
    return dates


def _optional_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _quiet_log(_: bytes) -> int:
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
