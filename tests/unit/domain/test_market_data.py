from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from ashare_lab.domain.market_data import (
    Board,
    DailyBar,
    EventEnvelope,
    InstrumentSession,
    MarketEvent,
    TimeQuality,
    TradingStatus,
)
from ashare_lab.domain.shared import (
    DomainValidationError,
    InstrumentId,
    Price,
    Quantity,
    StrongId,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


def price(value: str) -> Price:
    return Price(Decimal(value))


def test_daily_bar_rejects_impossible_ohlc() -> None:
    with pytest.raises(DomainValidationError, match="OHLC"):
        DailyBar(
            instrument_id=InstrumentId("300059.SZ"),
            session_date=date(2025, 1, 2),
            open=price("10.00"),
            high=price("9.90"),
            low=price("9.50"),
            close=price("9.80"),
            volume=Quantity(1000),
            turnover=Decimal("9800"),
            available_at=datetime(2025, 1, 2, 15, 0, tzinfo=SHANGHAI),
        )


def test_session_carries_historical_limits_instead_of_recomputing_from_prefix() -> None:
    session = InstrumentSession(
        instrument_id=InstrumentId("300059.SZ"),
        session_date=date(2025, 1, 2),
        board=Board.CHINEXT,
        status=TradingStatus.TRADING,
        previous_close=price("10.00"),
        upper_limit=price("12.00"),
        lower_limit=price("8.00"),
        minimum_buy_quantity=100,
        buy_quantity_increment=100,
    )

    assert session.upper_limit.amount == Decimal("12.00")
    assert session.t_plus_one is True


@pytest.mark.parametrize(
    ("board", "minimum", "increment", "valid", "invalid"),
    [
        (Board.MAIN, 100, 100, (100, 200), (1, 99, 101)),
        (Board.STOCK_ETF, 100, 100, (100, 200), (1, 99, 101)),
        (Board.CHINEXT, 100, 100, (100, 200), (1, 99, 101)),
        (Board.STAR, 200, 1, (200, 201, 299), (1, 199)),
        (Board.BSE, 100, 1, (100, 101, 299), (1, 99)),
    ],
)
def test_session_enforces_board_specific_buy_declaration_rules(
    board: Board,
    minimum: int,
    increment: int,
    valid: tuple[int, ...],
    invalid: tuple[int, ...],
) -> None:
    session = InstrumentSession(
        instrument_id=InstrumentId("600000.SH"),
        session_date=date(2025, 1, 2),
        board=board,
        status=TradingStatus.TRADING,
        previous_close=price("10.00"),
        upper_limit=price("12.00"),
        lower_limit=price("8.00"),
        minimum_buy_quantity=minimum,
        buy_quantity_increment=increment,
        price_tick=Decimal("0.001") if board is Board.STOCK_ETF else Decimal("0.01"),
    )

    assert all(session.is_valid_buy_quantity(item) for item in valid)
    assert not any(session.is_valid_buy_quantity(item) for item in invalid)
    assert session.floor_buy_quantity(max(valid)) == max(valid)


def test_session_rejects_a_quantity_rule_that_does_not_match_its_board() -> None:
    with pytest.raises(DomainValidationError, match="star requires minimum=200, increment=1"):
        InstrumentSession(
            instrument_id=InstrumentId("688001.SH"),
            session_date=date(2025, 1, 2),
            board=Board.STAR,
            status=TradingStatus.TRADING,
            previous_close=price("10.00"),
            upper_limit=price("12.00"),
            lower_limit=price("8.00"),
            minimum_buy_quantity=100,
            buy_quantity_increment=100,
        )


def test_stock_etf_session_requires_exchange_tick_and_t_plus_one() -> None:
    session = InstrumentSession(
        instrument_id=InstrumentId("510300.SH"),
        session_date=date(2025, 1, 2),
        board=Board.STOCK_ETF,
        status=TradingStatus.TRADING,
        previous_close=price("4.001"),
        upper_limit=price("4.401"),
        lower_limit=price("3.601"),
        minimum_buy_quantity=100,
        buy_quantity_increment=100,
        price_tick=Decimal("0.001"),
        t_plus_one=True,
    )

    assert session.price_tick == Decimal("0.001")
    assert session.t_plus_one is True

    with pytest.raises(DomainValidationError, match=r"stock_etf requires price_tick=0\.001"):
        InstrumentSession(
            instrument_id=InstrumentId("510300.SH"),
            session_date=date(2025, 1, 2),
            board=Board.STOCK_ETF,
            status=TradingStatus.TRADING,
            previous_close=price("4.00"),
            upper_limit=price("4.40"),
            lower_limit=price("3.60"),
            minimum_buy_quantity=100,
            buy_quantity_increment=100,
            price_tick=Decimal("0.01"),
        )


def test_event_visibility_uses_latest_release_vendor_and_ingestion_time() -> None:
    event = EventEnvelope(
        event=MarketEvent(
            event_id=StrongId("evt-1"),
            event_code="earnings.forecast.up",
            instrument_id=InstrumentId("300059.SZ"),
            attributes={"change_pct": Decimal("30")},
        ),
        occurred_at=datetime(2025, 1, 2, 10, 0, tzinfo=SHANGHAI),
        source_released_at=datetime(2025, 1, 2, 18, 0, tzinfo=SHANGHAI),
        vendor_first_available_at=datetime(2025, 1, 2, 18, 1, tzinfo=SHANGHAI),
        ingested_at=datetime(2025, 1, 2, 18, 3, tzinfo=SHANGHAI),
        revision_no=0,
        time_quality=TimeQuality.EXACT,
    )

    assert not event.is_visible_at(datetime(2025, 1, 2, 18, 2, tzinfo=SHANGHAI))
    assert event.is_visible_at(datetime(2025, 1, 2, 18, 3, tzinfo=SHANGHAI))


def test_estimated_event_is_research_only_and_never_trade_visible() -> None:
    event = EventEnvelope(
        event=MarketEvent(
            event_id=StrongId("evt-estimated"),
            event_code="financial.report.estimated",
            instrument_id=InstrumentId("300059.SZ"),
            attributes={},
        ),
        occurred_at=None,
        source_released_at=None,
        vendor_first_available_at=None,
        ingested_at=datetime(2025, 1, 2, 18, 3, tzinfo=SHANGHAI),
        revision_no=0,
        time_quality=TimeQuality.ESTIMATED_RESEARCH_ONLY,
    )

    assert event.available_at is None
    assert not event.is_visible_at(datetime(2025, 1, 3, 9, 30, tzinfo=SHANGHAI))


def test_validated_historical_snapshot_keeps_retrieval_and_replay_clocks_separate() -> None:
    event = EventEnvelope(
        event=MarketEvent(
            event_id=StrongId("evt-historical"),
            event_code="event.financial_results.annual_report",
            instrument_id=InstrumentId("300059.SZ"),
            attributes={},
        ),
        occurred_at=None,
        source_released_at=datetime(2025, 3, 18, 18, 0, tzinfo=SHANGHAI),
        vendor_first_available_at=datetime(2025, 3, 18, 18, 1, tzinfo=SHANGHAI),
        ingested_at=datetime(2026, 8, 29, 12, 0, tzinfo=SHANGHAI),
        replay_available_at=datetime(2025, 3, 18, 18, 1, tzinfo=SHANGHAI),
        revision_no=0,
        time_quality=TimeQuality.VENDOR_OBSERVED,
        source_event_id="AN202503180000000001",
        provider="eastmoney",
        source_url="https://example.test/announcement",
        raw_response_sha256="a" * 64,
        validation_status="validated",
    )

    assert event.available_at == datetime(2025, 3, 18, 18, 1, tzinfo=SHANGHAI)
    assert event.ingested_at.year == 2026


def test_replay_clock_cannot_backdate_source_or_vendor_time() -> None:
    with pytest.raises(DomainValidationError, match="cannot precede"):
        EventEnvelope(
            event=MarketEvent(
                event_id=StrongId("evt-backdated"),
                event_code="event.financial_results.annual_report",
                instrument_id=InstrumentId("300059.SZ"),
                attributes={},
            ),
            occurred_at=None,
            source_released_at=datetime(2025, 3, 18, 18, 0, tzinfo=SHANGHAI),
            vendor_first_available_at=datetime(2025, 3, 18, 18, 1, tzinfo=SHANGHAI),
            ingested_at=datetime(2026, 8, 29, 12, 0, tzinfo=SHANGHAI),
            replay_available_at=datetime(2025, 3, 18, 17, 59, tzinfo=SHANGHAI),
            revision_no=0,
            time_quality=TimeQuality.VENDOR_OBSERVED,
            validation_status="validated",
        )
