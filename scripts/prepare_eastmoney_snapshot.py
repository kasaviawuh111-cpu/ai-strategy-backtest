#!/usr/bin/env python3
"""Create an immutable Push2His daily research snapshot for one SH/SZ A-share."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from ashare_lab.adapters.market_data.choice_snapshot import (
    TECHNICAL_SNAPSHOT_SCHEMA_VERSION,
    ChoiceSnapshotSpec,
    DailySnapshotSource,
    build_daily_research_snapshot,
)
from ashare_lab.adapters.market_data.eastmoney_daily import (
    DATASET,
    EastmoneyDailyCollection,
    EastmoneyDailyResearchSource,
)
from ashare_lab.domain.market_data import Board, PriceBasis
from ashare_lab.domain.shared import InstrumentId

try:
    from scripts.prepare_choice_snapshot import (
        compare_price_prefix,
        load_corporate_action_source,
        load_session_reference_source,
    )
except ModuleNotFoundError as error:
    if error.name != "scripts":
        raise
    from prepare_choice_snapshot import (  # type: ignore[import-not-found]
        compare_price_prefix,
        load_corporate_action_source,
        load_session_reference_source,
    )

_PRICE_TICK_TOLERANCE = Decimal("0.01")
PUSH2_DAILY_SOURCE = DailySnapshotSource(
    schema_version=TECHNICAL_SNAPSHOT_SCHEMA_VERSION,
    snapshot_prefix="technical",
    provider="Eastmoney Push2His public endpoint",
    dataset=DATASET,
    account_scope="public_undocumented_research_demo",
    adjustment_field="eastmoneyFqt",
    execution_adjustment=0,
    signal_adjustment=2,
    acquisition_implementation=(
        "thin httpx adapter using AKShare stock_zh_a_hist at immutable commit "
        "8e95744b79ae22326308ccd2b4e62650c5b53c55 (MIT)"
    ),
    limit_event_flags_available=False,
    status_cross_check=(
        "Push2 date axis aligns one-to-one; BaoStock supplies historical tradestatus/isST"
    ),
    previous_close_cross_check=(
        "Push2 close minus f60 change agrees with BaoStock preclose within one CNY cent"
    ),
    limitations=(
        "Eastmoney Push2His is a public undocumented endpoint; not authorized for production use",
        "f56 is normalized from lots to shares with conservative 100-share resolution",
        "BaoStock remains the independent source for historical tradestatus, isST and preclose",
        "daily price limits are derived from the versioned A-share rulebook, not Push2 fields",
        "runtime replay is offline and never calls Push2His",
    ),
)


def main() -> int:
    args = _parse_args()
    try:
        session_rows, session_coverage, session_payload = load_session_reference_source(
            args.session_reference_json,
            symbol=args.symbol,
        )
        actions, action_coverage, action_payload = load_corporate_action_source(
            args.corporate_actions_json,
            symbol=args.symbol,
        )
        instrument_id = InstrumentId(args.symbol)
        with EastmoneyDailyResearchSource(timeout=args.timeout_seconds) as source:
            execution = source.fetch(
                instrument_id=instrument_id,
                start=args.start,
                end=args.end,
                price_basis=PriceBasis.UNADJUSTED,
            )
            signal = source.fetch(
                instrument_id=instrument_id,
                start=args.start,
                end=args.end,
                price_basis=PriceBasis.BACK_ADJUSTED,
            )
            prefix = source.fetch(
                instrument_id=instrument_id,
                start=args.start,
                end=args.prefix_end,
                price_basis=PriceBasis.BACK_ADJUSTED,
            )

        execution_rows = _execution_rows_with_session_evidence(
            execution,
            session_rows=session_rows,
        )
        signal_rows = [row.as_snapshot_row(instrument_id) for row in signal.rows]
        prefix_rows = [row.as_snapshot_row(instrument_id) for row in prefix.rows]
        prefix_stability = compare_price_prefix(
            signal_rows,
            prefix_rows,
            args.prefix_end,
        )
        request_audit: dict[str, object] = {
            "fallbackReason": args.fallback_reason,
            "sourcePolicy": "one provider for the entire price series; no cross-source splicing",
            "volumeNormalization": {
                "sourceUnit": execution.source_volume_unit,
                "lotSizeShares": execution.volume_lot_size_shares,
                "resolutionShares": execution.volume_resolution_shares,
                "policy": "whole lots multiplied by 100; no odd-share precision invented",
            },
            "requests": [
                _collection_request_audit("execution", execution),
                _collection_request_audit("signal", signal),
                _collection_request_audit("signalPrefix", prefix),
            ],
            "corporateActions": {
                "input": str(args.corporate_actions_json),
                "inputSha256": _sha256_file(args.corporate_actions_json),
                "provider": action_coverage["provider"],
            },
            "historicalSessions": {
                "input": str(args.session_reference_json),
                "inputSha256": _sha256_file(args.session_reference_json),
                "provider": session_coverage["provider"],
                "normalizedResponseSha256": session_coverage["normalizedResponseSha256"],
            },
        }
        raw_audit_payload: dict[str, object] = {
            "execution": execution.raw_payload,
            "signal": signal.raw_payload,
            "signalPrefix": prefix.raw_payload,
            "baostockSessionReference": session_payload,
            "eastmoneyCorporateActionReference": action_payload,
        }
        result = build_daily_research_snapshot(
            spec=ChoiceSnapshotSpec(
                symbol=args.symbol,
                start=args.start,
                end=args.end,
                listing_date=args.listing_date,
                board=Board(args.board),
            ),
            execution_rows=execution_rows,
            signal_rows=signal_rows,
            market_calendar=[date.fromisoformat(str(row["date"])) for row in session_rows],
            raw_audit_payload=raw_audit_payload,
            request_audit=request_audit,
            prefix_stability=prefix_stability,
            output_root=args.output_root,
            captured_at=datetime.now(UTC),
            sdk_archive_sha256=None,
            session_reference_rows=session_rows,
            session_reference_coverage=session_coverage,
            corporate_actions=actions,
            corporate_action_coverage=action_coverage,
            source=PUSH2_DAILY_SOURCE,
        )
        print(
            json.dumps(
                {
                    "status": "ok",
                    "snapshotId": result.snapshot_id,
                    "path": str(result.path),
                    "provider": result.manifest["provider"],
                    "rows": result.manifest["rowCounts"],
                    "prefixStability": prefix_stability,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except Exception as error:
        print(f"Eastmoney technical snapshot failed: {error}")
        return 1


def _execution_rows_with_session_evidence(
    collection: EastmoneyDailyCollection,
    *,
    session_rows: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    sessions = {str(row.get("date")): row for row in session_rows}
    if len(sessions) != len(session_rows):
        raise ValueError("BaoStock session reference contains duplicate dates")
    price_dates = {row.date.isoformat() for row in collection.rows}
    if price_dates != set(sessions):
        missing = sorted(set(sessions) - price_dates)
        unexpected = sorted(price_dates - set(sessions))
        raise ValueError(
            "Push2 daily rows must align one-to-one with BaoStock sessions; "
            f"missing={len(missing)}, unexpected={len(unexpected)}"
        )

    output: list[dict[str, object]] = []
    for row in collection.rows:
        session = sessions[row.date.isoformat()]
        raw_preclose = session.get("preclose")
        raw_status = session.get("tradestatus")
        if not isinstance(raw_preclose, str):
            raise TypeError("BaoStock session preclose must be decimal text")
        if raw_status not in {"0", "1"}:
            raise TypeError("BaoStock session tradestatus must be 0 or 1")
        preclose = Decimal(raw_preclose)
        implied_preclose = row.close - row.change_cny
        if abs(implied_preclose - preclose) > _PRICE_TICK_TOLERANCE:
            raise ValueError(
                f"Push2 implied preclose conflicts with BaoStock on {row.date.isoformat()}"
            )
        item = row.as_snapshot_row(collection.instrument_id)
        item["preclose"] = preclose
        item["tradestatus"] = "正常交易" if raw_status == "1" else "连续停牌"
        output.append(item)
    return output


def _collection_request_audit(
    purpose: str,
    collection: EastmoneyDailyCollection,
) -> dict[str, object]:
    return {
        "purpose": purpose,
        "url": collection.request_url,
        "params": dict(collection.request_params),
        "requestStartedAt": collection.request_started_at.isoformat(),
        "responseReceivedAt": collection.response_received_at.isoformat(),
        "rawWireSha256": collection.raw_wire_sha256,
        "canonicalPayloadSha256": collection.raw_payload_canonical_sha256,
        "rows": len(collection.rows),
        "priceBasis": collection.price_basis.value,
        "attemptCount": collection.attempt_count,
        "transientErrors": list(collection.transient_errors),
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="300059.SZ")
    parser.add_argument("--start", type=date.fromisoformat, default=date(2021, 2, 7))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 8, 20))
    parser.add_argument("--prefix-end", type=date.fromisoformat)
    parser.add_argument("--listing-date", type=date.fromisoformat, default=date(2010, 3, 19))
    parser.add_argument(
        "--board",
        choices=tuple(item.value for item in Board),
        default=Board.CHINEXT.value,
    )
    parser.add_argument("--output-root", type=Path, default=Path("var/snapshots/technical"))
    parser.add_argument("--session-reference-json", type=Path, required=True)
    parser.add_argument("--corporate-actions-json", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument(
        "--fallback-reason",
        default="Choice unadjusted daily read exhausted bounded retries with error 10002004",
    )
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("--start must not be after --end")
    if args.prefix_end is None:
        args.prefix_end = args.end - timedelta(days=365)
    if not args.start < args.prefix_end < args.end:
        parser.error("--prefix-end must be strictly inside the requested range")
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    return args


if __name__ == "__main__":
    raise SystemExit(main())
