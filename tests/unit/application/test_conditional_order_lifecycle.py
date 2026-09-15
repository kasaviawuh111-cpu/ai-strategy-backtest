"""Hand-calculated lifecycle cases, not the implementation as an oracle."""
from decimal import Decimal as D

import pytest

from ashare_lab.domain.strategy.price_plans import ConditionRule
from tests.unit.application.test_conditional_orders import params, run


def test_legacy_percentage_field_is_equivalent_but_cannot_override_cny():
    rule = ConditionRule.model_validate({"kind": "take_profit", "side": "sell",
                                         "threshold_pct": 5})
    assert rule.gap == 5 and rule.gap_unit == "percent"
    with pytest.raises(ValueError, match="单位冲突"):
        ConditionRule.model_validate({"kind": "stop_loss", "side": "sell",
                                      "threshold_pct": 3, "gap_unit": "cny"})
    with pytest.raises(ValueError, match="不一致"):
        ConditionRule.model_validate({"kind": "take_profit", "side": "sell",
                                      "threshold_pct": 5, "gap": 6})


@pytest.mark.parametrize(("kind", "unit", "gap", "close", "target", "taxes"), [
    ("take_profit", "cny", 1, "11", "11", "0.57"),
    ("take_profit", "percent", 1, "10.1", "10.1", "0.53"),
    ("stop_loss", "cny", 1, "9", "9", "0.47"),
    ("stop_loss", "percent", 1, "9.9", "9.9", "0.52"),
])
def test_cost_exits_use_actual_fill_and_keep_cny_distinct(kind, unit, gap, close, target, taxes):
    report = run([("12", "9"), ("10", close), (close, close)], [
        {"kind": "price", "side": "buy", "direction": "down", "target_price": 9},
        {"kind": kind, "side": "sell", "gap": gap, "gap_unit": unit},
    ])
    buy, sell = report["orders"]
    assert buy["price"] == 10  # Not the trigger at 9.
    assert sell["reference_price"] == 10
    assert sell["trigger_price"] == D(target)
    assert sell["filled_quantity"] == 100
    # 2025: sell stamp duty 0.05%, both-side transfer 0.001%,
    # rounded per fee to cents. Fixture disables commission/slippage only;
    # statutory taxes cannot be disabled by its legacy zero-rate fields.
    assert sell["cash_cny"] == 1000000 - 100 * 10 + 100 * D(close) - D(taxes)
    assert sell["sellable_before"] == 100


def test_oco_cancels_other_exit_at_trigger_even_if_winner_limit_has_not_filled():
    report = run([("10", "12"), ("12", "8"), ("8", "8")], [
        {"kind": "take_profit", "side": "sell", "gap": 10, "group": "exit", "limit_price": 15},
        {"kind": "stop_loss", "side": "sell", "gap": 5, "group": "exit"},
    ], initial_shares=100)
    assert report["condition_state"]["cancelled_rules"] == [{"cycle": 1, "rule_index": 1}]
    assert report["condition_state"]["completed_cycles"] == 0
    assert report["summary"]["final_shares"] == 100
    assert all(o["rule_id"] == "cycle-1:rule-1" for o in report["orders"] if not o["initial"])


def test_holding_deadline_uses_actual_entry_and_exchange_sessions_not_weekend_days():
    # Entry Friday Jan 3, one subsequent exchange session is Monday Jan 6.
    report = run([("10", "9"), ("9", "10"), ("11", "11")], [
        {"kind": "price", "side": "buy", "direction": "down", "target_price": 9},
        {"kind": "holding_period", "side": "sell", "sessions": 1},
    ])
    buy, sell = report["orders"]
    assert buy["date"] == "2025-01-03"
    assert sell["date"] == "2025-01-06" and sell["price"] == 11
    assert sell["trigger_price"] is None  # A time deadline, not an observed future price.


def test_staged_profit_taking_preserves_original_cost_after_partial_sale():
    report = run([("10", "11"), ("11", "12"), ("12", "12")], [
        {"kind": "take_profit", "side": "sell", "gap": 1, "gap_unit": "cny"},
        {"kind": "take_profit", "side": "sell", "gap": 2, "gap_unit": "cny"},
    ], initial_shares=200)
    exits = [o for o in report["orders"] if o["side"] == "sell"]
    assert [o["filled_quantity"] for o in exits] == [100, 100]
    assert [o["reference_price"] for o in exits] == [10, 10]
    assert report["summary"]["final_shares"] == 0


def test_relative_second_leg_uses_actual_sell_not_initial_or_trigger_price():
    report = run([("10", "11"), ("12", "11"), ("11", "11")], [
        {"kind": "price", "side": "sell", "target_price": 11},
        {"kind": "relative_price", "side": "buy", "direction": "down", "gap": 1, "gap_unit": "cny"},
    ], initial_shares=100)
    assert report["orders"][-1]["side"] == "buy"
    assert report["orders"][-1]["reference_price"] == 12
    assert report["orders"][-1]["trigger_price"] == 11
    assert report["summary"]["final_shares"] == 100


def test_cycles_restart_only_after_actual_second_leg_fill():
    report = run([("10", "9"), ("9", "10"), ("10", "9"), ("9", "10")], [
        {"kind": "price", "side": "buy", "direction": "down", "target_price": 9},
        {"kind": "relative_price", "side": "sell", "gap": 1, "gap_unit": "cny"},
    ], repeat_cycles=2)
    assert [o["rule_id"] for o in report["orders"]] == [
        "cycle-1:rule-1", "cycle-1:rule-2", "cycle-2:rule-1",
    ]
    assert report["condition_state"]["completed_cycles"] == 1


def test_amount_budget_cannot_expand_at_gap_up_execution_price():
    report = run([("10", "9"), ("10", "10")], [
        {"kind": "price", "side": "buy", "direction": "down", "target_price": 9,
         "sizing_mode": "amount", "amount_cny": 1800},
    ])
    assert report["orders"][0]["filled_quantity"] == 100
    assert report["orders"][0]["price"] == 10
    assert report["series"][-1]["cash_cny"] == D("998999.99")
    assert report["condition_state"]["completed_cycles"] == 1
    assert report["condition_state"]["pending_quantity"] == 0
    residual = report["condition_state"]["budget_residuals"][0]
    assert residual["cancelled_quantity"] == 100
    assert residual["unspent_amount_cny"] == 800


def test_rebound_activation_does_not_use_extreme_before_start_price():
    report = run([("5", "5"), ("10", "10"), ("11", "11"), ("11", "11")], [
        {"kind": "pullback", "side": "sell", "activation_price": 10, "gap": 1, "gap_unit": "cny"},
    ], initial_shares=100)
    assert report["summary"]["filled_orders"] == 1  # Initial buy only.


def test_fee_breakdown_and_inventory_can_be_reconciled_per_event():
    report = run([("10", "11"), ("11", "11")], [
        {"kind": "take_profit", "side": "sell", "gap": 1, "gap_unit": "cny"},
    ], initial_shares=100, commission_rate="0.0003", minimum_commission_cny=5,
       transfer_fee_rate="0.00001", stamp_tax_rate="0.0005")
    for event in report["orders"]:
        assert event["fees_cny"] == sum(event[k] for k in (
            "commission_cny", "transfer_fee_cny", "stamp_tax_cny",
        ))
        sign = 1 if event["side"] == "buy" else -1
        assert event["shares"] == event["shares_before"] + sign * event["filled_quantity"]
        assert event["cash_cny"] == event["cash_before_cny"] - (
            sign * event["filled_quantity"] * event["price"] + event["fees_cny"]
        )


def test_non_contiguous_oco_is_rejected_instead_of_canceling_later_stage():
    with pytest.raises(ValueError, match="相邻"):
        params([
            {"kind": "price", "side": "buy", "target_price": 10, "group": "x"},
            {"kind": "price", "side": "sell", "target_price": 11},
            {"kind": "price", "side": "buy", "target_price": 10, "group": "x"},
        ])
