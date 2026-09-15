from dataclasses import replace
from datetime import date
from decimal import Decimal as D
import pytest

from ashare_lab.application.minute_conditional_orders import MinuteConditionalOrders
from ashare_lab.application.minute_grid_replay import replay_grid
from ashare_lab.application.scheduled_execution import ScheduledOrder
from ashare_lab.domain.execution.bar_prices import BarPrices
from ashare_lab.domain.portfolio import PortfolioState
from ashare_lab.domain.shared import Money
from ashare_lab.domain.strategy.price_plans import ConditionRule
from tests.unit.application.test_minute_grid_replay import bar, FEES, SEC
from tests.unit.application.test_minute_corporate_actions import clock


def run(rules, bars, **kwargs):
    return replay_grid(MinuteConditionalOrders(rules), bars, PortfolioState(Money(D(1000000))), FEES, **kwargs)


@pytest.mark.parametrize("rule", [
    ConditionRule(kind="price", side="buy", target_price=10, quantity=100),
    ConditionRule(kind="rebound", side="buy", gap=5, gap_unit="percent", quantity=100),
])
def test_minute_entry_family_combines_with_independent_daily_exit(rule):
    from ashare_lab.application.minute_conditional_orders import execute_minute_conditions
    from ashare_lab.application.minute_replay_input import PreparedMinuteReplay
    from ashare_lab.domain.execution import AshareExchange
    from ashare_lab.domain.strategy.price_plans import ConditionParameters
    from tests.unit.application.test_minute_grid_replay import daily_intent
    prepared = PreparedMinuteReplay(
        (bar(9, 31, 10), bar(9, 32, 11), bar(9, 33, 11), bar(10, 31, 11)), (),
        (date(2026, 9, 9), date(2026, 9, 10), date(2026, 9, 11)))
    result = execute_minute_conditions(
        ConditionParameters(observation="minute_bar", rules=[rule], slippage_bps=0),
        prepared=prepared, exchange=AshareExchange.SHENZHEN,
        daily_signals=(daily_intent(9, 10, exit=True),))
    assert [(f.side.value, f.quantity.value, f.filled_at.day) for f in result.portfolio.fills] == [
        ("buy", 100, 9), ("sell", 100, 10)]
    assert result.portfolio.position_quantity(SEC).value == 0


@pytest.mark.parametrize("maximum,quantity", [(10000, 900), (100, 100)])
def test_daily_entry_with_minute_exit_keeps_inventory_limit(maximum, quantity):
    from ashare_lab.application.minute_conditional_orders import execute_minute_conditions
    from ashare_lab.application.minute_replay_input import PreparedMinuteReplay
    from ashare_lab.domain.execution import AshareExchange
    from ashare_lab.domain.strategy.price_plans import ConditionParameters
    from tests.unit.application.test_minute_grid_replay import daily_intent
    prepared = PreparedMinuteReplay(
        (bar(9, 31, 10), bar(9, 32, 11), bar(9, 33, 11), bar(10, 31, 11)), (),
        tuple(date(2026, 9, day) for day in (8, 9, 10, 11)))
    result = execute_minute_conditions(
        ConditionParameters(observation="minute_bar", initial_cash_cny=10000,
            max_shares=maximum, slippage_bps=0, rules=[ConditionRule(
                kind="price", side="sell", target_price=11, sizing_mode="all_position")]),
        prepared=prepared, exchange=AshareExchange.SHENZHEN,
        daily_signals=(daily_intent(8, 9, enter=True),))
    assert [(f.side.value, f.quantity.value) for f in result.portfolio.fills] == [
        ("buy", quantity), ("sell", quantity)]
    assert result.portfolio.position_quantity(SEC).value == 0
    assert quantity <= maximum


def test_recurring_schedule_exit_survives_t1_and_rearms_after_later_buy():
    policy = MinuteConditionalOrders([
        ConditionRule(kind="price", side="sell", target_price=11,
                      sizing_mode="all_position"),
    ], recurring_exits=True)
    result = replay_grid(policy,
        [bar(7, 31, 10), bar(7, 32, 11), bar(7, 33, 11), bar(8, 31, 11),
         bar(9, 31, 10), bar(9, 32, 11), bar(9, 33, 11), bar(10, 31, 11)],
        PortfolioState(Money(D(1000000))), FEES, slippage_bps=D(0),
        scheduled_orders=(
            ScheduledOrder("first", date(2026, 9, 7), clock(7, 9), quantity=100),
            ScheduledOrder("next", date(2026, 9, 9), clock(7, 9), quantity=100),
        ))
    assert [(fill.side.value, fill.quantity.value) for fill in result.portfolio.fills] == [
        ("buy", 100), ("sell", 100), ("buy", 100), ("sell", 100)]
    assert policy.cycles == 2
    assert result.portfolio.position_quantity(SEC).value == 0


def test_recurring_schedule_does_not_accept_buy_conditions():
    from ashare_lab.application.minute_grid_plan import MinuteGridCapabilityError
    with pytest.raises(MinuteGridCapabilityError, match="recurring_schedule_requires_exit_rules"):
        MinuteConditionalOrders([ConditionRule(kind="price", side="buy", target_price=10)],
                                recurring_exits=True)


def test_daily_exit_cancels_stale_price_exit_without_disabling_next_cycle():
    from tests.unit.application.test_minute_grid_replay import daily_intent
    policy = MinuteConditionalOrders([
        ConditionRule(kind="price", side="sell", target_price=11, sizing_mode="all_position"),
    ], recurring_exits=True)
    result = replay_grid(policy,
        [bar(7, 31, 10), bar(7, 32, 11), bar(7, 33, 11), bar(8, 31, 11),
         bar(9, 31, 10), bar(9, 32, 11), bar(9, 33, 11), bar(10, 31, 11)],
        PortfolioState(Money(D(10000))), FEES, slippage_bps=D(0),
        scheduled_orders=(
            ScheduledOrder("first", date(2026, 9, 7), clock(7, 9), quantity=100),
            ScheduledOrder("next", date(2026, 9, 9), clock(7, 9), quantity=100),
        ), daily_signals=(daily_intent(7, 8, exit=True),),
        market_sessions=tuple(date(2026, 9, day) for day in (7, 8, 9, 10)))
    assert [(f.side.value, f.quantity.value, f.filled_at.day) for f in result.portfolio.fills] == [
        ("buy", 100, 7), ("sell", 100, 8), ("buy", 100, 9), ("sell", 100, 10)]
    assert any(e.reason == "due_inventory_changed" and e.status == "cancelled"
               for e in result.events)
    assert not any(e.reason == "insufficient_position" for e in result.events)
    assert not policy.paused


@pytest.mark.parametrize("direction", ["up", "down"])
@pytest.mark.parametrize("comparison", ["inclusive", "strict"])
def test_minute_price_comparison_keeps_equality_distinct(direction, comparison):
    policy = MinuteConditionalOrders([ConditionRule(kind="price", side="buy", target_price=10,
        direction=direction, price_comparison=comparison)])
    portfolio = PortfolioState(Money(D(1000000)))
    first = bar(9, 31, 10)
    policy.prepare_bar(portfolio, first, 0)
    orders = policy.observe(first.prices, 0)
    assert bool(orders) == (comparison == "inclusive")
    if comparison == "strict":
        next_bar = bar(9, 32, D('10.01' if direction == 'up' else '9.99'))
        policy.prepare_bar(portfolio, next_bar, 1)
        orders = policy.observe(next_bar.prices, 1)
        assert len(orders) == 1 and orders[0].effective_bar == 2


def test_first_stage_cannot_reference_a_nonexistent_previous_fill():
    from ashare_lab.application.minute_grid_plan import MinuteGridCapabilityError
    with pytest.raises(MinuteGridCapabilityError, match="conditional_initial_reference_missing"):
        MinuteConditionalOrders([
            ConditionRule(kind="relative_price", side="sell", gap=1, gap_unit="cny"),
            ConditionRule(kind="relative_price", side="buy", direction="down", gap=1, gap_unit="cny"),
        ])


def test_sell_first_t_cycle_anchors_once_to_first_observation_then_previous_fill():
    from ashare_lab.domain.portfolio import PositionLot
    from ashare_lab.domain.shared import FillId, Quantity

    opening = PortfolioState(Money(D(990000)), lots=(PositionLot(
        FillId("opening-import:lot-1"), SEC, clock(8, 10), date(2026, 9, 8),
        date(2026, 9, 9), Quantity(1000), Money(D(10000)), None,
    ),))
    policy = MinuteConditionalOrders([
        ConditionRule(kind="relative_price", side="sell", direction="up", gap=1,
                      gap_unit="cny", quantity=100,
                      reference_mode="first_observation"),
        ConditionRule(kind="relative_price", side="buy", direction="down", gap=1,
                      gap_unit="cny", quantity=100,
                      reference_mode="previous_fill"),
    ])
    result = replay_grid(
        policy,
        [bar(9, 31, 10), bar(9, 32, 11), bar(9, 33, 11),
         bar(9, 34, 10), bar(9, 35, 10)],
        opening,
        FEES,
        slippage_bps=D(0),
        minimum_shares=500,
        initial_equity_cny=D(1000000),
    )
    assert [(fill.side.value, fill.price.amount) for fill in result.portfolio.fills] == [
        ("sell", D(11)), ("buy", D(10)),
    ]
    assert result.portfolio.position_quantity(SEC).value == 1000
    assert policy.cycles == 1


def test_price_trigger_limit_and_sequential_fill_use_separate_minutes():
    result = run([ConditionRule(kind="price", side="buy", direction="down", target_price=9, limit_price=8),
                  ConditionRule(kind="price", side="sell", direction="up", target_price=10)],
                 [bar(9, 31, 9), bar(9, 32, 8), bar(9, 33, 10), bar(10, 31, 10)])
    assert [fill.price.amount for fill in result.portfolio.fills] == [8, 10]
    assert [event.bar_index for event in result.events if event.status == "filled"] == [1, 3]
    assert result.portfolio.position_quantity(SEC).value == 0


def test_protection_oco_is_stop_first_and_retries_t1_without_duplicate_trigger():
    rules = [ConditionRule(kind="take_profit", side="sell", gap=5, group="exit"),
             ConditionRule(kind="stop_loss", side="sell", gap=5, group="exit")]
    both = replace(bar(9, 32, 10), prices=BarPrices(D(10), D(11), D(9), D(10)))
    result = run(rules, [bar(9, 31, 10), both, bar(9, 33, 9), bar(9, 34, 9), bar(10, 31, 9)],
                 scheduled_orders=(ScheduledOrder("seed", date(2026, 9, 9), clock(9, 9), quantity=100),))
    signals = [e for e in result.events if e.reason == "stop_loss"]
    assert len(signals) == 1 and signals[0].ambiguous_intrabar_order
    assert sum(e.reason == "t_plus_one_locked" and e.status == "retry_pending" for e in result.events) == 1
    assert len(result.portfolio.fills) == 2 and result.portfolio.position_quantity(SEC).value == 0
    assert result.unfinished_exit_quantity == 0
    submitted = {event.order.order_id for event in result.events if event.status == "submitted"}
    assert all(event.order.order_id in submitted for event in result.events if event.fill)
    assert any(event.reason == "order_rebuilt_next_session" for event in result.events)


@pytest.mark.parametrize("minimum_shares,expected", [(0, 300), (100, 200)])
def test_all_position_exit_uses_actual_inventory_and_keeps_t1_and_reserve(minimum_shares, expected):
    policy = MinuteConditionalOrders([
        ConditionRule(kind="stop_loss", side="sell", gap=1,
                      sizing_mode="all_position", quantity=100),
    ], minimum_shares=minimum_shares)
    result = replay_grid(policy,
        [bar(9, 31, 10), bar(9, 32, 9), bar(9, 33, 9), bar(10, 31, 9)],
        PortfolioState(Money(D(1000000))), FEES,
        minimum_shares=minimum_shares,
        scheduled_orders=(ScheduledOrder("seed", date(2026, 9, 9), clock(9, 9), quantity=300),))
    sells = [f for f in result.portfolio.fills if f.side.value == "sell"]
    assert len(sells) == 1
    assert sells[0].quantity.value == expected
    assert sells[0].trading_date == date(2026, 9, 10)
    assert result.portfolio.position_quantity(SEC).value == minimum_shares
    assert any(e.reason == "t_plus_one_locked" for e in result.events)


def test_close_trigger_is_not_expired_before_its_next_session_effective_time():
    result = run([ConditionRule(kind="price", side="buy", direction="down", target_price=9)],
                 [bar(9, 31, 10), replace(bar(9, 32, 9), ended_at=clock(9, 15)), bar(10, 31, 8)])
    assert len(result.portfolio.fills) == 1
    assert result.portfolio.fills[0].trading_date == date(2026, 9, 10)
    assert result.portfolio.fills[0].price.amount == 8
    fill_event = next(e for e in result.events if e.fill)
    assert fill_event.signal_at == clock(9, 15)
    assert fill_event.effective_at == clock(10, 9, 30)


def test_lunch_break_preserves_signal_clock_and_afternoon_effective_clock():
    result = run([ConditionRule(kind="price", side="buy", direction="down", target_price=9)],
                 [replace(bar(9, 31, 9), ended_at=clock(9, 11, 30)),
                  replace(bar(9, 32, 9), ended_at=clock(9, 13, 1))])
    fill = next(e for e in result.events if e.fill)
    assert fill.signal_at == clock(9, 11, 30)
    assert fill.effective_at == clock(9, 13)
    assert fill.observed_at == clock(9, 13, 1)


def test_rebound_does_not_use_same_bar_new_low_before_an_earlier_high():
    ambiguous = replace(bar(9, 31, 10), prices=BarPrices(D(10), D("10.5"), D(8), D(8)))
    result = run([ConditionRule(kind="rebound", side="buy", gap=10)],
                 [ambiguous, bar(9, 32, "8.5"), bar(9, 33, 9), bar(9, 34, 9)])
    signals = [e for e in result.events if e.reason == "rebound"]
    assert len(signals) == 1 and signals[0].bar_index == 2
    assert len(result.portfolio.fills) == 1 and result.portfolio.fills[0].filled_at.minute == 34


def test_rebound_then_cost_profit_uses_fill_price_and_t1():
    # P25: tracked low 9 -> 2% rebound at 9.18, actual next-bar buy 9.20.
    # Profit barrier is 9.20 * 1.05 = 9.66, not 9.18 * 1.05.
    result = run([
        ConditionRule(kind="rebound", side="buy", gap=2, quantity=100),
        ConditionRule(kind="take_profit", side="sell", gap=5, quantity=100),
    ], [bar(9, 31, 10), bar(9, 32, 9), bar(9, 33, "9.18"),
        bar(9, 34, "9.20"), bar(9, 35, "9.65"), bar(9, 36, "9.66"),
        bar(9, 37, "9.66"), bar(10, 31, "9.70")], slippage_bps=D(0))
    assert [(f.side.value, f.quantity.value, f.price.amount, f.trading_date)
            for f in result.portfolio.fills] == [
        ("buy", 100, D("9.20"), date(2026, 9, 9)),
        ("sell", 100, D("9.70"), date(2026, 9, 10)),
    ]
    profits = [e for e in result.events if e.reason == "take_profit"]
    assert len(profits) == 1 and profits[0].bar_index == 5
    assert any(e.reason == "t_plus_one_locked" for e in result.events)
    assert result.portfolio.position_quantity(SEC).value == 0


@pytest.mark.parametrize("principal", [None, Money(D(800))])
def test_sourced_opening_holdings_are_not_counted_as_new_returns(principal):
    from ashare_lab.domain.portfolio import PositionLot
    from ashare_lab.domain.shared import FillId, Quantity
    from ashare_lab.application.minute_result import minute_result_bundle
    opening = PortfolioState(Money(D(0)), lots=(PositionLot(
        FillId("opening-import:lot-1"), SEC, clock(8, 10), date(2026, 9, 8),
        date(2026, 9, 9), Quantity(100), Money(D(800)), principal,
    ),))
    rules = [ConditionRule(kind="price", side="buy", direction="down", target_price=8)]
    bars = [bar(9, 31, 10), bar(9, 32, 10)]
    with pytest.raises(ValueError, match="sourced initial equity"):
        replay_grid(MinuteConditionalOrders(rules), bars, opening, FEES)
    result = replay_grid(MinuteConditionalOrders(rules), bars, opening, FEES,
                         initial_equity_cny=D(1000))
    report = minute_result_bundle(run_id="opening-equity", result=result,
                                  initial_cash=D(0), snapshot_id="fixture:opening")
    assert not result.portfolio.fills  # No fabricated buy to establish T+1.
    assert report.summary.initial_cash_cny == 0
    assert report.summary.initial_equity_cny == 1000
    assert report.summary.total_return == 0
    assert report.summary.benchmark_return == 0
    if principal is None:
        paired = replay_grid(MinuteConditionalOrders([
            ConditionRule(kind="price", side="sell", target_price=10),
            ConditionRule(kind="relative_price", side="buy", direction="down",
                          gap=1, gap_unit="cny"),
        ]), [bar(9, 31, 10), bar(9, 32, 10), bar(9, 33, 9), bar(9, 34, "9.1")],
            opening, FEES, initial_equity_cny=D(1000), slippage_bps=D(0))
        assert [(f.side.value, f.price.amount) for f in paired.portfolio.fills] == [
            ("sell", D(10)), ("buy", D("9.1")),
        ]
        assert paired.portfolio.position_quantity(SEC).value == 100
        assert paired.portfolio.sellable_quantity(SEC, date(2026, 9, 9)).value == 0


@pytest.mark.parametrize(("scope", "expected_cash", "expected_equity"), [
    ("total_equity", D(990000), D(1000000)),
    ("cash_plus_opening_holdings", D(1000000), D(1010000)),
])
def test_declared_opening_holding_is_sellable_without_a_fake_buy(scope, expected_cash, expected_equity):
    from ashare_lab.application.minute_conditional_orders import execute_minute_conditions
    from ashare_lab.application.minute_replay_input import PreparedMinuteReplay
    from ashare_lab.domain.execution import AshareExchange
    from ashare_lab.domain.strategy.price_plans import ConditionParameters
    prepared = PreparedMinuteReplay(
        (bar(9, 31, 10), bar(9, 32, 10)), (),
        (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10)),
    )
    params = ConditionParameters(
        observation="minute_bar",
        rules=[ConditionRule(kind="price", side="sell", target_price=10, quantity=100)],
        opening_shares=1000, min_shares=500, max_shares=10000,
        initial_cash_cny=1000000, initial_capital_scope=scope, slippage_bps=0,
    )
    result = execute_minute_conditions(params, prepared=prepared, exchange=AshareExchange.SHENZHEN)
    assert result.initial_equity_cny == expected_equity
    assert result.portfolio.fills[0].side.value == "sell"
    assert result.portfolio.fills[0].quantity.value == 100
    assert all(fill.order_id.value != "condition-initial-build" for fill in result.portfolio.fills)
    # Opening cash precedes the sale; the delta is independently recorded.
    assert result.equity[0].cash == expected_cash
    assert result.portfolio.position_quantity(SEC).value == 900


def test_new_build_and_existing_opening_inventory_are_not_conflated():
    from ashare_lab.domain.strategy.price_plans import ConditionParameters
    with pytest.raises(ValueError, match="不能同时设置"):
        ConditionParameters(rules=[ConditionRule(kind="price", side="buy", target_price=10)],
                            initial_shares=100, opening_shares=100)
        from ashare_lab.application.minute_grid_plan import MinuteGridCapabilityError
        with pytest.raises(MinuteGridCapabilityError, match="conditional_cost_provenance_missing"):
            replay_grid(MinuteConditionalOrders([
                ConditionRule(kind="take_profit", side="sell", gap=5),
            ]), bars, opening, FEES, initial_equity_cny=D(1000))


def test_start_purchase_fills_before_cost_exit_and_keeps_t_plus_one():
    from ashare_lab.application.minute_conditional_orders import execute_minute_conditions
    from ashare_lab.application.minute_replay_input import PreparedMinuteReplay
    from ashare_lab.domain.execution import AshareExchange
    from ashare_lab.domain.strategy.price_plans import ConditionParameters
    prepared = PreparedMinuteReplay(
        (bar(9, 31, 10), bar(9, 32, 11), bar(9, 33, 11),
         bar(10, 31, 11), bar(10, 32, 11)), (),
        tuple(date(2026, 9, day) for day in (8, 9, 10, 11)))
    params = ConditionParameters(observation="minute_bar", initial_shares=1000,
        initial_cash_cny=1000000, slippage_bps=0,
        rules=[ConditionRule(kind="take_profit", side="sell", gap=5,
            gap_unit="percent", sizing_mode="all_position")])
    result = execute_minute_conditions(params, prepared=prepared, exchange=AshareExchange.SHENZHEN)
    fills = result.portfolio.fills
    assert [(f.side.value, f.quantity.value, f.trading_date) for f in fills] == [
        ("buy", 1000, date(2026, 9, 9)), ("sell", 1000, date(2026, 9, 10))]
    assert fills[0].order_id.value == "condition-initial-build"
    assert result.portfolio.position_quantity(SEC).value == 0


def test_holding_stage_exits_only_due_batches_without_resetting_older_clock():
    sessions = tuple(date(2026, 9, d) for d in (9, 10, 11, 14, 15))
    policy = MinuteConditionalOrders([ConditionRule(kind="holding_period", side="sell", sessions=2, quantity=200)],
                                     market_sessions=sessions)
    result = replay_grid(policy, [bar(9, 31, 10), bar(10, 31, 10), bar(11, 31, 10), bar(14, 31, 10)],
                         PortfolioState(Money(D(1000000))), FEES,
                         scheduled_orders=tuple(ScheduledOrder(f"buy:{day}", date(2026, 9, day), clock(9, 9), quantity=100)
                                                for day in (9, 10)))
    sells = [f for f in result.portfolio.fills if f.side.value == "sell"]
    assert [(f.trading_date, f.quantity.value) for f in sells] == [(sessions[2], 100), (sessions[3], 100)]
    assert policy.cycles == 1 and result.portfolio.position_quantity(SEC).value == 0


@pytest.mark.parametrize("reserve", [0, 100])
def test_holding_all_uses_actual_batches_not_fixed_quantity(reserve):
    sessions = tuple(date(2026, 9, d) for d in (9, 10, 11, 14, 15))
    policy = MinuteConditionalOrders([ConditionRule(kind="holding_period", side="sell",
        sessions=2, sizing_mode="all_position", quantity=100)],
        market_sessions=sessions, minimum_shares=reserve)
    result = replay_grid(policy, [bar(9, 31, 10), bar(10, 31, 10), bar(11, 31, 10), bar(14, 31, 10)],
        PortfolioState(Money(D(1000000))), FEES, minimum_shares=reserve,
        scheduled_orders=(ScheduledOrder("old", sessions[0], clock(9, 9), quantity=100),
                          ScheduledOrder("young", sessions[1], clock(10, 9), quantity=200)))
    sells = [f for f in result.portfolio.fills if f.side.value == "sell"]
    assert [(f.trading_date, f.quantity.value) for f in sells] == [(sessions[2], 100), (sessions[3], 200-reserve)]
    assert policy.cycles == 1
    assert result.portfolio.position_quantity(SEC).value == reserve


def test_holding_failed_at_open_waits_next_session_not_next_minute():
    sessions = tuple(date(2026, 9, d) for d in (9, 10, 11))
    policy = MinuteConditionalOrders([ConditionRule(kind="holding_period", side="sell", sessions=1)],
                                     market_sessions=sessions)
    result = replay_grid(policy, [bar(9, 31, 10), bar(10, 31, 8), bar(10, 32, 9), bar(11, 31, 9)],
                         PortfolioState(Money(D(1000000))), FEES,
                         scheduled_orders=(ScheduledOrder("seed", sessions[0], clock(9, 9), quantity=100),))
    assert [(e.bar_index, e.status) for e in result.events if e.status in {"retry_pending", "filled"}] == [
        (0, "filled"), (1, "retry_pending"), (3, "filled")]
    assert result.portfolio.position_quantity(SEC).value == 0


@pytest.mark.parametrize("resume", [False, True])
def test_holding_calendar_clock_runs_without_suspended_minute_bars(resume):
    from ashare_lab.domain.market_data import DailyBar, TradingStatus
    from ashare_lab.domain.shared import Price, Quantity
    sessions = tuple(date(2026, 9, d) for d in (9, 10, 11))
    suspended = replace(bar(10, 31, 10).session, status=TradingStatus.SUSPENDED)
    daily = DailyBar(SEC, sessions[1], *(Price(D(10)) for _ in range(4)),
                     Quantity(0), D(0), clock(10, 15))
    policy = MinuteConditionalOrders([ConditionRule(kind="holding_period", side="sell", sessions=1)],
                                     market_sessions=sessions)
    bars = [bar(9, 31, 10)] + ([bar(11, 31, 10)] if resume else [])
    result = replay_grid(policy, bars, PortfolioState(Money(D(1000000))), FEES,
        scheduled_orders=(ScheduledOrder("seed", sessions[0], clock(9, 9), quantity=100),),
        nontrading_closes=((suspended, daily),))
    exits = [e for e in result.events if e.order.cell_id.startswith("condition:")]
    assert exits[0].reason == "holding_period"
    assert exits[0].signal_at == clock(10, 9, 30)
    assert any(e.reason == "security_not_trading" and e.status == "retry_pending" for e in exits)
    assert not any(f.trading_date == sessions[1] for f in result.portfolio.fills)
    assert any(p.observed_at == clock(10, 15) for p in result.equity)
    if resume:
        filled = next(e for e in exits if e.fill)
        assert filled.signal_at == clock(10, 9, 30)
        assert filled.effective_at == clock(11, 9, 30)
        assert result.portfolio.position_quantity(SEC).value == 0
        assert result.unfinished_holding_exit_quantity == 0
    else:
        assert exits[-1].observed_at == clock(10, 15)
        assert exits[-1].reason == "backtest_ended"
        assert result.unfinished_holding_exit_quantity == 100
        assert result.portfolio.position_quantity(SEC).value == 100
    from ashare_lab.application.minute_result import minute_result_bundle
    bundle = minute_result_bundle(run_id="suspension", result=result, initial_cash=D(1000000), snapshot_id="fixture")
    report = bundle.model_dump(mode="json", by_alias=True)
    calendar_events = [a for a in report["activities"] if a["timeQuality"] == "market_session_clock"]
    assert calendar_events and all(a["executionDetails"]["observedBar"] is None for a in calendar_events)
    assert report["summary"]["dataRange"]["end"] == str(sessions[2] if resume else sessions[1])


def test_holding_limit_survives_day_expiry_without_becoming_market():
    sessions = tuple(date(2026, 9, d) for d in (9, 10, 11))
    policy = MinuteConditionalOrders([ConditionRule(kind="holding_period", side="sell", sessions=1, limit_price=10)],
                                     market_sessions=sessions)
    result = replay_grid(policy, [bar(9, 31, 10), bar(10, 31, 9), bar(10, 32, 9), bar(11, 31, 9), bar(11, 32, 10)],
                         PortfolioState(Money(D(1000000))), FEES,
                         scheduled_orders=(ScheduledOrder("seed", sessions[0], clock(9, 9), quantity=100),))
    events = [e for e in result.events if e.order.cell_id.startswith("condition:")]
    assert all(e.order.limit_price == 10 for e in events)
    assert [e.bar_index for e in events if e.status == "filled"] == [4]
    assert events[0].order.order_id != next(e.order.order_id for e in events if e.status == "filled")


@pytest.mark.parametrize("old_shares", [100, 200])
@pytest.mark.parametrize("sizing_mode", ["shares", "all_position"])
def test_other_sell_cannot_redirect_holding_exit_to_younger_batch(old_shares, sizing_mode):
    from ashare_lab.domain.orders import OrderSide
    sessions = tuple(date(2026, 9, d) for d in (9, 10, 11, 14))
    policy = MinuteConditionalOrders([ConditionRule(kind="holding_period", side="sell", sessions=2, quantity=200,
                                                    sizing_mode=sizing_mode)],
                                     market_sessions=sessions)
    orders = (ScheduledOrder("old", sessions[0], clock(9, 9), quantity=old_shares),
              ScheduledOrder("young", sessions[1], clock(9, 9), quantity=100),
              ScheduledOrder("other-sell", sessions[2], clock(9, 9), side=OrderSide.SELL, quantity=100))
    result = replay_grid(policy, [bar(9, 31, 10), bar(10, 31, 10), bar(11, 31, 10)],
                         PortfolioState(Money(D(1000000))), FEES, scheduled_orders=orders)
    assert result.portfolio.position_quantity(SEC).value == 100
    assert all(lot.acquired_on == sessions[1] for lot in result.portfolio.lots)
    holding_fills = [e for e in result.events if e.fill and e.order.cell_id.startswith("condition:")]
    assert sum(e.fill.quantity.value for e in holding_fills) == old_shares - 100
    assert any(e.reason == "due_inventory_changed" and e.status == "cancelled" for e in result.events)
    assert result.unfinished_holding_exit_quantity == 0
