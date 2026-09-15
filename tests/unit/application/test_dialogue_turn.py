from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from ashare_lab.adapters.language import RuleBasedCandidateGenerator
from ashare_lab.application.compile_strategy import CompileOutcome, CompileStatus, StrategyCompiler
from ashare_lab.application.dialogue_state import DialogueState
from ashare_lab.application.dialogue_turn import DialogueTurnOrchestrator, _resolved_instrument_memory
from ashare_lab.application.turn_intent import TurnIntent
from ashare_lab.domain.catalog import load_catalog_directory
from ashare_lab.domain.strategy import IndicatorCondition
from ashare_lab.ports.candidate_generation import CompileInput
from ashare_lab.ports.clarification_dialogue import (
    ClarificationDialogueAssessment,
    ClarificationDialogueRequest,
    ClarificationDialogueRouter,
)
from ashare_lab.ports.idea_routing import (
    IdeaAssetMapping,
    IdeaProposal,
    IdeaRoute,
    UnboundIdeaStrategy,
)

ROOT = Path(__file__).parents[3]


@pytest.mark.parametrize("label", ["东方财富300059.SZ", "东方财富（300059）", "300059.sz 东方财富"])
def test_verified_name_code_label_keeps_only_name_for_display(label: str) -> None:
    memory = _resolved_instrument_memory(instrument="300059.SZ", target=label, source="test")
    assert memory.name == "东方财富"
    assert memory.symbol == "300059.SZ"
    assert memory.evidence == label


def _resolve_instrument(name: str) -> str:
    if name == "同花顺":
        return "300033.SZ"
    raise LookupError(name)


def _compiler(dialogue: ClarificationDialogueRouter | None = None) -> StrategyCompiler:
    return StrategyCompiler(
        generator=RuleBasedCandidateGenerator(),
        catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals",
        release_version="2026.09.01",
        instrument_name_resolver=_resolve_instrument,
        clarification_dialogue_router=dialogue,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [
    "同花顺300033.SZ", "同花顺（300033.SZ）", "同花顺 300033",
    "300033.SZ 同花顺", "300033（同花顺）", "同花顺300033.sz",
])
async def test_identity_label_requires_name_code_agreement(answer: str) -> None:
    assert await _compiler().resolve_instrument_context(answer, require_details=True) == "300033.SZ"


@pytest.mark.asyncio
async def test_identity_label_does_not_prefer_code_over_conflicting_name() -> None:
    compiler = _compiler()
    assert await compiler.resolve_instrument_context("同花顺300059.SZ") is None
    with pytest.raises(LookupError, match="instrument_name_code_mismatch"):
        await compiler.resolve_instrument_context("同花顺300059.SZ", require_details=True)
    with pytest.raises(LookupError):
        await compiler.resolve_instrument_context("不存在300059.SZ", require_details=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["casual", "data_query", "safety"])
async def test_pending_turn_uses_model_intent_and_preserves_rules(intent: str) -> None:
    contexts = []

    class Dialogue:
        async def classify_initial(self, answer, as_of_date, *, context=None):
            contexts.append(context)
            return intent

        async def assess(self, request):
            return ClarificationDialogueAssessment(
                reply_kind="off_topic", acknowledgement_id="light_redirect",
                natural_reply="我听到了，我们可以先聊聊。",
            )

    compiler = _compiler(Dialogue())
    compiler.edit_current_strategy = AsyncMock(
        side_effect=AssertionError("non-edit intent must not reach strategy editor"),
    )
    original = CompileInput(
        utterance="东方财富MACD金叉买入", instrument_context="300059.SZ",
        as_of_date=date(2026, 9, 4),
    )
    prior = CompileOutcome(
        status=CompileStatus.NEEDS_CLARIFICATION, diagnostic_code="exit_rule_not_recognized",
        clarification="什么时候卖出？",
    )
    state = DialogueState.project(
        draft_id=uuid4(), revision=1, compile_input=original, outcome=prior,
        created_at=datetime(2026, 9, 4, tzinfo=UTC), recent_turns=(),
    )
    plan = await DialogueTurnOrchestrator(compiler).plan(
        state=state, answer="我现在不想讨论这个价格了",
    )
    assert plan.intent.value == intent
    compiler.edit_current_strategy.assert_not_awaited()
    assert len(contexts) == 1
    assert contexts[0]["prior_utterance"] == original.utterance
    assert contexts[0]["instrument_context"] == "300059.SZ"
    assert contexts[0]["pending_question"] == "什么时候卖出？"
    if intent == "data_query":
        assert plan.clarification_turn is None
    else:
        turn = plan.clarification_turn
        assert turn is not None and not turn.revision_changed
        assert turn.compile_input is original
        assert turn.outcome.strategy is None and not turn.outcome.run_requested


@pytest.mark.asyncio
async def test_unsupported_supplement_carries_both_turns_and_verified_stock() -> None:
    compiler = _compiler()
    original = CompileInput(
        utterance="东方财富用5分钟K线，5均线上穿20均线买，下穿卖",
        instrument_context="300059.SZ", as_of_date=date(2026, 9, 4),
    )
    prior = CompileOutcome(status=CompileStatus.UNSUPPORTED,
                           diagnostic_code="non_daily_timeframe_not_supported")
    state = DialogueState.project(
        draft_id=uuid4(), revision=1, compile_input=original, outcome=prior,
        created_at=datetime(2026, 9, 4, tzinfo=UTC), recent_turns=(),
    )
    compiler.compile = AsyncMock(return_value=prior)
    plan = await DialogueTurnOrchestrator(compiler).plan(
        state=state, answer="只改成日线，其他不变", semantic_intent=TurnIntent.SUPPLEMENT,
    )
    compiler.compile.assert_awaited_once()
    request = compiler.compile.await_args.args[0]
    assert original.utterance in request.utterance
    assert "只改成日线，其他不变" in request.utterance
    assert request.instrument_context == "300059.SZ"
    assert request.as_of_date == original.as_of_date
    assert request.semantic_intent == "new_strategy"
    assert plan.intent is TurnIntent.SUPPLEMENT
    assert plan.clarification_turn.outcome.status is CompileStatus.UNSUPPORTED
    assert plan.clarification_turn.outcome.strategy is None


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["改成同花顺这个", "换成300033.SZ"])
async def test_explicit_change_replaces_only_instrument(answer: str) -> None:
    compiler = _compiler()
    original_input = CompileInput(
        utterance="东方财富 MACD 金叉买入，死叉卖出，回测近 1 年",
        instrument_context="300059.SZ",
        as_of_date=date(2026, 8, 27),
    )
    original_outcome = await compiler.compile(original_input)
    assert original_outcome.status is CompileStatus.READY
    assert original_outcome.strategy is not None
    state = DialogueState.project(
        draft_id=uuid4(),
        revision=2,
        compile_input=original_input,
        outcome=original_outcome,
        created_at=datetime(2026, 8, 27, tzinfo=UTC),
        recent_turns=(),
    )

    plan = await DialogueTurnOrchestrator(compiler).plan(state=state, answer=answer)

    assert plan.intent is TurnIntent.CHANGE_INSTRUMENT
    assert plan.clarification_turn is not None
    turn = plan.clarification_turn
    assert turn.revision_changed is True
    assert turn.compile_input.utterance == original_input.utterance
    assert turn.compile_input.as_of_date == original_input.as_of_date
    assert turn.compile_input.instrument_context == "300033.SZ"
    assert turn.outcome.status is CompileStatus.READY
    assert turn.outcome.strategy is not None
    assert turn.outcome.strategy.instrument.symbol == "300033.SZ"
    assert turn.outcome.strategy.entry == original_outcome.strategy.entry
    assert turn.outcome.strategy.exit == original_outcome.strategy.exit
    assert turn.outcome.strategy.execution == original_outcome.strategy.execution
    assert turn.outcome.strategy.backtest == original_outcome.strategy.backtest


@pytest.mark.asyncio
async def test_unresolved_instrument_change_keeps_existing_strategy() -> None:
    compiler = _compiler()
    original_input = CompileInput(
        utterance="东方财富 MACD 金叉买入，死叉卖出，回测近 1 年",
        instrument_context="300059.SZ",
        as_of_date=date(2026, 8, 27),
    )
    original_outcome = await compiler.compile(original_input)
    assert original_outcome.status is CompileStatus.READY
    state = DialogueState.project(
        draft_id=uuid4(),
        revision=2,
        compile_input=original_input,
        outcome=original_outcome,
        created_at=datetime(2026, 8, 27, tzinfo=UTC),
        recent_turns=(),
    )

    plan = await DialogueTurnOrchestrator(compiler).plan(
        state=state,
        answer="改成不存在证券这个",
    )

    assert plan.intent is TurnIntent.CHANGE_INSTRUMENT
    assert plan.clarification_turn is not None
    assert plan.clarification_turn.revision_changed is False
    assert plan.clarification_turn.compile_input is original_input
    assert plan.clarification_turn.outcome is original_outcome


async def _paired_idea_state() -> DialogueState:
    original = CompileInput(
        utterance="收盘价上穿20日均线买入，下穿20日均线卖出，回测近1年，股票还没想好",
        as_of_date=date(2026, 9, 4),
    )
    compiled = await _compiler().compile(CompileInput(
        utterance="股价上穿20日均线买入，下穿20日均线卖出，回测近1年",
        instrument_context="300308.SZ", as_of_date=original.as_of_date,
    ))
    assert compiled.strategy is not None
    template = UnboundIdeaStrategy.model_validate(
        compiled.strategy.model_dump(exclude={"instrument", "schema_version"}),
    )
    route = IdeaRoute(
        understanding="保留已给出的买卖规则，等待用户选择股票。",
        hypothesis="仅选择回测样本。",
        asset_mapping=IdeaAssetMapping(
            instrument_symbol=None, relation="unbound", evidence_status="instrument_required",
        ),
        proposals=tuple(IdeaProposal(
            id=f"idea_{index:012x}", title="按当前买卖规则", hypothesis="仅选择回测样本。",
            entry_summary="上穿20日均线买入", exit_summary="下穿20日均线卖出",
            suggested_utterance="股价上穿20日均线买入，下穿20日均线卖出，回测近1年",
            capability_ids=("technical.ma",), assumptions=(), confidence=0.9,
            instrument_symbol=symbol, instrument_name=name, pairing_reason=reason,
            strategy=template.bind(symbol), strategy_template=template,
        ) for index, (symbol, name, reason) in enumerate((
            ("300308.SZ", "中际旭创", "成交额173.78亿"),
            ("000977.SZ", "浪潮信息", "成交额137.08亿"),
            ("300502.SZ", "新易盛", "成交额117.22亿"),
        ), start=1)),
    )
    return DialogueState.project(
        draft_id=uuid4(), revision=1, compile_input=original,
        outcome=CompileOutcome(
            status=CompileStatus.NEEDS_CLARIFICATION, diagnostic_code="idea_guidance_required",
            clarification="想先用哪只股票？", idea_route=route,
        ),
        created_at=datetime(2026, 9, 4, tzinfo=UTC), recent_turns=(),
    )


class _CandidateDialogue:
    def __init__(self, result: ClarificationDialogueAssessment | None) -> None:
        self.result = result
        self.requests: list[ClarificationDialogueRequest] = []

    async def assess(
        self, request: ClarificationDialogueRequest,
    ) -> ClarificationDialogueAssessment | None:
        self.requests.append(request)
        return self.result


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["change_instrument", "data_query"])
@pytest.mark.parametrize("recommend", [True, False])
async def test_pending_stock_recommendation_is_independent_of_pause(
    intent: str, recommend: bool,
) -> None:
    base = await _paired_idea_state()
    assert base.outcome.idea_route is not None
    proposal = base.outcome.idea_route.proposals[0]
    outcome = replace(
        base.outcome, diagnostic_code="instrument_required", idea_route=None,
        selected_idea_proposal=replace(proposal, instrument_symbol=None, strategy=None),
        instrument_suggestion_declined=True,
    )
    state = DialogueState.project(
        draft_id=base.draft_id, revision=2, compile_input=base.compile_input,
        outcome=outcome, created_at=base.created_at, recent_turns=(),
    )

    class Dialogue(_CandidateDialogue):
        async def classify_initial(self, answer, as_of_date, *, context=None):
            return intent

    dialogue = Dialogue(ClarificationDialogueAssessment(
        reply_kind="question", acknowledgement_id="answer_question",
        natural_reply="先保留规则，暂不运行。",
        instrument_recommendation_requested=recommend,
        run_requested=False, run_request_evidence="先别跑",
    ))
    plan = await DialogueTurnOrchestrator(_compiler(dialogue)).plan(
        state=state, answer="帮我找只适合这条策略的股票，先别跑" if recommend else "先别跑",
    )
    turn = plan.clarification_turn
    assert turn is not None
    assert turn.outcome.instrument_suggestion_declined is not recommend
    assert turn.outcome.selected_idea_proposal is outcome.selected_idea_proposal
    assert turn.compile_input is state.compile_input
    assert not turn.outcome.run_requested
    assert not turn.outcome.pending_edit_run_requested
    assert turn.revision_changed is recommend


@pytest.mark.asyncio
@pytest.mark.parametrize("available", [True, False])
async def test_ready_reply_is_model_authored_without_changing_execution(available: bool) -> None:
    reply = "规则已准备好，你可以先核对买卖条件。"
    dialogue = _CandidateDialogue(ClarificationDialogueAssessment(
        reply_kind="unclear", acknowledgement_id="ask_rephrase", natural_reply=reply,
    ) if available else None)
    compiler = _compiler(dialogue)
    request = CompileInput(
        utterance="东方财富 MACD 金叉买入，死叉卖出，回测近 1 年",
        instrument_context="300059.SZ", as_of_date=date(2026, 8, 27),
    )
    outcome = await compiler.compile(request)
    assert outcome.status is CompileStatus.READY
    message = await compiler.compose_ready_response(answer=request.utterance, outcome=outcome)
    assert message == (reply if available else "买卖规则已准备好，可以核对；本次尚未执行回测。")
    assert len(dialogue.requests) == 1
    submitted = dialogue.requests[0]
    assert submitted.response_only and not submitted.options
    assert outcome.strategy is not None
    assert outcome.strategy.model_dump_json() in submitted.context_summary
    assert not outcome.run_requested


@pytest.mark.asyncio
async def test_model_option_selection_reuses_its_reply_without_another_model_call() -> None:
    state = await _paired_idea_state()
    assert state.outcome.idea_route is not None
    selected = state.outcome.idea_route.proposals[1]
    reply = "已选中浪潮信息这组规则，可以先核对。"
    dialogue = _CandidateDialogue(ClarificationDialogueAssessment(
        reply_kind="preference", acknowledgement_id="respect_preference", natural_reply=reply,
        selected_option_id=selected.id,
    ))
    turn = await _compiler(dialogue).answer_clarification(
        original_input=state.compile_input, prior_outcome=state.outcome,
        answer="就选第二组吧",
    )
    assert turn.outcome.status is CompileStatus.READY
    assert turn.outcome.strategy is not None
    assert turn.outcome.strategy.instrument.symbol == selected.instrument_symbol
    assert turn.assistant_message == reply
    assert len(dialogue.requests) == 1
    assert not dialogue.requests[0].response_only


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [
    "帮我从查到的里面挑三只比较适合试这个规则的，别把整张表贴出来。",
    "这些候选按已有数据比较一下，推荐最多三只就好。",
])
async def test_stored_candidate_comparison_uses_dialogue_and_preserves_templates(
    answer: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = await _paired_idea_state()
    route = state.outcome.idea_route
    assert route is not None
    order = tuple(item.id for item in reversed(route.proposals))
    message = "新易盛、浪潮信息、中际旭创可作为回测样本，依据是此前查询的成交额。"
    dialogue = _CandidateDialogue(ClarificationDialogueAssessment(
        reply_kind="question", acknowledgement_id="answer_question",
        natural_reply=message, recommended_option_ids=order,
    ))
    compiler = _compiler(dialogue)
    compile_spy = AsyncMock(side_effect=AssertionError("must reuse stored templates"))
    monkeypatch.setattr(compiler, "compile", compile_spy)

    plan = await DialogueTurnOrchestrator(compiler).plan(state=state, answer=answer)

    assert plan.intent is not TurnIntent.DATA_QUERY
    turn = plan.clarification_turn
    assert turn is not None and turn.assistant_message == message
    assert turn.outcome is state.outcome and turn.compile_input is state.compile_input
    assert not turn.revision_changed
    assert tuple(item.id for item in turn.suggestions) == order
    assert turn.outcome.strategy is None
    assert turn.outcome.idea_route is route
    compile_spy.assert_not_awaited()
    assert len(dialogue.requests) == 1
    request = dialogue.requests[0]
    assert request.allow_data_query
    for item in route.proposals:
        assert item.instrument_name in request.context_summary
        assert item.instrument_symbol in request.context_summary
        assert item.pairing_reason in request.context_summary


@pytest.mark.asyncio
async def test_new_metric_request_keeps_the_data_route_after_contextual_assessment() -> None:
    state = await _paired_idea_state()
    dialogue = _CandidateDialogue(ClarificationDialogueAssessment(
        reply_kind="question", acknowledgement_id="answer_question",
        natural_reply="需要补查最新换手率。", requires_new_data=True,
    ))
    plan = await DialogueTurnOrchestrator(_compiler(dialogue)).plan(
        state=state, answer="查询中际旭创最新换手率。",
    )
    assert plan.intent is TurnIntent.DATA_QUERY
    assert plan.clarification_turn is None
    assert len(dialogue.requests) == 1


@pytest.mark.asyncio
async def test_invalid_candidate_dialogue_does_not_fall_through_to_an_unrelated_lookup() -> None:
    state = await _paired_idea_state()
    dialogue = _CandidateDialogue(None)
    plan = await DialogueTurnOrchestrator(_compiler(dialogue)).plan(
        state=state, answer="帮我从查到的里面挑三只比较适合试这个规则的。",
    )
    assert plan.intent is not TurnIntent.DATA_QUERY
    turn = plan.clarification_turn
    assert turn is not None and not turn.revision_changed
    assert turn.outcome is state.outcome
    assert "重试" in turn.assistant_message


async def _unsupported_timeframe_state(compiler: StrategyCompiler) -> DialogueState:
    original = CompileInput(
        utterance="东方财富用5分钟K线，5均线上穿20均线买，下穿卖，测最近一年。",
        instrument_context="300059.SZ", as_of_date=date(2026, 9, 4),
    )
    outcome = await compiler.compile(original)
    assert outcome.status is CompileStatus.UNSUPPORTED
    assert outcome.diagnostic_code == "non_daily_timeframe_not_supported"
    assert outcome.strategy is None and outcome.idea_route is None
    return DialogueState.project(
        draft_id=uuid4(), revision=1, compile_input=original, outcome=outcome,
        created_at=datetime(2026, 9, 4, tzinfo=UTC), recent_turns=(),
    )


@pytest.mark.asyncio
async def test_daily_replacement_after_unsupported_timeframe_keeps_only_verified_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiler = _compiler()
    state = await _unsupported_timeframe_state(compiler)
    answer = "那就改成日线，5日均线上穿20日均线买，5日均线下穿20日均线卖，近一年。"
    compile_spy = AsyncMock(wraps=compiler.compile)
    monkeypatch.setattr(compiler, "compile", compile_spy)

    plan = await DialogueTurnOrchestrator(compiler).plan(state=state, answer=answer)

    turn = plan.clarification_turn
    assert plan.intent is TurnIntent.NEW_STRATEGY
    assert turn is not None and turn.revision_changed
    assert turn.compile_input.utterance == answer
    assert turn.compile_input.instrument_context == "300059.SZ"
    assert turn.outcome.status is CompileStatus.READY
    assert turn.outcome.idea_route is None
    strategy = turn.outcome.strategy
    assert strategy is not None and strategy.instrument.symbol == "300059.SZ"
    assert strategy.execution.evaluation_frequency == "1d_close"
    assert strategy.backtest.start == date(2025, 9, 4)
    assert strategy.backtest.end == date(2026, 9, 4)
    entry = strategy.entry
    exit_rule = strategy.exit.children[0]
    assert isinstance(entry, IndicatorCondition) and isinstance(exit_rule, IndicatorCondition)
    assert entry.indicator_id == exit_rule.indicator_id == "technical.ma_cross"
    assert entry.params == exit_rule.params
    assert entry.params["fast_period"] == 5 and entry.params["slow_period"] == 20
    assert entry.trigger == "golden_cross" and exit_rule.trigger == "death_cross"
    assert not turn.outcome.run_requested
    assert state.outcome.strategy is None and state.outcome.revision_base_strategy is None
    compile_spy.assert_awaited_once_with(turn.compile_input)


@pytest.mark.asyncio
async def test_repeated_intraday_request_stays_unsupported_with_verified_identity() -> None:
    compiler = _compiler()
    state = await _unsupported_timeframe_state(compiler)
    answer = "还是用5分钟K线，5均线上穿20均线买，下穿卖，近一年。"

    plan = await DialogueTurnOrchestrator(compiler).plan(state=state, answer=answer)

    turn = plan.clarification_turn
    assert turn is not None
    assert turn.compile_input.utterance == answer
    assert turn.compile_input.instrument_context == "300059.SZ"
    assert turn.outcome.status is CompileStatus.UNSUPPORTED
    assert turn.outcome.diagnostic_code == "non_daily_timeframe_not_supported"
    assert turn.outcome.strategy is None and turn.outcome.revision_base_strategy is None
    assert turn.outcome.idea_route is None and not turn.suggestions
    assert not turn.outcome.run_requested


@pytest.mark.asyncio
async def test_daily_replacement_can_explicitly_choose_another_stock() -> None:
    compiler = _compiler()
    state = await _unsupported_timeframe_state(compiler)

    plan = await DialogueTurnOrchestrator(compiler).plan(
        state=state,
        answer="那就改成日线，300033.SZ，5日均线上穿20日均线买，5日均线下穿20日均线卖，近一年。",
    )

    turn = plan.clarification_turn
    assert turn is not None and turn.outcome.status is CompileStatus.READY
    assert turn.outcome.strategy is not None
    assert turn.outcome.strategy.instrument.symbol == "300033.SZ"
    assert not turn.outcome.run_requested


@pytest.mark.asyncio
@pytest.mark.parametrize("diagnostic", ["idea_guidance_required", "candidate_data_incomplete", "instrument_required"])
async def test_current_card_id_bypasses_instrument_dialogue_even_when_data_failed(diagnostic):
    state = await _paired_idea_state()
    state = replace(state, outcome=replace(state.outcome, diagnostic_code=diagnostic))
    selected = state.outcome.idea_route.proposals[1]
    dialogue = _CandidateDialogue(None)
    compiler = _compiler(dialogue)
    compiler.classify_dialogue_intent = AsyncMock(side_effect=AssertionError("card must not be classified"))
    compiler.assess_instrument_clarification = AsyncMock(side_effect=AssertionError("card is not a stock answer"))
    result = await DialogueTurnOrchestrator(compiler).plan(state=state, answer=selected.id)
    assert result.intent is TurnIntent.SELECT_OPTION
    assert result.clarification_turn.outcome.status is CompileStatus.READY
    assert result.clarification_turn.outcome.strategy == selected.strategy
    assert not result.clarification_turn.outcome.run_requested
    assert dialogue.requests == []
