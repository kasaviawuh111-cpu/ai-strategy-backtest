from decimal import Decimal as D

import pytest

from ashare_lab.application.fixed_grid_orders import FixedGridOrders
from ashare_lab.application.minute_grid_plan import LazyFixedGridOrders, compile_minute_grid
from ashare_lab.application.minute_grid_replay import replay_grid
from ashare_lab.domain.portfolio import PortfolioState
from ashare_lab.domain.shared import Money
from ashare_lab.domain.strategy.price_plans import GridParameters
from tests.unit.application.test_minute_grid_replay import bar, FEES


@pytest.mark.parametrize('grid_side', ['buy', 'sell'])
@pytest.mark.parametrize('plan_kind', ['grid', 'conditional'])
def test_daily_grid_adapter_executes_independent_signal_on_opposite_side(grid_side, plan_kind):
    from dataclasses import replace
    from datetime import date
    from ashare_lab.application.minute_grid_plan import execute_minute_grid
    from ashare_lab.application.minute_replay_input import PreparedMinuteReplay
    from ashare_lab.domain.execution.fees import AshareExchange
    from tests.unit.application.test_minute_grid_replay import daily_intent
    params = GridParameters(anchor_price=10, lower_price=8, upper_price=12,
        observation='daily_close', spacing=1, initial_cash_cny=1010,
        startup_mode='catch_up', price_mode='next_open', slippage_bps=0)
    execute = execute_minute_grid
    options = {'active_sides': (grid_side,)}
    if plan_kind == 'conditional':
        from ashare_lab.domain.strategy.price_plans import ConditionParameters
        from ashare_lab.application.minute_conditional_orders import execute_minute_conditions
        params = ConditionParameters(observation='daily_close', initial_cash_cny=1010, slippage_bps=0,
            rules=[dict(kind='price', side=grid_side, target_price=9 if grid_side == 'buy' else 11,
                        direction='down' if grid_side == 'buy' else 'up', quantity=100)])
        execute, options = execute_minute_conditions, {}
    days = tuple(date(2026, 9, d) for d in (9, 10, 11))
    bars = tuple(item for day in days for item in (bar(day.day, 31, 10),
        replace(bar(day.day, 31, 9 if grid_side == 'buy' else 11),
                ended_at=bar(day.day, 31, 10).ended_at.replace(hour=15, minute=0))))
    intent = daily_intent(10, 11, exit=True) if grid_side == 'buy' else daily_intent(8, 9, enter=True)
    result = execute(params, prepared=PreparedMinuteReplay(bars, (), (date(2026, 9, 8), *days)),
        exchange=AshareExchange.SHENZHEN, daily_signals=(intent,), **options)
    assert [(f.side.value, f.filled_at.day) for f in result.portfolio.fills] == (
        [('buy', 10), ('sell', 11)] if grid_side == 'buy' else [('buy', 9), ('sell', 10)])
    assert result.closed_position_cycles == 1


def test_scheduled_buys_and_grid_sells_share_inventory_across_cycles():
    from datetime import date, datetime
    from zoneinfo import ZoneInfo
    from ashare_lab.application.scheduled_execution import ScheduledOrder
    params = GridParameters(anchor_price=10, lower_price=8, upper_price=12,
        spacing=1, startup_mode="catch_up", price_mode="grid_limit")
    policy = LazyFixedGridOrders(params, first_open=D(10), active_sides=("sell",))
    known = datetime(2026, 9, 8, tzinfo=ZoneInfo('Asia/Shanghai'))
    result = replay_grid(policy, [bar(9, 31, 10), bar(9, 32, 11),
        bar(10, 31, 11), bar(10, 32, 11), bar(11, 31, 10),
        bar(14, 31, 11), bar(14, 32, 11)], PortfolioState(Money(D(100000))), FEES,
        slippage_bps=D(0), grid_composition=True,
        scheduled_orders=tuple(ScheduledOrder(f'calendar:{day}', date(2026, 9, day),
            known, quantity=100) for day in (9, 11)))
    assert [(f.side.value, f.filled_at.day, f.quantity.value) for f in result.portfolio.fills] == [
        ('buy', 9, 100), ('sell', 10, 100), ('buy', 11, 100), ('sell', 14, 100)]


def test_grid_buy_with_independent_protection_rearms_after_actual_flat():
    from ashare_lab.application.minute_grid_replay import Protection
    params = GridParameters(anchor_price=10, lower_price=8, upper_price=12,
        spacing=1, startup_mode="catch_up", price_mode="grid_limit")
    policy = LazyFixedGridOrders(params, first_open=D(10), active_sides=("buy",))
    result = replay_grid(policy, [bar(9, 31, 9), bar(9, 32, 9), bar(9, 33, 10),
        bar(9, 34, 10), bar(10, 31, 10), bar(10, 32, 9), bar(10, 33, 9)],
        PortfolioState(Money(D(100000))), FEES, slippage_bps=D(0),
        grid_composition=True, protection=Protection(take_profit=D('.05')))
    fills = [e.fill for e in result.events if e.fill]
    assert [(f.side.value, f.quantity.value) for f in fills] == [('buy', 100), ('sell', 100), ('buy', 100)]
    assert fills[1].filled_at.date() > fills[0].filled_at.date()


@pytest.mark.parametrize("anchor", ["first_entry_fill", "each_entry_fill"])
def test_grid_buy_holding_exit_keeps_market_clock_and_next_cycle(anchor):
    from datetime import date
    params = GridParameters(anchor_price=10, lower_price=8, upper_price=12,
        spacing=1, startup_mode="catch_up", price_mode="grid_limit")
    policy = LazyFixedGridOrders(params, first_open=D(10), active_sides=("buy",))
    result = replay_grid(policy, [bar(9, 31, 9), bar(9, 32, 9),
        bar(10, 31, 10), bar(10, 32, 9), bar(10, 33, 9), bar(11, 31, 10)],
        PortfolioState(Money(D(100000))), FEES, slippage_bps=D(0),
        grid_composition=True, holding_sessions=1, holding_anchor=anchor,
        market_sessions=tuple(date(2026, 9, d) for d in (9, 10, 11)))
    fills = result.portfolio.fills
    assert [(f.side.value, f.filled_at.day, f.quantity.value) for f in fills] == [
        ('buy', 9, 100), ('sell', 10, 100), ('buy', 10, 100), ('sell', 11, 100)]
    assert all(f.order_id.value.startswith('holding:') for f in fills if f.side.value == 'sell')


def test_fixed_grid_buy_leg_with_daily_exit_closes_and_restarts_real_cycle():
    from tests.unit.application.test_minute_grid_replay import daily_intent
    from datetime import date
    params = GridParameters(anchor_price=10, lower_price=8, upper_price=12,
        spacing=1, startup_mode="catch_up", price_mode="grid_limit")
    policy = LazyFixedGridOrders(params, first_open=D(10), active_sides=("buy",))
    result = replay_grid(policy, [bar(9, 31, 9), bar(9, 32, 9), bar(10, 31, 10),
        bar(10, 32, 9), bar(10, 33, 9)], PortfolioState(Money(D(100000))), FEES,
        slippage_bps=D(0), daily_signals=(daily_intent(9, 10, exit=True),),
        market_sessions=(date(2026, 9, 9), date(2026, 9, 10)), grid_composition=True)
    assert [f.side.value for f in result.portfolio.fills] == ["buy", "sell", "buy"]
    assert result.closed_position_cycles == 1
    assert all(e.order.side == "buy" or e.order.cell_id == "daily_signal" for e in result.events)


@pytest.mark.parametrize("dynamic", [False, True])
def test_daily_entry_with_grid_sell_leg_obeys_t1_and_can_start_another_cycle(dynamic):
    from tests.unit.application.test_minute_grid_replay import daily_intent
    from ashare_lab.application.trigger_grid_orders import TriggerGridOrders
    from datetime import date
    params = GridParameters(anchor_price=10, lower_price=8, upper_price=12,
        spacing=1, startup_mode="catch_up", price_mode="grid_limit",
        anchor_update="last_trigger" if dynamic else "fixed")
    policy = (TriggerGridOrders(params, FEES, active_sides=("sell",)) if dynamic else
              LazyFixedGridOrders(params, first_open=D(10), active_sides=("sell",)))
    result = replay_grid(policy, [bar(9, 31, 10), bar(9, 32, 11), bar(9, 33, 11),
        bar(10, 31, 11), bar(10, 32, 11), bar(11, 31, 10)],
        PortfolioState(Money(D(1010))), FEES, slippage_bps=D(0),
        daily_signals=(daily_intent(8, 9, enter=True), daily_intent(10, 11, enter=True)),
        market_sessions=tuple(date(2026, 9, day) for day in (8, 9, 10, 11)), grid_composition=True)
    assert not any(f.side.value == "sell" and f.filled_at.date() == date(2026, 9, 9)
                   for f in result.portfolio.fills)
    assert result.portfolio.fills[0].side.value == "buy"
    assert [f.side.value for f in result.portfolio.fills] == ["buy", "sell", "buy"]


@pytest.mark.parametrize("mode", ["cny", "anchor_percent", "percent"])
@pytest.mark.parametrize("startup", ["catch_up", "wait_for_crossing"])
@pytest.mark.parametrize("active_sides", [("buy", "sell"), ("buy",), ("sell",)])
def test_lazy_lattice_preserves_eager_events_and_ledger(mode, startup, active_sides):
    params = GridParameters(anchor_price=10, lower_price=5, upper_price=15,
        spacing_mode=mode, spacing=D("0.5") if mode == "cny" else D(5),
        startup_mode=startup, price_mode="grid_limit")
    eager = FixedGridOrders(list(compile_minute_grid(params, first_open=D(10)).cells),
                           wait_for_crossing=startup == "wait_for_crossing", active_sides=active_sides)
    lazy = LazyFixedGridOrders(params, first_open=D(10), active_sides=active_sides)
    prices = [9, 9, 10, 11, 10, 8.5, 8.5, 10, 11, 11, 9, 9]
    bars = [bar(9 + i // 4, 31 + i % 4, str(price)) for i, price in enumerate(prices)]
    results = [replay_grid(policy, bars, PortfolioState(Money(D(1000000))), FEES)
               for policy in (eager, lazy)]
    assert results[0].events == results[1].events
    assert results[0].portfolio == results[1].portfolio
    assert results[0].equity == results[1].equity
    assert len(lazy.cells) < len(eager.cells)


def test_large_default_bounds_do_not_materialize_unobserved_levels():
    params = GridParameters(anchor_price=D("79.32"), lower_price=D("0.01"),
        upper_price=1000000, buy_spacing=3, sell_spacing=1,
        buy_spacing_mode="anchor_percent", sell_spacing_mode="anchor_percent")
    policy = LazyFixedGridOrders(params, first_open=D("79.32"))
    assert policy.parameters == params
    assert policy.cells == {}
    from ashare_lab.domain.execution.bar_prices import BarPrices
    p = D("78")
    assert policy.observe(BarPrices(p, p, p, p), 0) == ()
    assert policy.cells == {}
    p = D("76")
    orders = policy.observe(BarPrices(p, p, p, p), 1)
    assert [order.cell_id for order in orders] == ["buy:1"]
    assert len(policy.cells) == 1


def test_lazy_unseen_cells_receive_each_historical_rebase_before_activation():
    from datetime import date
    from ashare_lab.application.minute_price_rebase import MinutePriceRebase
    from ashare_lab.domain.execution.bar_prices import BarPrices
    from tests.unit.application.test_minute_corporate_actions import clock
    from tests.unit.application.test_minute_grid_replay import SEC
    params = GridParameters(anchor_price=10, lower_price=5, upper_price=15,
                            spacing=D("0.5"), price_mode="grid_limit")
    eager = FixedGridOrders(list(compile_minute_grid(params, first_open=D(10)).cells))
    lazy = LazyFixedGridOrders(params, first_open=D(10))
    for index, price in enumerate(["10", "9.5", "7", "6", "10"]):
        if index in (2, 3):
            rebase = MinutePriceRebase(SEC, date(2026, 9, 10), D(".93"), clock(10, 9), "a" * 64)
            eager.rebase_prices(rebase)
            lazy.rebase_prices(rebase)
        p = D(price)
        observed = BarPrices(p, p, p, p)
        assert eager.observe(observed, index) == lazy.observe(observed, index)
        assert eager.pending == lazy.pending
