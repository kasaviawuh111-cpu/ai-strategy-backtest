import pytest
from pydantic import ValidationError

from ashare_lab.domain.strategy.independent_plans import IndependentPlanPair


def plan(kind, side):
    params = {"side": side, "sizing_mode": "shares"} if kind == "scheduled" else (
        {"anchor_mode": "first_open", "lower_price": 8, "upper_price": 12}
        if kind == "grid" else {"rules": [{"kind": "price", "side": side,
            "direction": "down" if side == "buy" else "up", "target_price": 10,
            "quantity": 100}]})
    return {"kind": kind, "parameters": params}


@pytest.mark.parametrize("entry_kind", ["scheduled", "conditional", "grid"])
@pytest.mark.parametrize("exit_kind", ["scheduled", "conditional", "grid"])
def test_all_plan_pairs_roundtrip_without_losing_either_leg(entry_kind, exit_kind):
    pair = IndependentPlanPair(entry_plan=plan(entry_kind, "buy"), exit_plan=plan(exit_kind, "sell"))
    assert IndependentPlanPair.model_validate_json(pair.model_dump_json()) == pair
    assert pair.entry_plan.kind == entry_kind
    assert pair.exit_plan.kind == exit_kind


def test_conflicting_cash_is_not_silently_selected_or_added():
    exit = plan("grid", "sell")
    exit["parameters"]["initial_cash_cny"] = 12345
    with pytest.raises(ValidationError, match="shared account field: initial_cash_cny"):
        IndependentPlanPair(entry_plan=plan("scheduled", "buy"), exit_plan=exit)


def test_opposite_side_schedule_cannot_be_dropped():
    with pytest.raises(ValidationError, match="declared side"):
        IndependentPlanPair(entry_plan=plan("scheduled", "sell"), exit_plan=plan("grid", "sell"))
