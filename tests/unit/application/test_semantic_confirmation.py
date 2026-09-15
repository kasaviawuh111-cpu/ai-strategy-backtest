from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from ashare_lab.application.compile_strategy import CompileOutcome, CompileStatus, StrategyCompiler
from ashare_lab.application.turn_intent import TurnIntent
from ashare_lab.domain.catalog import load_catalog_directory
from ashare_lab.domain.strategy import canonical_hash
from ashare_lab.ports.candidate_generation import (
    CandidateAst,
    CandidateGroundingEvidence,
    CompileInput,
    IndicatorIntent,
    PositionReturnIntent,
)
from ashare_lab.ports.clarification_dialogue import ClarificationDialogueAssessment

ROOT = Path(__file__).parents[3]
AS_OF_DATE = date(2026, 9, 6)
UTTERANCE = "300059.SZ，14日RSI低于30买入，高于70卖出"


def pending_candidate() -> CandidateAst:
    return CandidateAst(
        instrument_symbol="300059.SZ", confidence=0.95,
        entry=(IndicatorIntent("technical.rsi", "1.0.0", "below", (("period", 14),), 30),),
        exit=(IndicatorIntent("technical.rsi", "1.0.0", "above", (("period", 14),), 70),),
        unsupported_code="semantic_confirmation_required",
        semantic_review_issues=("卖出条件的触发含义仍需核对",),
    )


def make_compiler(*candidates: CandidateAst) -> StrategyCompiler:
    generator = Mock(generate=AsyncMock(side_effect=[(item,) for item in candidates]))
    return StrategyCompiler(
        generator=generator, catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01",
        trusted_date_provider=lambda: AS_OF_DATE,
    )


def request() -> CompileInput:
    return CompileInput(utterance=UTTERANCE, as_of_date=AS_OF_DATE, semantic_intent="new_strategy")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,mode,resolution,policy", [
    ("grid", "minute_bar", "1m", "next_bar_order_activation"),
    ("grid", "daily_close", "1d", "next_tradable_session_open"),
    ("grid", None, "1m", "next_bar_order_activation"),
    ("scheduled", "close", "1d", "scheduled_session_close"),
])
async def test_compilation_persists_plan_execution_without_changing_legacy_hash(kind, mode, resolution, policy):
    from ashare_lab.domain.strategy import DailyExecutionPolicy, StrategySpec
    from ashare_lab.domain.strategy.price_plans import GridPlan, GridParameters, ScheduledPlan, ScheduledParameters
    plan = (ScheduledPlan(parameters=ScheduledParameters(at=mode)) if kind == "scheduled" else
            GridPlan(parameters=GridParameters(anchor_price=20, observation=mode, lower_price=1, upper_price=100)))
    text = "300059.SZ按指定交易计划回测"
    candidate = CandidateAst(instrument_symbol="300059.SZ", confidence=.99, entry=(), exit=(), trading_plan=plan,
        grounding_evidence=(CandidateGroundingEvidence("/trading_plan", 0, len(text), text),))
    outcome = await make_compiler(candidate).compile(replace(request(), utterance=text))
    assert outcome.status is CompileStatus.READY
    assert outcome.strategy.execution.execution_resolution == resolution
    assert outcome.strategy.execution.entry_policy == policy
    restored = StrategySpec.model_validate_json(outcome.strategy.model_dump_json())
    assert restored == outcome.strategy
    legacy = outcome.strategy.model_copy(update={"execution": DailyExecutionPolicy(position_policy="bounded_inventory")})
    assert canonical_hash(StrategySpec.model_validate_json(legacy.model_dump_json())) == canonical_hash(legacy)


@pytest.mark.asyncio
async def test_missing_stock_keeps_grid_parameters_in_bindable_template():
    from ashare_lab.domain.strategy.price_plans import GridPlan, GridParameters
    from ashare_lab.ports.idea_routing import UnboundIdeaStrategy
    text = "网格策略，价格每下跌1元买入，每上涨1元卖出，最多持有10000股"
    plan = GridPlan(parameters=GridParameters(anchor_mode="first_open",
        lower_price=1, upper_price=1000000, spacing_mode="cny", spacing=1, max_shares=10000))
    candidate = CandidateAst(instrument_symbol=None, confidence=.99, entry=(), exit=(), trading_plan=plan,
        grounding_evidence=(CandidateGroundingEvidence("/trading_plan", 0, len(text), text),))
    compiler = make_compiler(candidate)
    outcome = await compiler.compile(replace(request(), utterance=text))
    assert outcome.diagnostic_code == "instrument_required"
    assert outcome.selected_idea_proposal is not None
    template = outcome.selected_idea_proposal.strategy_template
    assert template is not None
    # Persist and restore the same shape used by dialogue state, then select a stock.
    restored = UnboundIdeaStrategy.model_validate_json(template.model_dump_json())
    bound = restored.bind("300803.SZ")
    expected_plan = plan.model_copy(update={"parameters": plan.parameters.model_copy(
        update={"observation": "minute_bar"})})
    assert bound.trading_plan == expected_plan
    assert plan.parameters.observation is None  # Old persisted payload remains unchanged.
    assert bound.instrument.symbol == "300803.SZ"
    assert bound.entry is None and bound.exit is None
    selected = compiler.bind_selected_idea(
        replace(request(), utterance=text, instrument_context="300803.SZ"), outcome)
    assert selected is not None and selected.status is CompileStatus.READY
    assert selected.strategy.trading_plan == expected_plan
    assert selected.strategy.instrument.symbol == "300803.SZ"
    assert compiler._generator.generate.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("diagnostic", [
    "instrument_unconfirmed", "instrument_resolution_unavailable",
])
@pytest.mark.parametrize("result_kind", ["ready", "semantic_pending", "invalid_rule"])
async def test_identity_recovery_recompiles_once_without_bypassing_rule_checks(
    diagnostic: str, result_kind: str,
) -> None:
    candidate = pending_candidate()
    if result_kind == "ready":
        candidate = replace(candidate, unsupported_code=None, semantic_review_issues=())
    elif result_kind == "invalid_rule":
        candidate = replace(candidate, entry=(replace(candidate.entry[0], indicator_id="unknown"),))
    compiler = make_compiler(candidate)
    evidence = CandidateGroundingEvidence("/instrument/symbol", 0, 4, "东方财富")
    compiler.resolve_unsupported_instrument = AsyncMock(return_value=("300059.SZ", evidence))
    original = replace(request(), utterance="东方财富，14日RSI低于30买入，高于70卖出")
    failed = CompileOutcome(status=CompileStatus.UNSUPPORTED, diagnostic_code=diagnostic)

    effective, recovered = await compiler.recover_unsupported_identity(original, failed)

    assert effective.utterance == original.utterance
    assert effective.instrument_context == "300059.SZ"
    assert effective.resolved_instrument is not None
    assert effective.resolved_instrument.matches(effective)
    assert evidence in recovered.candidate_grounding
    assert compiler._generator.generate.await_count == 1
    assert compiler._generator.generate.await_args.args[0] == effective
    expected_status = {"ready": CompileStatus.READY,
                       "semantic_pending": CompileStatus.NEEDS_CLARIFICATION,
                       "invalid_rule": CompileStatus.UNSUPPORTED}[result_kind]
    assert recovered.status is expected_status
    assert not recovered.run_requested
    if result_kind == "semantic_pending":
        assert recovered.diagnostic_code == "semantic_confirmation_required"
        assert recovered.semantic_review_issues == candidate.semantic_review_issues
    elif result_kind == "invalid_rule":
        assert recovered.diagnostic_code == "candidate_provider_invalid_output"
        assert recovered.strategy is None and recovered.suggested_strategy is None
    # Calling the shared boundary again with the effective input cannot retry.
    repeated = await compiler.recover_unsupported_identity(effective, recovered)
    assert repeated == (effective, recovered)
    assert compiler._generator.generate.await_count == 1
    assert compiler.resolve_unsupported_instrument.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [
    "known_context", "semantic_confirmation", "unresolved", "other_failure",
])
async def test_identity_recovery_preserves_nonrecoverable_outcomes(case: str) -> None:
    compiler = make_compiler()
    original = replace(request(), utterance="东方财富用分钟线买卖")
    failed = CompileOutcome(
        status=CompileStatus.UNSUPPORTED, diagnostic_code="instrument_unconfirmed",
    )
    if case == "known_context":
        original = replace(original, instrument_context="300059.SZ")
    elif case == "semantic_confirmation":
        failed = replace(failed, status=CompileStatus.NEEDS_CLARIFICATION,
                         diagnostic_code="semantic_confirmation_required")
    elif case == "other_failure":
        failed = replace(failed, diagnostic_code="non_daily_timeframe_not_supported")
    evidence = CandidateGroundingEvidence("/instrument/symbol", 0, 4, "东方财富")
    compiler.resolve_unsupported_instrument = AsyncMock(
        return_value=None if case == "unresolved" else ("300059.SZ", evidence),
    )
    effective, recovered = await compiler.recover_unsupported_identity(original, failed)
    assert recovered.status == failed.status and recovered.diagnostic_code == failed.diagnostic_code
    assert recovered.strategy is None and not recovered.run_requested
    compiler._generator.generate.assert_not_awaited()
    if case == "known_context":
        compiler.resolve_unsupported_instrument.assert_not_awaited()
    if case in {"known_context", "unresolved"}:
        assert effective is original and recovered is failed
    else:
        compiler.resolve_unsupported_instrument.assert_awaited_once()
        assert effective.instrument_context == "300059.SZ"
        assert evidence in recovered.candidate_grounding
        # Resolving the stock cannot clear a semantic question or trigger a run.
        assert recovered.clarification == failed.clarification


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [
    "named", "no_stock", "negated", "multiple", "provider_failure", "wrong_span", "lookup_failure",
])
async def test_missing_candidate_stock_checks_original_intent_before_recommendations(case: str):
    utterance = {
        "no_stock": "涨停板打开就买入，第二天不涨停就卖出。",
        "negated": "不要东方财富，涨停板打开就买入，第二天不涨停就卖出。",
        "multiple": "东方财富或者贵州茅台，涨停板打开就买入，第二天不涨停就卖出。",
    }.get(case, "东方财富涨停板打开就买入，第二天不涨停就卖出。")
    missing_stock = replace(pending_candidate(), instrument_symbol=None,
                            unsupported_code=None, semantic_review_issues=(),
                            grounding_evidence=tuple(CandidateGroundingEvidence(
                                f"/{leg}/0", utterance.index(text),
                                utterance.index(text) + len(text), text,
                            ) for leg, text in (("entry", "涨停板打开就买入"),
                                                ("exit", "第二天不涨停就卖出"))))
    compiler = make_compiler(missing_stock, pending_candidate())
    selected = case not in {"no_stock", "negated", "multiple"}
    assessment = ClarificationDialogueAssessment(
        reply_kind="unclear" if case == "multiple" else "preference",
        acknowledgement_id="ask_rephrase" if case == "multiple" else "respect_preference",
        natural_reply="身份判定", instrument_selected=selected,
        instrument_name=("贵州茅台" if case == "wrong_span" else "东方财富") if selected else None,
    )
    router = Mock(assess=AsyncMock(return_value=None if case == "provider_failure" else assessment))
    resolver = Mock(return_value="300059.SZ")
    if case == "lookup_failure":
        resolver.side_effect = RuntimeError("private provider error")
    compiler._clarification_dialogue_router = router
    compiler._instrument_name_resolver = resolver
    original = replace(request(), utterance=utterance)
    missing = await compiler.compile(original)
    assert missing.diagnostic_code == "instrument_required"
    assert missing.selected_idea_proposal is not None
    effective, recovered = await compiler.recover_unsupported_identity(original, missing)
    assert effective.utterance == utterance and not recovered.run_requested
    assert router.assess.await_count == 1
    assert router.assess.await_args.args[0].answer == utterance
    assert router.assess.await_args.args[0].identity_only
    if case == "named":
        assert effective.instrument_context == "300059.SZ"
        assert effective.resolved_instrument.evidence.text == "东方财富"
        assert recovered.diagnostic_code == "semantic_confirmation_required"
        assert recovered.suggested_strategy.instrument.symbol == "300059.SZ"
        assert recovered.strategy is None  # Known stock does not approve disputed rules.
        assert compiler._generator.generate.await_count == 2
    else:
        assert effective.instrument_context is None
        assert recovered.selected_idea_proposal == missing.selected_idea_proposal
        assert compiler._generator.generate.await_count == 1
        if case in {"no_stock", "negated"}:
            assert recovered is missing  # Genuine missing stock keeps its normal offer path.
        else:
            assert recovered.diagnostic_code in {
                "instrument_unconfirmed", "instrument_resolution_unavailable",
            }
        if case != "lookup_failure":
            resolver.assert_not_called()


@pytest.mark.asyncio
async def test_semantic_disagreement_is_only_a_catalog_checked_preview() -> None:
    candidate = pending_candidate()
    outcome = await make_compiler(candidate).compile(request())
    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "semantic_confirmation_required"
    assert outcome.strategy is None and outcome.strategy_hash is None
    assert outcome.suggested_strategy is not None
    assert outcome.suggested_strategy_hash == canonical_hash(outcome.suggested_strategy)
    assert outcome.suggested_strategy.instrument.symbol == "300059.SZ"
    assert outcome.semantic_review_issues == candidate.semantic_review_issues
    assert candidate.semantic_review_issues[0] in outcome.clarification
    assert outcome.idea_route is None and outcome.suggested_strategy_choice_id is None
    assert outcome.revision_base_strategy is None and outcome.selected_idea_proposal is None
    assert not outcome.run_requested and not outcome.pending_edit_run_requested
    assert not outcome.refresh_data and not outcome.pending_edit_refresh_data


@pytest.mark.asyncio
async def test_missing_inventory_is_an_execution_prerequisite_not_a_semantic_difference() -> None:
    from ashare_lab.domain.strategy.price_plans import ConditionalPlan, ConditionParameters

    candidate = CandidateAst(
        instrument_symbol="300059.SZ", entry=(), exit=(), confidence=.95,
        trading_plan=ConditionalPlan(parameters=ConditionParameters.model_validate({
            "rules": [{"kind": "pullback", "side": "sell", "gap": 2}],
        })),
        unsupported_code="execution_prerequisite_required",
        semantic_review_issues=(
            "已识别卖出条件，但当前新策略没有买入规则或期初可卖持仓；"
            "卖出规则仍保留，补充买入规则或期初持仓后，将继续检查数据与执行条件。",
        ),
    )
    outcome = await make_compiler(candidate).compile(
        replace(request(), utterance="300059.SZ涨起来先拿着，回落2%再卖")
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "execution_prerequisite_required"
    assert outcome.strategy is None and outcome.suggested_strategy is not None
    assert "策略规则已识别并保留" in outcome.clarification
    assert "具体差异" not in outcome.clarification
    assert outcome.clarification.count("将继续检查数据与执行条件") == 1
    assert outcome.semantic_review_issues == candidate.semantic_review_issues


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    "missing_entry", "missing_exit", "missing_identity", "unresolved_name",
    "wrong_identity", "invalid_period", "unknown_indicator", "missing_issues",
])
async def test_semantic_preview_never_bypasses_structural_or_identity_checks(failure: str) -> None:
    candidate = pending_candidate()
    if failure == "missing_entry":
        candidate = replace(candidate, entry=())
    elif failure == "missing_exit":
        candidate = replace(candidate, exit=())
    elif failure == "missing_identity":
        candidate = replace(candidate, instrument_symbol=None)
    elif failure == "unresolved_name":
        candidate = replace(candidate, instrument_name="东方财富")
    elif failure == "wrong_identity":
        candidate = replace(candidate, instrument_symbol="600519.SH")
    elif failure == "invalid_period":
        invalid = replace(candidate.entry[0], params=(("period", -1),))
        candidate = replace(candidate, entry=(invalid,))
    elif failure == "unknown_indicator":
        candidate = replace(candidate, entry=(replace(candidate.entry[0], indicator_id="unknown"),))
    else:
        candidate = replace(candidate, semantic_review_issues=())
    outcome = await make_compiler(candidate).compile(request())
    assert outcome.status is not CompileStatus.READY
    assert outcome.suggested_strategy is None and outcome.suggested_strategy_hash is None
    assert outcome.strategy is None and not outcome.run_requested


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["rule", "remaining_issue"])
async def test_partial_clarification_preserves_stock_and_updates_same_diagnostic(
    change: str,
) -> None:
    original = pending_candidate()
    updated = (replace(original, exit=(replace(original.exit[0], value=65),))
               if change == "rule" else
               replace(original, semantic_review_issues=("现在只剩买入阈值需要核对",)))
    compiler = make_compiler(original, updated)
    initial = await compiler.compile(request())
    turn = await compiler.answer_clarification(
        original_input=request(), prior_outcome=initial,
        answer="卖出阈值改为65，其他不变", semantic_intent=TurnIntent.SUPPLEMENT,
    )
    assert turn.revision_changed
    assert turn.outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert turn.outcome.diagnostic_code == initial.diagnostic_code
    assert turn.compile_input.instrument_context == "300059.SZ"
    assert UTTERANCE in turn.compile_input.utterance
    assert "卖出阈值改为65，其他不变" in turn.compile_input.utterance
    assert turn.outcome.semantic_review_issues == updated.semantic_review_issues
    assert turn.outcome.suggested_strategy is not None
    assert turn.outcome.suggested_strategy.instrument.symbol == "300059.SZ"
    assert turn.outcome.strategy is None and not turn.outcome.run_requested


@pytest.mark.asyncio
@pytest.mark.parametrize("diagnostic", [
    "idea_guidance_required", "entry_rule_not_recognized", "exit_rule_not_recognized",
    "semantic_confirmation_required", "execution_prerequisite_required",
])
@pytest.mark.parametrize("reverse", [False, True])
async def test_model_supplements_receive_both_unabridged_user_turns(diagnostic, reverse):
    stops = "东方财富涨3个点就卖，亏2个点就割"
    entry = "买入条件用5日均线上穿20日均线，止盈止损保持刚才的设置，先不回测"
    original, answer = (entry, stops) if reverse else (stops, entry)
    candidate = CandidateAst(
        instrument_symbol="300059.SZ", confidence=0.95,
        entry=(IndicatorIntent(
            "technical.ma_cross", "1.0.0", "golden_cross",
            (("fast_period", 5), ("slow_period", 20), ("price_field", "close")),
        ),),
        exit=(PositionReturnIntent("take_profit", 3), PositionReturnIntent("stop_loss", 2)),
    )
    generator = Mock(generate=AsyncMock(return_value=(candidate,)))
    compiler = StrategyCompiler(
        generator=generator, catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01",
        trusted_date_provider=lambda: AS_OF_DATE,
    )
    turn = await compiler.answer_clarification(
        original_input=CompileInput(
            utterance=original, instrument_context="300059.SZ", as_of_date=AS_OF_DATE,
        ),
        prior_outcome=CompileOutcome(
            status=CompileStatus.NEEDS_CLARIFICATION, diagnostic_code=diagnostic,
            clarification="继续补充条件",
        ),
        answer=answer, semantic_intent=TurnIntent.SUPPLEMENT,
    )
    generator.generate.assert_awaited_once()
    interpreted = generator.generate.call_args.args[0]
    assert original in interpreted.utterance and answer in interpreted.utterance
    assert interpreted.semantic_intent == "new_strategy"
    assert turn.outcome.status is CompileStatus.READY
    assert turn.outcome.strategy.instrument.symbol == "300059.SZ"
    assert [(item.trigger, item.threshold_pct) for item in turn.outcome.strategy.exit.children] == [
        ("take_profit", 3), ("stop_loss", 2),
    ]
    assert not turn.outcome.run_requested
