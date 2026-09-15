from decimal import Decimal as D

from ashare_lab.application.fixed_grid_orders import FixedGridOrders, GridCell
from ashare_lab.application.independent_order_routing import IndependentOrderRouting
from ashare_lab.domain.execution.bar_prices import BarPrices


def test_daily_clock_ignores_intraday_touch_and_uses_only_closing_price():
    from dataclasses import replace
    from ashare_lab.domain.portfolio import PortfolioState
    from ashare_lab.domain.shared import Money
    from tests.unit.application.test_minute_grid_replay import bar
    entry = FixedGridOrders([GridCell('a', D(9), D(10), 100)], active_sides=('buy',))
    router = IndependentOrderRouting(entry=entry, exit=FixedGridOrders([]), observations={'entry': 'daily_close'})
    account = PortfolioState(Money(D(10000)))
    source = bar(9, 31, 9)
    router.before_observe(account, source, 0)
    assert router.observe(source.prices, 0) == ()
    close = replace(source, ended_at=source.ended_at.replace(hour=15, minute=0),
                    prices=BarPrices(D(10), D(11), D(8), D(10)))
    router.before_observe(account, close, 1)
    assert router.observe(close.prices, 1) == ()
    close = replace(close, prices=BarPrices(D(10), D(11), D(8), D(9)))
    router.before_observe(account, close, 2)
    order, = router.observe(close.prices, 2)
    assert not router.can_match(order, bar(10, 32, 9), 3)
    assert router.can_match(order, bar(10, 31, 9), 3)
    opening = replace(bar(10, 31, 10), prices=BarPrices(D(10), D(12), D(8), D(9)))
    assert router.matching_prices(order, opening) == BarPrices(D(10), D(10), D(10), D(10))


def test_partial_exit_defers_other_leg_buy_without_losing_its_ticket():
    from types import SimpleNamespace
    from ashare_lab.domain.orders import OrderSide
    entry = FixedGridOrders([GridCell('same', D(9), D(10), 100)], active_sides=('buy',))
    exit = FixedGridOrders([GridCell('same', D(9), D(10), 100, first_side='sell')], active_sides=('sell',))
    router = IndependentOrderRouting(entry=entry, exit=exit)
    orders = router.observe(BarPrices(D(10), D(10), D(9), D(10)), 0)
    buy = next(order for order in orders if order.side == 'buy')
    assert router.can_match(buy, None, 1)
    router.after_account_fill(SimpleNamespace(side=OrderSide.SELL), before=200, after=160, index=1)
    assert not router.can_match(buy, None, 1)
    assert router.pending[buy.cell_id] == buy
    assert router.remaining[buy.cell_id] == 100
    assert router.can_match(buy, None, 2)


def test_two_trigger_policies_complete_on_one_replay_ledger():
    from ashare_lab.application.minute_grid_replay import replay_grid
    from ashare_lab.domain.portfolio import PortfolioState
    from ashare_lab.domain.shared import Money
    from tests.unit.application.test_minute_grid_replay import bar, FEES
    router = IndependentOrderRouting(
        entry=FixedGridOrders([GridCell('same', D(9), D(10), 100)], active_sides=('buy',)),
        exit=FixedGridOrders([GridCell('same', D(9), D(10), 100, first_side='sell')], active_sides=('sell',)))
    result = replay_grid(router, [bar(9, 31, 9), bar(9, 32, 9), bar(9, 33, 10),
        bar(10, 31, 10), bar(10, 32, 10), bar(10, 33, 9), bar(10, 34, 9),
        bar(11, 31, 10), bar(11, 32, 10)], PortfolioState(Money(D(100000))), FEES,
        slippage_bps=D(0))
    fills = result.portfolio.fills
    assert [(f.side.value, f.filled_at.day) for f in fills] == [
        ('buy', 9), ('sell', 10), ('buy', 10), ('sell', 11)]
    assert fills[0].order_id.value.startswith('entry:')
    assert fills[1].order_id.value.startswith('exit:')
    assert result.portfolio.cash.amount == 100000 + sum(
        (1 if f.side.value == 'sell' else -1) * f.price.amount * f.quantity.value
        - f.fees.total.amount for f in fills)


def test_identical_cell_names_and_partial_fills_do_not_cross_legs():
    entry = FixedGridOrders([GridCell('same', D(9), D(10), 100)], active_sides=('buy',))
    exit = FixedGridOrders([GridCell('same', D(9), D(10), 100, first_side='sell')], active_sides=('sell',))
    router = IndependentOrderRouting(entry=entry, exit=exit)
    orders = router.observe(BarPrices(D(10), D(10), D(9), D(10)), 0)
    assert [o.side for o in orders] == ['sell', 'buy']
    assert len({o.order_id for o in orders}) == 2
    router.on_fill(orders[0].order_id, 40, 1, price=D(10))
    assert router.remaining == {'entry:same': 100, 'exit:same': 60}
    router.on_fill(orders[1].order_id, 100, 1, price=D(9))
    assert list(router.pending) == ['exit:same']
    assert entry.sides['same'] == 'sell'
    assert exit.sides['same'] == 'sell'
    router.on_fill(orders[0].order_id, 60, 2, price=D(10))
    assert router.pending == {}
    assert exit.sides['same'] == 'buy'
