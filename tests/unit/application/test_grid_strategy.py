from dataclasses import replace
from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from ashare_lab.adapters.market_data.mx_daily_history import MxDailyHistoryClient
from ashare_lab.adapters.persistence.backtest_runs import InMemoryBacktestRunStore
from ashare_lab.api import create_app
from ashare_lab.application.grid_strategy import (
    GridParameters,
    GridSpecificationError,
    grid_order,
    run_grid_backtest,
    advance_grid_fill,
)
from ashare_lab.application.skill_backtest_service import SkillBacktestService
from ashare_lab.domain.market_data import Board, TradingStatus
from ashare_lab.ports.provider_indicator_data import HistoricalIndicatorData
from tests.unit.application.test_skill_backtest import _history, _row

DATES = (date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6), date(2025, 1, 7))


def parameters(**changes: object) -> GridParameters:
    return GridParameters.model_validate({
        "anchor_price": 10, "lower_price": 5, "upper_price": 15, "spacing": 1,
        "commission_rate": 0, "minimum_commission_cny": 0, "stamp_tax_rate": 0,
        "transfer_fee_rate": 0, "slippage_bps": 0, **changes,
    })


def run(prices: list[tuple[str, str]], *, no_trades=(), **changes: object):
    from ashare_lab.application.backtest_submission import BacktestRunConfig
    from ashare_lab.domain.execution import CapacityMode
    history = _history((_row(date(2024, 12, 31), raw_open=prices[0][0]), *(
        _row(day, raw_open=op, raw_close=close)
        for day, (op, close) in zip(DATES, prices, strict=False)
    )))
    history = replace(history, rows=tuple(replace(row, volume=0, amount=D(0))
        if row.session_date in {DATES[i] for i in no_trades} else row for row in history.rows))
    return run_grid_backtest(params=parameters(**changes), history=history,
                             start=DATES[0], end=DATES[-1],
                             execution_config=BacktestRunConfig(capacity_mode=CapacityMode.UNLIMITED) if no_trades else None)


def test_latest_anchor_is_pinned_and_never_inferred_from_replay_end():
    result = run([("10", "11"), ("11", "14")],
                 anchor_mode="latest_price", anchor_price=D("12"))
    assert result["parameters"]["anchor_price"] == "12"


@pytest.mark.parametrize('side,trigger', [('buy', D(8)), ('sell', D(14))])
def test_directional_grid_partial_fills_advance_the_same_distance_as_one_fill(side, trigger):
    params = parameters(buy_spacing=1, sell_spacing=2, order_shares=100)
    intent = grid_order(params, price=trigger, session=DATES[0], filled_units=D(0),
                        board=Board.CHINEXT, reference_price=D(10))
    assert intent is not None and intent.side == side and intent.quantity == 200
    complete = advance_grid_fill(params, intent, quantity=200, price=trigger,
                                 filled_units=D(0), reference_price=D(10))
    first = advance_grid_fill(params, intent, quantity=60, price=trigger,
                              filled_units=D(0), reference_price=D(10))
    second = advance_grid_fill(first[0], intent, quantity=140, price=trigger,
                               filled_units=first[2], reference_price=first[3])
    assert second == complete


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("kind", ["grid", "conditional"])
def test_price_plan_capacity_clips_execution_not_declaration_lots(side, kind):
    from ashare_lab.application.backtest_submission import BacktestRunConfig
    from ashare_lab.application.conditional_orders import run_conditional_backtest
    from ashare_lab.domain.strategy.price_plans import ConditionParameters
    target = D(9) if side == "buy" else D(11)
    first = replace(_row(DATES[0], raw_open="10", raw_close=str(target)), volume=1140)
    history = _history((_row(date(2024, 12, 31), raw_open="10"), first,
                        _row(DATES[1], raw_open="10", raw_close="10")))
    common = dict(opening_shares=100 if side == "sell" else 0, commission_rate=0,
                  minimum_commission_cny=0, slippage_bps=0)
    config = BacktestRunConfig(participation_rate=D("0.05"))
    if kind == "grid":
        result = run_grid_backtest(params=parameters(price_mode="grid_limit", **common),
            history=history, start=DATES[0], end=DATES[1], execution_config=config)
    else:
        params = ConditionParameters.model_validate(dict(rules=[dict(kind="price", side=side,
            direction="down" if side == "buy" else "up", target_price=target,
            quantity=100, limit_price=target)], **common))
        result = run_conditional_backtest(params=params, history=history,
            start=DATES[0], end=DATES[1], execution_config=config)
    order, = result["orders"]
    assert order["requested_quantity"] == 100
    # Previous 1,140 shares x 5%, independent of the limit price (9/11)
    # versus today's open (10). Partial fills need not be new round-lot orders.
    assert order["filled_quantity"] == 57
    assert order["price"] == target
    assert order["reason"] == "partial_fill"
    assert result["summary"]["final_shares"] == (57 if side == "buy" else 43)
    sign = D(-1) if side == "buy" else D(1)
    assert order["cash_cny"] == order["cash_before_cny"] + sign * target * 57 - order["fees_cny"]


def test_latest_anchor_without_quote_does_not_use_future_close():
    with pytest.raises(GridSpecificationError, match="行情最新价尚未取得"):
        run([("10", "11"), ("11", "14")],
            anchor_mode="latest_price", anchor_price=None)


def test_fixed_price_slippage_reaches_grid_fills_and_ledger():
    result = run([("10", "9"), ("9", "9"), ("10", "10"), ("10", "10")],
                 slippage_cny=D("0.02"))
    buy, sell = result["orders"]
    assert buy["price"] == D("9.02") and sell["price"] == D("9.98")
    assert result["summary"]["final_shares"] == 0
    assert sell["cash_cny"] == D("1000095.48")  # transfer .01 + .01, sell stamp .50


def test_million_cash_does_not_allow_selling_without_stock():
    result = run([("10", "11"), ("11", "11")])
    order = result["orders"][0]
    assert order["cash_before_cny"] == D("1000000")
    assert order["filled_quantity"] == 0
    assert order["reason"] == "no_position_to_sell"
    from ashare_lab.application.price_plan_result import _UNFILLED_REASONS
    assert "没有可卖股票" in _UNFILLED_REASONS[order["reason"]]


@pytest.mark.parametrize("scope,cash,equity", [
    ("total_equity", D(990000), D(1000000)),
    ("cash_plus_opening_holdings", D(1000000), D(1010000)),
])
def test_grid_imports_opening_inventory_without_buy_or_extra_equity(scope, cash, equity):
    result = run([("10", "11"), ("11", "11")], opening_shares=1000,
                 initial_capital_scope=scope)
    sell, = result["orders"]
    assert sell["side"] == "sell" and sell["filled_quantity"] == 100
    assert sell["shares_before"] == sell["sellable_before"] == 1000
    assert sell["cash_before_cny"] == cash and not sell["initial"]
    assert result["summary"]["initial_equity_cny"] == equity
    assert result["summary"]["final_shares"] == 900
    assert result["summary"]["total_return"] == (equity + 1000 - sell["fees_cny"]) / equity - 1


def test_opening_inventory_larger_than_total_equity_is_not_free_stock():
    from ashare_lab.application.minute_grid_plan import MinuteGridCapabilityError
    with pytest.raises(MinuteGridCapabilityError, match="opening_holding_exceeds_initial_equity"):
        run([("10", "11"), ("11", "11")], opening_shares=1000, initial_cash_cny=5000)


@pytest.mark.parametrize("changes,reason", [
    ({"initial_cash_cny": 100}, "insufficient_cash_including_fees"),
    ({"sizing_mode": "amount", "order_amount_cny": 100}, "order_amount_below_minimum"),
    ({"max_shares": 50}, "maximum_inventory_exceeded"),
    ({"max_position_cny": 100}, "maximum_position_value_exceeded"),
])
def test_buy_constraints_have_separate_reasons_without_manufacturing_fills(changes, reason):
    result = run([("10", "9"), ("9", "9")], **changes)
    order = result["orders"][0]
    assert order["filled_quantity"] == 0
    assert order["reason"] == reason


def test_zero_fill_feedback_counts_orders_not_events_or_closed_cycles():
    from ashare_lab.application.execution_feedback import zero_fill_execution_note
    activities = [
        {"chainId": "one", "kind": "order"},
        {"chainId": "one", "kind": "unfilled", "outcomeReason": "no_position_to_sell"},
        {"chainId": "one", "kind": "expired", "outcomeReason": "day_end"},
    ]
    note = zero_fill_execution_note(activities)
    assert "1笔委托均未成交" in note and "没有可卖股票" in note
    assert zero_fill_execution_note([*activities, {"kind": "partial_fill"}]) is None
    assert "未产生可执行委托" in zero_fill_execution_note([])


def test_zero_fill_feedback_uses_expiry_not_initial_submission_or_one_empty_bar():
    from ashare_lab.application.execution_feedback import zero_fill_execution_note
    activities = [
        {"orderId": "first", "kind": "order", "outcomeReason": "grid_cell_trigger"},
        {"orderId": "first", "kind": "order", "outcomeReason": "no_market_trades"},
        {"orderId": "first", "kind": "expired", "outcomeReason": "day_expired"},
        {"orderId": "rebuilt", "kind": "order", "outcomeReason": "order_rebuilt_next_session"},
        {"orderId": "rebuilt", "kind": "expired", "outcomeReason": "backtest_ended"},
    ]
    note = zero_fill_execution_note(activities)
    assert "2笔委托均未成交" in note
    assert "当日委托有效期结束" in note
    assert "回测区间结束" in note
    assert "具体原因见" not in note
    assert "无市场成交" not in note


@pytest.mark.parametrize(("mode", "lower", "upper"), [
    ("cny", D(99), D(101)),
    ("anchor_percent", D(99), D(101)),
    ("percent", D(100) / D("1.01"), D(101)),
])
def test_spacing_retains_unit_and_percent_definition(mode, lower, upper):
    spec = parameters(anchor_price=100, lower_price=80, upper_price=120,
                      spacing_mode=mode, spacing=1)
    assert spec.line(D(1)) == lower
    assert spec.line(D(-1)) == upper
    assert spec.distance(lower) == 1
    assert spec.distance(upper) == -1


def test_half_grid_does_not_buy_and_signal_does_not_change_inventory():
    spec = parameters()
    assert grid_order(spec, price=D("9.5"), session=DATES[0], filled_units=D(0),
                      board=Board.MAIN) is None
    order = grid_order(spec, price=D(9), session=DATES[0], filled_units=D(0), board=Board.MAIN)
    assert order.quantity == 100
    assert order.grid_units == 1
    result = run([("10", "9"), ("9", "9"), ("10", "10"), ("10", "10")])
    assert result["series"][2]["shares"] == 100  # Sell signal, not yet a fill.
    assert result["summary"]["final_shares"] == 0


def test_grid_amount_budget_is_not_exceeded_by_a_gap_up():
    result = run([("10", "9"), ("10", "10")], sizing_mode="amount", order_amount_cny=1800)
    order = result["orders"][0]
    assert order["requested_quantity"] == 200
    assert order["filled_quantity"] == 100
    assert order["price"] * order["filled_quantity"] <= 1800


def test_last_fill_reference_is_explicit_and_uses_execution_not_trigger():
    result = run([("10", "9"), ("8.5", "9.5"), ("9.5", "9.5")],
                 anchor_update="last_fill")
    buy, sell = result["orders"]
    assert buy["reference_price"] == 10 and buy["trigger_price"] == 9
    assert buy["price"] == D("8.5")
    assert sell["reference_price"] == D("8.5")
    assert sell["trigger_price"] == D("9.5")
    assert sell["filled_quantity"] == 100
    assert result["initialization"]["anchor_price"] == 10
    assert result["initialization"]["final_anchor_price"] == D("9.5")


def test_failed_grid_order_never_moves_last_fill_reference():
    result = run([("10", "9"), ("10", "9"), ("9", "9")],
                 anchor_update="last_fill", price_mode="grid_limit", no_trades=(1,))
    assert result["orders"][0]["filled_quantity"] == 0
    assert result["orders"][1]["reference_price"] == 10
    assert result["initialization"]["final_anchor_price"] == 9


def test_partial_grid_fill_updates_reference_without_phantom_remaining_quantity():
    result = run([("10", "8"), ("8", "9"), ("9", "9")],
                 anchor_update="last_fill", max_shares=100)
    buy, sell = result["orders"]
    assert buy["requested_quantity"] == 200 and buy["filled_quantity"] == 100
    assert sell["requested_quantity"] == 100 and sell["filled_quantity"] == 100
    assert sell["reference_price"] == 8


def test_limit_uses_next_session_range_not_signal_session_range():
    result = run([("10", "9"), ("10", "9"), ("9", "9")], price_mode="grid_limit")
    orders = result["orders"]
    assert orders[0]["filled_quantity"] == 100
    assert orders[0]["signal_date"] == str(DATES[0])
    assert orders[0]["date"] == str(DATES[1])
    assert orders[0]["price"] == 9


def test_maximum_position_clips_partial_fill_and_never_creates_phantom_shares():
    result = run([("10", "8"), ("8", "8"), ("8", "8")], max_shares=100)
    assert result["orders"][0]["requested_quantity"] == 200
    assert result["orders"][0]["filled_quantity"] == 100
    assert result["orders"][0]["grid_units"] == 1
    assert result["orders"][1]["filled_quantity"] == 0
    assert result["summary"]["final_shares"] == 100


def test_inventory_floor_and_initial_purchase_cost_are_real():
    result = run([("10", "12"), ("12", "12"), ("12", "12")],
                 initial_shares=200, min_shares=100)
    first = result["orders"][0]
    assert first["initial"] and first["price"] == 10
    assert first["cash_cny"] == D("997999.98")
    assert first["date"] == str(DATES[0])
    assert result["orders"][1]["filled_quantity"] == 100
    assert result["orders"][1]["date"] == str(DATES[1])
    assert result["summary"]["final_shares"] == 100


def test_amount_budget_max_value_cash_and_fees_all_bound_fills():
    result = run([("10", "9"), ("9", "9"), ("9", "9")], sizing_mode="amount",
                 order_amount_cny=5000, max_position_cny=1900, initial_cash_cny=2000,
                 commission_rate="0.001", minimum_commission_cny=5)
    assert result["orders"][0]["filled_quantity"] == 200
    assert result["orders"][0]["fees_cny"] == D("5.02")
    assert result["orders"][0]["cash_cny"] == D("194.98")
    assert all(point["cash_cny"] >= 0 for point in result["series"])
    assert result["summary"]["final_shares"] == 200


@pytest.mark.parametrize("first,sell_day,stamp", [
    (date(2023, 8, 24), date(2023, 8, 25), D("1.10")),
    (date(2023, 8, 28), date(2023, 8, 29), D(".55")),
])
def test_daily_grid_uses_trade_date_fee_policy_not_saved_flat_tax(first, sell_day, stamp):
    previous = date(2023, 8, 23) if first.day == 24 else date(2023, 8, 25)
    history = _history((_row(previous, raw_open="10"), _row(first, raw_open="10", raw_close="11"), _row(sell_day, raw_open="11")))
    result = run_grid_backtest(params=parameters(initial_shares=100), history=history, start=first, end=sell_day)
    buy, sell = result["orders"]
    assert buy["stamp_tax_cny"] == 0
    assert sell["stamp_tax_cny"] == stamp
    assert sell["transfer_fee_cny"] == D(".01")
    assert sell["fees_cny"] == stamp + D(".01")
    assert sell["cash_cny"] == D(1000000) - buy["price"] * buy["filled_quantity"] - buy["fees_cny"] + sell["price"] * sell["filled_quantity"] - sell["fees_cny"]
    assert all(point["cash_cny"] >= 0 for point in result["series"])
    assert result["summary"]["final_shares"] == 0


@pytest.mark.parametrize("status", ["up", "suspended"])
def test_price_limits_and_suspension_never_fill(status):
    second = _row(DATES[1], raw_open="9", at_limit="up" if status == "up" else None,
                  status=(TradingStatus.SUSPENDED if status == "suspended"
                          else TradingStatus.TRADING))
    history = _history((_row(DATES[0], raw_open="10", raw_close="9"), second))
    result = run_grid_backtest(params=parameters(), history=history, start=DATES[0], end=DATES[-1])
    assert result["orders"][0]["filled_quantity"] == 0
    assert result["orders"][0]["grid_units"] == 0
    assert result["summary"]["final_shares"] == 0


def test_star_board_uses_200_minimum_and_one_share_increment():
    history = replace(_history((_row(DATES[0], raw_open="10", raw_close="9"),
                                _row(DATES[1], raw_open="9"))), board=Board.STAR)
    result = run_grid_backtest(params=parameters(order_shares=201), history=history,
                              start=DATES[0], end=DATES[-1])
    assert result["orders"][0]["filled_quantity"] == 201
    with pytest.raises(GridSpecificationError, match="200"):
        run_grid_backtest(params=parameters(order_shares=100), history=history,
                          start=DATES[0], end=DATES[-1])


def test_same_day_buy_is_never_sold_and_outside_grid_pauses():
    result = run([("10", "15"), ("16", "16"), ("16", "16")], initial_shares=100)
    assert result["orders"][0]["side"] == "buy"
    assert result["orders"][1]["side"] == "sell"
    assert result["orders"][1]["date"] > result["orders"][0]["date"]
    assert grid_order(parameters(), price=D(16), session=DATES[0], filled_units=D(0),
                      board=Board.MAIN) is None


def test_fixed_buy_limit_is_not_confused_with_grid_spacing():
    result = run([("10", "8"), ("9", "8"), ("8", "8")],
                 price_mode="fixed_limit", buy_limit=8, sell_limit=12)
    assert result["orders"][0]["filled_quantity"] == 200
    assert result["orders"][0]["price"] == 8


@pytest.mark.parametrize(("price_mode", "expected"), [("next_open", 50_000),
                                                       ("grid_limit", 100_000)])
def test_star_single_order_ceiling(price_mode, expected):
    liquid = replace(_row(DATES[0], raw_open="10", raw_close="9"), volume=100_000_000)
    history = replace(_history((liquid, _row(DATES[1], raw_open="9"))), board=Board.STAR)
    result = run_grid_backtest(
        params=parameters(order_shares=200_000, max_shares=500_000,
                          initial_cash_cny=10_000_000, price_mode=price_mode),
        history=history, start=DATES[0], end=DATES[1],
    )
    assert result["orders"][0]["filled_quantity"] == expected


@pytest.mark.parametrize("auto", [False, True])
def test_grid_http_endpoint_reuses_history_and_returns_parameter_bound_report(auto):
    history = _history((_row(DATES[0], raw_open="10", raw_close="9"),
                        _row(DATES[1], raw_open="9", raw_close="10"),
                        _row(DATES[2], raw_open="10")))
    load = AsyncMock(return_value=history)
    service = SkillBacktestService(history=cast(MxDailyHistoryClient, SimpleNamespace(load=load)),
                                   indicators=cast(HistoricalIndicatorData, object()),
                                   store=InMemoryBacktestRunStore())
    try:
        with TestClient(create_app(backtest_submission=service, run_store=service.store)) as client:
            response = client.post("/api/v1/grid/backtests", json={
                "instrument_id": "300059.SZ", "start": str(DATES[0]), "end": str(DATES[2]),
                "parameters": parameters(**({"anchor_mode": "first_open", "anchor_price": None,
                                             "startup_mode": "wait_for_crossing"} if auto
                                            else {})).model_dump(mode="json"),
            })
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["state"] == "succeeded"
            assert result["summary"]["filled_orders"] == 2
            assert result["request"]["parameters"]["spacing_mode"] == "cny"
            assert result["result_hash"].startswith("sha256:")
            assert result["provenance"]["instrument_id"] == "300059.SZ"
            assert result["initialization"]["anchor_price"] == 10
            assert result["parameters"]["anchor_price"] == "10"
            assert result["request"]["parameters"]["anchor_price"] == (None if auto else "10")
            assert load.await_count == 1
    finally:
        service.shutdown()


def test_auto_anchor_uses_first_requested_open_not_warmup_or_future():
    history = _history((_row(date(2024, 12, 31), raw_open="7"),
                        _row(DATES[0], raw_open="10", raw_close="9"),
                        _row(DATES[1], raw_open="9", raw_close="9"),
                        _row(DATES[2], raw_open="13", raw_close="13")))
    spec = parameters(anchor_mode="first_open", anchor_price=8,
                      startup_mode="wait_for_crossing")
    short = run_grid_backtest(params=spec, history=history, start=DATES[0], end=DATES[1])
    long = run_grid_backtest(params=spec, history=history, start=DATES[0], end=DATES[2])
    assert short["initialization"] == long["initialization"]
    assert short["initialization"]["anchor_price"] == 10
    assert short["initialization"]["reference_date"] == str(DATES[0])
    assert short["series"] == long["series"][:2]
    assert short["orders"] == long["orders"][:len(short["orders"])]
    assert spec.anchor_price == 8  # Effective copy does not mutate submitted parameters.


def test_auto_anchor_is_not_delayed_initial_fill_price():
    history = _history((_row(date(2024, 12, 31), raw_open="10"),
                        _row(DATES[0], raw_open="10", at_limit="up"),
                        _row(DATES[1], raw_open="9")))
    result = run_grid_backtest(params=parameters(anchor_mode="first_open", anchor_price=None,
                                                initial_shares=100),
                               history=history, start=DATES[0], end=DATES[1])
    assert result["initialization"]["anchor_price"] == 10
    assert result["orders"][0]["filled_quantity"] == 0
    assert result["orders"][1]["price"] == 9
    assert result["orders"][1]["cash_cny"] == D("999099.99")


def test_auto_anchor_outside_chosen_bounds_reports_actual_price_and_date():
    with pytest.raises(GridSpecificationError, match=r"2025-01-02开盘价16.*不在网格范围"):
        run([("16", "16")], anchor_mode="first_open", anchor_price=None)


def test_manual_wait_skips_startup_gap_but_catch_up_preserves_legacy():
    prices = [("8", "8"), ("8", "7"), ("7", "7")]
    wait = run(prices, startup_mode="wait_for_crossing")
    catch_up = run(prices)
    assert len(wait["orders"]) == 1
    assert wait["orders"][0]["filled_quantity"] == 100
    assert wait["orders"][0]["date"] == str(DATES[2])
    assert catch_up["orders"][0]["filled_quantity"] == 200
    assert catch_up["orders"][0]["date"] == str(DATES[1])


@pytest.mark.parametrize("mode", ["cny", "anchor_percent", "percent"])
def test_fractional_start_only_trades_whole_new_crossings(mode):
    spec = parameters(anchor_price=100, lower_price=80, upper_price=120,
                      spacing_mode=mode, startup_mode="wait_for_crossing", price_mode="grid_limit")
    startup = D("1.5")
    assert grid_order(spec, price=spec.line(startup), session=DATES[0], filled_units=D(0),
                      board=Board.MAIN, startup_distance=startup) is None
    for distance, side, baseline in ((2, "buy", 1), (1, "sell", 2)):
        order = grid_order(spec, price=spec.line(D(distance)), session=DATES[0],
                           filled_units=D(0), board=Board.MAIN, startup_distance=startup)
        assert order.side == side
        assert order.quantity == 100
        assert abs(order.grid_units) == 1
        assert order.baseline_units == baseline


def test_failed_first_order_does_not_commit_startup_baseline():
    result = run([("8.5", "8"), ("8.5", "9"), ("9", "9")],
                 startup_mode="wait_for_crossing", price_mode="grid_limit", initial_shares=200, no_trades=(1,))
    initial, buy, sell = result["orders"]
    assert initial["initial"]
    assert buy["side"] == "buy" and buy["filled_quantity"] == 0
    assert buy["baseline_units"] == 1
    assert sell["side"] == "sell" and sell["filled_quantity"] == 100
    assert sell["baseline_units"] == 2
    assert sell["grid_units"] == -1
