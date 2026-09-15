from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from ashare_lab.domain.execution import (
    CapacityMode,
    DailyBarMatchingModel,
    DailyBarMatchRequest,
    ExecutionTimeQuality,
    LimitHandling,
    MatchOutcome,
    PointInTimeVolume,
    VolumeSource,
    previous_session_volume_proxy,
)
from ashare_lab.domain.market_data import Board, DailyBar, InstrumentSession, TradingStatus
from ashare_lab.domain.orders import OrderSide, OrderStateMachine
from ashare_lab.domain.shared import (
    DecisionId,
    DomainValidationError,
    InstrumentId,
    OrderEventId,
    OrderId,
    Price,
    Quantity,
)

TZ = ZoneInfo("Asia/Shanghai")
DAY = date(2025, 1, 3)
INSTRUMENT = InstrumentId("300059.SZ")
PREVIOUS_DAY = date(2025, 1, 2)


def dt(hour: int, minute: int) -> datetime:
    return datetime(2025, 1, 3, hour, minute, tzinfo=TZ)


def make_order(side: OrderSide = OrderSide.BUY, quantity: int = 1000):
    limit = "12.00" if side is OrderSide.BUY else "8.00"
    created = OrderStateMachine.create(
        order_id=OrderId("ord-1"),
        event_id=OrderEventId("oev-1"),
        decision_id=DecisionId("decision-1"),
        instrument_id=INSTRUMENT,
        side=side,
        quantity=Quantity(quantity),
        limit_price=Price(Decimal(limit)),
        created_at=dt(9, 20),
        valid_from=dt(9, 30),
        valid_until=dt(15, 0),
    )
    submitted = OrderStateMachine.submit(
        created.order, event_id=OrderEventId("oev-2"), submitted_at=dt(9, 21)
    )
    return OrderStateMachine.accept(
        submitted.order,
        event_id=OrderEventId("oev-3"),
        accepted_at=dt(9, 22),
        fill_eligible_at=dt(9, 30),
    ).order


def make_bar(open_: str, high: str, low: str, close: str, volume: int = 100_000) -> DailyBar:
    return DailyBar(
        instrument_id=INSTRUMENT,
        session_date=DAY,
        open=Price(Decimal(open_)),
        high=Price(Decimal(high)),
        low=Price(Decimal(low)),
        close=Price(Decimal(close)),
        volume=Quantity(volume),
        turnover=Decimal("1000000"),
        available_at=dt(15, 0),
    )


def session(
    status: TradingStatus = TradingStatus.TRADING,
    *,
    board: Board = Board.CHINEXT,
) -> InstrumentSession:
    minimum, increment = {
        Board.MAIN: (100, 100),
        Board.CHINEXT: (100, 100),
        Board.STAR: (200, 1),
        Board.BSE: (100, 1),
    }[board]
    return InstrumentSession(
        instrument_id=INSTRUMENT,
        session_date=DAY,
        board=board,
        status=status,
        previous_close=Price(Decimal("10")),
        upper_limit=Price(Decimal("12")),
        lower_limit=Price(Decimal("8")),
        minimum_buy_quantity=minimum,
        buy_quantity_increment=increment,
    )


def match(bar: DailyBar, **overrides: object):
    values: dict[str, object] = {
        "order": make_order(),
        "bar": bar,
        "session": session(),
        "opening_price_proxy_at": dt(9, 30),
        "point_in_time_volume": PointInTimeVolume(
            quantity=Quantity(100_000),
            known_at=datetime(2025, 1, 2, 15, 0, tzinfo=TZ),
            source=VolumeSource.PREVIOUS_SESSION_DAILY_BAR,
            source_session_date=PREVIOUS_DAY,
        ),
    }
    values.update(overrides)
    return DailyBarMatchingModel.match(DailyBarMatchRequest(**values))  # type: ignore[arg-type]


def test_next_session_open_fills_with_separate_conservative_slippage() -> None:
    result = match(
        make_bar("10.00", "10.50", "9.90", "10.40"),
        slippage_bps=Decimal("10"),
    )

    assert result.outcome is MatchOutcome.FILLED
    assert result.price == Price(Decimal("10.01"))
    assert result.filled_at == dt(9, 30)
    assert result.time_quality is ExecutionTimeQuality.DAILY_BAR_OPEN_PROXY
    assert result.capacity_reason_code == "capacity_previous_session_volume_proxy"


@pytest.mark.parametrize(("side", "ohlc", "slippage_cny", "expected"), [
    (OrderSide.BUY, ("10", "10", "9", "9.5"), "0", "10"),
    (OrderSide.SELL, ("10", "11", "10", "10.5"), "0", "10"),
    (OrderSide.BUY, ("10", "10.2", "9.8", "10"), "1", "10.2"),
    (OrderSide.SELL, ("10", "10.2", "9.8", "10"), "100", "9.8"),
])
def test_daily_market_friction_is_bounded_by_the_actual_bar(side, ohlc, slippage_cny, expected):
    result = match(make_bar(*ohlc), order=make_order(side),
                   slippage_bps=Decimal(5), slippage_cny=Decimal(slippage_cny))
    assert result.outcome is MatchOutcome.FILLED
    assert result.price.amount == Decimal(expected)
    assert result.filled_at == dt(9, 30)


def test_one_price_limit_up_is_not_filled_by_default_even_when_volume_exists() -> None:
    result = match(make_bar("12", "12", "12", "12"))

    assert result.outcome is MatchOutcome.NO_FILL
    assert result.reason_code == "one_price_limit_up"


def test_daily_open_at_limit_then_unlock_remains_ambiguous_in_default_mode() -> None:
    result = match(make_bar("12", "12", "11.50", "11.80"))

    assert result.outcome is MatchOutcome.NO_FILL
    assert result.reason_code == "daily_unlock_timing_unknown"


def test_optimistic_mode_is_explicit_and_fills_at_close_observation_time() -> None:
    result = match(
        make_bar("12", "12", "12", "12"),
        limit_handling=LimitHandling.ALLOW_LIMIT_VOLUME,
        slippage_bps=Decimal("5"),
    )

    assert result.outcome is MatchOutcome.FILLED
    assert result.price == Price(Decimal("12"))
    assert result.filled_at == dt(15, 0)
    assert result.time_quality is ExecutionTimeQuality.DAILY_BAR_AVAILABLE_AT_PROXY


def test_optimistic_limit_down_sell_clamps_slippage_to_exchange_band() -> None:
    result = match(
        make_bar("8", "8", "8", "8"),
        order=make_order(OrderSide.SELL),
        limit_handling=LimitHandling.ALLOW_LIMIT_VOLUME,
        slippage_bps=Decimal("5"),
    )

    assert result.outcome is MatchOutcome.FILLED
    assert result.price == Price(Decimal("8"))
    assert result.filled_at == dt(15, 0)


def test_volume_participation_can_produce_an_odd_lot_partial_fill() -> None:
    result = match(
        make_bar("10", "10", "10", "10", volume=1),
        participation_rate=Decimal("0.05"),
        point_in_time_volume=PointInTimeVolume(
            quantity=Quantity(5_900),
            known_at=datetime(2025, 1, 2, 15, 0, tzinfo=TZ),
            source=VolumeSource.PREVIOUS_SESSION_DAILY_BAR,
            source_session_date=PREVIOUS_DAY,
        ),
    )

    assert result.outcome is MatchOutcome.PARTIALLY_FILLED
    assert result.quantity == Quantity(295)


@pytest.mark.parametrize(
    ("board", "quantity"),
    [
        (Board.MAIN, 150),
        (Board.CHINEXT, 101),
        (Board.STAR, 199),
        (Board.BSE, 99),
    ],
)
def test_invalid_buy_declaration_fails_closed(board: Board, quantity: int) -> None:
    with pytest.raises(DomainValidationError, match="buy order quantity violates"):
        match(
            make_bar("10", "10", "10", "10"),
            order=make_order(OrderSide.BUY, quantity=quantity),
            session=session(board=board),
            capacity_mode=CapacityMode.UNLIMITED,
            point_in_time_volume=None,
        )


@pytest.mark.parametrize(
    ("board", "quantity"),
    [(Board.STAR, 201), (Board.BSE, 101)],
)
def test_star_and_bse_buy_declarations_increment_by_one_share(
    board: Board,
    quantity: int,
) -> None:
    result = match(
        make_bar("10", "10", "10", "10"),
        order=make_order(OrderSide.BUY, quantity=quantity),
        session=session(board=board),
        capacity_mode=CapacityMode.UNLIMITED,
        point_in_time_volume=None,
    )

    assert result.outcome is MatchOutcome.FILLED
    assert result.quantity == Quantity(quantity)


@pytest.mark.parametrize("quantity", [50, 150, 299])
def test_sell_can_liquidate_integer_odd_lot_without_buy_lot_rounding(quantity: int) -> None:
    result = match(
        make_bar("10", "10", "10", "10"),
        order=make_order(OrderSide.SELL, quantity=quantity),
        capacity_mode=CapacityMode.UNLIMITED,
        point_in_time_volume=None,
    )

    assert result.outcome is MatchOutcome.FILLED
    assert result.quantity == Quantity(quantity)


def test_current_session_final_volume_is_never_used_for_capacity() -> None:
    small_final = match(make_bar("10", "10", "10", "10", volume=1))
    large_final = match(make_bar("10", "10", "10", "10", volume=99_999_999))

    assert small_final == large_final
    assert small_final.outcome is MatchOutcome.FILLED


def test_completed_bar_builds_auditable_next_session_proxy() -> None:
    completed = make_bar("10", "10", "10", "10", volume=12_345)
    proxy = previous_session_volume_proxy(completed)

    assert proxy.quantity == Quantity(12_345)
    assert proxy.known_at == dt(15, 0)
    assert proxy.source_session_date == DAY
    assert proxy.reason_code == "capacity_previous_session_volume_proxy"


def test_missing_point_in_time_capacity_fails_closed() -> None:
    result = match(
        make_bar("10", "10", "10", "10"),
        point_in_time_volume=None,
    )

    assert result.outcome is MatchOutcome.NO_FILL
    assert result.reason_code == "point_in_time_capacity_unknown"


def test_unlimited_capacity_requires_an_explicit_mode() -> None:
    result = match(
        make_bar("10", "10", "10", "10", volume=1),
        capacity_mode=CapacityMode.UNLIMITED,
        point_in_time_volume=None,
    )

    assert result.outcome is MatchOutcome.FILLED
    assert result.quantity == Quantity(1000)
    assert result.capacity_reason_code == "capacity_unlimited_explicit"


@pytest.mark.parametrize("side", [OrderSide.BUY, OrderSide.SELL])
@pytest.mark.parametrize("mode", list(CapacityMode))
def test_zero_session_volume_never_creates_a_fill_even_with_unlimited_capacity(side, mode):
    overrides = {"order": make_order(side), "capacity_mode": mode}
    if mode is CapacityMode.UNLIMITED:
        overrides["point_in_time_volume"] = None
    result = match(make_bar("10", "10", "10", "10", volume=0), **overrides)
    assert result.outcome is MatchOutcome.NO_FILL
    assert result.reason_code == "no_market_trades"
    assert result.quantity.value == 0 and result.price is None


def test_unlimited_capacity_rejects_a_conflicting_volume_observation() -> None:
    observation = PointInTimeVolume(
        quantity=Quantity(100_000),
        known_at=datetime(2025, 1, 2, 15, 0, tzinfo=TZ),
        source=VolumeSource.PREVIOUS_SESSION_DAILY_BAR,
        source_session_date=PREVIOUS_DAY,
    )

    with pytest.raises(DomainValidationError, match="unlimited capacity"):
        match(
            make_bar("10", "10", "10", "10"),
            capacity_mode=CapacityMode.UNLIMITED,
            point_in_time_volume=observation,
        )


def test_volume_known_after_open_is_rejected_as_lookahead() -> None:
    with pytest.raises(DomainValidationError, match="known by opening_price_proxy_at"):
        match(
            make_bar("10", "10", "10", "10"),
            point_in_time_volume=PointInTimeVolume(
                quantity=Quantity(100_000),
                known_at=dt(15, 0),
                source=VolumeSource.EXPLICIT_POINT_IN_TIME_OBSERVATION,
            ),
        )


def test_previous_session_proxy_must_actually_precede_the_trade_date() -> None:
    with pytest.raises(DomainValidationError, match="date before"):
        match(
            make_bar("10", "10", "10", "10"),
            point_in_time_volume=PointInTimeVolume(
                quantity=Quantity(100_000),
                known_at=dt(9, 0),
                source=VolumeSource.PREVIOUS_SESSION_DAILY_BAR,
                source_session_date=DAY,
            ),
        )


def test_zero_known_volume_is_a_specific_conservative_rejection() -> None:
    result = match(
        make_bar("10", "10", "10", "10"),
        point_in_time_volume=PointInTimeVolume(
            quantity=Quantity.zero(),
            known_at=datetime(2025, 1, 2, 15, 0, tzinfo=TZ),
            source=VolumeSource.PREVIOUS_SESSION_DAILY_BAR,
            source_session_date=PREVIOUS_DAY,
        ),
    )

    assert result.outcome is MatchOutcome.NO_FILL
    assert result.reason_code == "point_in_time_volume_zero"


def test_positive_volume_that_rounds_to_zero_capacity_is_not_a_fake_fill() -> None:
    result = match(
        make_bar("10", "10", "10", "10"),
        participation_rate=Decimal("0.05"),
        point_in_time_volume=PointInTimeVolume(
            quantity=Quantity(1),
            known_at=datetime(2025, 1, 2, 15, 0, tzinfo=TZ),
            source=VolumeSource.PREVIOUS_SESSION_DAILY_BAR,
            source_session_date=PREVIOUS_DAY,
        ),
    )

    assert result.outcome is MatchOutcome.NO_FILL
    assert result.reason_code == "participation_capacity_zero"


def test_suspension_never_fills() -> None:
    result = match(
        make_bar("10", "10", "10", "10"),
        session=session(TradingStatus.SUSPENDED),
    )

    assert result.outcome is MatchOutcome.NO_FILL
    assert result.reason_code == "security_suspended"


def test_sell_at_one_price_limit_down_is_symmetric() -> None:
    result = match(
        make_bar("8", "8", "8", "8"),
        order=make_order(OrderSide.SELL),
    )

    assert result.outcome is MatchOutcome.NO_FILL
    assert result.reason_code == "one_price_limit_down"


def test_order_cannot_fill_before_its_eligibility_time() -> None:
    order = make_order()
    delayed = replace(order, fill_eligible_at=dt(9, 31))
    result = match(make_bar("10", "10", "10", "10"), order=delayed)

    assert result.outcome is MatchOutcome.NO_FILL
    assert result.reason_code == "fill_not_yet_eligible"


def test_no_fill_result_does_not_claim_a_retroactive_timestamp() -> None:
    result = match(make_bar("12", "12", "11", "11.5"))

    assert result.filled_at is None
    assert result.price is None
    assert result.quantity == Quantity.zero()
