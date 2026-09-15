from dataclasses import replace
from decimal import Decimal as D

from ashare_lab.application.trigger_grid_orders import TriggerGridOrders
from ashare_lab.application.minute_grid_replay import replay_grid
from ashare_lab.application.minute_result import minute_result_bundle
from ashare_lab.domain.execution import CapacityMode
from ashare_lab.domain.execution.bar_prices import BarPrices
from ashare_lab.domain.portfolio import PortfolioState
from ashare_lab.domain.shared import Money
from ashare_lab.domain.shared import Price
from ashare_lab.domain.strategy.price_plans import GridParameters
from tests.unit.application.test_minute_grid_replay import bar, FEES, SEC


def policy(**kw):
    return TriggerGridOrders(GridParameters(**dict(
        dict(anchor_price=10, lower_price=5, upper_price=15, anchor_update="last_trigger",
             price_mode="grid_limit", slippage_bps=0), **kw)), FEES)


def run(p, bars, cash=1000000, **kw):
    return replay_grid(p, bars, PortfolioState(Money(D(cash))), FEES,
                       slippage_bps=D(0), **kw)


def test_buy_only_trigger_grid_ignores_sell_threshold_without_sleep_or_anchor_change():
    p = TriggerGridOrders(policy().parameters, FEES, active_sides=("buy",))
    r = run(p, [bar(9, 31, 11), bar(9, 32, 9), bar(9, 33, 9), bar(10, 31, 11)])
    assert p.anchor == 9
    assert "sell" not in p.sleeping
    assert r.portfolio.fills and all(f.side.value == "buy" for f in r.portfolio.fills)
    assert all(e.order.side == "buy" for e in r.events)


def test_sell_only_trigger_grid_keeps_explicit_initial_build_but_never_rebuys():
    p = TriggerGridOrders(policy(initial_shares=100).parameters, FEES, active_sides=("sell",))
    r = run(p, [bar(9, 31, 10), bar(10, 31, 11), bar(10, 32, 11), bar(11, 31, 9)])
    assert p.anchor == 11
    assert [f.side.value for f in r.portfolio.fills] == ["buy", "sell"]
    assert all(e.order.side == "sell" or e.order.order_id == "grid-initial-build" for e in r.events)


def test_trigger_moves_base_before_fill_and_unfilled_ticket_is_not_rebuilt():
    p = policy()
    r = run(p, [bar(9, 31, 9), bar(9, 32, D("9.5")),
                bar(10, 31, D("9.5")), bar(10, 32, D("9.5"))])
    assert p.anchor == 9
    assert not r.portfolio.fills
    assert len([e for e in r.events if e.status == "submitted"]) == 1
    assert not any(e.reason == "order_rebuilt_next_session" for e in r.events)
    assert not p.pending and not p.remaining


def test_empty_sell_sleeps_without_fake_orders_and_buy_side_remains_active():
    p = policy()
    r = run(p, [bar(9, 31, 11), bar(9, 32, 11), bar(9, 33, 9), bar(9, 34, 9)])
    assert [e.order.side for e in r.events if e.status == "submitted"] == ["buy"]
    assert r.portfolio.position_quantity(SEC).value == 100
    assert [e["reason"] for e in r.monitoring_events if e["state"] == "sleeping" and e["side"] == "sell"] == [
        "insufficient_position"]
    assert p.anchor == 9  # Neither the blocked sell nor the fill resets it.


def test_partial_quantity_survives_then_expires_without_next_day_top_up():
    p = policy(order_shares=300)
    bars = [replace(bar(9, 31, 9), volume_shares=2000), bar(9, 32, 9),
            bar(10, 31, D("9.5")), bar(10, 32, D("9.5"))]
    r = run(p, bars, capacity_mode=CapacityMode.POINT_IN_TIME_VOLUME)
    assert [(f.quantity.value, f.price.amount) for f in r.portfolio.fills] == [(100, D(9))]
    assert len([e for e in r.events if e.status == "submitted"]) == 1
    assert p.anchor == 9 and not p.remaining
    report = minute_result_bundle(run_id="partial-grid", result=r, initial_cash=D(1000000), snapshot_id="fixture")
    assert any(a.kind == "partial_fill" for a in report.activities)
    assert "eastmoney-trigger-grid.research.v1" in report.audit.price_plan_ledger


def test_new_trigger_does_not_add_old_remainder_and_reserves_pending_cash():
    p = policy(buy_limit=7, sell_limit=12, price_mode="fixed_limit")
    r = run(p, [bar(9, 31, 9), bar(9, 32, 8), bar(9, 33, 8)], cash=1000)
    # The first 100-share order reserves ~705. No second 100-share order is affordable.
    assert len([e for e in r.events if e.status == "submitted"]) == 1
    assert any(e.get("reason") == "insufficient_cash_including_fees" for e in r.monitoring_events)
    p = policy(buy_limit=7, sell_limit=12, price_mode="fixed_limit")
    r = run(p, [bar(9, 31, 9), bar(9, 32, 8), bar(9, 33, 8)])
    assert [e.order.quantity for e in r.events if e.status == "submitted"] == [100, 100]
    assert p.anchor == 8


def test_t_plus_one_sleep_does_not_resume_when_shares_become_sellable_next_day():
    p = policy()
    r = run(p, [bar(9, 31, 9), bar(9, 32, 9), bar(9, 33, 10),
                bar(10, 31, 10), bar(10, 32, 10)])
    assert [(f.side.value, f.quantity.value) for f in r.portfolio.fills] == [("buy", 100)]
    assert any(e.get("reason") == "t_plus_one_locked" for e in r.monitoring_events)
    assert not any(e["state"] == "monitoring" for e in r.monitoring_events)
    assert r.portfolio.sellable_quantity(SEC, bar(10, 32, 10).session.session_date).value == 100


def test_empty_inventory_does_not_sleep_before_a_sell_condition_is_touched():
    p = policy()
    r = run(p, [bar(9, 31, 10), bar(9, 32, 10)])
    assert not p.sleeping and not r.monitoring_events and not r.events


def test_no_initial_purchase_and_no_repeated_sleep_orders():
    p = policy()
    r = run(p, [bar(9, i, 11) for i in range(31, 60)])
    assert not r.events and not r.portfolio.fills
    assert len(r.monitoring_events) == 1
    report = minute_result_bundle(run_id="sleep", result=r, initial_cash=D(1000000), snapshot_id="fixture")
    assert "休眠" in report.summary.execution_note
    assert not report.activities


def test_gap_uses_grid_threshold_not_quote_or_fill_price():
    p = policy()
    r = run(p, [bar(9, 31, D("8.8")), bar(9, 32, D("8.8"))])
    assert p.anchor == D(9)
    assert r.portfolio.fills[0].price.amount == D("8.8")
    assert r.events[0].order.limit_price == D(9)
    assert p.serial == 1


def test_temporary_matching_restriction_keeps_old_day_order_alive():
    p = policy()
    restricted = bar(9, 32, 9)
    restricted = replace(restricted, session=replace(restricted.session, upper_limit=Price(D(9))))
    r = run(p, [bar(9, 31, 9), restricted, bar(9, 33, D("8.9"))])
    assert len([e for e in r.events if e.status == "submitted"]) == 1
    assert any(e.status == "retry_pending" and e.reason == "buy_at_upper_limit" for e in r.events)
    assert r.portfolio.fills[0].price.amount == D("8.9")
    assert not p.sleeping


def test_explicit_build_is_one_day_order_with_capacity_remainder_and_real_fees():
    p = policy(initial_shares=300)
    bars = [replace(bar(9, i, 10), volume_shares=2000) for i in range(31, 35)]
    r = run(p, bars, capacity_mode=CapacityMode.POINT_IN_TIME_VOLUME)
    assert [e.order.order_id for e in r.events if e.status == "submitted"] == ["grid-initial-build"]
    assert [f.quantity.value for f in r.portfolio.fills] == [100, 100, 100]
    assert r.portfolio.position_quantity(SEC).value == 300
    assert r.portfolio.sellable_quantity(SEC, bars[-1].session.session_date).value == 0
    assert r.portfolio.cash.amount < D(1000000) - D(3000)
    assert p.anchor == 10 and not p.monitoring_events


def test_explicit_build_partial_remainder_is_not_resubmitted_next_day():
    p = policy(initial_shares=300)
    bars = [replace(bar(9, 31, 10), volume_shares=2000), bar(9, 32, 10),
            bar(10, 31, 10), bar(10, 32, 10)]
    r = run(p, bars, capacity_mode=CapacityMode.POINT_IN_TIME_VOLUME)
    assert [f.quantity.value for f in r.portfolio.fills] == [100]
    assert len([e for e in r.events if e.status == "submitted"]) == 1
    assert not p.pending and p.anchor == 10


def test_two_sided_bar_is_not_ordered_using_future_extremes():
    p = policy()
    # Buy first so both directions can operate in the following session.
    r = run(p, [bar(9, 31, 9), bar(9, 32, 9), replace(bar(10, 31, 9),
                prices=BarPrices(D(9), D(10), D(8), D(9)))])
    assert len([e for e in r.events if e.status == "submitted"]) == 1
    assert any(e["state"] == "ambiguous" for e in r.monitoring_events)


def test_closing_auction_is_not_a_new_trigger():
    p = policy()
    closing = replace(bar(9, 31, 9), ended_at=bar(9, 31, 9).ended_at.replace(hour=14, minute=58))
    r = run(p, [bar(9, 31, 10), closing])
    assert not r.events


def test_new_plan_schema_default_does_not_migrate_saved_grids():
    from ashare_lab.adapters.language.vibe_candidates import _apply_new_plan_schema_defaults, _GRID_DEFAULT_EXECUTION_GUIDANCE
    definitions = {"GridParameters": GridParameters.model_json_schema(),
                   "ScheduledParameters": {"properties": {"budget_cny": {}}}}
    _apply_new_plan_schema_defaults(definitions)
    assert definitions["GridParameters"]["properties"]["anchor_update"]["default"] == "last_trigger"
    assert "initial_shares" in definitions["GridParameters"]["required"]
    assert "建仓策略" in _GRID_DEFAULT_EXECUTION_GUIDANCE
    assert GridParameters(anchor_price=10, lower_price=5, upper_price=15).anchor_update == "fixed"
