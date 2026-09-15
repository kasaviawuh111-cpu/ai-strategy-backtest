from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from ashare_lab.adapters.market_data.mx_daily_history import MxDailyHistoryClient
from ashare_lab.adapters.persistence.backtest_runs import InMemoryBacktestRunStore
from ashare_lab.adapters.strategies.vnpy_conditions import price_reached, trailing_price
from ashare_lab.api import create_app
from ashare_lab.application.conditional_orders import ConditionParameters, run_conditional_backtest
from ashare_lab.application.skill_backtest_service import SkillBacktestService
from ashare_lab.ports.provider_indicator_data import HistoricalIndicatorData
from tests.unit.application.test_grid_strategy import DATES
from tests.unit.application.test_skill_backtest import _history, _row


def params(rules, **kwargs):
    return ConditionParameters.model_validate({
        "rules": rules, "commission_rate": 0, "minimum_commission_cny": 0,
        "transfer_fee_rate": 0, "stamp_tax_rate": 0, "slippage_bps": 0, **kwargs,
    })


def history(prices):
    return _history((_row(date(2024, 12, 31), raw_open="10"), *(
        _row(day, raw_open=op, raw_close=close)
        for day, (op, close) in zip(DATES, prices, strict=False)
    )))


def test_small_amount_condition_records_rejection_and_ends_instead_of_disappearing():
    result = run_conditional_backtest(params=params([
        {"kind": "price", "side": "buy", "direction": "down", "target_price": 10,
         "sizing_mode": "amount", "amount_cny": 100},
    ]), history=history([("10", "10")] * 3), start=DATES[0], end=DATES[2])
    assert len(result["orders"]) == 1
    order = result["orders"][0]
    assert order["requested_quantity"] == order["filled_quantity"] == 0
    assert order["reason"] == "order_amount_below_minimum"
    assert result["condition_state"]["terminal_order"]["disposition"] == "ended"
    assert result["summary"]["final_shares"] == 0


def test_daily_holding_requires_status_rows_for_every_market_day():
    from dataclasses import replace
    from ashare_lab.application.minute_replay_input import MinuteReplayDataError
    data = history([("10", "10"), ("10", "10"), ("10", "10")])
    data = replace(data, rows=tuple(row for row in data.rows if row.session_date != DATES[1]))
    with pytest.raises(MinuteReplayDataError, match="security_session_missing"):
        run_conditional_backtest(params=params([
            {"kind": "holding_period", "side": "sell", "sessions": 1},
        ], initial_shares=100), history=data, start=DATES[0], end=DATES[2], market_sessions=DATES)


def test_daily_holding_counts_surviving_batches_separately():
    from ashare_lab.application.conditional_orders import ConditionalOrderPolicy
    from ashare_lab.application.grid_strategy import PricePolicyContext
    from ashare_lab.domain.market_data import Board
    from ashare_lab.domain.strategy.price_plans import ConditionRule
    policy = ConditionalOrderPolicy([ConditionRule(kind="holding_period", side="sell", sessions=2, quantity=200)])
    first = policy.on_open(session=DATES[2], board=Board.CHINEXT,
        context=PricePolicyContext(200, 200, D(10), 2, ((2, 100), (1, 100))))
    assert first.quantity == 100
    policy.on_fill(order=first, quantity=100, price=D(10))
    assert policy.cycles == 0
    assert policy.on_open(session=DATES[2], board=Board.CHINEXT,
        context=PricePolicyContext(100, 100, D(10), 2, ((1, 100),))) is None
    second = policy.on_open(session=DATES[3], board=Board.CHINEXT,
        context=PricePolicyContext(100, 100, D(10), 3, ((2, 100),)))
    assert second.quantity == 100
    policy.on_fill(order=second, quantity=100, price=D(10))
    assert policy.cycles == 1


@pytest.mark.parametrize("reserve", [0, 100])
def test_daily_holding_all_waits_for_each_actual_batch_and_keeps_reserve(reserve):
    from ashare_lab.application.conditional_orders import ConditionalOrderPolicy
    from ashare_lab.application.grid_strategy import PricePolicyContext
    from ashare_lab.domain.market_data import Board
    from ashare_lab.domain.strategy.price_plans import ConditionRule
    policy = ConditionalOrderPolicy([ConditionRule(kind="holding_period", side="sell",
        sessions=2, sizing_mode="all_position", quantity=100)], minimum_shares=reserve)
    first = policy.on_open(session=DATES[2], board=Board.CHINEXT,
        context=PricePolicyContext(300, 300, D(10), 2, ((2, 100), (1, 200))))
    assert first.quantity == 100
    policy.on_fill(order=first, quantity=100, price=D(10))
    assert policy.cycles == 0
    assert policy.on_open(session=DATES[2], board=Board.CHINEXT,
        context=PricePolicyContext(200, 200, D(10), 2, ((1, 200),))) is None
    second = policy.on_open(session=DATES[3], board=Board.CHINEXT,
        context=PricePolicyContext(200, 200, D(10), 3, ((2, 200),)))
    assert second.quantity == 200 - reserve
    policy.on_fill(order=second, quantity=100, price=D(10))
    if reserve == 0:
        assert policy.cycles == 0
        remainder = policy.on_open(session=DATES[3], board=Board.CHINEXT,
            context=PricePolicyContext(100, 100, D(10), 3, ((3, 100),)))
        assert remainder.quantity == 100
        policy.on_fill(order=remainder, quantity=100, price=D(10))
    assert policy.cycles == 1


def run(prices, rules, **kwargs):
    return run_conditional_backtest(params=params(rules, **kwargs), history=history(prices),
                                    start=DATES[0], end=DATES[-1])


@pytest.mark.parametrize("direction", ["up", "down"])
def test_vnpy_trigger_equality_is_preserved(direction):
    assert price_reached(D(10), D(10), direction)


@pytest.mark.parametrize("direction", ["up", "down"])
@pytest.mark.parametrize("comparison", ["inclusive", "strict"])
def test_daily_price_comparison_equality_and_crossing(direction, comparison):
    from ashare_lab.application.conditional_orders import ConditionalOrderPolicy
    from ashare_lab.application.grid_strategy import PricePolicyContext
    from ashare_lab.domain.market_data import Board
    from ashare_lab.domain.strategy.price_plans import ConditionRule
    policy = ConditionalOrderPolicy([ConditionRule(kind="price", side="buy", target_price=10,
        direction=direction, price_comparison=comparison)])
    context = PricePolicyContext(0, 0, None, 0, ())
    order = policy.on_close(price=D(10), session=DATES[0], board=Board.CHINEXT, context=context)
    assert (order is not None) == (comparison == "inclusive")
    if comparison == "strict":
        assert policy.on_close(price=D('10.01' if direction == 'up' else '9.99'),
            session=DATES[1], board=Board.CHINEXT, context=context) is not None


def test_vnpy_percentage_and_extended_cny_are_not_confused():
    assert trailing_price(D(20), D(1), unit="percent", direction="down") == D("19.8")
    assert trailing_price(D(20), D(1), unit="cny", direction="down") == 19
    assert trailing_price(D(20), D(1), unit="percent", direction="up") == D("20.2")


@pytest.mark.parametrize("reserve,expected", [(0, 300), (100, 200)])
def test_daily_all_position_uses_actual_holdings_not_ignored_fixed_quantity(reserve, expected):
    result = run([("10", "9")] * 4, [
        {"kind": "stop_loss", "side": "sell", "gap": 1,
         "sizing_mode": "all_position", "quantity": 100},
    ], opening_shares=300, min_shares=reserve)
    sells = [o for o in result["orders"] if o["side"] == "sell" and o["filled_quantity"]]
    assert len(sells) == 1
    assert sells[0]["filled_quantity"] == expected
    assert result["summary"]["final_shares"] == reserve


def test_price_rules_sequence_uses_actual_fills_and_t_plus_one():
    result = run([("10", "9"), ("9", "11"), ("11", "12")], [
        {"kind": "price", "side": "buy", "direction": "down", "target_price": 9},
        {"kind": "price", "side": "sell", "direction": "up", "target_price": 11},
    ])
    buy, sell = result["orders"]
    assert buy["date"] == str(DATES[1]) and buy["price"] == 9
    assert sell["date"] == str(DATES[2]) and sell["price"] == 11
    assert result["summary"]["final_shares"] == 0
    assert result["summary"]["final_equity_cny"] == D("1000199.43")
    assert result["condition_state"]["completed_rules"] == 2

    assert "initialization" not in result
    assert "grid_units" not in buy


def test_daily_sell_then_buy_keeps_opening_lot_and_new_lot_clocks_separate():
    result = run([("10", "10"), ("10", "9"), ("9", "9")], [
        {"kind": "price", "side": "sell", "direction": "up", "target_price": 10},
        {"kind": "relative_price", "side": "buy", "direction": "down", "gap": 1, "gap_unit": "cny"},
    ], opening_shares=1000)
    sell, buy = result["orders"]
    assert sell["filled_quantity"] == buy["filled_quantity"] == 100
    assert sell["shares_before"] == sell["sellable_before"] == 1000
    assert sell["cash_before_cny"] == D(990000)
    assert buy["shares_before"] == buy["sellable_before"] == 900
    assert buy["date"] > sell["date"] and not any(o["initial"] for o in result["orders"])
    assert result["summary"]["final_shares"] == 1000
    assert any("期初已有持仓直接导入" in w for w in result["warnings"])
    assert any("历史日期计算" in w for w in result["warnings"])
    assert not any("跳过多格" in w for w in result["warnings"])


def test_daily_pullback_can_protect_declared_opening_odd_lot():
    result = run([("10", "11"), ("11", "10"), ("10", "10")], [
        {"kind": "pullback", "side": "sell", "gap": 5, "quantity": 105},
    ], opening_shares=105)
    sell, = result["orders"]
    assert sell["filled_quantity"] == 105 and sell["side"] == "sell"
    assert sell["sellable_before"] == 105
    assert result["summary"]["final_shares"] == 0


@pytest.mark.parametrize(("unit", "gap"), [("cny", 1), ("percent", 10)])
def test_rebound_tracks_low_only_since_activation_and_confirms_at_close(unit, gap):
    result = run([("10", "10"), ("10", "9"), ("9", "10"), ("10", "10")], [
        {"kind": "rebound", "side": "buy", "gap": gap, "gap_unit": unit},
    ])
    assert len(result["orders"]) == 1
    assert result["orders"][0]["signal_date"] == str(DATES[2])
    assert result["orders"][0]["date"] == str(DATES[3])
    assert result["orders"][0]["filled_quantity"] == 100


def test_trailing_high_begins_after_real_entry_not_pre_entry_high():
    result = run([("20", "20"), ("20", "10"), ("10", "11"), ("11", "11")], [
        {"kind": "price", "side": "buy", "direction": "down", "target_price": 10},
        {"kind": "pullback", "side": "sell", "gap": 5},
    ])
    assert len(result["orders"]) == 1
    assert result["condition_state"]["completed_rules"] == 1
    assert result["summary"]["final_shares"] == 100


def test_end_cancels_unfilled_exit_without_liquidating_position():
    result = run([("10", "12"), ("12", "12")], [
        {"kind": "take_profit", "side": "sell", "gap": 1, "gap_unit": "cny", "limit_price": 20},
    ], initial_shares=100)
    state = result["condition_state"]
    assert state["pending_quantity"] == 0
    assert state["unfinished_exit_quantity"] == 100
    assert state["end_cancellation"]["quantity"] == 100
    assert state["end_cancellation"]["limit_price"] == D(20)
    assert result["summary"]["final_shares"] == 100
    assert result["summary"]["filled_orders"] == 1  # Initial buy only.


def test_fifo_sale_reprices_remaining_cost_before_next_protection_signal():
    result = run([("10", "20"), ("20", "20"), ("20", "19"), ("19", "21")], [
        {"kind": "price", "side": "buy", "direction": "up", "target_price": 20},
        {"kind": "price", "side": "sell", "direction": "up", "target_price": 20},
        {"kind": "take_profit", "side": "sell", "gap": 10, "gap_unit": "percent"},
    ], initial_shares=100)
    # Buy 100 at 10, add 100 at 20, then FIFO sell the old 100.
    # Remaining basis is 20, so 19/21 must not trigger the 22 take-profit.
    assert [order["filled_quantity"] for order in result["orders"]] == [100, 100, 100]
    assert result["orders"][-1]["average_entry_price"] == D(20)
    assert result["summary"]["final_shares"] == 100
    assert result["condition_state"]["completed_rules"] == 2
    ledger = result["portfolio_ledger"]
    assert ledger["engine"] == "shared_portfolio.v1"
    assert ledger["fills"] == ledger["journal_entries"] == 3
    assert ledger["cash_cny"] == result["series"][-1]["cash_cny"]
    assert ledger["lots"][0]["principal_cny"] == D(2000)
    assert ledger["lots"][0]["cost_basis_cny"] > D(2000)  # Transfer fee stays outside signal cost.


def test_ordinary_partial_exit_ends_remainder_and_respects_bottom_inventory():
    result = run([("10", "12"), ("12", "10"), ("10", "10"), ("10", "10")], [
        {"kind": "pullback", "side": "sell", "gap": 1, "gap_unit": "cny", "quantity": 200},
    ], initial_shares=200, min_shares=100)
    assert result["summary"]["final_shares"] == 100
    assert result["orders"][1]["filled_quantity"] == 100
    assert len(result["orders"]) == 2
    assert result["condition_state"]["completed_rules"] == 0
    assert result["condition_state"]["pending_quantity"] == 0
    assert result["condition_state"]["terminal_order"]["unfilled_quantity"] == 100


def test_trigger_is_not_a_fill_and_failed_limit_does_not_advance_rule():
    result = run([("10", "11"), ("12", "12"), ("10", "9")], [
        {"kind": "price", "side": "buy", "direction": "up", "target_price": 11,
         "limit_price": 10},
    ])
    assert result["orders"][0]["filled_quantity"] == 0
    assert len(result["orders"]) == 1
    assert result["orders"][0]["remainder_disposition"] == "expired_day"
    assert result["condition_state"]["completed_rules"] == 0
    assert result["condition_state"]["terminal_order"]["reason"] == "limit_not_reached"


@pytest.mark.parametrize("kind,price", [("take_profit", "11"), ("stop_loss", "9"), ("holding_period", "10")])
def test_exit_retry_preserves_original_trigger_quantity_and_limit(kind, price):
    from ashare_lab.application.conditional_orders import ConditionalOrderPolicy
    from ashare_lab.application.grid_strategy import PricePolicyContext
    from ashare_lab.domain.market_data import Board
    from ashare_lab.domain.strategy.price_plans import ConditionRule
    rule = ConditionRule(kind=kind, side="sell", quantity=100, limit_price=20,
                         **({"sessions": 2} if kind == "holding_period" else {"gap": 1, "gap_unit": "cny"}))
    policy = ConditionalOrderPolicy([rule])
    context = PricePolicyContext(100, 100, D(10), 2, ((2, 100),))
    order = (policy.on_open(session=DATES[0], board=Board.CHINEXT, context=context)
             if kind == "holding_period" else
             policy.on_close(price=D(price), session=DATES[0], board=Board.CHINEXT, context=context))
    assert order is not None
    assert policy.on_session_end(order=order, session=DATES[1], reason="limit_not_reached") == "retry_next_session"
    retried = policy.on_open(session=DATES[2], board=Board.CHINEXT, context=context)
    assert retried == order
    assert policy.terminal is None


def test_prefix_does_not_change_when_future_history_is_added():
    data = history([("10", "10"), ("10", "9"), ("9", "8"), ("8", "13")])
    spec = params([{"kind": "rebound", "side": "buy", "gap": 1, "gap_unit": "cny"}])
    short = run_conditional_backtest(params=spec, history=data, start=DATES[0], end=DATES[1])
    long = run_conditional_backtest(params=spec, history=data, start=DATES[0], end=DATES[3])
    assert short["series"] == long["series"][:2]
    assert short["orders"] == [o for o in long["orders"] if o["date"] <= str(DATES[1])]


def test_sell_condition_cannot_create_a_short_position():
    result = run([("10", "10"), ("10", "11")], [
        {"kind": "price", "side": "sell", "direction": "up", "target_price": 10},
    ])
    assert result["summary"]["filled_orders"] == 0
    assert result["summary"]["final_shares"] == 0


def test_conditional_api_and_minute_input_boundary():
    load = AsyncMock(return_value=history([("10", "9"), ("9", "10")]))
    service = SkillBacktestService(history=cast(MxDailyHistoryClient, SimpleNamespace(load=load)),
                                   indicators=cast(HistoricalIndicatorData, object()),
                                   store=InMemoryBacktestRunStore())
    try:
        with TestClient(create_app(backtest_submission=service, run_store=service.store)) as client:
            body = {"instrument_id": "300059.SZ", "start": str(DATES[0]), "end": str(DATES[1]),
                    "parameters": params([{"kind": "price", "side": "buy", "direction": "down",
                                            "target_price": 9}]).model_dump(mode="json")}
            response = client.post("/api/v1/conditional/backtests", json=body)
            assert response.status_code == 200, response.text
            report = response.json()
            assert report["summary"]["filled_orders"] == 1
            assert report["request"] == body
            assert report["result_hash"].startswith("sha256:")
            assert report["provenance"]["algorithm"] == "vnpy-price-conditions-adapter.v1"
            body["parameters"]["observation"] = "minute_close"
            assert client.post("/api/v1/conditional/backtests", json=body).status_code == 422
            assert load.await_count == 1
    finally:
        service.shutdown()
