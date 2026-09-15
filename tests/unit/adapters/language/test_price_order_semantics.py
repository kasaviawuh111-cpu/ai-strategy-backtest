"""Listing buy/sell conditions must not invent fill dependencies."""

from datetime import date

import pytest

from ashare_lab.adapters.language.generation_preflight import validate_generated_plan
from ashare_lab.adapters.language.vibe_candidates import VibeBoundedCandidateGenerator
from ashare_lab.domain.strategy.price_plans import ConditionalPlan, GridSpecificationError
from ashare_lab.ports.candidate_generation import CompileInput
from tests.unit.adapters.language.test_vibe_candidates import CAPABILITY_MATRIX


@pytest.mark.parametrize("text", [
    "平安银行一年前买入10000股，盈利20%全部卖出",
    "平安银行1年前买10000股，涨20%卖出",
    "回测开始时买入10000股，盈利20%卖出",
    "首个交易日开盘买10000股，涨20%卖出",
])
@pytest.mark.parametrize("initial,opening", [(0, 0), (0, 10000), (100, 0)])
def test_start_purchase_cannot_disappear_or_become_opening_inventory(text, initial, opening):
    plan = ConditionalPlan(parameters={"initial_shares": initial, "opening_shares": opening,
        "rules": [{"kind": "take_profit", "side": "sell", "gap": 20,
                   "gap_unit": "percent", "sizing_mode": "all_position"}]})
    with pytest.raises(GridSpecificationError) as caught:
        validate_generated_plan(plan, utterance=text)
    assert caught.value.code == "initial_purchase_not_preserved"


def test_start_purchase_is_not_a_missing_buy_stage_and_does_not_copy_sell_quantity():
    plan = ConditionalPlan(parameters={"initial_shares": 1000, "rules": [
        {"kind": "take_profit", "side": "sell", "gap": 20, "quantity": 100}]})
    validate_generated_plan(plan, utterance="平安银行一年前买入1000股，盈利20%卖100股")
    validate_generated_plan(plan, utterance="平安银行一年前买入，盈利20%卖100股")
    assert plan.parameters.initial_shares == 1000
    assert plan.parameters.rules[0].quantity == 100


@pytest.mark.parametrize("text", [
    "回测近一年，盈利20%卖100股", "已有1000股，盈利20%卖出", "买了以后盈利20%卖出",
    "不要一年前买入，改为已有持仓", "不在首日买入", "一年前开始每月月初买入",
    "如果一年前买入会怎样", "卖出改为200股",
])
def test_range_hypothetical_negation_and_existing_inventory_do_not_invent_a_purchase(text):
    plan = ConditionalPlan(parameters={"rules": [
        {"kind": "take_profit", "side": "sell", "gap": 20}]})
    validate_generated_plan(plan, utterance=text)
    assert plan.parameters.initial_shares == 0


def test_once_schedule_preserves_start_purchase_amount_or_explicit_shares():
    from ashare_lab.domain.strategy.price_plans import ScheduledPlan
    plan = ScheduledPlan(parameters={"frequency": "once", "side": "buy",
        "sizing_mode": "amount", "budget_cny": 10000})
    validate_generated_plan(plan, utterance="一年前买入一万元，盈利20%卖出")
    with pytest.raises(GridSpecificationError):
        validate_generated_plan(plan, utterance="一年前买入10000股，盈利20%卖出")
    shares = ScheduledPlan(parameters={"frequency": "once", "side": "buy",
        "sizing_mode": "shares", "quantity": 10000})
    validate_generated_plan(shares, utterance="一年前买入10000股，盈利20%卖出")


def test_unrequested_daily_protection_returns_actionable_repair_guidance():
    from ashare_lab.adapters.language.vibe_candidates import (
        _candidate_repair_hints, _safe_validation_reason,
    )
    reason = _safe_validation_reason(ValueError("daily position-return observation lacks lexical evidence"))
    assert reason == "daily_protection_not_requested"
    hints = " ".join(_candidate_repair_hints([reason]))
    assert "initial_shares" in hints and "take_profit" in hints
    assert "不要" in hints and "持有1日" in hints


def staged_plan(first="sell", *, group=None):
    return ConditionalPlan(parameters={"rules": [
        {"kind": "relative_price", "side": side,
         "direction": "up" if side == "sell" else "down", "gap": 1,
         "gap_unit": "cny", "quantity": 100, "group": group,
         "reference_mode": "first_observation" if index == 0 else "previous_fill"}
        for index, side in enumerate([first, "buy" if first == "sell" else "sell"])
    ]})


@pytest.mark.parametrize("text", [
    "东方财富涨1卖，跌1买", "东方财富跌1买，涨1卖",
    "贵州茅台网格1%买卖", "东方财富涨1元卖，跌1元买",
    "东方财富跌3%买，涨1%卖", "东方财富涨1元卖，然后跌1元买",
    "先看看东方财富，涨1卖跌1买，然后回测",
    "不是先卖后买，涨1卖跌1买要独立触发",
    "不要阶段依赖，分别触发", "东方财富跌到18元买，涨到20元卖",
])
@pytest.mark.parametrize("first,group", [("sell", None), ("buy", None), ("sell", "oco")])
def test_unordered_conditions_reject_both_invented_sequences_and_oco(text, first, group):
    with pytest.raises(GridSpecificationError) as caught:
        validate_generated_plan(staged_plan(first, group=group), utterance=text)
    assert caught.value.code == "plan_order_not_grounded"


@pytest.mark.parametrize("text,first", [
    ("已有1000股，先涨1元卖100股，再跌1元买100股", "sell"),
    ("卖出成交后跌1元再买回", "sell"),
    ("先买后卖", "buy"),
    ("跌1元买入成交后，涨1元再卖出", "buy"),
    ("第一阶段涨1元卖，第二阶段跌1元买", "sell"),
    ("已有底仓，先涨1元卖；再跌1元买", "sell"),
    ("卖出后再买回", "sell"),
])
def test_explicit_trade_sequence_is_preserved_and_reversal_rejected(text, first):
    plan = staged_plan(first)
    validate_generated_plan(plan, utterance=text)
    with pytest.raises(GridSpecificationError) as caught:
        validate_generated_plan(staged_plan("buy" if first == "sell" else "sell"), utterance=text)
    assert caught.value.code == "plan_order_mismatch"


def test_explicit_sequence_cannot_be_replaced_by_mutually_exclusive_legs():
    with pytest.raises(GridSpecificationError) as caught:
        validate_generated_plan(staged_plan(group="oco"), utterance="先卖后买")
    assert caught.value.code == "plan_order_mismatch"


def test_existing_plan_and_quantity_only_edit_are_not_silently_migrated():
    plan = staged_plan()
    original = plan.model_dump()
    validate_generated_plan(plan)
    validate_generated_plan(plan, utterance="每次改为200股")
    assert plan.model_dump() == original


def test_cost_protection_after_purchase_and_oco_exit_are_preserved():
    plan = ConditionalPlan(parameters={"rules": [
        {"kind": "price", "side": "buy", "target_price": 18, "direction": "down"},
        {"kind": "take_profit", "side": "sell", "gap": 5, "gap_unit": "percent", "group": "exit"},
        {"kind": "stop_loss", "side": "sell", "gap": 3, "gap_unit": "percent", "group": "exit"},
    ]})
    validate_generated_plan(plan, utterance="18元买入，盈利5%或亏损3%卖出")


class RepairTransport:
    def __init__(self, text, corrected, *, repeat_invalid=False):
        self.text = text
        self.corrected = corrected
        self.repeat_invalid = repeat_invalid
        self.requests = []

    async def generate_json(self, request):
        self.requests.append(request)
        if request.response_schema_name == "strategy_semantic_review":
            return {"instrument": "equivalent", "requested_bar_interval": "unspecified",
                "differences": [], "requirements": [{"status": "represented",
                    "candidate_path": "/trading_plan", "source_quote": self.text,
                    "requested_meaning": "双向网格", "candidate_meaning": "双向网格"}]}
        plan = (staged_plan().model_dump(mode="json") if len(self.requests) == 1
                or self.repeat_invalid else self.corrected)
        return {"candidates": [{"confidence": .99, "entry": [], "exit": [],
            "trading_plan": plan,
            "plan_span": {"start": 0, "end": len(self.text), "text": self.text}}]}


@pytest.mark.asyncio
@pytest.mark.parametrize("text,mode,buy_gap,sell_gap", [
    ("东方财富涨1卖，跌1买", "cny", 1, 1),
    ("东方财富跌1买，涨1卖", "cny", 1, 1),
    ("贵州茅台网格1%买卖", "anchor_percent", 1, 1),
    ("东方财富涨1%卖，跌3%买", "anchor_percent", 3, 1),
])
async def test_bad_order_is_repaired_without_changing_units_or_inventing_holdings(
    text, mode, buy_gap, sell_gap,
):
    corrected = {"kind": "grid", "parameters": {
        "anchor_mode": "previous_close", "anchor_price": None,
        "lower_price": .01, "upper_price": 1000000,
        "anchor_update": "last_trigger", "startup_mode": "wait_for_crossing",
        "buy_spacing_mode": mode, "buy_spacing": buy_gap,
        "sell_spacing_mode": mode, "sell_spacing": sell_gap,
        "initial_shares": 0, "opening_shares": 0,
    }}
    transport = RepairTransport(text, corrected)
    candidate = (await VibeBoundedCandidateGenerator(
        transport, capability_matrix=CAPABILITY_MATRIX,
        model_semantic_review=True, repair_invalid_output=True,
    ).generate(CompileInput(text, instrument_context="300059.SZ", as_of_date=date(2026, 9, 14))))[0]
    assert candidate.unsupported_code is None
    assert candidate.trading_plan.kind == "grid"
    params = candidate.trading_plan.parameters
    assert params.spacing_for("buy") == (mode, buy_gap)
    assert params.spacing_for("sell") == (mode, sell_gap)
    assert params.initial_shares == params.opening_shares == 0
    assert params.anchor_price is None
    assert "plan_order_not_grounded" in str(transport.requests[1])
    assert len(transport.requests) == 3  # invalid generation, bounded repair, semantic review


@pytest.mark.asyncio
async def test_repeated_invented_order_cannot_pass_even_with_approving_semantic_reviewer():
    text = "东方财富涨1卖，跌1买"
    transport = RepairTransport(text, {}, repeat_invalid=True)
    result = (await VibeBoundedCandidateGenerator(
        transport, capability_matrix=CAPABILITY_MATRIX,
        model_semantic_review=True, repair_invalid_output=True,
    ).generate(CompileInput(text, instrument_context="300059.SZ", as_of_date=date(2026, 9, 14))))[0]
    assert result.unsupported_code is not None
    assert result.trading_plan is None
    assert len(transport.requests) == 2


@pytest.mark.parametrize("path,value,accepted", [
    ("/trading_plan/parameters/max_shares", 10000, True),
    ("/trading_plan/parameters/max_shares", 9000, False),
    ("/trading_plan/parameters/does_not_exist", 10000, False),
])
@pytest.mark.asyncio
async def test_only_real_schema_default_labels_are_compatible(path, value, accepted):
    from tests.unit.adapters.language.test_vibe_candidates import _bounded, _FakeTransport
    text = "以20元为基准，每跌1元买100股，每涨1元卖100股"
    payload = {"candidates": [{"confidence": .99, "entry": [], "exit": [],
        "trading_plan": {"kind": "grid", "parameters": {
            "anchor_price": 20, "spacing": 1, "spacing_mode": "cny",
            "lower_price": 1, "upper_price": 100, "max_shares": value}},
        "plan_span": {"start": 0, "end": len(text), "text": text},
        "defaulted_fields": [path]}]}
    result = (await _bounded(_FakeTransport(payload)).generate(CompileInput(
        text, instrument_context="300059.SZ", as_of_date=date(2026, 9, 14))))[0]
    assert (result.unsupported_code is None) == accepted
    if accepted:
        assert result.trading_plan.parameters.max_shares == value
        assert result.defaulted_fields == ()
