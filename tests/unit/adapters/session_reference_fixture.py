from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path

from ashare_lab.adapters.market_data.choice_snapshot import (
    STRICT_CORPORATE_ACTION_CATEGORIES,
    STRICT_CORPORATE_ACTION_COVERAGE_SCOPE,
    ChoiceSnapshotResult,
    ChoiceSnapshotSpec,
    build_choice_snapshot,
)
from ashare_lab.domain.market_data import Board

FIELDS = ("date", "preclose", "tradestatus", "isST")


def choice_snapshot_fixture(output_root: Path) -> ChoiceSnapshotResult:
    """Publish one structurally real two-session Choice producer fixture."""

    start = date(2025, 1, 2)
    end = date(2025, 1, 3)
    symbol = "300059.SZ"
    execution_rows: list[dict[str, object]] = [
        {
            "date": "2025-01-02",
            "open": 10,
            "high": 11,
            "low": 9,
            "close": 10.5,
            "preclose": 9.8,
            "volume": 1_000,
            "amount": 10_500,
            "highlimit": "否",
            "lowlimit": "否",
            "tradestatus": "正常交易",
        },
        {
            "date": "2025-01-03",
            "open": 10.6,
            "high": 11.2,
            "low": 10.1,
            "close": 11,
            "preclose": 10.5,
            "volume": 1_200,
            "amount": 13_200,
            "highlimit": "否",
            "lowlimit": "否",
            "tradestatus": "正常交易",
        },
    ]
    signal_rows = [
        {
            "date": row["date"],
            "open": float(row["open"]) * 1.1,
            "high": float(row["high"]) * 1.1,
            "low": float(row["low"]) * 1.1,
            "close": float(row["close"]) * 1.1,
            "volume": row["volume"],
            "amount": row["amount"],
        }
        for row in execution_rows
    ]
    reference_rows, reference_coverage = baostock_session_reference(
        symbol=symbol,
        start=start,
        end=end,
        execution_rows=execution_rows,
    )
    return build_choice_snapshot(
        spec=ChoiceSnapshotSpec(
            symbol=symbol,
            start=start,
            end=end,
            listing_date=date(2010, 3, 19),
            board=Board.CHINEXT,
        ),
        execution_rows=execution_rows,
        signal_rows=signal_rows,
        market_calendar=(start, end),
        raw_audit_payload={"responses": ["fixture"]},
        request_audit={"AdjustFlag": [1, 2]},
        prefix_stability={"status": "passed", "overlapRows": 1},
        output_root=output_root,
        captured_at=datetime(2025, 1, 4, tzinfo=UTC),
        sdk_archive_sha256="a" * 64,
        session_reference_rows=reference_rows,
        session_reference_coverage=reference_coverage,
        corporate_action_coverage={
            "status": "complete",
            "querySucceeded": True,
            "provider": "fixture-source",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "rawResponseSha256": "b" * 64,
            "coverageScope": STRICT_CORPORATE_ACTION_COVERAGE_SCOPE,
            "supportedCategories": list(STRICT_CORPORATE_ACTION_CATEGORIES),
            "unsupportedCategories": [],
        },
    )


def baostock_session_reference(
    *,
    symbol: str,
    start: date,
    end: date,
    execution_rows: Sequence[Mapping[str, object]],
) -> tuple[list[dict[str, object]], dict[str, object]]:
    rows = [
        {
            "date": _as_date(row["date"]).isoformat(),
            "preclose": str(row["preclose"]),
            "tradestatus": "1",
            "isST": "0",
        }
        for row in execution_rows
    ]
    audits: list[dict[str, object]] = []
    intervals: list[dict[str, object]] = []
    provider_code = f"sh.{symbol[:6]}" if symbol.endswith(".SH") else f"sz.{symbol[:6]}"
    for year in range(start.year, end.year + 1):
        interval_start = max(start, date(year, 1, 1))
        interval_end = min(end, date(year, 12, 31))
        interval_rows = [
            row
            for row in rows
            if interval_start <= date.fromisoformat(str(row["date"])) <= interval_end
        ]
        canonical_rows = sorted(
            interval_rows,
            key=lambda row: json.dumps(
                row,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
        params = {
            "code": provider_code,
            "fields": ",".join(FIELDS),
            "start_date": interval_start.isoformat(),
            "end_date": interval_end.isoformat(),
            "frequency": "d",
            "adjustflag": "3",
        }
        body = {
            "method": "query_history_k_data_plus",
            "params": params,
            "fields": sorted(FIELDS),
            "rows": canonical_rows,
            "errorCode": "0",
            "errorMessage": "success",
        }
        digest = _sha256(body)
        audit = {
            **body,
            "rowCount": len(canonical_rows),
            "zeroResult": not canonical_rows,
            "normalizedResponseSha256": digest,
        }
        audits.append(audit)
        intervals.append(
            {
                "start": interval_start.isoformat(),
                "end": interval_end.isoformat(),
                "rowCount": len(canonical_rows),
                "zeroResult": not canonical_rows,
                "normalizedResponseSha256": digest,
            }
        )
    coverage = {
        "status": "complete",
        "querySucceeded": True,
        "provider": "BaoStock Python API",
        "instrumentId": symbol,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "fields": list(FIELDS),
        "frequency": "d",
        "adjustFlag": "3",
        "priceBasis": "unadjusted",
        "rowCount": len(rows),
        "zeroResult": not rows,
        "returnedStart": rows[0]["date"] if rows else None,
        "returnedEnd": rows[-1]["date"] if rows else None,
        "normalizedResponseSha256": _sha256(audits),
        "canonicalRowsSha256": _sha256(rows),
        "hashSemantics": "sha256_of_canonical_normalized_results_not_raw_wire_bytes",
        "paginationPolicy": "annual_queries_bounded_to_at_most_366_calendar_days",
        "intervals": intervals,
        "queryAudits": audits,
    }
    return rows, coverage


def _as_date(value: object) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).replace("/", "-"))


def _sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
