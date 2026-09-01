#!/usr/bin/env python3
"""Publish one immutable BaoStock daily snapshot for a single SH/SZ stock.

This is an explicit server-side source choice, not a fallback.  It does not
download the market universe, and it does not silently switch to Choice or
Push2 when BaoStock fails.  Corporate-action evidence remains an independently
validated input because BaoStock's dividend endpoint does not cover every
A-share action category required by the account ledger.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from contextlib import redirect_stdout
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from ashare_lab.adapters.market_data.baostock_daily import (
    DATASET,
    BaoStockDailyCollection,
    BaoStockDailyResearchSource,
)
from ashare_lab.adapters.market_data.choice_snapshot import (
    TECHNICAL_SNAPSHOT_SCHEMA_VERSION,
    ChoiceSnapshotSpec,
    DailySnapshotSource,
    build_daily_research_snapshot,
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


BAOSTOCK_DAILY_SOURCE = DailySnapshotSource(
    schema_version=TECHNICAL_SNAPSHOT_SCHEMA_VERSION,
    snapshot_prefix="technical",
    provider="BaoStock Python API",
    dataset=DATASET,
    account_scope="personal_research_demo",
    adjustment_field="adjustflag",
    execution_adjustment=3,
    signal_adjustment=1,
    acquisition_implementation="thin BaoStock Python SDK adapter with annual bounded queries",
    limit_event_flags_available=False,
    status_cross_check="BaoStock daily rows exact-match separately acquired BaoStock sessions",
    previous_close_cross_check="exact decimal equality across BaoStock daily and sessions",
    limitations=(
        "BaoStock SDK exposes normalized parsed rows, not original network-response bytes",
        "this explicit daily source supports SH/SZ stocks only, not stock ETFs or BSE shares",
        "corporate-action ledger evidence remains the separately validated Eastmoney input",
        "financial values and announcement events are not inferred from this daily snapshot",
        "runtime replay is offline and never calls BaoStock",
    ),
)


def main() -> int:
    args = _parse_args()
    try:
        import baostock as bs
    except ImportError:
        print("BaoStock is not installed; install the project demo extra.")
        return 2

    # BaoStock emits connection notices with ``print``.  This CLI is consumed
    # by the on-demand preparer, whose stdout contract is exactly one terminal
    # JSON object, so provider chatter belongs on stderr.
    with redirect_stdout(sys.stderr):
        login = bs.login()
    if str(login.error_code) != "0":
        print("BaoStock login failed")
        return 1
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
        source = BaoStockDailyResearchSource(bs)
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
        prefix_stability = compare_price_prefix(signal_rows, prefix_rows, args.prefix_end)
        request_audit: dict[str, object] = {
            "sourcePolicy": "explicit BaoStock daily source; no Choice or Push2 fallback",
            "execution": _collection_request_audit("execution", execution),
            "signal": _collection_request_audit("signal", signal),
            "signalPrefix": _collection_request_audit("signalPrefix", prefix),
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
            "execution": _normalized_collection_payload(execution),
            "signal": _normalized_collection_payload(signal),
            "signalPrefix": _normalized_collection_payload(prefix),
            "baostockSessionReference": session_payload,
            "corporateActionReference": action_payload,
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
            source=BAOSTOCK_DAILY_SOURCE,
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
        print(f"BaoStock technical snapshot failed: {type(error).__name__}: {error}")
        return 1
    finally:
        with redirect_stdout(sys.stderr):
            bs.logout()


def _execution_rows_with_session_evidence(
    collection: BaoStockDailyCollection,
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
            "BaoStock daily rows must align one-to-one with BaoStock sessions; "
            f"missing={len(missing)}, unexpected={len(unexpected)}"
        )

    output: list[dict[str, object]] = []
    for row in collection.rows:
        session = sessions[row.date.isoformat()]
        raw_preclose = session.get("preclose")
        raw_status = session.get("tradestatus")
        raw_is_st = session.get("isST")
        if not isinstance(raw_preclose, str) or raw_status not in {"0", "1"}:
            raise TypeError("BaoStock session reference must contain preclose and tradestatus")
        if raw_is_st not in {"0", "1"}:
            raise TypeError("BaoStock session reference must contain isST")
        if Decimal(raw_preclose) != row.preclose:
            raise ValueError(f"BaoStock preclose conflicts on {row.date.isoformat()}")
        if raw_status != row.tradestatus or (raw_is_st == "1") is not row.is_st:
            raise ValueError(f"BaoStock session status conflicts on {row.date.isoformat()}")
        item = row.as_snapshot_row(collection.instrument_id)
        item["preclose"] = row.preclose
        item["tradestatus"] = "正常交易" if row.tradestatus == "1" else "连续停牌"
        output.append(item)
    return output


def _collection_request_audit(
    purpose: str,
    collection: BaoStockDailyCollection,
) -> dict[str, object]:
    return {
        "purpose": purpose,
        "providerCode": collection.provider_code,
        "priceBasis": collection.price_basis.value,
        "adjustFlag": collection.adjustflag,
        "sourceVolumeUnit": collection.source_volume_unit,
        "hashSemantics": collection.normalized_response_hash_semantics,
        "rawWireCaptured": False,
        "requests": [audit.as_dict() for audit in collection.query_audits],
    }


def _normalized_collection_payload(collection: BaoStockDailyCollection) -> dict[str, object]:
    return {
        "provider": collection.provider_name,
        "dataset": collection.dataset_name,
        "providerCode": collection.provider_code,
        "priceBasis": collection.price_basis.value,
        "adjustFlag": collection.adjustflag,
        "queryAudits": [audit.as_dict() for audit in collection.query_audits],
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--prefix-end", type=date.fromisoformat)
    parser.add_argument("--listing-date", type=date.fromisoformat, required=True)
    parser.add_argument("--board", choices=tuple(item.value for item in Board), required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--session-reference-json", type=Path, required=True)
    parser.add_argument("--corporate-actions-json", type=Path, required=True)
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("--start must not be after --end")
    if args.prefix_end is None:
        args.prefix_end = args.end - timedelta(days=365)
    if not args.start < args.prefix_end < args.end:
        parser.error("--prefix-end must be strictly inside the requested range")
    return args


if __name__ == "__main__":
    raise SystemExit(main())
