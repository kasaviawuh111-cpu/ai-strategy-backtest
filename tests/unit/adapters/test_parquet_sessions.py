from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from ashare_lab.adapters.market_data import (
    ParquetInstrumentSessionProvider,
    SessionReferenceIntegrityError,
    SessionReferenceSchemaError,
)
from ashare_lab.domain.market_data import Board, DailyBar, TradingStatus
from ashare_lab.domain.shared import InstrumentId, Price, Quantity


def _session_rows() -> list[dict[str, object]]:
    return [
        {
            "stock_code": "300059",
            "date": date(2025, 1, 2),
            "board": "chinext",
            "trading_status": "trading",
            "previous_close": Decimal("10.00"),
            "upper_limit": Decimal("12.00"),
            "lower_limit": Decimal("8.00"),
            "minimum_buy_quantity": 100,
            "buy_quantity_increment": 100,
            "price_tick": Decimal("0.01"),
            "t_plus_one": True,
            "is_st": False,
        },
        {
            "stock_code": "300059",
            "date": date(2025, 1, 3),
            "board": "chinext",
            "trading_status": "suspended",
            "previous_close": Decimal("10.50"),
            "upper_limit": Decimal("12.60"),
            "lower_limit": Decimal("8.40"),
            "minimum_buy_quantity": 100,
            "buy_quantity_increment": 100,
            "price_tick": Decimal("0.01"),
            "t_plus_one": True,
            "is_st": False,
        },
    ]


def _write_sessions(path: Path, rows: list[dict[str, object]]) -> None:
    table: Any = pa.Table.from_pylist(rows)  # pyright: ignore[reportUnknownMemberType]
    pq.write_table(table, path)  # pyright: ignore[reportUnknownMemberType]


def _bar(day: date, *, instrument: str = "300059.SZ") -> DailyBar:
    return DailyBar(
        instrument_id=InstrumentId(instrument),
        session_date=day,
        open=Price(Decimal("10.00")),
        high=Price(Decimal("10.60")),
        low=Price(Decimal("9.80")),
        close=Price(Decimal("10.50")),
        volume=Quantity(1_000),
        turnover=Decimal("10500"),
        available_at=datetime(2025, 1, 3, 7, tzinfo=UTC),
    )


def test_loads_exactly_one_canonical_session_per_bar(tmp_path: Path) -> None:
    path = tmp_path / "instrument_sessions.parquet"
    _write_sessions(path, _session_rows())
    provider = ParquetInstrumentSessionProvider(path)

    sessions = provider.sessions_for((_bar(date(2025, 1, 2)), _bar(date(2025, 1, 3))))

    assert provider.version.startswith("parquet-instrument-sessions.v2+sha256:")
    assert [item.session_date for item in sessions] == [date(2025, 1, 2), date(2025, 1, 3)]
    assert sessions[0].board is Board.CHINEXT
    assert sessions[1].status is TradingStatus.SUSPENDED
    assert sessions[0].upper_limit == Price(Decimal("12.00"))
    assert sessions[0].t_plus_one is True


def test_loads_one_instrument_period_without_a_global_bar_context(tmp_path: Path) -> None:
    path = tmp_path / "instrument_sessions.parquet"
    _write_sessions(path, _session_rows())
    provider = ParquetInstrumentSessionProvider(path)

    sessions = provider.sessions_for_period(
        InstrumentId("300059.SZ"),
        start=date(2025, 1, 2),
        end=date(2025, 1, 3),
    )

    assert [item.session_date for item in sessions] == [date(2025, 1, 2), date(2025, 1, 3)]
    assert all(item.instrument_id == InstrumentId("300059.SZ") for item in sessions)


def test_period_read_rejects_an_inverted_range(tmp_path: Path) -> None:
    path = tmp_path / "instrument_sessions.parquet"
    _write_sessions(path, _session_rows())
    provider = ParquetInstrumentSessionProvider(path)

    with pytest.raises(SessionReferenceSchemaError, match="start must not exceed end"):
        provider.sessions_for_period(
            InstrumentId("300059.SZ"),
            start=date(2025, 1, 3),
            end=date(2025, 1, 2),
        )


def test_rejects_missing_or_unexpected_session_dates(tmp_path: Path) -> None:
    path = tmp_path / "instrument_sessions.parquet"
    _write_sessions(path, _session_rows()[:1])
    provider = ParquetInstrumentSessionProvider(path)

    with pytest.raises(SessionReferenceSchemaError, match="missing=2025-01-03"):
        provider.sessions_for((_bar(date(2025, 1, 2)), _bar(date(2025, 1, 3))))


def test_rejects_duplicate_session_rows(tmp_path: Path) -> None:
    path = tmp_path / "instrument_sessions.parquet"
    rows = _session_rows()
    _write_sessions(path, [rows[0], rows[0]])
    provider = ParquetInstrumentSessionProvider(path)

    with pytest.raises(SessionReferenceSchemaError, match="duplicate"):
        provider.sessions_for((_bar(date(2025, 1, 2)),))


def test_rejects_cross_instrument_bar_input(tmp_path: Path) -> None:
    path = tmp_path / "instrument_sessions.parquet"
    _write_sessions(path, _session_rows())
    provider = ParquetInstrumentSessionProvider(path)

    with pytest.raises(SessionReferenceSchemaError, match="one instrument"):
        provider.sessions_for(
            (_bar(date(2025, 1, 2)), _bar(date(2025, 1, 3), instrument="000001.SZ"))
        )


@pytest.mark.parametrize(
    ("field_name", "bad_value", "message"),
    [
        ("board", "unknown", "invalid board"),
        ("previous_close", Decimal("0"), "previous_close must be positive"),
        (
            "minimum_buy_quantity",
            0,
            "minimum_buy_quantity must be positive",
        ),
        (
            "buy_quantity_increment",
            0,
            "buy_quantity_increment must be positive",
        ),
        ("price_tick", Decimal("0"), "price_tick must be positive"),
        ("t_plus_one", "true", "t_plus_one must be BOOLEAN"),
    ],
)
def test_rejects_invalid_session_values(
    tmp_path: Path,
    field_name: str,
    bad_value: object,
    message: str,
) -> None:
    path = tmp_path / "instrument_sessions.parquet"
    row = _session_rows()[0]
    row[field_name] = bad_value
    _write_sessions(path, [row])
    provider = ParquetInstrumentSessionProvider(path)

    with pytest.raises(SessionReferenceSchemaError, match=message):
        provider.sessions_for((_bar(date(2025, 1, 2)),))


def test_rejects_file_replaced_after_provider_is_pinned(tmp_path: Path) -> None:
    path = tmp_path / "instrument_sessions.parquet"
    rows = _session_rows()
    _write_sessions(path, rows)
    provider = ParquetInstrumentSessionProvider(path)
    rows[0]["previous_close"] = Decimal("10.01")
    _write_sessions(path, rows)

    with pytest.raises(SessionReferenceIntegrityError, match="changed"):
        provider.sessions_for((_bar(date(2025, 1, 2)),))


def test_constructor_rejects_missing_required_columns(tmp_path: Path) -> None:
    path = tmp_path / "instrument_sessions.parquet"
    table: Any = pa.table(  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
        {"stock_code": ["300059"], "date": [date(2025, 1, 2)]}
    )
    pq.write_table(table, path)  # pyright: ignore[reportUnknownMemberType]

    with pytest.raises(SessionReferenceSchemaError, match="does not match"):
        ParquetInstrumentSessionProvider(path)


def test_constructor_rejects_legacy_lot_size_only_schema(tmp_path: Path) -> None:
    path = tmp_path / "instrument_sessions.parquet"
    row = _session_rows()[0]
    row["lot_size"] = 100
    del row["minimum_buy_quantity"]
    del row["buy_quantity_increment"]
    _write_sessions(path, [row])

    with pytest.raises(SessionReferenceSchemaError, match="does not match v2 schema"):
        ParquetInstrumentSessionProvider(path)


@pytest.mark.parametrize(
    ("board", "minimum", "increment"),
    [
        ("main", 100, 100),
        ("chinext", 100, 100),
        ("star", 200, 1),
        ("bse", 100, 1),
    ],
)
def test_loads_explicit_board_specific_buy_quantity_rule(
    tmp_path: Path,
    board: str,
    minimum: int,
    increment: int,
) -> None:
    path = tmp_path / "instrument_sessions.parquet"
    row = _session_rows()[0]
    row["board"] = board
    row["minimum_buy_quantity"] = minimum
    row["buy_quantity_increment"] = increment
    _write_sessions(path, [row])

    session = ParquetInstrumentSessionProvider(path).sessions_for((_bar(date(2025, 1, 2)),))[0]

    assert session.minimum_buy_quantity == minimum
    assert session.buy_quantity_increment == increment
