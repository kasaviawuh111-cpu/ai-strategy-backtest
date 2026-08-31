#!/usr/bin/env python3
"""Download a reproducible 300059.SZ research demo dataset from BaoStock.

The generated files are intentionally labelled research/demo inputs.  Report
publication dates from BaoStock are date-only, so the event file records them
with ``date_only_conservative`` quality; the runtime makes them tradable only
after that date's close.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Iterable
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq

from ashare_lab.domain.execution import HistoricalAshareRuleBook, PriceLimitRuleInput
from ashare_lab.domain.market_data import Board, TradingStatus
from ashare_lab.domain.shared import InstrumentId, Price

SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_SYMBOL = "300059.SZ"


class DemoDataError(RuntimeError):
    """The vendor response cannot produce a safe demo snapshot."""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2021, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 8, 20))
    parser.add_argument("--output", type=Path, default=Path("var/data"))
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("--start must be on or before --end")

    try:
        import baostock as bs
    except ImportError:
        print(
            "BaoStock is not installed; run `uv sync --extra dev --extra demo` first.",
            file=sys.stderr,
        )
        return 2

    symbol = _normalize_symbol(args.symbol)
    provider_symbol = _provider_symbol(symbol)
    output_root = args.output.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    login = bs.login()
    if login.error_code != "0":
        raise DemoDataError(f"BaoStock login failed: {login.error_code} {login.error_msg}")
    try:
        daily_rows = _download_daily(bs, provider_symbol, symbol, args.start, args.end)
        event_rows = _download_report_events(
            bs,
            provider_symbol,
            symbol,
            args.start,
            args.end,
        )
    finally:
        bs.logout()

    if len(daily_rows) < 2:
        raise DemoDataError("BaoStock returned fewer than two daily bars")
    _write_daily(output_root / "daily_ohlcv.parquet", daily_rows)
    _write_events(output_root / "events.parquet", event_rows)
    session_rows = _derive_session_rows(symbol, daily_rows)
    _write_sessions(output_root / "instrument_sessions.parquet", session_rows)

    files = (
        output_root / "daily_ohlcv.parquet",
        output_root / "events.parquet",
        output_root / "instrument_sessions.parquet",
    )
    manifest = {
        "schemaVersion": "ashare-lab.research-demo.v1",
        "source": "BaoStock",
        "symbol": symbol,
        "requestedRange": [args.start.isoformat(), args.end.isoformat()],
        "dailyRows": len(daily_rows),
        "eventRows": len(event_rows),
        "sessionRows": len(session_rows),
        "sessionReferenceProvenance": {
            "kind": "research-derived",
            "profile": "300059.SZ / ChiNext / listed 2010-03-19 / no ST periods supplied",
            "ruleVersion": HistoricalAshareRuleBook.version,
        },
        "generatedAt": datetime.now(UTC).isoformat(),
        "files": {
            path.name: {
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
            for path in files
        },
        "limitations": [
            "research/demo only",
            "unadjusted daily OHLCV",
            "report publication timestamps are date-only and conservatively mapped after close",
            "not a licensed production market-data feed",
            "session reference is research-derived and assumes no historical ST periods",
        ],
    }
    manifest_path = output_root / "demo_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


def _download_daily(
    bs: Any,
    provider_symbol: str,
    canonical_symbol: str,
    start: date,
    end: date,
) -> list[dict[str, object]]:
    query = bs.query_history_k_data_plus(
        provider_symbol,
        "date,code,open,high,low,close,preclose,volume,amount,tradestatus",
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        frequency="d",
        adjustflag="3",
    )
    rows = _query_rows(query, "daily bars")
    provider_code = canonical_symbol.split(".", 1)[0]
    result: list[dict[str, object]] = []
    for row in rows:
        if row[9] != "1" or not row[2]:
            continue
        result.append(
            {
                "stock_code": provider_code,
                "date": date.fromisoformat(row[0]),
                "open": float(row[2]),
                "high": float(row[3]),
                "low": float(row[4]),
                "close": float(row[5]),
                "previous_close": Decimal(row[6]),
                "volume": int(row[7]),
                "amount": float(row[8] or 0),
            }
        )
    result.sort(key=lambda item: item["date"])
    return result


def _download_report_events(
    bs: Any,
    provider_symbol: str,
    canonical_symbol: str,
    start: date,
    end: date,
) -> list[dict[str, object]]:
    selected: dict[str, dict[str, object]] = {}
    provider_code = canonical_symbol.split(".", 1)[0]
    for year in range(start.year - 1, end.year + 1):
        for quarter in range(1, 5):
            rows = _query_rows(
                bs.query_profit_data(code=provider_symbol, year=year, quarter=quarter),
                f"profit data {year}Q{quarter}",
            )
            for row in rows:
                publication_date = date.fromisoformat(row[1])
                statement_date = date.fromisoformat(row[2])
                if not start <= publication_date <= end:
                    continue
                report_type, event_code = _report_type(statement_date)
                event_id = f"baostock:{provider_code}:{statement_date.isoformat()}:report"
                selected[event_id] = {
                    "stock_code": provider_code,
                    "event_id": event_id,
                    "event_code": event_code,
                    "occurred_at": statement_date,
                    "source_released_at": publication_date,
                    "vendor_first_available_at": None,
                    "ingested_at": datetime.combine(
                        publication_date,
                        time(hour=15),
                        tzinfo=SHANGHAI,
                    ),
                    "revision_no": 0,
                    "time_quality": "date_only_conservative",
                    "attributes_json": json.dumps(
                        {
                            "report_type": report_type,
                            "source": "baostock_research_demo",
                            "stat_date": statement_date.isoformat(),
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                }
    return sorted(
        selected.values(),
        key=lambda item: (item["source_released_at"], item["event_id"]),
    )


def _query_rows(query: Any, label: str) -> list[list[str]]:
    if query.error_code != "0":
        raise DemoDataError(f"BaoStock {label} failed: {query.error_code} {query.error_msg}")
    rows: list[list[str]] = []
    while query.next():
        rows.append(query.get_row_data())
    return rows


def _write_daily(path: Path, rows: Iterable[dict[str, object]]) -> None:
    schema = pa.schema(
        [
            ("stock_code", pa.string()),
            ("date", pa.date32()),
            ("open", pa.float64()),
            ("high", pa.float64()),
            ("low", pa.float64()),
            ("close", pa.float64()),
            ("volume", pa.int64()),
            ("amount", pa.float64()),
        ]
    )
    _atomic_write_table(path, pa.Table.from_pylist(list(rows), schema=schema))


def _write_events(path: Path, rows: Iterable[dict[str, object]]) -> None:
    schema = pa.schema(
        [
            ("stock_code", pa.string()),
            ("event_id", pa.string()),
            ("event_code", pa.string()),
            ("occurred_at", pa.date32()),
            ("source_released_at", pa.date32()),
            ("vendor_first_available_at", pa.timestamp("us", tz="Asia/Shanghai")),
            ("ingested_at", pa.timestamp("us", tz="Asia/Shanghai")),
            ("revision_no", pa.int32()),
            ("time_quality", pa.string()),
            ("attributes_json", pa.string()),
        ]
    )
    _atomic_write_table(path, pa.Table.from_pylist(list(rows), schema=schema))


def _derive_session_rows(
    canonical_symbol: str,
    daily_rows: Iterable[dict[str, object]],
) -> list[dict[str, object]]:
    if canonical_symbol != DEFAULT_SYMBOL:
        raise DemoDataError("research-derived session reference is published only for 300059.SZ")
    instrument_id = InstrumentId(canonical_symbol)
    rule_book = HistoricalAshareRuleBook()
    result: list[dict[str, object]] = []
    for row in daily_rows:
        session_date = row["date"]
        previous_close = row["previous_close"]
        if not isinstance(session_date, date) or not isinstance(previous_close, Decimal):
            raise DemoDataError("daily row cannot derive a canonical instrument session")
        session = rule_book.build_session(
            PriceLimitRuleInput(
                instrument_id=instrument_id,
                session_date=session_date,
                board=Board.CHINEXT,
                status=TradingStatus.TRADING,
                previous_close=Price(previous_close),
                listing_date=date(2010, 3, 19),
                listing_session_number=6,
                is_st=False,
            )
        )
        result.append(
            {
                "stock_code": canonical_symbol.split(".", 1)[0],
                "date": session.session_date,
                "board": session.board.value,
                "trading_status": session.status.value,
                "previous_close": session.previous_close.amount,
                "upper_limit": (
                    session.upper_limit.amount if session.upper_limit is not None else None
                ),
                "lower_limit": (
                    session.lower_limit.amount if session.lower_limit is not None else None
                ),
                "minimum_buy_quantity": session.minimum_buy_quantity,
                "buy_quantity_increment": session.buy_quantity_increment,
                "price_tick": session.price_tick,
                "t_plus_one": session.t_plus_one,
                "is_st": session.is_st,
            }
        )
    return result


def _write_sessions(path: Path, rows: Iterable[dict[str, object]]) -> None:
    price_type = pa.decimal128(20, 4)
    schema = pa.schema(
        [
            ("stock_code", pa.string()),
            ("date", pa.date32()),
            ("board", pa.string()),
            ("trading_status", pa.string()),
            ("previous_close", price_type),
            ("upper_limit", price_type),
            ("lower_limit", price_type),
            ("minimum_buy_quantity", pa.int32()),
            ("buy_quantity_increment", pa.int32()),
            ("price_tick", price_type),
            ("t_plus_one", pa.bool_()),
            ("is_st", pa.bool_()),
        ]
    )
    _atomic_write_table(path, pa.Table.from_pylist(list(rows), schema=schema))


def _atomic_write_table(path: Path, table: pa.Table) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    pq.write_table(table, temporary, compression="zstd")
    temporary.replace(path)


def _normalize_symbol(value: str) -> str:
    canonical = value.strip().upper()
    if len(canonical) != 9 or canonical[6] != ".":
        raise DemoDataError("symbol must use the form 300059.SZ")
    code, exchange = canonical.split(".", 1)
    if not code.isdigit() or exchange not in {"SH", "SZ"}:
        raise DemoDataError("the BaoStock demo supports Shanghai/Shenzhen A shares")
    return canonical


def _provider_symbol(symbol: str) -> str:
    code, exchange = symbol.split(".", 1)
    return f"{exchange.lower()}.{code}"


def _report_type(statement_date: date) -> tuple[str, str]:
    suffix = (statement_date.month, statement_date.day)
    if suffix == (12, 31):
        return "annual", "event.financial_results.annual_report"
    if suffix == (6, 30):
        return "semiannual", "event.financial_results.semiannual_report"
    return "quarterly", "event.financial_results.quarterly_report"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
