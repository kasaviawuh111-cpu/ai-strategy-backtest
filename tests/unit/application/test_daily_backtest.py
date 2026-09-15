from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from ashare_lab.application.daily_backtest import (
    DailyBacktestConfig,
    DailyBacktestInput,
    DailyBacktestInputError,
    DecisionStatus,
    EntrySignalSemantics,
    run_daily_backtest,
)
from ashare_lab.domain.execution import (
    AshareExchange,
    CapacityMode,
    FeeCalculator,
    FeePolicy,
    LimitHandling,
    TradingCalendar,
)
from ashare_lab.domain.market_data import (
    Board,
    CorporateAction,
    CorporateActionKind,
    DailyBar,
    EventEnvelope,
    InstrumentSession,
    MarketEvent,
    PriceBasis,
    TimeQuality,
    TradingStatus,
)
from ashare_lab.domain.orders import OrderSide
from ashare_lab.domain.shared import InstrumentId, Money, Price, Quantity, StrongId
from ashare_lab.domain.signals import SignalEvidence, SignalFact
from ashare_lab.domain.strategy import (
    AllCondition,
    BacktestConfig,
    CatalogRef,
    DailyExecutionPolicy,
    EventCondition,
    FirstOfExit,
    HoldingPeriodExit,
    IndicatorCondition,
    Instrument,
    PositionReturnExit,
    StrategySpec,
    TrailingDrawdownExit,
)
from ashare_lab.domain.strategy.models import JsonScalar

TZ = ZoneInfo("Asia/Shanghai")
INSTRUMENT = InstrumentId("300059.SZ")
START = date(2025, 1, 2)


def strategy(*, end_offset: int = 5) -> StrategySpec:
    params: dict[str, JsonScalar] = {"period": 2, "price_field": "close"}
    return StrategySpec(
        catalog=CatalogRef(catalog_id="cn_a.signals", release_version="2026.08.28"),
        instrument=Instrument(symbol=INSTRUMENT.value),
        entry=IndicatorCondition(
            indicator_id="technical.ma",
            definition_version="1.0.0",
            params=params,
            trigger="price_crosses_above",
        ),
        exit=FirstOfExit(
            children=(
                IndicatorCondition(
                    indicator_id="technical.ma",
                    definition_version="1.0.0",
                    params=params,
                    trigger="price_crosses_below",
                ),
            )
        ),
        backtest=BacktestConfig(
            start=START,
            end=START + timedelta(days=end_offset),
            initial_cash_cny=100_000,
        ),
    )


def event_strategy(*, end_offset: int = 5) -> StrategySpec:
    params: dict[str, JsonScalar] = {"period": 2, "price_field": "close"}
    return StrategySpec(
        catalog=CatalogRef(catalog_id="cn_a.signals", release_version="2026.08.28"),
        instrument=Instrument(symbol=INSTRUMENT.value),
        entry=EventCondition(
            event_code="event.financial_results.earnings_forecast_published",
            definition_version="1.0.0",
        ),
        exit=FirstOfExit(
            children=(
                IndicatorCondition(
                    indicator_id="technical.ma",
                    definition_version="1.0.0",
                    params=params,
                    trigger="price_crosses_below",
                ),
            )
        ),
        execution=DailyExecutionPolicy(
            data_capability="daily_ohlcv_events",
            evaluation_frequency="event_available_plus_1d_close",
        ),
        backtest=BacktestConfig(
            start=START,
            end=START + timedelta(days=end_offset),
            initial_cash_cny=100_000,
        ),
    )


def holding_strategy(*, holding_sessions: int, end: date) -> StrategySpec:
    base = strategy(end_offset=(end - START).days)
    return base.model_copy(
        update={
            "exit": FirstOfExit(children=(HoldingPeriodExit(sessions=holding_sessions),)),
            "backtest": BacktestConfig(
                start=START,
                end=end,
                initial_cash_cny=100_000,
            ),
        }
    )


def risk_strategy(
    *rules: PositionReturnExit | TrailingDrawdownExit,
    end: date,
) -> StrategySpec:
    base = strategy(end_offset=(end - START).days)
    return base.model_copy(
        update={
            "exit": FirstOfExit(children=rules),
            "backtest": BacktestConfig(
                start=START,
                end=end,
                initial_cash_cny=100_000,
            ),
        }
    )


def market_event(
    available_at: datetime,
    *,
    quality: TimeQuality = TimeQuality.EXACT,
) -> EventEnvelope:
    return EventEnvelope(
        event=MarketEvent(
            event_id=StrongId("event-test-1"),
            event_code="event.financial_results.earnings_forecast_published",
            instrument_id=INSTRUMENT,
            attributes={},
        ),
        occurred_at=None,
        source_released_at=available_at,
        vendor_first_available_at=available_at,
        ingested_at=available_at,
        revision_no=0,
        time_quality=quality,
    )


def bars(
    closes: tuple[str, ...] = ("10", "9", "11", "12", "8", "7", "7"),
) -> tuple[DailyBar, ...]:
    result: list[DailyBar] = []
    for offset, close in enumerate(closes):
        value = Decimal(close)
        result.append(
            DailyBar(
                instrument_id=INSTRUMENT,
                session_date=START + timedelta(days=offset),
                open=Price(value),
                high=Price(value),
                low=Price(value),
                close=Price(value),
                volume=Quantity(1_000_000),
                turnover=value * Decimal("1000000"),
                available_at=datetime.combine(
                    START + timedelta(days=offset),
                    datetime.min.time().replace(hour=15),
                    tzinfo=TZ,
                ),
            )
        )
    return tuple(result)


def sessions(
    source_bars: tuple[DailyBar, ...],
    *,
    suspended_offsets: tuple[int, ...] = (),
) -> tuple[InstrumentSession, ...]:
    return tuple(
        InstrumentSession(
            instrument_id=INSTRUMENT,
            session_date=bar.session_date,
            board=Board.CHINEXT,
            status=(
                TradingStatus.SUSPENDED if index in suspended_offsets else TradingStatus.TRADING
            ),
            previous_close=Price(Decimal("10")),
            upper_limit=Price(Decimal("50")),
            lower_limit=Price(Decimal("1")),
            minimum_buy_quantity=100,
            buy_quantity_increment=100,
        )
        for index, bar in enumerate(source_bars)
    )


def fees() -> FeeCalculator:
    return FeeCalculator(
        FeePolicy(
            exchange=AshareExchange.SHENZHEN,
            commission_rate=Decimal("0.0003"),
            minimum_commission=Money(Decimal("5")),
        )
    )


def run(
    *,
    source_bars: tuple[DailyBar, ...] | None = None,
    source_signal_bars: tuple[DailyBar, ...] | None = None,
    source_sessions: tuple[InstrumentSession, ...] | None = None,
    spec: StrategySpec | None = None,
    config: DailyBacktestConfig | None = None,
    source_events: tuple[EventEnvelope, ...] = (),
    source_actions: tuple[CorporateAction, ...] = (),
    benchmark_close: tuple[tuple[date, Decimal], ...] = (),
    benchmark_equity: tuple[tuple[date, Decimal], ...] = (),
    benchmark_initial_equity: Decimal | None = None,
    benchmark_entry_filled: bool | None = None,
    source_calendar_dates: tuple[date, ...] | None = None,
    provider_entry_timeline: tuple[SignalFact | None, ...] | None = None,
    provider_exit_timeline: tuple[SignalFact | None, ...] | None = None,
    risk_price_rebases=None,
):
    actual_bars = source_bars or bars()
    actual_sessions = source_sessions or sessions(actual_bars)
    calendar_dates = source_calendar_dates or tuple(
        START + timedelta(days=offset) for offset in range(len(actual_bars) + 3)
    )
    return run_daily_backtest(
        DailyBacktestInput(
            run_key="test-run",
            strategy=spec or strategy(),
            bars=actual_bars,
            signal_bars=source_signal_bars,
            sessions=actual_sessions,
            calendar=TradingCalendar(version="test-calendar", sessions=calendar_dates),
            fee_calculator=fees(),
            events=source_events,
            provider_entry_timeline=provider_entry_timeline,
            provider_exit_timeline=provider_exit_timeline,
            risk_price_rebases=risk_price_rebases,
            corporate_actions=source_actions,
            benchmark_close=benchmark_close,
            benchmark_equity=benchmark_equity,
            benchmark_initial_equity=benchmark_initial_equity,
            benchmark_entry_filled=benchmark_entry_filled,
            config=config
            or DailyBacktestConfig(
                participation_rate=Decimal("1"),
                slippage_bps=Decimal("0"),
                allocation_ratio=Decimal("0.9"),
            ),
        )
    )


def _provider_fact(
    bar: DailyBar,
    *,
    condition_ref: str,
    triggered: bool,
) -> SignalFact:
    return SignalFact(
        instrument_id=bar.instrument_id,
        session_date=bar.session_date,
        condition_ref=condition_ref,
        triggered=triggered,
        observed_at=bar.available_at,
        available_at=bar.available_at,
        reason="exact provider-returned indicator comparison",
        left_value=Decimal("30"),
        right_value=Decimal("20"),
        evidence=(
            SignalEvidence(
                evidence_type="provider_indicator",
                evidence_id=f"eastmoney:{condition_ref}:{bar.session_date}",
                available_at=bar.available_at,
                provider="eastmoney_mx_finance_data",
                timestamp_precision="second",
                validation_status="provider_value_validated",
                raw_response_sha256="sha256:" + "b" * 64,
            ),
        ),
    )


@pytest.mark.parametrize("accumulate,expected_buys", [(False, 1), (True, 2)])
@pytest.mark.parametrize("state", [False, True])
def test_entry_policy_new_occurrences_can_accumulate_and_exit_total(accumulate, expected_buys, state):
    source = bars(("10",) * 7)
    spec = strategy(end_offset=6)
    if state:
        spec = spec.model_copy(update={"entry": spec.entry.model_copy(update={"trigger": "price_above"})})
    if accumulate:
        spec = spec.model_copy(update={"execution": DailyExecutionPolicy(position_policy="accumulate_on_new_entry_signal")})
    result = run(source_bars=source, spec=spec,
        provider_entry_timeline=tuple(_provider_fact(bar, condition_ref="entry",
            triggered=i in ((0, 1, 3, 4) if state else (0, 3))) for i, bar in enumerate(source)),
        provider_exit_timeline=tuple(_provider_fact(bar, condition_ref="exit", triggered=i == 5)
            for i, bar in enumerate(source)),
        config=DailyBacktestConfig(allocation_ratio=Decimal("0.5"), capacity_mode=CapacityMode.UNLIMITED,
                                  slippage_bps=Decimal(0)))
    buys = [fill for fill in result.fills if fill.side is OrderSide.BUY]
    sells = [fill for fill in result.fills if fill.side is OrderSide.SELL]
    assert len(buys) == expected_buys and len(sells) == 1
    assert sells[0].quantity.value == sum(fill.quantity.value for fill in buys)


def test_pending_accumulation_yields_to_close_confirmed_account_exit():
    source = bars(("10", "10", "8", "8", "8", "8"))
    signal = tuple(replace(bar, price_basis=PriceBasis.BACK_ADJUSTED) for bar in source)
    spec = risk_strategy(
        PositionReturnExit(trigger="stop_loss", threshold_pct=10),
        end=source[-1].session_date,
    ).model_copy(
        update={
            "execution": DailyExecutionPolicy(
                position_policy="accumulate_on_new_entry_signal"
            )
        }
    )
    controls = list(sessions(source))
    controls[2] = replace(controls[2], upper_limit=source[2].open)
    result = run(
        source_bars=source,
        source_signal_bars=signal,
        source_sessions=tuple(controls),
        spec=spec,
        provider_entry_timeline=tuple(
            _provider_fact(bar, condition_ref="entry", triggered=index in (0, 1))
            for index, bar in enumerate(source)
        ),
        config=DailyBacktestConfig(
            allocation_ratio=Decimal("0.5"),
            capacity_mode=CapacityMode.UNLIMITED,
            slippage_bps=Decimal(0),
            edge_entry_validity_sessions=3,
        ),
    )

    assert [(fill.side, fill.trading_date) for fill in result.fills] == [
        (OrderSide.BUY, source[1].session_date),
        (OrderSide.SELL, source[3].session_date),
    ]
    cancelled_add = next(
        decision
        for decision in result.decisions
        if decision.side is OrderSide.BUY and decision.status is DecisionStatus.CANCELLED
    )
    assert cancelled_add.attempts == 1
    assert cancelled_add.outcome_reason == (
        "opposite_exit_signal_invalidated_entry:position_return_exit:stop_loss:10.0pct"
    )


@pytest.mark.parametrize("side", [OrderSide.BUY, OrderSide.SELL])
@pytest.mark.parametrize("failure", ["suspended", "price_limit", "capacity", "no_market_trades"])
def test_default_indicator_order_failure_does_not_replay_old_signal(side, failure):
    source = list(bars(("10",) * 7))
    target = 1 if side is OrderSide.BUY else 3
    if failure == "capacity":
        source[target - 1] = replace(source[target - 1], volume=Quantity.zero())
    elif failure == "no_market_trades":
        source[target] = replace(source[target], volume=Quantity.zero(), turnover=Decimal(0))
    controls = list(sessions(tuple(source)))
    if failure == "suspended":
        controls[target] = replace(controls[target], status=TradingStatus.SUSPENDED)
    elif failure == "price_limit":
        field = "upper_limit" if side is OrderSide.BUY else "lower_limit"
        controls[target] = replace(controls[target], **{field: source[target].open})
    result = run(source_bars=tuple(source), source_sessions=tuple(controls),
        spec=strategy(end_offset=6),
        provider_entry_timeline=tuple(_provider_fact(bar, condition_ref="entry", triggered=i == 0)
                                      for i, bar in enumerate(source)),
        provider_exit_timeline=tuple(_provider_fact(bar, condition_ref="exit", triggered=i == 2)
                                     for i, bar in enumerate(source)))
    # A single historical signal cannot silently create later DAY orders.
    assert not [fill for fill in result.fills if fill.side is side]
    decision = next(item for item in result.decisions if item.side is side)
    assert decision.status is DecisionStatus.UNFILLED
    assert decision.attempts <= 1
    if failure == "suspended":
        assert decision.outcome_reason == "security_not_trading"
    elif failure == "no_market_trades":
        assert decision.outcome_reason == "no_market_trades"


def test_provider_timelines_bypass_local_indicator_calculation() -> None:
    source_bars = bars(("10", "10", "10", "10", "10", "10"))
    entry = tuple(
        _provider_fact(
            bar,
            condition_ref="technical.ma@1.0.0:price_crosses_above",
            triggered=index == 1,
        )
        for index, bar in enumerate(source_bars)
    )
    exit_timeline = tuple(
        _provider_fact(
            bar,
            condition_ref="technical.ma@1.0.0:price_crosses_below",
            triggered=index == 3,
        )
        for index, bar in enumerate(source_bars)
    )

    result = run(
        source_bars=source_bars,
        provider_entry_timeline=entry,
        provider_exit_timeline=exit_timeline,
    )

    assert tuple(fill.side for fill in result.fills) == (
        OrderSide.BUY,
        OrderSide.SELL,
    )
    assert all(
        signal.evidence[0].provider == "eastmoney_mx_finance_data"
        for signal in result.signals
    )


@pytest.mark.parametrize("evidence_type,validation_status", [
    ("skill_ohlcv_derived_indicator", "local_formula_on_point_in_time_rebases"),
    ("skill_numeric_history", "provider_history_bound"),
])
def test_source_hashed_local_formula_timeline_is_valid_provider_mode_input(
    evidence_type, validation_status,
) -> None:
    source_bars = bars(("10", "10", "10", "10", "10", "10"))

    def derived(bar: DailyBar, *, triggered: bool) -> SignalFact:
        fact = _provider_fact(bar, condition_ref="technical.ma@1.0.0", triggered=triggered)
        return replace(fact, evidence=(SignalEvidence(
            evidence_type=evidence_type,
            evidence_id=f"derived:{bar.session_date}",
            available_at=bar.available_at,
            provider="eastmoney_mx_finance_data",
            validation_status=validation_status,
            raw_response_sha256="sha256:" + "c" * 64,
        ),))

    entry = tuple(derived(bar, triggered=index == 1)
                  for index, bar in enumerate(source_bars))
    exits = tuple(derived(bar, triggered=index == 3)
                  for index, bar in enumerate(source_bars))
    result = run(source_bars=source_bars, provider_entry_timeline=entry,
                 provider_exit_timeline=exits)
    assert [fill.side for fill in result.fills] == [OrderSide.BUY, OrderSide.SELL]

    invalid = replace(entry[0], evidence=(replace(entry[0].evidence[0], raw_response_sha256=None),))
    with pytest.raises(DailyBacktestInputError, match="auditable source provenance"):
        run(source_bars=source_bars, provider_entry_timeline=(invalid, *entry[1:]),
            provider_exit_timeline=exits)
    unbound = replace(entry[0], evidence=(replace(entry[0].evidence[0], validation_status="unverified"),))
    with pytest.raises(DailyBacktestInputError, match="auditable source provenance"):
        run(source_bars=source_bars, provider_entry_timeline=(unbound, *entry[1:]),
            provider_exit_timeline=exits)


@pytest.mark.parametrize("both_true", [False, True])
def test_market_only_all_exit_preserves_and_semantics_in_share_ledger(both_true):
    spec = strategy()
    below = spec.exit.children[0]
    second = (below.model_copy(update={"params": {"period": 3, "price_field": "close"}})
              if both_true else spec.entry)
    spec = spec.model_copy(update={"exit": FirstOfExit(op="all", children=(below, second))})
    result = run(spec=spec)
    assert [fill.side for fill in result.fills] == (
        [OrderSide.BUY, OrderSide.SELL] if both_true else [OrderSide.BUY])
    assert bool(result.final_portfolio.position_quantity(INSTRUMENT).value) is not both_true


@pytest.mark.parametrize("late_signal", [False, True])
def test_all_holding_and_market_exit_needs_same_close_after_maturity(late_signal):
    source_bars = bars(("10",) * 6)
    spec = strategy()
    spec = spec.model_copy(update={"exit": FirstOfExit(op="all",
        children=(HoldingPeriodExit(sessions=2), spec.exit.children[0]))})
    entry = tuple(_provider_fact(bar, condition_ref="entry", triggered=i == 0)
                  for i, bar in enumerate(source_bars))
    exits = tuple(_provider_fact(bar, condition_ref="exit", triggered=i == 2 or late_signal and i == 4)
                  for i, bar in enumerate(source_bars))
    result = run(spec=spec, source_bars=source_bars,
                 provider_entry_timeline=entry, provider_exit_timeline=exits)
    buys = [f for f in result.fills if f.side is OrderSide.BUY]
    sells = [f for f in result.fills if f.side is OrderSide.SELL]
    assert buys[0].trading_date == source_bars[1].session_date
    assert [f.trading_date for f in sells] == ([source_bars[5].session_date] if late_signal else [])
    if late_signal:
        decision = next(d for d in result.decisions if d.side is OrderSide.SELL)
        assert decision.signal.session_date == source_bars[4].session_date


@pytest.mark.parametrize("late_profit", [False, True])
def test_all_holding_and_profit_does_not_latch_earlier_profit(late_profit):
    source = bars(("10", "10", "12", "10", "12" if late_profit else "10", "12"))
    signal = tuple(replace(bar, price_basis=PriceBasis.BACK_ADJUSTED) for bar in source)
    spec = strategy().model_copy(update={"exit": FirstOfExit(op="all", children=(
        HoldingPeriodExit(sessions=2), PositionReturnExit(trigger="take_profit", threshold_pct=10)))})
    entry = tuple(_provider_fact(bar, condition_ref="entry", triggered=i == 0) for i, bar in enumerate(source))
    result = run(spec=spec, source_bars=source, source_signal_bars=signal, provider_entry_timeline=entry)
    sells = [f for f in result.fills if f.side is OrderSide.SELL]
    assert [f.trading_date for f in sells] == ([source[5].session_date] if late_profit else [])
    if late_profit:
        decision = next(d for d in result.decisions if d.side is OrderSide.SELL)
        assert decision.signal.condition_ref == "all:account_and_market"
        assert decision.signal.session_date == source[4].session_date


def test_holding_target_uses_calendar_and_does_not_skip_missing_security_day():
    complete = bars(("10",) * 6)
    source = tuple(bar for i, bar in enumerate(complete) if i != 3)
    spec = strategy().model_copy(update={"exit": FirstOfExit(children=(HoldingPeriodExit(sessions=2),))})
    entry = tuple(_provider_fact(bar, condition_ref="entry", triggered=i == 0) for i, bar in enumerate(source))
    with pytest.raises(DailyBacktestInputError, match="holding target market session has no security data"):
        run(spec=spec, source_bars=source, provider_entry_timeline=entry,
            source_calendar_dates=tuple(bar.session_date for bar in complete))


@pytest.mark.parametrize("later_fall", [False, True])
def test_raw_risk_anchor_rebases_on_ex_date_without_false_stop(later_fall):
    from ashare_lab.application.minute_price_rebase import MinutePriceRebase
    from tests.unit.portfolio.test_corporate_actions import _action
    tail = "8.5" if later_fall else "9.5"
    source = bars(("10", "10", "10", "10", "9.5", tail, tail))
    dates = tuple(date(2025, 1, day) for day in (2, 3, 6, 7, 8, 9, 10))
    source = tuple(replace(bar, session_date=day, available_at=datetime.combine(day, time(15), TZ))
                   for bar, day in zip(source, dates))
    action = replace(_action(CorporateActionKind.CASH_DIVIDEND, cash_per_share=Decimal(".5"), pay_date=dates[5]),
                     record_date=dates[3], ex_date=dates[4])
    factor = MinutePriceRebase(INSTRUMENT, action.ex_date, Decimal(".95"),
                              datetime.combine(action.ex_date, time(9), TZ), "a" * 64)
    spec = strategy(end_offset=6).model_copy(update={"exit": FirstOfExit(children=(
        PositionReturnExit(trigger="stop_loss", threshold_pct=3),))})
    spec = spec.model_copy(update={"backtest": spec.backtest.model_copy(update={"end": dates[-1]})})
    entry = tuple(_provider_fact(bar, condition_ref="entry", triggered=i == 0) for i, bar in enumerate(source))
    result = run(spec=spec, source_bars=source, provider_entry_timeline=entry,
                 source_actions=(action,), risk_price_rebases=(factor,),
                 source_calendar_dates=dates + (date(2025, 1, 13),))
    sells = [f for f in result.fills if f.side is OrderSide.SELL]
    assert [f.trading_date for f in sells] == ([source[6].session_date] if later_fall else [])
    assert not any(s.triggered and s.session_date == action.ex_date and "stop_loss" in s.condition_ref
                   for s in result.signals)


def test_provider_mode_never_mixes_with_local_exit_calculation() -> None:
    source_bars = bars(("10", "10", "10", "10"))
    entry = tuple(
        _provider_fact(
            bar,
            condition_ref="technical.ma@1.0.0:price_crosses_above",
            triggered=False,
        )
        for bar in source_bars
    )

    with pytest.raises(
        DailyBacktestInputError,
        match="requires an exit signal timeline",
    ):
        run(source_bars=source_bars, provider_entry_timeline=entry)


def test_signal_close_executes_only_at_next_session_open() -> None:
    result = run()

    assert [fill.side for fill in result.fills] == [OrderSide.BUY, OrderSide.SELL]
    assert result.fills[0].trading_date == START + timedelta(days=3)
    assert result.fills[1].trading_date == START + timedelta(days=5)
    assert result.decisions[0].signal.session_date == START + timedelta(days=2)
    assert result.decisions[0].status is DecisionStatus.FILLED
    assert result.round_trips[0].net_pnl < 0
    assert result.final_portfolio.position_quantity(INSTRUMENT) == Quantity.zero()
    assert result.orders[0].match.capacity_reason_code == ("capacity_previous_session_volume_proxy")


def test_last_session_buy_without_proven_t_plus_one_session_fails_closed() -> None:
    source_bars = bars()[:4]
    final_session = source_bars[-1].session_date
    result = run(
        source_bars=source_bars,
        spec=strategy(end_offset=3),
        source_calendar_dates=tuple(bar.session_date for bar in source_bars),
    )

    assert result.fills == ()
    assert result.decisions[-1].status is DecisionStatus.NO_FUTURE_SESSION
    assert result.decisions[-1].outcome_reason == (
        "no_proven_t_plus_one_session_after_buy_fill"
    )
    assert result.equity_curve[-1].session_date == final_session


@pytest.mark.parametrize(
    ("board", "minimum", "increment", "expected"),
    [
        (Board.MAIN, 100, 100, 1_700),
        (Board.CHINEXT, 100, 100, 1_700),
        (Board.STAR, 200, 1, 1_799),
        (Board.BSE, 100, 1, 1_799),
    ],
)
def test_strategy_sizing_uses_the_session_buy_declaration_rule(
    board: Board,
    minimum: int,
    increment: int,
    expected: int,
) -> None:
    source_bars = bars()
    source_sessions = tuple(
        replace(
            item,
            board=board,
            minimum_buy_quantity=minimum,
            buy_quantity_increment=increment,
        )
        for item in sessions(source_bars)
    )

    result = run(source_bars=source_bars, source_sessions=source_sessions)

    assert result.fills[0].side is OrderSide.BUY
    assert result.fills[0].quantity == Quantity(expected)


def test_pre_open_buy_quantity_is_invariant_to_the_future_open() -> None:
    source = bars()
    fixed_sessions = sessions(source)
    changed = list(source)
    changed[3] = replace(
        changed[3],
        open=Price(Decimal("20")),
        high=Price(Decimal("20")),
        low=Price(Decimal("12")),
    )

    baseline = run(source_bars=source, source_sessions=fixed_sessions)
    counterfactual = run(
        source_bars=tuple(changed),
        source_sessions=fixed_sessions,
    )

    assert baseline.orders[0].order.submitted_at == counterfactual.orders[0].order.submitted_at
    assert baseline.orders[0].order.submitted_at == datetime.combine(
        START + timedelta(days=3),
        time(9, 15),
        tzinfo=TZ,
    )
    assert baseline.orders[0].order.quantity == counterfactual.orders[0].order.quantity
    assert baseline.fills[0].price == Price(Decimal("12"))
    assert counterfactual.fills[0].price == Price(Decimal("20"))


def test_pre_open_buy_sizing_reserves_upper_limit_notional_fees_and_slippage() -> None:
    source = bars()
    source_sessions = list(sessions(source))
    source_sessions[3] = replace(
        source_sessions[3],
        upper_limit=Price(Decimal("13")),
    )
    config = DailyBacktestConfig(
        participation_rate=Decimal("1"),
        slippage_bps=Decimal("1000"),
        allocation_ratio=Decimal("0.9"),
    )

    result = run(
        source_bars=source,
        source_sessions=tuple(source_sessions),
        config=config,
    )

    order = result.orders[0].order
    assert order.limit_price == Price(Decimal("13"))
    assert order.quantity == Quantity(6_900)
    budget = Decimal("100000") * config.allocation_ratio
    quoted_fees = fees().calculate(
        side=OrderSide.BUY,
        price=order.limit_price,
        quantity=order.quantity,
        trade_date=order.created_at.date(),
    )
    assert (
        order.limit_price.amount * Decimal(order.quantity.value) + (quoted_fees.total.amount)
        <= budget
    )
    next_quantity = Quantity(7_000)
    next_fees = fees().calculate(
        side=OrderSide.BUY,
        price=order.limit_price,
        quantity=next_quantity,
        trade_date=order.created_at.date(),
    )
    assert (
        order.limit_price.amount * Decimal(next_quantity.value) + (next_fees.total.amount) > budget
    )


def test_pre_open_buy_without_an_upper_limit_is_rejected_before_order_creation() -> None:
    source = bars()
    source_sessions = list(sessions(source))
    source_sessions[3] = replace(
        source_sessions[3],
        upper_limit=None,
        lower_limit=None,
    )

    result = run(source_bars=source, source_sessions=tuple(source_sessions))
    entry = result.decisions[0]

    assert entry.status is DecisionStatus.REJECTED
    assert entry.outcome_reason == "pre_open_buy_sizing_upper_limit_unavailable"
    assert entry.order_ids == ()
    assert result.orders == ()
    assert result.fills == ()


def test_first_of_exit_is_not_blocked_by_another_indicator_still_in_warmup() -> None:
    base = strategy()
    original_exit = base.exit.children[0]
    warming_exit = IndicatorCondition(
        indicator_id="technical.rsi",
        definition_version="1.0.0",
        params={"period": 14},
        trigger="above",
        value=50,
    )
    spec = base.model_copy(update={"exit": FirstOfExit(children=(original_exit, warming_exit))})

    result = run(spec=spec)

    assert [fill.side for fill in result.fills] == [OrderSide.BUY, OrderSide.SELL]
    assert result.fills[1].trading_date == START + timedelta(days=5)
    sell_decision = next(item for item in result.decisions if item.side is OrderSide.SELL)
    assert sell_decision.signal.triggered
    assert sell_decision.signal.reason == "any[true,unknown] => true"


def test_holding_period_starts_at_first_buy_fill_and_exits_on_target_session_open() -> None:
    source = bars(closes=("10", "9", "11", "12", "12", "12", "12"))
    spec = holding_strategy(
        holding_sessions=2,
        end=source[-1].session_date,
    )

    result = run(source_bars=source, spec=spec)

    assert [(fill.side, fill.trading_date) for fill in result.fills] == [
        (OrderSide.BUY, START + timedelta(days=3)),
        (OrderSide.SELL, START + timedelta(days=5)),
    ]
    sell = next(item for item in result.decisions if item.side is OrderSide.SELL)
    assert sell.signal.session_date == START + timedelta(days=5)
    assert sell.signal.condition_ref.startswith("holding_period_exit:2:")
    assert sell.signal.evidence[0].evidence_type == "entry_fill_anchor"
    assert sell.signal.evidence[0].evidence_id == result.fills[0].fill_id.value
    assert sell.signal.available_at == datetime.combine(
        START + timedelta(days=5),
        time(9, 14, 59),
        tzinfo=TZ,
    )


def test_position_risk_exit_requires_explicit_back_adjusted_signal_bars() -> None:
    source = bars(closes=("10", "9", "11", "12", "10", "9", "9"))
    spec = risk_strategy(
        PositionReturnExit(trigger="stop_loss", threshold_pct=10),
        end=source[-1].session_date,
    )

    with pytest.raises(
        DailyBacktestInputError,
        match="require explicit back-adjusted signal bars",
    ):
        run(source_bars=source, spec=spec)


def test_stop_loss_is_close_confirmed_and_fills_only_at_next_session_open() -> None:
    execution = bars(closes=("10", "9", "11", "12", "10", "9", "9"))
    signal = tuple(replace(item, price_basis=PriceBasis.BACK_ADJUSTED) for item in execution)
    spec = risk_strategy(
        PositionReturnExit(trigger="stop_loss", threshold_pct=10),
        end=execution[-1].session_date,
    )

    result = run(
        source_bars=execution,
        source_signal_bars=signal,
        source_sessions=sessions(execution),
        spec=spec,
    )

    assert [(fill.side, fill.trading_date) for fill in result.fills] == [
        (OrderSide.BUY, START + timedelta(days=3)),
        (OrderSide.SELL, START + timedelta(days=5)),
    ]
    sell = next(item for item in result.decisions if item.side is OrderSide.SELL)
    assert sell.signal.session_date == START + timedelta(days=4)
    assert sell.signal.available_at == datetime.combine(
        START + timedelta(days=4),
        time(15),
        tzinfo=TZ,
    )
    assert sell.signal.condition_ref == "position_return_exit:stop_loss:10.0pct"
    assert sell.signal.evidence[0].evidence_id == result.fills[0].fill_id.value


def test_trailing_drawdown_uses_post_entry_peak_and_never_same_close_fill() -> None:
    execution = bars(closes=("10", "9", "11", "12", "15", "12", "11", "11"))
    signal = tuple(replace(item, price_basis=PriceBasis.BACK_ADJUSTED) for item in execution)
    spec = risk_strategy(
        TrailingDrawdownExit(threshold_pct=15),
        end=execution[-1].session_date,
    )

    result = run(
        source_bars=execution,
        source_signal_bars=signal,
        source_sessions=sessions(execution),
        spec=spec,
    )

    sell = next(item for item in result.decisions if item.side is OrderSide.SELL)
    assert sell.signal.session_date == START + timedelta(days=5)
    assert result.fills[-1].trading_date == START + timedelta(days=6)
    assert sell.signal.condition_ref == "trailing_drawdown_exit:15.0pct"


def test_position_risk_exit_prefix_is_unchanged_when_future_bars_change() -> None:
    execution = bars(closes=("10", "9", "11", "12", "10", "9", "9"))
    baseline_signal = tuple(
        replace(item, price_basis=PriceBasis.BACK_ADJUSTED) for item in execution
    )
    changed_signal = tuple(
        replace(item, price_basis=PriceBasis.BACK_ADJUSTED)
        for item in bars(closes=("10", "9", "11", "12", "10", "99", "100"))
    )
    spec = risk_strategy(
        PositionReturnExit(trigger="stop_loss", threshold_pct=10),
        end=execution[-1].session_date,
    )

    baseline = run(
        source_bars=execution,
        source_signal_bars=baseline_signal,
        source_sessions=sessions(execution),
        spec=spec,
    )
    changed = run(
        source_bars=execution,
        source_signal_bars=changed_signal,
        source_sessions=sessions(execution),
        spec=spec,
    )

    baseline_sell = next(item for item in baseline.decisions if item.side is OrderSide.SELL)
    changed_sell = next(item for item in changed.decisions if item.side is OrderSide.SELL)
    assert baseline_sell.signal == changed_sell.signal
    assert baseline.fills[:2] == changed.fills[:2]


def test_holding_period_counts_pinned_sessions_across_a_weekend() -> None:
    session_dates = (
        date(2025, 1, 2),
        date(2025, 1, 3),
        date(2025, 1, 6),
        date(2025, 1, 7),
        date(2025, 1, 8),
        date(2025, 1, 9),
    )
    source = tuple(
        replace(
            item,
            session_date=session_dates[index],
            available_at=datetime.combine(session_dates[index], time(15), tzinfo=TZ),
        )
        for index, item in enumerate(bars(closes=("10", "9", "11", "12", "12", "12")))
    )
    spec = holding_strategy(holding_sessions=2, end=session_dates[-1])
    calendar_dates = (*session_dates, date(2025, 1, 10))

    result = run(
        source_bars=source,
        source_sessions=sessions(source),
        source_calendar_dates=calendar_dates,
        spec=spec,
    )

    assert [(fill.side, fill.trading_date) for fill in result.fills] == [
        (OrderSide.BUY, date(2025, 1, 7)),
        (OrderSide.SELL, date(2025, 1, 9)),
    ]


def test_unfilled_buy_never_starts_holding_period_clock() -> None:
    source = bars(closes=("10", "9", "11", "12"))
    source_sessions = list(sessions(source))
    source_sessions[-1] = replace(
        source_sessions[-1],
        upper_limit=source[-1].open,
    )
    spec = holding_strategy(holding_sessions=1, end=source[-1].session_date)

    result = run(
        source_bars=source,
        source_sessions=tuple(source_sessions),
        spec=spec,
        config=DailyBacktestConfig(
            participation_rate=Decimal("1"),
            slippage_bps=Decimal("0"),
            allocation_ratio=Decimal("0.9"),
            edge_entry_validity_sessions=1,
        ),
    )

    assert result.fills == ()
    assert all(item.side is not OrderSide.SELL for item in result.decisions)
    assert not any(item.condition_ref.startswith("holding_period_exit:") for item in result.signals)


def test_holding_exit_waits_through_target_session_suspension() -> None:
    source = bars(closes=("10", "9", "11", "12", "12", "12", "12"))
    spec = holding_strategy(holding_sessions=2, end=source[-1].session_date)

    result = run(
        source_bars=source,
        source_sessions=sessions(source, suspended_offsets=(5,)),
        spec=spec,
    )

    assert [(fill.side, fill.trading_date) for fill in result.fills] == [
        (OrderSide.BUY, START + timedelta(days=3)),
        (OrderSide.SELL, START + timedelta(days=6)),
    ]
    sell = next(item for item in result.decisions if item.side is OrderSide.SELL)
    assert sell.signal.session_date == START + timedelta(days=5)
    assert sell.attempts == 1


@pytest.mark.parametrize("ordinary_retry_budget", [1, 20])
def test_holding_exit_retries_after_target_session_one_price_limit_down(ordinary_retry_budget) -> None:
    source = bars(closes=("10", "9", "11", "12", "12", "12", "12"))
    source_sessions = list(sessions(source))
    source_sessions[5] = replace(
        source_sessions[5],
        lower_limit=source[5].open,
    )
    spec = holding_strategy(holding_sessions=2, end=source[-1].session_date)

    result = run(
        source_bars=source,
        source_sessions=tuple(source_sessions),
        spec=spec,
        config=DailyBacktestConfig(max_exit_attempts=ordinary_retry_budget),
    )

    sell = next(item for item in result.decisions if item.side is OrderSide.SELL)
    sell_orders = tuple(
        trace for trace in result.orders if trace.order.decision_id == sell.decision_id
    )
    assert [trace.match.reason_code for trace in sell_orders] == [
        "one_price_limit_down",
        "matched_at_open",
    ]
    assert result.fills[-1].trading_date == START + timedelta(days=6)


def test_holding_exit_retries_remaining_position_after_partial_target_fill() -> None:
    source = list(bars(closes=("10", "9", "11", "12", "12", "12", "12")))
    source[4] = replace(source[4], volume=Quantity(100_000))
    spec = holding_strategy(holding_sessions=2, end=source[-1].session_date)

    result = run(
        source_bars=tuple(source),
        source_sessions=sessions(tuple(source)),
        spec=spec,
        config=DailyBacktestConfig(
            participation_rate=Decimal("0.001"),
            slippage_bps=Decimal("0"),
            allocation_ratio=Decimal("0.9"),
        ),
    )

    sell = next(item for item in result.decisions if item.side is OrderSide.SELL)
    sell_fills = tuple(fill for fill in result.fills if fill.side is OrderSide.SELL)
    assert [fill.quantity for fill in sell_fills] == [Quantity(100), Quantity(900)]
    assert [fill.trading_date for fill in sell_fills] == [
        START + timedelta(days=5),
        START + timedelta(days=6),
    ]
    assert sell.attempts == 2
    assert sell.status is DecisionStatus.FILLED
    assert result.final_portfolio.position_quantity(INSTRUMENT) == Quantity.zero()


def test_market_exit_wins_first_of_before_later_holding_target() -> None:
    source = bars(closes=("10", "9", "11", "12", "8", "7", "7"))
    base = strategy(end_offset=6)
    spec = base.model_copy(
        update={
            "exit": FirstOfExit(children=(base.exit.children[0], HoldingPeriodExit(sessions=3)))
        }
    )

    result = run(source_bars=source, spec=spec)

    sell = next(item for item in result.decisions if item.side is OrderSide.SELL)
    assert result.fills[1].trading_date == START + timedelta(days=5)
    assert not sell.signal.condition_ref.startswith("holding_period_exit:")


def test_holding_target_after_backtest_end_does_not_force_liquidation() -> None:
    source = bars(closes=("10", "9", "11", "12", "12", "12", "12"))
    spec = holding_strategy(holding_sessions=10, end=source[-1].session_date)

    result = run(source_bars=source, spec=spec)

    assert [fill.side for fill in result.fills] == [OrderSide.BUY]
    assert all(item.side is not OrderSide.SELL for item in result.decisions)
    assert result.final_portfolio.position_quantity(INSTRUMENT).value > 0


def test_open_capacity_uses_only_the_previous_completed_session_volume() -> None:
    source = list(bars())
    expected_entry_index = 3
    source[expected_entry_index - 1] = replace(
        source[expected_entry_index - 1], volume=Quantity(1_000_000)
    )
    # A traded session is required, but its final volume must not be used to
    # size the pre-open order. The distinct zero-trade test covers no execution.
    source[expected_entry_index] = replace(source[expected_entry_index], volume=Quantity(1))

    result = run(source_bars=tuple(source), source_sessions=sessions(tuple(source)))

    assert result.fills[0].trading_date == source[expected_entry_index].session_date
    assert result.orders[0].match.capacity_reason_code == ("capacity_previous_session_volume_proxy")


def test_first_session_without_a_prior_completed_bar_fails_capacity_closed() -> None:
    first_day_event = market_event(datetime(2025, 1, 2, 8, 0, tzinfo=TZ))

    result = run(spec=event_strategy(), source_events=(first_day_event,))

    assert result.fills == ()
    assert result.orders[0].match.reason_code == "point_in_time_capacity_unknown"
    assert result.decisions[0].status is DecisionStatus.UNFILLED
    assert result.decisions[0].attempts == 1
    assert len(result.orders) == 1


def test_unlimited_capacity_is_an_explicit_audited_override() -> None:
    first_day_event = market_event(datetime(2025, 1, 2, 8, 0, tzinfo=TZ))
    config = DailyBacktestConfig(
        participation_rate=Decimal("1"),
        slippage_bps=Decimal("0"),
        allocation_ratio=Decimal("0.9"),
        capacity_mode=CapacityMode.UNLIMITED,
    )

    result = run(spec=event_strategy(), source_events=(first_day_event,), config=config)

    assert result.fills[0].trading_date == START
    assert result.orders[0].match.capacity_reason_code == "capacity_unlimited_explicit"


def test_result_hash_covers_the_capacity_evidence_path() -> None:
    point_in_time = run()
    unlimited = run(
        config=DailyBacktestConfig(
            participation_rate=Decimal("1"),
            slippage_bps=Decimal("0"),
            allocation_ratio=Decimal("0.9"),
            capacity_mode=CapacityMode.UNLIMITED,
        )
    )

    assert point_in_time.fills == unlimited.fills
    assert point_in_time.content_hash != unlimited.content_hash


def test_adjusted_signal_bars_drive_signal_but_raw_bars_set_fill_price() -> None:
    execution = bars(closes=("5", "5", "5", "30", "30", "30", "30"))
    signal = tuple(
        replace(item, price_basis=PriceBasis.BACK_ADJUSTED)
        for item in bars(closes=("10", "9", "11", "12", "8", "7", "7"))
    )

    result = run(
        source_bars=execution,
        source_signal_bars=signal,
        source_sessions=sessions(execution),
    )

    assert result.fills[0].trading_date == START + timedelta(days=3)
    assert result.fills[0].price == Price(Decimal("30"))
    assert result.decisions[0].signal.left_value == Decimal("11")


def test_explicit_signal_bars_must_align_with_execution_dates() -> None:
    execution = bars()
    signal = tuple(replace(item, price_basis=PriceBasis.BACK_ADJUSTED) for item in bars()[:-2])

    with pytest.raises(DailyBacktestInputError, match="align one-to-one"):
        run(
            source_bars=execution,
            source_signal_bars=signal,
            source_sessions=sessions(execution),
        )


def test_full_path_is_deterministic_including_audit_hash() -> None:
    first = run()
    replay = run()

    assert first.content_hash == replay.content_hash
    assert first.fills == replay.fills
    assert first.orders == replay.orders
    assert [event.sequence for event in first.orders[0].events] == [1, 2, 3, 4]


def test_suspended_target_session_ends_default_signal_without_inventing_an_order() -> None:
    source_bars = bars()
    result = run(
        source_bars=source_bars,
        source_sessions=sessions(source_bars, suspended_offsets=(3,)),
    )

    assert result.fills == ()
    assert result.orders == ()
    assert result.decisions[0].status is DecisionStatus.UNFILLED
    assert result.decisions[0].outcome_reason == "security_not_trading"


def test_late_signal_waits_until_the_first_open_after_availability() -> None:
    source = list(bars())
    source[2] = replace(
        source[2],
        available_at=datetime.combine(
            START + timedelta(days=3),
            datetime.min.time().replace(hour=10),
            tzinfo=TZ,
        ),
    )
    result = run(source_bars=tuple(source), source_sessions=sessions(tuple(source)))

    assert result.fills[0].trading_date == START + timedelta(days=4)


def test_late_old_signal_cannot_override_a_newer_known_signal() -> None:
    source = list(bars(closes=("10", "9", "11", "8", "7", "9", "10", "10", "10")))
    source[2] = replace(
        source[2],
        available_at=datetime.combine(
            START + timedelta(days=5),
            datetime.min.time().replace(hour=16),
            tzinfo=TZ,
        ),
    )
    actual_bars = tuple(source)

    result = run(
        source_bars=actual_bars,
        source_sessions=sessions(actual_bars),
        spec=strategy(end_offset=8),
    )

    assert result.decisions[0].signal.session_date == START + timedelta(days=5)
    assert result.fills[0].trading_date == START + timedelta(days=6)


def test_result_hash_covers_the_full_benchmark_path() -> None:
    dates = tuple(START + timedelta(days=offset) for offset in range(7))
    first_benchmark = tuple(
        zip(
            dates,
            map(Decimal, ("10", "11", "9", "12", "8", "13", "14")),
            strict=True,
        )
    )
    second_benchmark = tuple(
        zip(
            dates,
            map(Decimal, ("10", "8", "12", "7", "15", "13", "14")),
            strict=True,
        )
    )

    first = run(benchmark_close=first_benchmark)
    second = run(benchmark_close=second_benchmark)

    assert first.metrics.benchmark_return == second.metrics.benchmark_return
    assert first.content_hash != second.content_hash


def test_funded_benchmark_keeps_pre_entry_cash_as_the_return_base() -> None:
    dates = tuple(START + timedelta(days=offset) for offset in range(7))
    funded = tuple((day, Decimal("99000") + Decimal(offset)) for offset, day in enumerate(dates))

    result = run(
        benchmark_equity=funded,
        benchmark_initial_equity=Decimal("100000"),
        benchmark_entry_filled=True,
    )

    assert result.equity_curve[0].benchmark == Decimal("100000")
    assert result.equity_curve[1].benchmark == Decimal("99000")
    assert result.metrics.benchmark_return == pytest.approx(-0.00995)


def test_cash_dividend_is_staged_and_included_in_round_trip_pnl() -> None:
    record_date = START + timedelta(days=3)
    ex_date = START + timedelta(days=4)
    pay_date = START + timedelta(days=5)
    action = CorporateAction(
        action_id=StrongId("action:cash:2025"),
        source_action_id="source:cash:2025",
        instrument_id=INSTRUMENT,
        action_type=CorporateActionKind.CASH_DIVIDEND,
        record_date=record_date,
        ex_date=ex_date,
        source_released_at=datetime.combine(
            record_date,
            datetime.min.time().replace(hour=10),
            tzinfo=TZ,
        ),
        vendor_first_available_at=datetime.combine(
            record_date,
            datetime.min.time().replace(hour=10, minute=1),
            tzinfo=TZ,
        ),
        ingested_at=datetime.combine(
            record_date,
            datetime.min.time().replace(hour=10, minute=3),
            tzinfo=TZ,
        ),
        replay_available_at=datetime.combine(
            record_date,
            datetime.min.time().replace(hour=10, minute=2),
            tzinfo=TZ,
        ),
        revision_no=0,
        time_quality=TimeQuality.EXACT,
        provider="fixture",
        source_url="https://example.test/dividend",
        raw_response_sha256="c" * 64,
        validation_status="validated",
        gross_cash_per_share=Decimal("0.5"),
        cash_pay_date=pay_date,
    )

    baseline = run()
    result = run(source_actions=(action,))
    entitled_shares = result.final_portfolio.corporate_action_entitlements[0].entitled_quantity
    dividend = Decimal(entitled_shares.value) * Decimal("0.5")

    assert result.round_trips[0].net_pnl == baseline.round_trips[0].net_pnl + dividend
    assert result.final_portfolio.cash.amount == baseline.final_portfolio.cash.amount + dividend
    assert result.final_portfolio.dividend_receivable(INSTRUMENT) == Money.zero()
    assert [item.phase.value for item in result.final_portfolio.corporate_action_entries] == [
        "entitlement",
        "accrual",
        "settlement",
    ]
    assert result.final_portfolio.corporate_action_entries[-1].occurred_at.hour == 15
    assert result.content_hash != baseline.content_hash


def test_rights_issue_is_explicitly_declined_without_changing_account_economics() -> None:
    record_date = START + timedelta(days=3)
    ex_date = START + timedelta(days=4)
    action = CorporateAction(
        action_id=StrongId("action:rights:declined"),
        source_action_id="source:rights:declined",
        instrument_id=INSTRUMENT,
        action_type=CorporateActionKind.RIGHTS_ISSUE,
        record_date=record_date,
        ex_date=ex_date,
        source_released_at=datetime(2025, 1, 5, 10, tzinfo=TZ),
        vendor_first_available_at=datetime(2025, 1, 5, 10, 1, tzinfo=TZ),
        ingested_at=datetime(2025, 1, 5, 10, 3, tzinfo=TZ),
        replay_available_at=datetime(2025, 1, 5, 10, 2, tzinfo=TZ),
        revision_no=0,
        time_quality=TimeQuality.EXACT,
        provider="fixture",
        source_url="https://example.test/rights-declined",
        raw_response_sha256="7" * 64,
        validation_status="validated",
        rights_ratio=Decimal("0.3"),
        rights_subscription_price=Decimal("5"),
        rights_payment_deadline=START + timedelta(days=6),
        rights_listing_date=START + timedelta(days=8),
    )

    baseline = run()
    result = run(source_actions=(action,))

    assert result.final_portfolio.cash == baseline.final_portfolio.cash
    assert result.final_portfolio.lots == baseline.final_portfolio.lots
    assert result.final_portfolio.corporate_action_entitlements == ()
    assert [item.phase.value for item in result.final_portfolio.corporate_action_entries] == [
        "declined"
    ]
    assert result.content_hash != baseline.content_hash


def test_one_implementation_announcement_can_supply_cash_and_share_legs() -> None:
    record_date = START + timedelta(days=3)
    ex_date = START + timedelta(days=4)
    cash = CorporateAction(
        action_id=StrongId("action:combined:cash"),
        source_action_id="announcement:combined:2025",
        instrument_id=INSTRUMENT,
        action_type=CorporateActionKind.CASH_DIVIDEND,
        record_date=record_date,
        ex_date=ex_date,
        source_released_at=datetime(2025, 1, 5, 10, tzinfo=TZ),
        vendor_first_available_at=datetime(2025, 1, 5, 10, 1, tzinfo=TZ),
        ingested_at=datetime(2025, 1, 5, 10, 3, tzinfo=TZ),
        replay_available_at=datetime(2025, 1, 5, 10, 2, tzinfo=TZ),
        revision_no=0,
        time_quality=TimeQuality.EXACT,
        provider="fixture",
        source_url="https://example.test/combined",
        raw_response_sha256="e" * 64,
        validation_status="validated",
        gross_cash_per_share=Decimal("0.2"),
        cash_pay_date=START + timedelta(days=5),
    )
    shares = replace(
        cash,
        action_id=StrongId("action:combined:shares"),
        action_type=CorporateActionKind.SHARE_DISTRIBUTION,
        gross_cash_per_share=None,
        cash_pay_date=None,
        share_multiplier=Decimal("1.5"),
        share_credit_date=ex_date,
        share_sellable_date=ex_date,
    )

    result = run(source_actions=(cash, shares))

    assert [item.action_type for item in result.final_portfolio.corporate_action_entitlements] == [
        CorporateActionKind.CASH_DIVIDEND,
        CorporateActionKind.SHARE_DISTRIBUTION,
    ]
    assert len(result.final_portfolio.corporate_action_entries) == 6
    assert result.final_portfolio.position_quantity(INSTRUMENT) == Quantity.zero()


def test_share_distribution_integer_non_round_lot_can_exit_in_full() -> None:
    record_date = START + timedelta(days=3)
    ex_date = START + timedelta(days=4)
    action = CorporateAction(
        action_id=StrongId("action:integer-non-round-lot"),
        source_action_id="announcement:integer-non-round-lot",
        instrument_id=INSTRUMENT,
        action_type=CorporateActionKind.SHARE_DISTRIBUTION,
        record_date=record_date,
        ex_date=ex_date,
        source_released_at=datetime(2025, 1, 5, 10, tzinfo=TZ),
        vendor_first_available_at=datetime(2025, 1, 5, 10, 1, tzinfo=TZ),
        ingested_at=datetime(2025, 1, 5, 10, 3, tzinfo=TZ),
        replay_available_at=datetime(2025, 1, 5, 10, 2, tzinfo=TZ),
        revision_no=0,
        time_quality=TimeQuality.EXACT,
        provider="fixture",
        source_url="https://example.test/integer-non-round-lot",
        raw_response_sha256="f" * 64,
        validation_status="validated",
        share_multiplier=Decimal("1.5"),
        share_credit_date=ex_date,
        share_sellable_date=ex_date,
    )
    small_account = strategy().model_copy(
        update={
            "backtest": BacktestConfig(
                start=START,
                end=START + timedelta(days=5),
                initial_cash_cny=2_000,
            )
        }
    )

    result = run(
        spec=small_account,
        source_sessions=tuple(
            replace(item, upper_limit=Price(Decimal("13"))) for item in sessions(bars())
        ),
        source_actions=(action,),
        config=DailyBacktestConfig(
            participation_rate=Decimal("1"),
            slippage_bps=Decimal("0"),
            allocation_ratio=Decimal("0.9"),
        ),
    )

    assert [(fill.side, fill.quantity) for fill in result.fills] == [
        (OrderSide.BUY, Quantity(100)),
        (OrderSide.SELL, Quantity(150)),
    ]
    assert result.final_portfolio.position_quantity(INSTRUMENT) == Quantity.zero()
    sell_trace = next(trace for trace in result.orders if trace.order.side is OrderSide.SELL)
    assert sell_trace.match.capacity_reason_code == "capacity_previous_session_volume_proxy"


def test_one_price_limit_up_is_an_audited_no_fill_under_default_policy() -> None:
    source = list(bars(closes=("10", "9", "11", "12")))
    source_sessions = list(sessions(tuple(source)))
    source_sessions[3] = replace(
        source_sessions[3],
        upper_limit=Price(Decimal("12")),
    )
    result = run(
        source_bars=tuple(source),
        source_sessions=tuple(source_sessions),
        spec=strategy(end_offset=3),
    )

    assert result.fills == ()
    assert result.orders[0].match.reason_code == "one_price_limit_up"
    assert result.decisions[0].status is DecisionStatus.UNFILLED
    assert result.decisions[0].attempts == 1


def test_explicit_edge_validity_override_retries_with_new_day_orders() -> None:
    source = bars(closes=("10", "9", "11", "12", "12", "12", "12"))
    source_sessions = list(sessions(source))
    for offset in (3, 4):
        source_sessions[offset] = replace(
            source_sessions[offset],
            upper_limit=source[offset].open,
        )

    result = run(source_bars=source, source_sessions=tuple(source_sessions),
                 config=DailyBacktestConfig(edge_entry_validity_sessions=3))
    entry = result.decisions[0]
    entry_orders = tuple(
        trace for trace in result.orders if trace.order.decision_id == entry.decision_id
    )

    assert entry.signal_semantics is EntrySignalSemantics.EDGE
    assert entry.signal_validity_sessions == 3
    assert entry.attempts == 3
    assert entry.status is DecisionStatus.FILLED
    assert [trace.match.reason_code for trace in entry_orders] == [
        "one_price_limit_up",
        "one_price_limit_up",
        "matched_at_open",
    ]
    assert [trace.order.created_at.date() for trace in entry_orders] == [
        START + timedelta(days=3),
        START + timedelta(days=4),
        START + timedelta(days=5),
    ]
    assert len({trace.order.order_id for trace in entry_orders}) == 3
    assert all(trace.order.time_in_force.value == "day" for trace in entry_orders)


def test_edge_entry_validity_is_configurable_and_stops_at_its_limit() -> None:
    source = bars(closes=("10", "9", "11", "12", "12", "12", "12"))
    source_sessions = list(sessions(source))
    for offset in (3, 4, 5):
        source_sessions[offset] = replace(
            source_sessions[offset],
            upper_limit=source[offset].open,
        )

    result = run(
        source_bars=source,
        source_sessions=tuple(source_sessions),
        config=DailyBacktestConfig(
            participation_rate=Decimal("1"),
            slippage_bps=Decimal("0"),
            allocation_ratio=Decimal("0.9"),
            edge_entry_validity_sessions=2,
        ),
    )
    entry = result.decisions[0]

    assert entry.signal_validity_sessions == 2
    assert entry.attempts == 2
    assert entry.status is DecisionStatus.UNFILLED
    assert len(entry.order_ids) == 2
    assert result.fills == ()


def test_event_entry_has_one_attempt_without_revision_invalidation() -> None:
    source = bars(closes=("10", "10", "10", "10", "10", "10"))
    source_sessions = list(sessions(source))
    for offset in range(4):
        source_sessions[offset] = replace(
            source_sessions[offset],
            upper_limit=source[offset].open,
        )
    source_event = market_event(datetime(2025, 1, 2, 8, 0, tzinfo=TZ))

    result = run(
        source_bars=source,
        source_sessions=tuple(source_sessions),
        spec=event_strategy(),
        source_events=(source_event,),
        config=DailyBacktestConfig(
            participation_rate=Decimal("1"),
            slippage_bps=Decimal("0"),
            allocation_ratio=Decimal("0.9"),
            capacity_mode=CapacityMode.UNLIMITED,
        ),
    )
    entry = result.decisions[0]

    assert entry.signal_semantics is EntrySignalSemantics.EVENT
    assert entry.signal_validity_sessions == 1
    assert entry.attempts == 1
    assert entry.status is DecisionStatus.UNFILLED
    assert len(entry.order_ids) == 1
    assert result.fills == ()


def test_same_session_later_event_is_not_merged_into_earlier_signal_evidence() -> None:
    event_date = START + timedelta(days=2)
    first = market_event(datetime.combine(event_date, time(8), tzinfo=TZ))
    later_base = market_event(datetime.combine(event_date, time(14), tzinfo=TZ))
    later = replace(
        later_base,
        event=replace(later_base.event, event_id=StrongId("event-test-later")),
    )

    result = run(spec=event_strategy(), source_events=(later, first))
    signal = next(item for item in result.signals if item.triggered)

    assert signal.available_at == first.available_at
    assert [item.evidence_id for item in signal.evidence] == ["event-test-1"]


def test_state_entry_expires_after_one_attempt_but_a_new_day_can_signal_again() -> None:
    base = strategy()
    state_entry = base.entry.model_copy(update={"trigger": "price_above"})
    state_strategy = base.model_copy(update={"entry": state_entry})
    source = bars(closes=("10", "9", "11", "12", "13", "14", "14"))
    source_sessions = list(sessions(source))
    source_sessions[3] = replace(source_sessions[3], upper_limit=source[3].open)

    result = run(
        source_bars=source,
        source_sessions=tuple(source_sessions),
        spec=state_strategy,
    )
    buy_decisions = tuple(item for item in result.decisions if item.side is OrderSide.BUY)

    assert len(buy_decisions) == 2
    assert buy_decisions[0].signal_semantics is EntrySignalSemantics.STATE
    assert buy_decisions[0].signal_validity_sessions == 1
    assert buy_decisions[0].status is DecisionStatus.UNFILLED
    assert buy_decisions[0].attempts == 1
    assert buy_decisions[1].status is DecisionStatus.FILLED
    assert buy_decisions[1].attempts == 1


def test_state_entry_cannot_retry_a_stale_condition_without_revalidation() -> None:
    with pytest.raises(DailyBacktestInputError, match="state revalidation"):
        DailyBacktestConfig(state_entry_validity_sessions=2)


def test_composite_entry_fails_closed_after_one_unfilled_day_order() -> None:
    base = event_strategy()
    state_params: dict[str, JsonScalar] = {"period": 2, "price_field": "close"}
    composite = AllCondition(
        children=(
            base.entry,
            IndicatorCondition(
                indicator_id="technical.ma",
                definition_version="1.0.0",
                params=state_params,
                trigger="price_above",
            ),
        )
    )
    composite_strategy = base.model_copy(update={"entry": composite})
    source = bars(closes=("10", "10", "12", "8", "8", "8"))
    source_sessions = list(sessions(source))
    source_sessions[3] = replace(
        source_sessions[3],
        upper_limit=source[3].open,
    )
    source_event = market_event(datetime(2025, 1, 4, 8, 0, tzinfo=TZ))

    result = run(
        source_bars=source,
        source_sessions=tuple(source_sessions),
        spec=composite_strategy,
        source_events=(source_event,),
    )
    entry = next(item for item in result.decisions if item.side is OrderSide.BUY)

    assert entry.signal_semantics is EntrySignalSemantics.COMPOSITE
    assert entry.signal_validity_sessions == 1
    assert entry.attempts == 1
    assert entry.status is DecisionStatus.UNFILLED
    assert len(entry.order_ids) == 1
    assert result.fills == ()


def test_partial_entry_fill_does_not_retry_the_remaining_quantity() -> None:
    result = run(
        config=DailyBacktestConfig(
            participation_rate=Decimal("0.0001"),
            slippage_bps=Decimal("0"),
            allocation_ratio=Decimal("0.9"),
        )
    )
    entry = next(item for item in result.decisions if item.side is OrderSide.BUY)
    entry_orders = tuple(
        trace for trace in result.orders if trace.order.decision_id == entry.decision_id
    )

    assert entry.status is DecisionStatus.PARTIALLY_FILLED
    assert entry.attempts == 1
    assert len(entry_orders) == 1
    assert entry_orders[0].order.filled_quantity == Quantity(100)


def test_insufficient_cash_rejects_entry_without_retrying() -> None:
    base = strategy()
    small_account = base.model_copy(
        update={
            "backtest": BacktestConfig(
                start=base.backtest.start,
                end=base.backtest.end,
                initial_cash_cny=500,
            )
        }
    )

    result = run(spec=small_account)
    entry = result.decisions[0]

    assert entry.status is DecisionStatus.REJECTED
    assert entry.outcome_reason == "insufficient_cash_for_minimum_buy_quantity"
    assert entry.attempts == 0
    assert entry.order_ids == ()
    assert result.orders == ()


def test_event_entry_does_not_remain_pending_until_a_later_revision_or_exit_signal() -> None:
    source = bars(closes=("10", "9", "11", "12", "8", "7", "7"))
    source_sessions = list(sessions(source))
    for offset in (2, 3, 4):
        source_sessions[offset] = replace(
            source_sessions[offset],
            upper_limit=source[offset].open,
        )
    source_event = market_event(datetime(2025, 1, 4, 8, 0, tzinfo=TZ))

    result = run(
        source_bars=source,
        source_sessions=tuple(source_sessions),
        spec=event_strategy(),
        source_events=(source_event,),
    )
    entry = result.decisions[0]

    assert entry.status is DecisionStatus.UNFILLED
    assert entry.attempts == 1
    assert len(entry.order_ids) == 1
    assert entry.outcome_reason == "one_price_limit_up"
    assert result.fills == ()


def test_mutating_bars_after_backtest_end_cannot_change_result() -> None:
    source = bars()
    first = run(source_bars=source, source_sessions=sessions(source))
    changed = (
        *source[:-1],
        replace(
            source[-1],
            close=Price(Decimal("40")),
            high=Price(Decimal("40")),
        ),
    )
    replay = run(source_bars=changed, source_sessions=sessions(changed))

    assert first.content_hash == replay.content_hash


def test_explicit_optimistic_limit_mode_is_not_the_default() -> None:
    source = list(bars(closes=("10", "9", "11", "12")))
    source_sessions = list(sessions(tuple(source)))
    source_sessions[3] = replace(source_sessions[3], upper_limit=Price(Decimal("12")))
    result = run(
        source_bars=tuple(source),
        source_sessions=tuple(source_sessions),
        spec=strategy(end_offset=3),
        config=DailyBacktestConfig(
            participation_rate=Decimal("1"),
            slippage_bps=Decimal("5"),
            allocation_ratio=Decimal("0.9"),
            limit_handling=LimitHandling.ALLOW_LIMIT_VOLUME,
        ),
    )

    assert len(result.fills) == 1
    assert result.fills[0].price == Price(Decimal("12"))


def test_pre_open_event_can_buy_at_the_same_session_open() -> None:
    event_date = START + timedelta(days=2)
    source_event = market_event(datetime(2025, 1, 4, 8, 0, tzinfo=TZ))

    result = run(spec=event_strategy(), source_events=(source_event,))

    assert result.fills[0].side is OrderSide.BUY
    assert result.fills[0].trading_date == event_date
    assert result.decisions[0].created_at == source_event.available_at
    assert source_event.available_at is not None
    assert result.orders[0].order.created_at >= source_event.available_at
    assert result.orders[0].order.submitted_at == datetime.combine(
        event_date,
        datetime.min.time().replace(hour=9, minute=15),
        tzinfo=TZ,
    )
    assert result.fills[0].filled_at == datetime.combine(
        event_date,
        datetime.min.time().replace(hour=9, minute=30),
        tzinfo=TZ,
    )


@pytest.mark.parametrize(
    ("available_time", "expected_offset"),
    [
        (datetime.min.time().replace(hour=9, minute=14, second=59), 0),
        (datetime.min.time().replace(hour=9, minute=15), 1),
    ],
)
def test_event_published_open_proxy_eligibility_includes_one_second_processing_latency(
    available_time: time,
    expected_offset: int,
) -> None:
    event_date = START + timedelta(days=2)
    source_event = market_event(datetime.combine(event_date, available_time, tzinfo=TZ))

    result = run(spec=event_strategy(), source_events=(source_event,))

    assert result.fills[0].trading_date == event_date + timedelta(days=expected_offset)


def test_intraday_or_date_only_close_event_waits_for_next_session_open() -> None:
    event_date = START + timedelta(days=2)
    intraday = market_event(
        datetime.combine(event_date, datetime.min.time().replace(hour=10), tzinfo=TZ)
    )
    at_close = market_event(
        datetime.combine(event_date, datetime.min.time().replace(hour=15), tzinfo=TZ),
        quality=TimeQuality.DATE_ONLY_CONSERVATIVE,
    )

    intraday_result = run(spec=event_strategy(), source_events=(intraday,))
    close_result = run(spec=event_strategy(), source_events=(at_close,))

    expected = event_date + timedelta(days=1)
    assert intraday_result.fills[0].trading_date == expected
    assert close_result.fills[0].trading_date == expected


def test_event_at_exact_open_is_not_backfilled_into_that_open() -> None:
    event_date = START + timedelta(days=2)
    at_open = market_event(
        datetime.combine(event_date, datetime.min.time().replace(hour=9, minute=30), tzinfo=TZ)
    )

    result = run(spec=event_strategy(), source_events=(at_open,))

    assert result.fills[0].trading_date == event_date + timedelta(days=1)


def test_estimated_research_event_never_creates_a_decision() -> None:
    estimated = market_event(
        datetime(2025, 1, 4, 8, 0, tzinfo=TZ),
        quality=TimeQuality.ESTIMATED_RESEARCH_ONLY,
    )

    result = run(spec=event_strategy(), source_events=(estimated,))

    assert result.decisions == ()
    assert result.fills == ()
