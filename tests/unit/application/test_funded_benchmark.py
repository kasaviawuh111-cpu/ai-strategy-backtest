from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from ashare_lab.application.corporate_action_timeline import TimelineCorporateActionApplier
from ashare_lab.application.funded_benchmark import (
    FundedBenchmarkInput,
    FundedBenchmarkInputError,
    FundedBuyAndHoldConfig,
    run_funded_buy_and_hold,
)
from ashare_lab.domain.execution import (
    AshareExchange,
    CapacityMode,
    FeeCalculator,
    FeePolicy,
    TradingCalendar,
)
from ashare_lab.domain.market_data import (
    Board,
    CorporateAction,
    CorporateActionKind,
    DailyBar,
    InstrumentSession,
    PriceBasis,
    TimeQuality,
    TradingStatus,
)
from ashare_lab.domain.orders import OrderSide
from ashare_lab.domain.portfolio import FeeBreakdown, PortfolioState
from ashare_lab.domain.shared import InstrumentId, Money, Price, Quantity, StrongId
from ashare_lab.ports.corporate_actions import (
    AppliedCorporateAction,
    CorporateActionApplication,
    ExplicitNoCorporateActions,
)

TZ = ZoneInfo("Asia/Shanghai")
INSTRUMENT = InstrumentId("300059.SZ")
PREVIOUS = date(2025, 1, 1)
START = date(2025, 1, 2)


class ZeroFees:
    def calculate(self, **_: object) -> FeeBreakdown:
        return FeeBreakdown.zero()


class TwoForOneSplit:
    policy_id = "test.split.v1"

    def __init__(self, effective_on: date) -> None:
        self._effective_on = effective_on

    def apply_before_session(
        self,
        *,
        portfolio: PortfolioState,
        instrument_id: InstrumentId,
        session: InstrumentSession,
        as_of: datetime,
    ) -> CorporateActionApplication:
        del instrument_id, as_of
        if session.session_date != self._effective_on or not portfolio.lots:
            return CorporateActionApplication(portfolio)
        adjusted = PortfolioState(
            cash=portfolio.cash,
            lots=tuple(
                replace(
                    lot,
                    remaining_quantity=Quantity(lot.remaining_quantity.value * 2),
                )
                for lot in portfolio.lots
            ),
            fills=portfolio.fills,
            ledger_entries=portfolio.ledger_entries,
        )
        return CorporateActionApplication(
            portfolio=adjusted,
            applied_actions=(
                AppliedCorporateAction(
                    action_id="split-2025-01-03",
                    reason_code="two_for_one_share_adjustment",
                ),
            ),
        )


def bar(
    day: date,
    price: str,
    *,
    volume: int = 1_000_000,
    price_basis: PriceBasis = PriceBasis.UNADJUSTED,
) -> DailyBar:
    value = Decimal(price)
    return DailyBar(
        instrument_id=INSTRUMENT,
        session_date=day,
        open=Price(value),
        high=Price(value),
        low=Price(value),
        close=Price(value),
        volume=Quantity(volume),
        turnover=value * Decimal(volume),
        available_at=datetime(day.year, day.month, day.day, 15, tzinfo=TZ),
        price_basis=price_basis,
    )


def market_session(
    source: DailyBar,
    *,
    status: TradingStatus = TradingStatus.TRADING,
    upper: str = "50",
    lower: str = "1",
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
        session_date=source.session_date,
        board=board,
        status=status,
        previous_close=Price(Decimal("10")),
        upper_limit=Price(Decimal(upper)),
        lower_limit=Price(Decimal(lower)),
        minimum_buy_quantity=minimum,
        buy_quantity_increment=increment,
    )


def fee_calculator() -> FeeCalculator:
    return FeeCalculator(
        FeePolicy(
            exchange=AshareExchange.SHENZHEN,
            commission_rate=Decimal("0.0003"),
            minimum_commission=Money(Decimal("5")),
        )
    )


def request(
    source_bars: tuple[DailyBar, ...],
    *,
    source_sessions: tuple[InstrumentSession, ...] | None = None,
    period_start: date = START,
    period_end: date | None = None,
    config: FundedBuyAndHoldConfig | None = None,
    fees: object | None = None,
    corporate_actions: object | None = None,
) -> FundedBenchmarkInput:
    sessions = source_sessions or tuple(market_session(item) for item in source_bars)
    end = period_end or source_bars[-1].session_date
    calendar_dates = tuple(
        PREVIOUS + timedelta(days=offset) for offset in range((end - PREVIOUS).days + 3)
    )
    return FundedBenchmarkInput(
        run_key="funded-benchmark-test",
        period_start=period_start,
        period_end=end,
        bars=source_bars,
        sessions=sessions,
        calendar=TradingCalendar(version="test", sessions=calendar_dates),
        fee_calculator=fees or fee_calculator(),  # type: ignore[arg-type]
        corporate_actions=corporate_actions or ExplicitNoCorporateActions(),  # type: ignore[arg-type]
        config=config
        or FundedBuyAndHoldConfig(
            initial_cash=Money(Decimal("10000")),
            participation_rate=Decimal("1"),
        ),
    )


def test_flat_price_benchmark_includes_fees_and_clips_slippage_to_bar() -> None:
    source_bars = (
        bar(PREVIOUS, "10"),
        bar(START, "10"),
        bar(START + timedelta(days=1), "10"),
    )
    result = run_funded_buy_and_hold(
        request(
            source_bars,
            config=FundedBuyAndHoldConfig(
                initial_cash=Money(Decimal("10000")),
                participation_rate=Decimal("1"),
                slippage_bps=Decimal("10"),
            ),
        )
    )

    fill = result.entry_fill
    assert fill is not None
    assert fill.price == Price(Decimal("10"))
    assert fill.quantity.value % 100 == 0
    assert fill.fees.total.amount > 0
    assert result.initial_cash == Money(Decimal("10000"))
    assert result.final_portfolio.lots[0].sellable_on == START + timedelta(days=1)
    assert result.points[0].shares == 100
    assert result.final_equity < result.initial_cash.amount
    assert result.total_return < 0
    assert result.entry_attempts[0].match.capacity_reason_code == (
        "capacity_previous_session_volume_proxy"
    )


def test_capacity_uses_previous_session_volume_not_current_final_volume() -> None:
    source_bars = (
        bar(PREVIOUS, "10", volume=3_900),
        bar(START, "10", volume=99_999_999),
    )
    result = run_funded_buy_and_hold(
        request(
            source_bars,
            config=FundedBuyAndHoldConfig(
                initial_cash=Money(Decimal("10000")),
                participation_rate=Decimal("0.05"),
                slippage_bps=Decimal("0"),
            ),
            fees=ZeroFees(),
        )
    )

    assert result.entry_fill is not None
    assert result.entry_fill.quantity == Quantity(195)
    assert result.entry_attempts[0].match.capacity_reason_code == (
        "capacity_previous_session_volume_proxy"
    )


@pytest.mark.parametrize(
    ("board", "expected"),
    [
        (Board.MAIN, 200),
        (Board.CHINEXT, 200),
        (Board.STAR, 205),
        (Board.BSE, 205),
    ],
)
def test_funded_benchmark_uses_board_specific_buy_declaration_sizing(
    board: Board,
    expected: int,
) -> None:
    source_bars = (bar(PREVIOUS, "10"), bar(START, "10"))
    source_sessions = tuple(market_session(item, board=board) for item in source_bars)

    result = run_funded_buy_and_hold(
        request(
            source_bars,
            source_sessions=source_sessions,
            fees=ZeroFees(),
            config=FundedBuyAndHoldConfig(
                initial_cash=Money(Decimal("10250")),
                participation_rate=Decimal("1"),
                slippage_bps=Decimal("0"),
            ),
        )
    )

    assert result.entry_fill is not None
    assert result.entry_fill.quantity == Quantity(expected)


def test_benchmark_pre_open_quantity_is_invariant_to_the_future_open() -> None:
    source = (bar(PREVIOUS, "10"), bar(START, "10"))
    fixed_sessions = tuple(market_session(item) for item in source)
    changed = (
        source[0],
        replace(
            source[1],
            open=Price(Decimal("20")),
            high=Price(Decimal("20")),
            low=Price(Decimal("10")),
        ),
    )

    baseline = run_funded_buy_and_hold(request(source, source_sessions=fixed_sessions))
    counterfactual = run_funded_buy_and_hold(request(changed, source_sessions=fixed_sessions))

    baseline_order = baseline.entry_attempts[0].order
    counterfactual_order = counterfactual.entry_attempts[0].order
    assert baseline_order is not None and counterfactual_order is not None
    assert baseline_order.submitted_at == counterfactual_order.submitted_at
    assert baseline_order.submitted_at == datetime(2025, 1, 2, 9, 29, tzinfo=TZ)
    assert baseline_order.quantity == counterfactual_order.quantity
    assert baseline.entry_fill is not None and counterfactual.entry_fill is not None
    assert baseline.entry_fill.price == Price(Decimal("10"))
    assert counterfactual.entry_fill.price == Price(Decimal("20"))


def test_benchmark_pre_open_sizing_reserves_upper_limit_notional_fees_and_slippage() -> None:
    source = (bar(PREVIOUS, "10"), bar(START, "10"))
    source_sessions = tuple(market_session(item, upper="13") for item in source)
    config = FundedBuyAndHoldConfig(
        initial_cash=Money(Decimal("100000")),
        participation_rate=Decimal("1"),
        slippage_bps=Decimal("1000"),
    )

    result = run_funded_buy_and_hold(
        request(
            source,
            source_sessions=source_sessions,
            config=config,
        )
    )

    order = result.entry_attempts[0].order
    assert order is not None
    assert order.limit_price == Price(Decimal("13"))
    assert order.quantity == Quantity(7_600)
    quoted_fees = fee_calculator().calculate(
        side=OrderSide.BUY,
        price=order.limit_price,
        quantity=order.quantity,
        trade_date=order.created_at.date(),
    )
    assert (
        order.limit_price.amount * Decimal(order.quantity.value) + (quoted_fees.total.amount)
        <= config.initial_cash.amount
    )
    next_quantity = Quantity(7_700)
    next_fees = fee_calculator().calculate(
        side=OrderSide.BUY,
        price=order.limit_price,
        quantity=next_quantity,
        trade_date=order.created_at.date(),
    )
    assert (
        order.limit_price.amount * Decimal(next_quantity.value) + (next_fees.total.amount)
        > config.initial_cash.amount
    )


def test_benchmark_without_an_upper_limit_fails_closed_before_order_creation() -> None:
    source = (bar(PREVIOUS, "10"), bar(START, "10"))
    source_sessions = list(market_session(item) for item in source)
    source_sessions[1] = replace(
        source_sessions[1],
        upper_limit=None,
        lower_limit=None,
    )

    result = run_funded_buy_and_hold(request(source, source_sessions=tuple(source_sessions)))
    attempt = result.entry_attempts[0]

    assert attempt.match.reason_code == "pre_open_buy_sizing_upper_limit_unavailable"
    assert attempt.order is None
    assert attempt.fill is None
    assert result.entry_fill is None


def test_missing_prior_bar_fails_closed_then_retries_with_known_proxy() -> None:
    source_bars = (
        bar(START, "10"),
        bar(START + timedelta(days=1), "10"),
    )
    result = run_funded_buy_and_hold(request(source_bars, fees=ZeroFees()))

    assert result.entry_attempts[0].match.reason_code == "point_in_time_capacity_unknown"
    assert result.points[0].shares == 0
    assert result.entry_attempts[1].fill is not None
    assert result.points[1].shares > 0


def test_unlimited_capacity_only_works_when_explicitly_selected() -> None:
    # Unlimited removes the volume participation cap, not the requirement
    # that the session actually traded.
    source_bars = (bar(START, "10", volume=1),)
    result = run_funded_buy_and_hold(
        request(
            source_bars,
            fees=ZeroFees(),
            config=FundedBuyAndHoldConfig(
                initial_cash=Money(Decimal("10000")),
                participation_rate=Decimal("1"),
                slippage_bps=Decimal("0"),
                capacity_mode=CapacityMode.UNLIMITED,
            ),
        )
    )

    assert result.entry_fill is not None
    assert result.entry_attempts[0].match.capacity_reason_code == "capacity_unlimited_explicit"


def test_benchmark_retries_suspension_and_one_price_limit_without_fake_fills() -> None:
    source_bars = (
        bar(PREVIOUS, "10"),
        bar(START, "10"),
        bar(START + timedelta(days=1), "12"),
        bar(START + timedelta(days=2), "10"),
    )
    source_sessions = (
        market_session(source_bars[0]),
        market_session(source_bars[1], status=TradingStatus.SUSPENDED),
        market_session(source_bars[2], upper="12", lower="8"),
        market_session(source_bars[3]),
    )
    result = run_funded_buy_and_hold(
        request(source_bars, source_sessions=source_sessions, fees=ZeroFees())
    )

    assert [item.match.reason_code for item in result.entry_attempts] == [
        "security_suspended",
        "one_price_limit_up",
        "matched_at_open",
    ]
    assert result.entry_fill is not None
    assert result.entry_fill.trading_date == START + timedelta(days=2)


def test_corporate_action_interface_keeps_split_day_equity_continuous() -> None:
    split_day = START + timedelta(days=1)
    source_bars = (
        bar(PREVIOUS, "10"),
        bar(START, "10"),
        bar(split_day, "5"),
    )
    result = run_funded_buy_and_hold(
        request(
            source_bars,
            fees=ZeroFees(),
            corporate_actions=TwoForOneSplit(split_day),
            config=FundedBuyAndHoldConfig(
                initial_cash=Money(Decimal("10000")),
                participation_rate=Decimal("1"),
                slippage_bps=Decimal("0"),
            ),
        )
    )

    assert result.points[0].shares == 200
    assert result.points[1].shares == 400
    assert result.points[0].equity == result.points[1].equity == Decimal("10000")
    assert result.corporate_action_policy_id == "test.split.v1"
    assert result.corporate_actions[0].action.action_id == "split-2025-01-03"


def test_timeline_applier_books_dividend_entitlement_receivable_and_cash() -> None:
    ex_date = START + timedelta(days=1)
    action = CorporateAction(
        action_id=StrongId("action:dividend:2025"),
        source_action_id="source:dividend:2025",
        instrument_id=INSTRUMENT,
        action_type=CorporateActionKind.CASH_DIVIDEND,
        record_date=START,
        ex_date=ex_date,
        source_released_at=datetime(2025, 1, 2, 8, 0, tzinfo=TZ),
        vendor_first_available_at=datetime(2025, 1, 2, 8, 1, tzinfo=TZ),
        ingested_at=datetime(2025, 1, 2, 8, 3, tzinfo=TZ),
        replay_available_at=datetime(2025, 1, 2, 8, 2, tzinfo=TZ),
        revision_no=0,
        time_quality=TimeQuality.EXACT,
        provider="fixture",
        source_url="https://example.test/dividend",
        raw_response_sha256="d" * 64,
        validation_status="validated",
        gross_cash_per_share=Decimal("1"),
        cash_pay_date=ex_date,
    )
    source_bars = (
        bar(PREVIOUS, "10"),
        bar(START, "10"),
        bar(ex_date, "9"),
    )

    result = run_funded_buy_and_hold(
        request(
            source_bars,
            fees=ZeroFees(),
            corporate_actions=TimelineCorporateActionApplier((action,)),
            config=FundedBuyAndHoldConfig(
                initial_cash=Money(Decimal("10000")),
                participation_rate=Decimal("1"),
                slippage_bps=Decimal("0"),
            ),
        )
    )

    assert [point.equity for point in result.points] == [Decimal("10000"), Decimal("10000")]
    assert result.points[-1].cash == Decimal("8000")
    assert result.final_portfolio.cash == Money(Decimal("8200"))
    assert [item.action.phase for item in result.corporate_actions] == [
        "entitlement",
        "accrual",
        "settlement",
    ]
    assert len(result.final_portfolio.corporate_action_entries) == 3
    settlement = result.final_portfolio.corporate_action_entries[-1]
    assert settlement.occurred_at == datetime(2025, 1, 3, 15, tzinfo=TZ)
    assert result.corporate_actions[-1].action.reason_code == (
        "date_only_cash_settled_after_close_conservative"
    )


def test_adjusted_prices_are_rejected_for_funded_accounting() -> None:
    source_bars = (
        bar(PREVIOUS, "10"),
        bar(START, "10", price_basis=PriceBasis.BACK_ADJUSTED),
    )

    with pytest.raises(FundedBenchmarkInputError, match="unadjusted"):
        run_funded_buy_and_hold(request(source_bars, fees=ZeroFees()))


def test_funded_benchmark_declines_rights_without_external_cash() -> None:
    ex_date = START + timedelta(days=1)
    action = CorporateAction(
        action_id=StrongId("action:rights:declined"),
        source_action_id="source:rights:declined",
        instrument_id=INSTRUMENT,
        action_type=CorporateActionKind.RIGHTS_ISSUE,
        record_date=START,
        ex_date=ex_date,
        source_released_at=datetime(2025, 1, 2, 8, 0, tzinfo=TZ),
        vendor_first_available_at=datetime(2025, 1, 2, 8, 1, tzinfo=TZ),
        ingested_at=datetime(2025, 1, 2, 8, 3, tzinfo=TZ),
        replay_available_at=datetime(2025, 1, 2, 8, 2, tzinfo=TZ),
        revision_no=0,
        time_quality=TimeQuality.EXACT,
        provider="fixture",
        source_url="https://example.test/rights-declined",
        raw_response_sha256="8" * 64,
        validation_status="validated",
        rights_ratio=Decimal("0.2"),
        rights_subscription_price=Decimal("6"),
        rights_payment_deadline=START + timedelta(days=5),
        rights_listing_date=START + timedelta(days=10),
    )
    source_bars = (
        bar(PREVIOUS, "10"),
        bar(START, "10"),
        bar(ex_date, "9"),
    )
    config = FundedBuyAndHoldConfig(
        initial_cash=Money(Decimal("10000")),
        participation_rate=Decimal("1"),
        slippage_bps=Decimal("0"),
    )

    baseline = run_funded_buy_and_hold(request(source_bars, fees=ZeroFees(), config=config))
    result = run_funded_buy_and_hold(
        request(
            source_bars,
            fees=ZeroFees(),
            config=config,
            corporate_actions=TimelineCorporateActionApplier((action,)),
        )
    )

    assert result.points == baseline.points
    assert result.final_portfolio.cash == baseline.final_portfolio.cash
    assert result.final_portfolio.lots == baseline.final_portfolio.lots
    assert result.final_portfolio.corporate_action_entitlements == ()
    assert [item.action.phase for item in result.corporate_actions] == ["declined"]
    assert result.corporate_actions[0].action.reason_code == (
        "rights_issue_declined_no_external_cash"
    )
