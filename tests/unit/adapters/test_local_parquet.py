from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from ashare_lab.adapters.market_data import (
    LocalParquetMarketDataRepository,
    MarketDataCapabilityError,
    MarketDataSchemaError,
    SnapshotIntegrityError,
    SnapshotScopeError,
    normalize_instrument_id,
)
from ashare_lab.domain.market_data import (
    Board,
    CorporateActionKind,
    DataSnapshotRef,
    InstrumentSession,
    TradingStatus,
)
from ashare_lab.domain.shared import InstrumentId, Price
from ashare_lab.ports.market_data import DataRequirements, DateRange

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _write_daily_file(path: Path, *, first_close: float = 10.1) -> None:
    rows = [
        ("000001", date(2025, 1, 2), 10.0, 10.2, 9.8, first_close, 1000, 10100.0, 0.1),
        ("000001", date(2025, 1, 3), 10.1, 10.4, 10.0, 10.3, 1200, 12360.0, 0.2),
        ("600000", date(2025, 1, 3), 9.0, 9.3, 8.9, 9.2, 900, 8280.0, 0.3),
        ("000001", date(2025, 1, 6), 10.3, 10.6, 10.2, 10.5, 1500, 15750.0, 0.4),
    ]
    with duckdb.connect(database=":memory:") as connection:
        connection.execute(
            """
            CREATE TABLE daily (
                stock_code VARCHAR,
                "date" DATE,
                "open" DOUBLE,
                high DOUBLE,
                low DOUBLE,
                "close" DOUBLE,
                volume BIGINT,
                amount DOUBLE,
                turnover_rate DOUBLE
            )
            """
        )
        connection.executemany(
            "INSERT INTO daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        # ``turnover_rate`` is intentionally present but never projected by the adapter.
        connection.execute("COPY daily TO ? (FORMAT PARQUET)", [str(path)])


def _write_events_file(path: Path, *, source: str = "fixture") -> None:
    rows = [
        (
            "000001",
            "event-annual-2024",
            "event.financial_results.annual_report",
            "2024-12-31",
            "2025-01-02",
            None,
            "2025-01-02",
            0,
            "date_only_conservative",
            f'{{"report_type":"annual","source":"{source}","eps":1.25}}',
        ),
        (
            "000001",
            "event-forecast-1",
            "event.financial_results.earnings_forecast_published",
            None,
            "2025-01-03T08:00:00+08:00",
            "2025-01-03T08:01:00+08:00",
            "2025-01-03T08:02:00+08:00",
            0,
            "exact",
            '{"provider":"eastmoney","report_type":"earnings_forecast",'
            '"forecast_direction":"increase"}',
        ),
    ]
    with duckdb.connect(database=":memory:") as connection:
        connection.execute(
            """
            CREATE TABLE events (
                stock_code VARCHAR,
                event_id VARCHAR,
                event_code VARCHAR,
                occurred_at VARCHAR,
                source_released_at VARCHAR,
                vendor_first_available_at VARCHAR,
                ingested_at VARCHAR,
                revision_no INTEGER,
                time_quality VARCHAR,
                attributes_json VARCHAR
            )
            """
        )
        connection.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        connection.execute("COPY events TO ? (FORMAT PARQUET)", [str(path)])


def _write_corporate_actions_file(path: Path) -> None:
    schema = pa.schema(
        [
            ("stock_code", pa.string()),
            ("action_id", pa.string()),
            ("source_action_id", pa.string()),
            ("action_type", pa.string()),
            ("record_date", pa.date32()),
            ("ex_date", pa.date32()),
            ("source_released_at", pa.string()),
            ("vendor_first_available_at", pa.string()),
            ("ingested_at", pa.string()),
            ("replay_available_at", pa.string()),
            ("revision_no", pa.int32()),
            ("time_quality", pa.string()),
            ("provider", pa.string()),
            ("source_url", pa.string()),
            ("raw_response_sha256", pa.string()),
            ("validation_status", pa.string()),
            ("currency", pa.string()),
            ("gross_cash_per_share", pa.decimal128(20, 8)),
            ("cash_pay_date", pa.date32()),
            ("share_multiplier", pa.decimal128(20, 8)),
            ("share_credit_date", pa.date32()),
            ("share_sellable_date", pa.date32()),
            ("rights_ratio", pa.decimal128(20, 8)),
            ("rights_subscription_price", pa.decimal128(20, 8)),
            ("rights_payment_deadline", pa.date32()),
            ("rights_listing_date", pa.date32()),
        ]
    )
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "stock_code": "000001",
                    "action_id": "action:cash:2025",
                    "source_action_id": "source:cash:2025",
                    "action_type": "cash_dividend",
                    "record_date": date(2025, 1, 2),
                    "ex_date": date(2025, 1, 3),
                    "source_released_at": "2025-01-02T08:00:00+08:00",
                    "vendor_first_available_at": "2025-01-02T08:01:00+08:00",
                    "ingested_at": "2025-01-02T08:03:00+08:00",
                    "replay_available_at": "2025-01-02T08:02:00+08:00",
                    "revision_no": 0,
                    "time_quality": "exact",
                    "provider": "fixture",
                    "source_url": "https://example.test/dividend",
                    "raw_response_sha256": "a" * 64,
                    "validation_status": "validated",
                    "currency": "CNY",
                    "gross_cash_per_share": Decimal("0.5"),
                    "cash_pay_date": date(2025, 1, 6),
                }
            ],
            schema=schema,
        ),
        path,
    )


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    _write_daily_file(tmp_path / "daily_ohlcv.parquet")
    return tmp_path


def _requirements(*codes: str) -> DataRequirements:
    return DataRequirements(
        instruments=tuple(InstrumentId(code) for code in codes),
        datasets=("daily_ohlcv",),
    )


def _event_requirements(*codes: str) -> DataRequirements:
    return DataRequirements(
        instruments=tuple(InstrumentId(code) for code in codes),
        datasets=("daily_ohlcv", "events"),
    )


def _corporate_action_requirements(*codes: str) -> DataRequirements:
    return DataRequirements(
        instruments=tuple(InstrumentId(code) for code in codes),
        datasets=("daily_ohlcv", "corporate_actions"),
    )


def test_pin_snapshot_is_deterministic_for_the_same_selection(data_root: Path) -> None:
    repository = LocalParquetMarketDataRepository(data_root)
    requirements = _requirements("000001", "600000.SH")
    period = DateRange(date(2025, 1, 2), date(2025, 1, 6))

    first = repository.pin_snapshot(requirements, period)
    second = repository.pin_snapshot(requirements, period)

    assert first == second
    assert first.checksum.startswith("sha256:")
    assert str(first.snapshot_id) == f"snapshot:{first.checksum.removeprefix('sha256:')}"


def test_load_daily_bars_prunes_instrument_range_and_columns(data_root: Path) -> None:
    repository = LocalParquetMarketDataRepository(data_root)
    snapshot = repository.pin_snapshot(
        _requirements("000001"),
        DateRange(date(2025, 1, 2), date(2025, 1, 6)),
    )

    bars = repository.load_daily_bars(
        snapshot,
        InstrumentId("000001.SZ"),
        DateRange(date(2025, 1, 3), date(2025, 1, 6)),
    )

    assert [bar.session_date for bar in bars] == [date(2025, 1, 3), date(2025, 1, 6)]
    assert all(bar.instrument_id == InstrumentId("000001.SZ") for bar in bars)
    assert all(bar.available_at.tzinfo == SHANGHAI for bar in bars)
    assert all(bar.available_at.hour == 15 and bar.available_at.minute == 0 for bar in bars)
    assert [int(bar.volume) for bar in bars] == [1200, 1500]


def test_snapshot_rejects_file_changed_after_pin(data_root: Path) -> None:
    repository = LocalParquetMarketDataRepository(data_root)
    period = DateRange(date(2025, 1, 2), date(2025, 1, 6))
    snapshot = repository.pin_snapshot(_requirements("000001"), period)
    _write_daily_file(data_root / "daily_ohlcv.parquet", first_close=10.15)

    with pytest.raises(SnapshotIntegrityError, match="changed"):
        repository.load_daily_bars(snapshot, InstrumentId("000001"), period)


def test_read_cannot_expand_pinned_range_into_the_future(data_root: Path) -> None:
    repository = LocalParquetMarketDataRepository(data_root)
    snapshot = repository.pin_snapshot(
        _requirements("000001"),
        DateRange(date(2025, 1, 2), date(2025, 1, 3)),
    )

    with pytest.raises(SnapshotScopeError, match="exceeds"):
        repository.load_daily_bars(
            snapshot,
            InstrumentId("000001"),
            DateRange(date(2025, 1, 2), date(2025, 1, 6)),
        )

    bars = repository.load_daily_bars(
        snapshot,
        InstrumentId("000001"),
        DateRange(date(2025, 1, 2), date(2025, 1, 2)),
    )
    assert [bar.session_date for bar in bars] == [date(2025, 1, 2)]


def test_session_and_event_capabilities_are_explicit(data_root: Path) -> None:
    repository = LocalParquetMarketDataRepository(data_root)
    period = DateRange(date(2025, 1, 2), date(2025, 1, 3))
    snapshot = repository.pin_snapshot(_requirements("000001"), period)

    with pytest.raises(MarketDataCapabilityError, match="SessionFactory"):
        repository.load_sessions(snapshot, InstrumentId("000001"), period)
    with pytest.raises(MarketDataCapabilityError, match="event"):
        repository.load_events(snapshot, InstrumentId("000001"), period)


def test_load_events_converts_date_only_to_close_and_preserves_exact_availability(
    data_root: Path,
) -> None:
    _write_events_file(data_root / "events.parquet")
    repository = LocalParquetMarketDataRepository(data_root)
    period = DateRange(date(2025, 1, 2), date(2025, 1, 6))
    snapshot = repository.pin_snapshot(_event_requirements("000001"), period)

    events = repository.load_events(snapshot, InstrumentId("000001.SZ"), period)

    assert len(events) == 2
    annual, forecast = events
    assert annual.source_released_at == datetime(2025, 1, 2, 15, 0, tzinfo=SHANGHAI)
    assert annual.available_at == datetime(2025, 1, 2, 15, 0, tzinfo=SHANGHAI)
    assert annual.event.attributes["eps"] == Decimal("1.25")
    assert forecast.available_at == datetime(2025, 1, 3, 8, 2, tzinfo=SHANGHAI)
    assert forecast.event.attributes["source"] == "eastmoney"
    assert forecast.event.attributes["forecast_type"] == "earnings_forecast"
    assert forecast.event.attributes["direction"] == "increase"
    assert forecast.event.attributes["forecast_direction"] == "increase"


def test_load_corporate_actions_preserves_terms_and_point_in_time_provenance(
    data_root: Path,
) -> None:
    _write_corporate_actions_file(data_root / "corporate_actions.parquet")
    repository = LocalParquetMarketDataRepository(data_root)
    period = DateRange(date(2025, 1, 2), date(2025, 1, 6))
    snapshot = repository.pin_snapshot(_corporate_action_requirements("000001"), period)

    actions = repository.load_corporate_actions(
        snapshot,
        InstrumentId("000001.SZ"),
        period,
    )

    assert len(actions) == 1
    action = actions[0]
    assert action.action_type is CorporateActionKind.CASH_DIVIDEND
    assert action.record_date == date(2025, 1, 2)
    assert action.ex_date == date(2025, 1, 3)
    assert action.cash_pay_date == date(2025, 1, 6)
    assert action.gross_cash_per_share == Decimal("0.50000000")
    assert action.available_at == datetime(2025, 1, 2, 8, 2, tzinfo=SHANGHAI)


def test_event_file_is_part_of_snapshot_integrity(data_root: Path) -> None:
    event_path = data_root / "events.parquet"
    _write_events_file(event_path)
    repository = LocalParquetMarketDataRepository(data_root)
    period = DateRange(date(2025, 1, 2), date(2025, 1, 6))
    snapshot = repository.pin_snapshot(_event_requirements("000001"), period)
    _write_events_file(event_path, source="changed")

    with pytest.raises(SnapshotIntegrityError, match="changed"):
        repository.load_events(snapshot, InstrumentId("000001"), period)


def test_injected_session_factory_is_used_without_guessing_rules(data_root: Path) -> None:
    def build_sessions(
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> list[InstrumentSession]:
        assert snapshot.schema_version
        return [
            InstrumentSession(
                instrument_id=instrument_id,
                session_date=period.start,
                board=Board.MAIN,
                status=TradingStatus.TRADING,
                previous_close=Price(Decimal("10.00")),
                upper_limit=None,
                lower_limit=None,
                minimum_buy_quantity=100,
                buy_quantity_increment=100,
            )
        ]

    repository = LocalParquetMarketDataRepository(
        data_root,
        session_factory=build_sessions,
    )
    period = DateRange(date(2025, 1, 2), date(2025, 1, 3))
    snapshot = repository.pin_snapshot(_requirements("000001"), period)

    sessions = repository.load_sessions(snapshot, InstrumentId("000001"), period)

    assert len(sessions) == 1
    assert sessions[0].upper_limit is None


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("000001", "000001.SZ"),
        ("300059", "300059.SZ"),
        ("600000", "600000.SH"),
        ("688001", "688001.SH"),
        ("430001", "430001.BJ"),
        ("920001", "920001.BJ"),
    ],
)
def test_normalize_six_digit_stock_code(raw: str, canonical: str) -> None:
    assert normalize_instrument_id(raw) == InstrumentId(canonical)


@pytest.mark.parametrize("raw", ["300059.SH", "399001.SZ", "900901.SH", "200001.SZ"])
def test_normalize_rejects_suffix_conflicts_and_non_share_codes(raw: str) -> None:
    with pytest.raises(MarketDataSchemaError):
        normalize_instrument_id(raw)
