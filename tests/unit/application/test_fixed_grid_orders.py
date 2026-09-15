from decimal import Decimal as D

import pytest

from ashare_lab.application.fixed_grid_orders import FixedGridOrders, GridCell
from ashare_lab.domain.execution.bar_prices import BarPrices

BAR = BarPrices(D(10), D(12), D(8), D(10))


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_single_grid_leg_does_not_emit_an_unrequested_reverse_order(side):
    policy = FixedGridOrders([GridCell("buy-cell", D(9), D(11), 100),
                              GridCell("sell-cell", D(9), D(11), 100, "sell")],
                             active_sides=(side,))
    order, = policy.observe(BAR, 0)
    assert order.side == side
    policy.on_fill(order.order_id, 40, 1)
    policy.expire_day(2)
    remainder = policy.pending[order.cell_id]
    assert remainder.side == side and remainder.quantity == 60
    policy.on_fill(remainder.order_id, 60, 2)
    assert policy.observe(BAR, 3) == ()


def test_buy_only_grid_rearms_after_external_exit_without_own_sell():
    policy = FixedGridOrders([GridCell("a", D(9), D(11), 100)], active_sides=("buy",))
    order, = policy.observe(BAR, 0)
    policy.on_fill(order.order_id, 100, 1)
    assert policy.observe(BAR, 2) == ()
    policy.release_after_external_exit("a", 3)
    order, = policy.observe(BAR, 4)
    assert order.side == "buy" and order.effective_bar == 5


@pytest.mark.parametrize("grid_side,external_side,before,after", [
    ("buy", "sell", 100, 0), ("sell", "buy", 0, 100),
])
def test_external_cycle_transition_rearms_single_side_and_cancels_stale_orders(grid_side, external_side, before, after):
    policy = FixedGridOrders([GridCell("a", D(9), D(11), 100, grid_side)], active_sides=(grid_side,))
    old, = policy.observe(BAR, 0)
    assert policy.after_external_fill(side=external_side, before=before, after=after, index=1) == (old,)
    assert not policy.pending and not policy.remaining
    assert policy.observe(BAR, 1) == ()
    fresh, = policy.observe(BAR, 2)
    assert fresh.side == grid_side and fresh.order_id != old.order_id


def test_partial_external_exit_does_not_rearm_completed_grid_cell():
    policy = FixedGridOrders([GridCell("a", D(9), D(11), 100)], active_sides=("buy",))
    order, = policy.observe(BAR, 0)
    policy.on_fill(order.order_id, 100, 1)
    assert policy.after_external_fill(side="sell", before=100, after=50, index=2) == ()
    assert policy.observe(BAR, 3) == ()


def test_cells_not_netted_and_reverse_waits_for_next_bar():
    policy = FixedGridOrders([GridCell("a", D(9), D(11), 100),
                              GridCell("b", D(9), D(11), 100, "sell")])
    orders = policy.observe(BAR, 0)
    assert [o.side for o in orders] == ["buy", "sell"]
    with pytest.raises(ValueError):
        policy.on_fill(orders[0].order_id, 100, 0)
    policy.on_fill(orders[0].order_id, 100, 1)
    assert policy.observe(BAR, 1) == ()
    reverse = policy.observe(BAR, 2)
    assert len(reverse) == 1 and reverse[0].side == "sell" and reverse[0].effective_bar == 3


def test_partial_fill_expiry_and_exit_takeover():
    policy = FixedGridOrders([GridCell("a", D(9), D(11), 100)])
    order, = policy.observe(BAR, 0)
    policy.on_fill(order.order_id, 40, 1)
    assert policy.observe(BAR, 1) == ()
    policy.expire_day(2)
    replacement = policy.pending["a"]
    assert replacement.quantity == 60 and replacement.order_id != order.order_id
    assert policy.cancel_for_exit() == (replacement,)
    assert policy.observe(BAR, 2) == ()


def test_wait_mode_does_not_catch_up_already_crossed_startup_levels():
    policy = FixedGridOrders([GridCell("a", D(9), D(11), 100)], wait_for_crossing=True)
    assert policy.observe(BarPrices(D(8), D(8), D(8), D(8)), 0) == ()
    assert policy.observe(BarPrices(D(10), D(10), D(10), D(10)), 1) == ()
    order, = policy.observe(BarPrices(D(10), D(10), D(9), D(9)), 2)
    assert order.effective_bar == 3


def test_amount_order_quantity_is_fixed_at_signal_trigger_not_future_fill():
    policy = FixedGridOrders([GridCell("a", D(9), D(11), 100, amount_cny=D(2000))])
    order, = policy.observe(BAR, 0)
    assert order.quantity == 200
    assert policy.observe(BarPrices(D(8), D(8), D(8), D(8)), 1) == ()
    assert policy.pending["a"].quantity == 200
