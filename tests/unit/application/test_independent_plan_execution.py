from datetime import date

import pytest

from ashare_lab.domain.strategy.independent_plans import IndependentPlanPair
from ashare_lab.application.independent_plan_execution import execute_independent_plans
from ashare_lab.application.minute_replay_input import PreparedMinuteReplay
from ashare_lab.domain.execution.fees import AshareExchange
from tests.unit.application.test_minute_grid_replay import bar


@pytest.mark.parametrize('condition_side', ['buy', 'sell'])
def test_calendar_daily_condition_keeps_both_clocks(condition_side):
    from dataclasses import replace
    common = dict(initial_cash_cny=100000, slippage_bps=0)
    condition = dict(kind='conditional', parameters=dict(**common, observation='daily_close',
        rules=[dict(kind='price', side=condition_side, quantity=100, target_price=10,
                    direction='down' if condition_side == 'buy' else 'up')]))
    schedule = dict(kind='scheduled', parameters=dict(**common, frequency='weekly', at='close',
        day=5 if condition_side == 'buy' else 3, side='sell' if condition_side == 'buy' else 'buy',
        sizing_mode='shares', quantity=100))
    pair = IndependentPlanPair(entry_plan=condition if condition_side == 'buy' else schedule,
                               exit_plan=schedule if condition_side == 'buy' else condition)
    days = tuple(date(2026, 9, d) for d in (9, 10, 11))
    bars = tuple(b for day in days for b in (bar(day.day, 31, 10),
        replace(bar(day.day, 31, 10), ended_at=bar(day.day, 31, 10).ended_at.replace(hour=15, minute=0))))
    result = execute_independent_plans(pair, prepared=PreparedMinuteReplay(bars, (), days),
        exchange=AshareExchange.SHENZHEN, start=days[0], end=days[-1])
    fills = result.portfolio.fills
    assert [f.side.value for f in fills] == ['buy', 'sell']
    scheduled_fill = next(f for f in fills if f.side.value != condition_side)
    assert scheduled_fill.filled_at.hour == 15
    condition_fill = next(f for f in fills if f.side.value == condition_side)
    assert condition_fill.filled_at.hour == 9


@pytest.mark.parametrize('boundary_fault', [None, 'missing_close', 'missing_open', 'duplicate_close'])
def test_daily_condition_pair_uses_close_then_next_open_and_requires_both(boundary_fault):
    from dataclasses import replace
    common = dict(initial_cash_cny=100000, slippage_bps=0, observation='daily_close')
    pair = IndependentPlanPair(
        entry_plan=dict(kind='conditional', parameters=dict(**common, rules=[dict(
            kind='price', side='buy', direction='down', target_price=9, quantity=100)])),
        exit_plan=dict(kind='conditional', parameters=dict(**common, rules=[dict(
            kind='price', side='sell', direction='up', target_price=11, quantity=100)])))
    days = tuple(date(2026, 9, d) for d in (9, 10, 11))
    bars = []
    for day in days:
        opening = bar(day.day, 31, 10)
        if boundary_fault != 'missing_open' or day.day != 10:
            bars.append(opening)
        if boundary_fault != 'missing_close' or day.day != 10:
            bars.append(replace(opening, ended_at=opening.ended_at.replace(hour=15, minute=0),
                prices=bar(day.day, 31, 9 if day.day == 9 else 11).prices))
            if boundary_fault == 'duplicate_close' and day.day == 10:
                bars.append(bars[-1])
    kwargs = dict(prepared=PreparedMinuteReplay(tuple(bars), (), days), exchange=AshareExchange.SHENZHEN,
                  start=days[0], end=days[-1])
    if boundary_fault:
        expected = ('minute bars must be strictly ordered' if boundary_fault == 'duplicate_close'
                    else 'independent_daily_open_or_close_missing')
        with pytest.raises(ValueError, match=expected):
            execute_independent_plans(pair, **kwargs)
    else:
        result = execute_independent_plans(pair, **kwargs)
        assert [(f.side.value, f.filled_at.day, f.price.amount) for f in result.portfolio.fills] == [
            ('buy', 10, 10), ('sell', 11, 10)]


@pytest.mark.parametrize('entry_kind', ['grid', 'conditional'])
@pytest.mark.parametrize('exit_kind', ['grid', 'conditional'])
def test_trigger_pairs_execute_both_legs_without_a_second_account(entry_kind, exit_kind):
    def plan(kind, side):
        p = dict(initial_cash_cny=100000, slippage_bps=0, observation='minute_bar')
        if kind == 'grid':
            p.update(anchor_mode='first_open', lower_price=8, upper_price=12,
                     spacing=1, price_mode='grid_limit', order_shares=100)
        else:
            p.update(rules=[dict(kind='price', side=side, quantity=100,
                target_price=9 if side == 'buy' else 11,
                direction='down' if side == 'buy' else 'up')], repeat_cycles=2)
        return dict(kind=kind, parameters=p)
    pair = IndependentPlanPair(entry_plan=plan(entry_kind, 'buy'), exit_plan=plan(exit_kind, 'sell'))
    days = tuple(date(2026, 9, d) for d in (9, 10, 11))
    bars = tuple(bar(day.day, minute, price) for day in days for minute, price in
        ((31, 10), (32, 9), (33, 9), (34, 11), (35, 11)))
    result = execute_independent_plans(pair, prepared=PreparedMinuteReplay(bars, (), days),
        exchange=AshareExchange.SHENZHEN, start=days[0], end=days[-1])
    fills = result.portfolio.fills
    assert {f.side.value for f in fills} == {'buy', 'sell'}
    assert all(f.order_id.value.startswith('entry:' if f.side.value == 'buy' else 'exit:') for f in fills)
    assert result.portfolio.cash.amount == 100000 + sum(
        (1 if f.side.value == 'sell' else -1) * f.price.amount * f.quantity.value
        - f.fees.total.amount for f in fills)


@pytest.mark.parametrize('grid_side', ['buy', 'sell'])
@pytest.mark.parametrize('anchor_update', ['fixed', 'last_fill'])
def test_daily_grid_combines_with_daily_condition(grid_side, anchor_update):
    from dataclasses import replace
    common = dict(initial_cash_cny=100000, slippage_bps=0, observation='daily_close')
    grid = dict(kind='grid', parameters=dict(**common, anchor_mode='first_open', anchor_update=anchor_update,
        lower_price=8, upper_price=12, spacing=1, order_shares=100, price_mode='next_open'))
    condition = dict(kind='conditional', parameters=dict(**common, rules=[dict(kind='price',
        side='sell' if grid_side == 'buy' else 'buy', quantity=100,
        direction='up' if grid_side == 'buy' else 'down', target_price=11 if grid_side == 'buy' else 9)]))
    pair = IndependentPlanPair(entry_plan=grid if grid_side == 'buy' else condition,
                               exit_plan=condition if grid_side == 'buy' else grid)
    days = tuple(date(2026, 9, d) for d in (9, 10, 11))
    bars = tuple(b for day, close in zip(days, (9, 11, 10)) for b in (
        bar(day.day, 31, 10), replace(bar(day.day, 31, close),
            ended_at=bar(day.day, 31, close).ended_at.replace(hour=15, minute=0))))
    result = execute_independent_plans(pair, prepared=PreparedMinuteReplay(bars, (), days),
        exchange=AshareExchange.SHENZHEN, start=days[0], end=days[-1])
    assert [(f.side.value, f.filled_at.day) for f in result.portfolio.fills] == [('buy', 10), ('sell', 11)]
    if grid_side == 'buy':
        from ashare_lab.application.minute_grid_plan import execute_minute_grid
        direct = execute_minute_grid(pair.entry_plan.parameters,
            prepared=PreparedMinuteReplay(bars, (), days), exchange=AshareExchange.SHENZHEN,
            active_sides=('buy',))
        assert [(f.side.value, f.filled_at.day) for f in direct.portfolio.fills] == [('buy', 10)]


@pytest.mark.parametrize('condition_side', ['buy', 'sell'])
@pytest.mark.parametrize('stateful', [False, True])
def test_schedule_and_price_condition_preserve_independent_order_origins(condition_side, stateful):
    common = dict(initial_cash_cny=100000, slippage_bps=0)
    rule = (dict(kind='rebound' if condition_side == 'buy' else 'pullback',
                 gap=5, gap_unit='percent') if stateful else
            dict(kind='price', target_price=10, direction='down' if condition_side == 'buy' else 'up'))
    condition = dict(kind='conditional', parameters=dict(**common, observation='minute_bar',
        rules=[dict(**rule, side=condition_side, quantity=100)], repeat_cycles=1))
    schedule = dict(kind='scheduled', parameters=dict(**common, frequency='weekly',
        day=4 if condition_side == 'buy' else 3, sizing_mode='shares', quantity=100,
        side='sell' if condition_side == 'buy' else 'buy'))
    pair = IndependentPlanPair(entry_plan=condition if condition_side == 'buy' else schedule,
                               exit_plan=schedule if condition_side == 'buy' else condition)
    days = tuple(date(2026, 9, d) for d in (9, 10, 11, 14, 15, 16, 17))
    bars = tuple(bar(day.day, minute, price) for day in days
                 for minute, price in ((31, 10), (32, 9 if stateful else 10), (33, 10), (34, 10)))
    result = execute_independent_plans(pair, prepared=PreparedMinuteReplay(bars, (), days),
        exchange=AshareExchange.SHENZHEN, start=days[0], end=days[-1])
    fills = result.portfolio.fills
    expected = [('buy', 100), ('sell', 100)] + ([('buy', 100)] if condition_side == 'sell' else [])
    assert [(f.side.value, f.quantity.value) for f in fills] == expected
    assert fills[1].filled_at.date() > fills[0].filled_at.date()
    assert sum(f.side.value == condition_side for f in fills) == 1  # preserve repeat_cycles=1
    assert all(f.order_id.value.startswith('scheduled:') for f in fills if f.side.value != condition_side)


@pytest.mark.parametrize('exit_day', [3, 4])
def test_two_calendars_keep_orders_distinct_and_preserve_exit_priority(exit_day):
    common = dict(initial_cash_cny=100000, slippage_bps=0, frequency='weekly', day=3,
                  sizing_mode='shares', quantity=100)
    pair = IndependentPlanPair(
        entry_plan=dict(kind='scheduled', parameters=dict(**common, side='buy')),
        exit_plan=dict(kind='scheduled', parameters={**common, 'side': 'sell', 'day': exit_day}))
    days = tuple(date(2026, 9, d) for d in (9, 10, 11, 14, 15, 16, 17))
    prepared = PreparedMinuteReplay(tuple(bar(day.day, 31, 10) for day in days), (), days)
    result = execute_independent_plans(pair, prepared=prepared, exchange=AshareExchange.SHENZHEN,
                                       start=days[0], end=days[-1])
    fills = result.portfolio.fills
    expected = [('buy', 9), ('sell', 16)] if exit_day == 3 else [
        ('buy', 9), ('sell', 10), ('buy', 16), ('sell', 17)]
    assert [(f.side.value, f.filled_at.day) for f in fills] == expected
    assert len({f.order_id for f in fills}) == len(expected)
    if exit_day == 3:
        assert any(e.order.order_id == 'scheduled:2026-09-16' and e.reason == 'exit_takeover'
                   for e in result.events)
    assert result.portfolio.cash.amount == 100000 + sum(
        (1 if f.side.value == 'sell' else -1) * f.price.amount * f.quantity.value
        - f.fees.total.amount for f in fills)


@pytest.mark.parametrize("grid_side", ["buy", "sell"])
@pytest.mark.parametrize('observation', ['minute_bar', 'daily_close'])
def test_calendar_grid_pair_executes_both_directions_in_one_account(grid_side, observation):
    from dataclasses import replace
    common = dict(initial_cash_cny=100000, slippage_bps=0)
    grid = dict(kind="grid", parameters=dict(**common, anchor_mode="first_open",
        observation=observation, lower_price=8, upper_price=12, spacing=1,
        price_mode="grid_limit", order_shares=100))
    scheduled = dict(kind="scheduled", parameters=dict(**common, frequency="weekly",
        day=4 if grid_side == "buy" else 3, side="sell" if grid_side == "buy" else "buy",
        sizing_mode="shares", quantity=100))
    pair = IndependentPlanPair(entry_plan=grid if grid_side == "buy" else scheduled,
                               exit_plan=scheduled if grid_side == "buy" else grid)
    days = tuple(date(2026, 9, d) for d in (9, 10, 11, 14, 15, 16, 17, 18, 21, 22, 23, 24))
    bars = tuple(b for day in days for b in (
        bar(day.day, 31, 10), bar(day.day, 32, 9 if grid_side == "buy" else 11),
        bar(day.day, 33, 9 if grid_side == "buy" else 11),
        replace(bar(day.day, 33, 9 if grid_side == 'buy' else 11),
                ended_at=bar(day.day, 33, 10).ended_at.replace(hour=15, minute=0))))
    result = execute_independent_plans(pair, prepared=PreparedMinuteReplay(bars, (), days),
        exchange=AshareExchange.SHENZHEN, start=days[0], end=days[-1])
    fills = result.portfolio.fills
    assert sum(f.side.value == 'sell' for f in fills) >= 2
    assert all(f.order_id.value.startswith('scheduled:')
               for f in fills if f.side.value == ('sell' if grid_side == 'buy' else 'buy'))
    expected_cash = 100000 + sum(
        (1 if f.side.value == 'sell' else -1) * f.price.amount * f.quantity.value
        - f.fees.total.amount for f in fills)
    assert result.portfolio.cash.amount == expected_cash
    if observation == 'daily_close':
        grid_fills = [f for f in fills if f.side.value == grid_side]
        assert all(f.filled_at.minute == 32 for f in grid_fills)
        assert all(f.filled_at.day > 9 for f in grid_fills)
