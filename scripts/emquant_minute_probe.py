#!/usr/bin/env python3
"""Read-only Choice CMC capability and one-session quality probe."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from EmQuantAPI import c

from ashare_lab.adapters.market_data.choice_minute import (
    ChoiceMinuteDecodeError,
    canonical_payload_sha256,
    classify_choice_error,
    decode_cmc_batch,
    decode_csd_batch,
    infer_timestamp_semantics,
    reconcile_minute_daily,
    serialize_sdk_result,
)

EXECUTION_INDICATORS = ("OPEN", "HIGH", "LOW", "CLOSE", "VOLUME", "AMOUNT")
SIGNAL_INDICATORS = ("CLOSE",)


def main() -> int:
    args = _parse_args()
    captured_at = datetime.now(UTC)
    output = _base_output(args, captured_at)
    login = c.start("ForceLogin=0,RecordLoginInfo=0", _quiet_log)
    if login.ErrorCode != 0:
        output["status"] = "blocked"
        output["errors"] = [
            {
                "stage": "login",
                "code": login.ErrorCode,
                "message": login.ErrorMsg,
                "classification": classify_choice_error(login.ErrorCode),
            }
        ]
        print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
        return 1

    try:
        execution = c.cmc(
            args.code,
            ",".join(EXECUTION_INDICATORS),
            args.trade_date.isoformat(),
            args.trade_date.isoformat(),
            _cmc_options(adjust_flag=1),
        )
        if execution.ErrorCode != 0:
            _record_provider_failure(output, execution, stage="minute_unadjusted")
            print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
            return 1
        signal = c.cmc(
            args.code,
            ",".join(SIGNAL_INDICATORS),
            args.trade_date.isoformat(),
            args.trade_date.isoformat(),
            _cmc_options(adjust_flag=2),
        )
        if signal.ErrorCode != 0:
            _record_provider_failure(output, signal, stage="minute_back_adjusted_close")
            print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
            return 1
        daily_execution = c.csd(
            args.code,
            ",".join(EXECUTION_INDICATORS),
            args.trade_date.isoformat(),
            args.trade_date.isoformat(),
            "Period=1,AdjustFlag=1,Order=1,Ispandas=0,RECVtimeout=30",
        )
        if daily_execution.ErrorCode != 0:
            _record_provider_failure(output, daily_execution, stage="daily_unadjusted")
            print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
            return 1
        daily_signal = c.csd(
            args.code,
            ",".join(SIGNAL_INDICATORS),
            args.trade_date.isoformat(),
            args.trade_date.isoformat(),
            "Period=1,AdjustFlag=2,Order=1,Ispandas=0,RECVtimeout=30",
        )
        if daily_signal.ErrorCode != 0:
            _record_provider_failure(output, daily_signal, stage="daily_back_adjusted_close")
            print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
            return 1

        execution_rows = decode_cmc_batch(
            execution,
            symbol=args.code,
            expected_indicators=EXECUTION_INDICATORS,
        )
        signal_rows = decode_cmc_batch(
            signal,
            symbol=args.code,
            expected_indicators=SIGNAL_INDICATORS,
        )
        timestamp_contract = infer_timestamp_semantics(execution_rows)
        dimensions_match = len(execution_rows) == len(signal_rows) and [
            str(item["timestamp"]) for item in execution_rows
        ] == [str(item["timestamp"]) for item in signal_rows]
        quality = _sample_quality(execution_rows)
        signal_quality = _sample_signal_quality(signal_rows)
        reconciliation: dict[str, object] = {
            "status": "unverified",
            "reason": "timestamp_semantics_unverified",
        }
        semantics = timestamp_contract.get("semantics")
        if semantics in {"bar_start", "bar_end"}:
            reconciliation = reconcile_minute_daily(
                execution_rows=execution_rows,
                signal_rows=signal_rows,
                daily_execution_rows=decode_csd_batch(
                    daily_execution,
                    symbol=args.code,
                    expected_indicators=EXECUTION_INDICATORS,
                ),
                daily_signal_rows=decode_csd_batch(
                    daily_signal,
                    symbol=args.code,
                    expected_indicators=SIGNAL_INDICATORS,
                ),
                timestamp_semantics=semantics,
            )

        execution_audit = serialize_sdk_result(execution)
        signal_audit = serialize_sdk_result(signal)
        output.update(
            {
                "status": "verified",
                "capabilities": {
                    "minute_unadjusted": "verified",
                    "minute_back_adjusted_close": "verified",
                    "five_year_continuous_coverage": "not_verified",
                },
                "response": {
                    "error_code": 0,
                    "error_message": "success",
                    "codes": list(execution.Codes),
                    "indicators": list(execution.Indicators),
                    "row_count": len(execution_rows),
                    "first_timestamp": (
                        str(execution_rows[0]["timestamp"]) if execution_rows else None
                    ),
                    "last_timestamp": (
                        str(execution_rows[-1]["timestamp"]) if execution_rows else None
                    ),
                    "decoded_response_sha256": canonical_payload_sha256(
                        {"execution": execution_audit, "signal": signal_audit}
                    ),
                    "hash_semantics": "canonical_sdk_decoded_response_json",
                },
                "timestamp_contract": {
                    "timezone": "Asia/Shanghai",
                    "available_at_policy": (
                        "completed_bar_end.v1"
                        if timestamp_contract.get("status") == "verified"
                        else "unverified"
                    ),
                    **timestamp_contract,
                },
                "validation": {
                    "dimensions_match": dimensions_match,
                    **quality,
                    **signal_quality,
                    "minute_daily_reconciliation": reconciliation,
                },
                "history_depth": {
                    "oldest_sample_verified": args.trade_date.isoformat(),
                    "continuous_coverage_verified": False,
                    "probes": [args.trade_date.isoformat()],
                },
            }
        )
        ready = (
            bool(execution_rows)
            and dimensions_match
            and all(quality.values())
            and all(signal_quality.values())
            and timestamp_contract.get("status") == "verified"
            and reconciliation.get("status") == "passed"
        )
        output["ready_for_snapshot"] = ready
        if not ready:
            output["status"] = "invalid_data"
        print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
        return 0 if ready else 1
    except (ChoiceMinuteDecodeError, KeyError, TypeError, ValueError) as error:
        output["status"] = "invalid_data"
        output["errors"] = [
            {
                "stage": "validation",
                "classification": "provider_schema_mismatch",
                "message": str(error),
            }
        ]
        print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
        return 1
    finally:
        c.stop()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code", default="300059.SZ")
    parser.add_argument("--trade-date", type=date.fromisoformat, default=date(2026, 8, 27))
    parser.add_argument(
        "--sdk-archive",
        type=Path,
        default=Path.home() / "Downloads" / "EMQuantAPI_Python.zip",
    )
    return parser.parse_args()


def _base_output(args: argparse.Namespace, captured_at: datetime) -> dict[str, object]:
    return {
        "schema_version": "choice.minute-capability-probe.v1",
        "status": "unknown",
        "captured_at": captured_at.isoformat(),
        "provider": {
            "name": "Choice Quant API",
            "function": "cmc",
            "sdk_archive_sha256": _optional_sha256(args.sdk_archive),
        },
        "request": {
            "symbol": args.code,
            "period_minutes": 1,
            "trade_date": args.trade_date.isoformat(),
            "execution_indicators": list(EXECUTION_INDICATORS),
            "signal_indicators": list(SIGNAL_INDICATORS),
        },
        "capabilities": {
            "minute_unadjusted": "unknown",
            "minute_back_adjusted_close": "unknown",
            "five_year_continuous_coverage": "not_verified",
        },
        "ready_for_snapshot": False,
        "errors": [],
    }


def _cmc_options(*, adjust_flag: int) -> str:
    return (
        f"Period=1,Market=CNSESH,Order=1,AdjustFlag={adjust_flag},"
        "Curtype=1,Pricetype=1,Type=1,mode=batch,RECVtimeout=30,Ispandas=0"
    )


def _record_provider_failure(output: dict[str, object], result: Any, *, stage: str) -> None:
    classification = classify_choice_error(result.ErrorCode)
    output["status"] = "denied" if classification == "account_permission_denied" else "blocked"
    output["errors"] = [
        {
            "stage": stage,
            "code": result.ErrorCode,
            "message": result.ErrorMsg,
            "classification": classification,
        }
    ]


def _sample_quality(rows: Sequence[Mapping[str, object]]) -> dict[str, bool]:
    timestamps = [str(item.get("timestamp")) for item in rows]
    ohlc_consistent = True
    volume_amount_nonnegative = True
    for row in rows:
        try:
            open_price = _number(row.get("open"))
            high = _number(row.get("high"))
            low = _number(row.get("low"))
            close = _number(row.get("close"))
            volume = _number(row.get("volume"))
            amount = _number(row.get("amount"))
        except ValueError:
            ohlc_consistent = False
            volume_amount_nonnegative = False
            continue
        if (
            min(open_price, high, low, close) <= 0
            or low > min(open_price, high, close)
            or high < max(open_price, low, close)
        ):
            ohlc_consistent = False
        if volume < 0 or amount < 0 or volume != volume.to_integral_value():
            volume_amount_nonnegative = False
    return {
        "timestamps_unique": len(timestamps) == len(set(timestamps)),
        "ohlc_consistent": ohlc_consistent,
        "volume_amount_nonnegative": volume_amount_nonnegative,
    }


def _sample_signal_quality(rows: Sequence[Mapping[str, object]]) -> dict[str, bool]:
    timestamps = [str(item.get("timestamp")) for item in rows]
    closes_positive_finite = True
    for row in rows:
        try:
            closes_positive_finite = closes_positive_finite and _number(row.get("close")) > 0
        except ValueError:
            closes_positive_finite = False
    return {
        "signal_timestamps_unique": len(timestamps) == len(set(timestamps)),
        "signal_closes_positive_finite": closes_positive_finite,
    }


def _number(value: object) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError("not numeric")
    try:
        converted = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("not numeric") from exc
    if not converted.is_finite():
        raise ValueError("not finite")
    return converted


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
