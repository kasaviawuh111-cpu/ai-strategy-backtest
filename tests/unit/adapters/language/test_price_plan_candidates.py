from datetime import date

import pytest

from ashare_lab.ports.candidate_generation import CompileInput
from tests.unit.adapters.language.test_vibe_candidates import _bounded, _FakeTransport


@pytest.mark.parametrize("route", ["candidate", "bound_idea", "unbound_idea"])
def test_model_grid_defaults_match_new_plan_guidance_without_migrating_saved_schema(route):
    from ashare_lab.adapters.language.vibe_candidates import _bounded_response_schema
    from ashare_lab.adapters.language.vibe_ideas import _idea_response_schema
    from ashare_lab.domain.strategy.price_plans import GridParameters
    from tests.unit.adapters.language.test_vibe_candidates import CAPABILITY_MATRIX

    schema = (_bounded_response_schema(CAPABILITY_MATRIX) if route == "candidate" else
              _idea_response_schema(require_strategy=True, unbound=route == "unbound_idea"))
    grid = schema["$defs"]["GridParameters"]
    for name, expected in {"anchor_mode": "previous_close", "anchor_update": "last_trigger", "startup_mode": "wait_for_crossing",
                           "price_mode": "grid_limit", "limit_offset_cny": "0"}.items():
        assert grid["properties"][name]["default"] == expected
        assert name in grid["required"]
    assert schema["$defs"]["ScheduledParameters"]["properties"]["budget_cny"]["default"] == "10000"
    persisted = GridParameters.model_json_schema()["properties"]
    assert persisted["anchor_mode"]["default"] == "manual"
    assert persisted["startup_mode"]["default"] == "catch_up"
    assert persisted["price_mode"]["default"] == "next_open"
    assert persisted["anchor_update"]["default"] == "fixed"
    assert {"initial_shares", "opening_shares"} <= set(grid["required"])


@pytest.mark.asyncio
@pytest.mark.parametrize("amount,expected", [(None, 10000), (1000, 1000), (25000, 25000)])
@pytest.mark.parametrize("review", [False, True])
async def test_new_scheduled_budget_suggestion_never_replaces_explicit_amount(amount, expected, review):
    from ashare_lab.adapters.language.vibe_candidates import VibeBoundedCandidateGenerator
    from tests.unit.adapters.language.test_vibe_candidates import CAPABILITY_MATRIX
    text = "东方财富月定投" + (f"{amount}元" if amount is not None else "")
    parameters = {"frequency": "monthly", "sizing_mode": "amount"}
    if amount is not None:
        parameters["budget_cny"] = amount
    payload = {"candidates": [{
        "confidence": .99, "entry": [], "exit": [],
        "trading_plan": {"kind": "scheduled", "parameters": parameters},
        "plan_span": {"start": 0, "end": len(text), "text": text},
    }]}
    class Transport:
        async def generate_json(self, request):
            if request.response_schema_name == "strategy_semantic_review":
                return {"instrument": "equivalent", "requested_bar_interval": "unspecified",
                    "differences": [], "requirements": [{"status": "represented",
                        "candidate_path": "/trading_plan", "source_quote": text,
                        "requested_meaning": "月定投", "candidate_meaning": "月定投"}]}
            return payload
    generator = VibeBoundedCandidateGenerator(Transport(), capability_matrix=CAPABILITY_MATRIX,
                                             model_semantic_review=review)
    candidate = (await generator.generate(CompileInput(
        text, instrument_context="300059.SZ", as_of_date=date(2026, 9, 11))))[0]
    assert candidate.unsupported_code is None
    assert candidate.trading_plan.parameters.budget_cny == expected
    assert ("/trading_plan/parameters/budget_cny" in candidate.defaulted_fields) == (amount is None)


def test_new_budget_defaults_do_not_migrate_saved_plans_or_share_sizing():
    from ashare_lab.domain.strategy.price_plans import ScheduledPlan, with_new_strategy_defaults
    old = ScheduledPlan(parameters={"frequency": "monthly"})
    assert old.parameters.budget_cny == 1000
    saved = ScheduledPlan.model_validate(old.model_dump(mode="json"))
    assert with_new_strategy_defaults(saved).parameters.budget_cny == 1000
    shares = ScheduledPlan(parameters={"frequency": "monthly", "sizing_mode": "shares", "quantity": 100})
    assert with_new_strategy_defaults(shares) is shares


def test_plan_repair_feedback_keeps_known_paths_and_reasons_without_provider_content():
    from pydantic import ValidationError
    from ashare_lab.adapters.language.vibe_candidates import _candidate_schema_feedback
    exc = ValidationError.from_exception_data("Candidate", [
        {"type": "value_error", "loc": ("candidates", 0, "trading_plan", "grid", "parameters"),
         "input": {"private": "must-not-leak"},
         "ctx": {"error": ValueError("初始建仓和最小底仓均不得超过最大持仓")}},
        {"type": "value_error", "loc": ("unknown-provider-key",), "input": "must-not-leak",
         "ctx": {"error": ValueError("private-provider-message")}},
    ])
    feedback = _candidate_schema_feedback(exc)
    assert "candidates/0/trading_plan/grid/parameters:value_error" in feedback
    assert "初始建仓和最小底仓均不得超过最大持仓" in feedback
    assert "unknown-provider-key" not in feedback
    assert "private-provider-message" not in feedback
    assert "must-not-leak" not in feedback


@pytest.mark.asyncio
@pytest.mark.parametrize("quoted_code,model_code,resolved_code,expected", [
    ("300059.SZ", "300059.SZ", "300059.SZ", None),
    ("300059.sz", "300059.SZ", "300059.SZ", None),
    ("300059", "300059.SZ", "300059.SZ", None),
    ("1300059", "300059.SZ", "300059.SZ", "candidate_provider_invalid_output"),
    ("300059.SH", "300059.SZ", "300059.SZ", "candidate_provider_invalid_output"),
    ("", "300059.SZ", "300059.SZ", "candidate_provider_invalid_output"),
    ("300803.SZ", "300059.SZ", "300059.SZ", "candidate_provider_invalid_output"),
    ("300803.SZ", "300803.SZ", "300059.SZ", "instrument_context_mismatch"),
])
async def test_name_and_explicit_code_require_source_and_security_lookup_agreement(
    quoted_code, model_code, resolved_code, expected,
):
    from ashare_lab.adapters.language.vibe_candidates import HybridCandidateGenerator
    from ashare_lab.adapters.language.rule_based import RuleBasedCandidateGenerator
    text = f"东方财富{quoted_code}，基准20元，每跌1元买100股，每涨1元卖100股"
    transport = _FakeTransport({"candidates": [{
        "confidence": .99, "entry": [], "exit": [],
        "instrument_name": "东方财富", "instrument_symbol": model_code,
        "instrument_span": {"start": 0, "end": 4, "text": "东方财富"},
        "trading_plan": {"kind": "grid", "parameters": {
            "anchor_mode": "manual", "anchor_price": 20,
            "lower_price": .01, "upper_price": 1000000,
            "spacing_mode": "cny", "spacing": 1,
        }}, "plan_span": {"start": 0, "end": len(text), "text": text},
    }]})
    generator = HybridCandidateGenerator(
        deterministic=RuleBasedCandidateGenerator(), bounded_fallback=_bounded(transport),
        model_first=True, instrument_name_resolver=lambda name: resolved_code,
    )
    result = (await generator.generate(CompileInput(text, as_of_date=date(2026, 9, 11))))[0]
    assert result.unsupported_code == expected
    if expected is None:
        assert result.instrument_symbol == resolved_code
        assert result.trading_plan.parameters.spacing == 1
    elif expected == "instrument_context_mismatch":
        assert result.instrument_symbol is None


@pytest.mark.asyncio
@pytest.mark.parametrize("interval,observation,accepted", [
    ("1m", "minute_bar", True), ("1m", "daily_close", False),
    ("intraday", "minute_bar", True), ("other_intraday", "minute_bar", False),
    ("tick", "minute_bar", False),
])
async def test_minute_grid_review_keeps_exact_execution_period(interval, observation, accepted):
    from ashare_lab.adapters.language.vibe_candidates import VibeBoundedCandidateGenerator
    from tests.unit.adapters.language.test_vibe_candidates import CAPABILITY_MATRIX
    text = "按分钟网格，每跌1元买入每涨1元卖出"
    class Transport:
        async def generate_json(self, request):
            if request.response_schema_name == "strategy_semantic_review":
                return {"instrument": "equivalent", "requested_bar_interval": interval,
                    "differences": [], "requirements": [{"status": "represented",
                        "candidate_path": "/trading_plan", "source_quote": text,
                        "requested_meaning": "分钟网格", "candidate_meaning": "分钟网格"}]}
            return {"candidates": [{"confidence": .99, "entry": [], "exit": [],
                "trading_plan": {"kind": "grid", "parameters": {
                    "anchor_mode": "first_open", "lower_price": .01, "upper_price": 1000000,
                    "observation": observation, "spacing_mode": "cny", "spacing": 1}},
                "plan_span": {"start": 0, "end": len(text), "text": text}}]}
    candidates = await VibeBoundedCandidateGenerator(Transport(), capability_matrix=CAPABILITY_MATRIX,
        model_semantic_review=True).generate(CompileInput(text, instrument_context="300059.SZ",
                                                        as_of_date=date(2026, 9, 11)))
    assert (candidates[0].unsupported_code is None) == accepted
    if accepted:
        assert candidates[0].trading_plan.parameters.observation == "minute_bar"


@pytest.mark.asyncio
async def test_identity_uncertainty_without_any_selected_stock_preserves_grid_rules():
    from ashare_lab.adapters.language.vibe_candidates import VibeBoundedCandidateGenerator
    from tests.unit.adapters.language.test_vibe_candidates import CAPABILITY_MATRIX
    text = "网格每跌1元买入，每涨1元卖出"
    class Transport:
        async def generate_json(self, request):
            if request.response_schema_name == "strategy_semantic_review":
                return {"instrument": "uncertain", "requested_bar_interval": "unspecified",
                    "differences": [], "requirements": [{"status": "represented",
                        "candidate_path": "/trading_plan", "source_quote": text,
                        "requested_meaning": "跌1元买涨1元卖", "candidate_meaning": "跌1元买涨1元卖"}]}
            return {"candidates": [{"confidence": .99, "entry": [], "exit": [],
                "trading_plan": {"kind": "grid", "parameters": {
                    "anchor_mode": "first_open", "lower_price": .01, "upper_price": 1000000,
                    "spacing_mode": "cny", "spacing": 1}},
                "plan_span": {"start": 0, "end": len(text), "text": text}}]}
    candidates = await VibeBoundedCandidateGenerator(Transport(), capability_matrix=CAPABILITY_MATRIX,
        model_semantic_review=True).generate(CompileInput(text, as_of_date=date(2026, 9, 11)))
    assert candidates[0].instrument_symbol is None
    assert candidates[0].unsupported_code is None
    assert candidates[0].trading_plan.parameters.spacing_mode == "cny"
    assert candidates[0].trading_plan.parameters.spacing == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("anchor_mode", ["latest_price", "previous_close"])
async def test_latest_quote_is_not_a_model_generated_price(anchor_mode):
    basis = "行情最新价" if anchor_mode == "latest_price" else "回测起始日昨收价"
    utterance = f"指南针网格，基准价按{basis}，间距1%，每格买入100股，卖出100股"
    transport = _FakeTransport({"candidates": [{
        "confidence": .99, "entry": [], "exit": [],
        "trading_plan": {"kind": "grid", "parameters": {
            "anchor_mode": anchor_mode, "anchor_price": 85,
            "anchor_quote_response_sha256": "made-up",
            "spacing_mode": "anchor_percent", "spacing": 1,
            "lower_price": .01, "upper_price": 1000000,
        }}, "plan_span": {"start": 0, "end": len(utterance), "text": utterance},
    }]})
    candidates = await _bounded(transport).generate(CompileInput(
        utterance, instrument_context="300803.SZ", as_of_date=date(2026, 9, 11)))
    p = candidates[0].trading_plan.parameters
    assert p.anchor_mode == anchor_mode and p.anchor_price is None
    assert p.anchor_quote_response_sha256 is None
    assert p.spacing == 1 and p.spacing_mode == "anchor_percent"


@pytest.mark.parametrize(("mode", "unit"), [("cny", "元"), ("anchor_percent", "%")])
@pytest.mark.asyncio
async def test_price_plan_keeps_unit_and_ignores_indicator_only_default_annotations(mode, unit):
    utterance = f"以20元为基准，每跌1{unit}买100股，每涨1{unit}卖100股"
    transport = _FakeTransport({"candidates": [{
        "confidence": .99, "entry": [], "exit": [],
        "trading_plan": {"kind": "grid", "parameters": {
            "anchor_price": 20, "spacing": 1, "spacing_mode": mode,
            "lower_price": 1, "upper_price": 100,
        }},
        "plan_span": {"start": 0, "end": len(utterance), "text": utterance},
        "defaulted_fields": ["/trading_plan/parameters/max_shares"],
    }]})
    candidates = await _bounded(transport).generate(CompileInput(
        utterance, instrument_context="300059.SZ", as_of_date=date(2026, 9, 8),
    ))
    plan = candidates[0].trading_plan
    assert plan is not None, candidates
    assert plan.parameters.spacing_mode == mode
    assert plan.parameters.spacing == 1
    assert candidates[0].defaulted_fields == ()
    assert candidates[0].grounding_evidence[0].path == "/trading_plan"
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_asymmetric_candidate_keeps_both_directions():
    utterance = "指南针网格，1%卖，3%买"
    transport = _FakeTransport({"candidates": [{
        "confidence": .99, "entry": [], "exit": [],
        "trading_plan": {"kind": "grid", "parameters": {
            "anchor_mode": "first_open", "lower_price": .01, "upper_price": 1000000,
            "buy_spacing": 3, "buy_spacing_mode": "anchor_percent",
            "sell_spacing": 1, "sell_spacing_mode": "anchor_percent",
        }}, "plan_span": {"start": 0, "end": len(utterance), "text": utterance},
    }]})
    candidates = await _bounded(transport).generate(CompileInput(
        utterance, instrument_context="300803.SZ", as_of_date=date(2026, 9, 9),
    ))
    p = candidates[0].trading_plan.parameters
    assert p.spacing_for("buy") == ("anchor_percent", 3)
    assert p.spacing_for("sell") == ("anchor_percent", 1)


@pytest.mark.asyncio
async def test_sell_first_t_candidate_keeps_opening_inventory_order_and_reference_chain():
    utterance = (
        "东方财富300059.SZ已有1000股可卖底仓，先涨1元卖100股，"
        "再跌1元买回100股，至少保留500股"
    )
    transport = _FakeTransport({"candidates": [{
        "confidence": .99, "entry": [], "exit": [],
        "instrument_name": "东方财富", "instrument_symbol": "300059.SZ",
        "instrument_span": {"start": 0, "end": 4, "text": "东方财富"},
        "trading_plan": {"kind": "conditional", "parameters": {
            "observation": "minute_bar", "opening_shares": 1000,
            "initial_shares": 0, "initial_capital_scope": "total_equity",
            "min_shares": 500, "max_shares": 10000,
            "rules": [
                {"kind": "relative_price", "side": "sell", "direction": "up",
                 "gap": 1, "gap_unit": "cny", "quantity": 100,
                 "reference_mode": "first_observation"},
                {"kind": "relative_price", "side": "buy", "direction": "down",
                 "gap": 1, "gap_unit": "cny", "quantity": 100,
                 "reference_mode": "previous_fill"},
            ],
        }},
        "plan_span": {"start": 0, "end": len(utterance), "text": utterance},
    }]})
    candidate = (await _bounded(transport).generate(CompileInput(
        utterance, instrument_context="300059.SZ", as_of_date=date(2026, 9, 11),
    )))[0]
    assert candidate.unsupported_code is None
    params = candidate.trading_plan.parameters
    assert params.opening_shares == 1000 and params.initial_shares == 0
    assert params.min_shares == 500
    assert [(rule.side, rule.reference_mode) for rule in params.rules] == [
        ("sell", "first_observation"), ("buy", "previous_fill"),
    ]
