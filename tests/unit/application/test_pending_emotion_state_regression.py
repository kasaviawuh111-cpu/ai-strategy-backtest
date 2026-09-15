"""Narrow state regressions with local fakes, not live/model-quality evidence."""

from dataclasses import replace
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from ashare_lab.adapters.language import RuleBasedCandidateGenerator
from ashare_lab.adapters.language.vibe_candidates import HybridCandidateGenerator
from ashare_lab.adapters.language.vibe_ideas import (
    _ProviderIdeaProposal,  # pyright: ignore[reportPrivateUsage]
    _to_proposal,  # pyright: ignore[reportPrivateUsage]
)
from ashare_lab.application.compile_strategy import CompileOutcome, CompileStatus, StrategyCompiler
from ashare_lab.application.dialogue_state import (
    DialogueState,
    DialogueTurn,
    VerifiedInstrumentMemory,
)
from ashare_lab.application.dialogue_turn import DialogueTurnOrchestrator
from ashare_lab.application.turn_intent import TurnIntent
from ashare_lab.domain.strategy import IndicatorCondition
from ashare_lab.ports.candidate_generation import CompileInput
from ashare_lab.ports.clarification_dialogue import (
    ClarificationDialogueAssessment,
    ClarificationDialogueRequest,
)
from ashare_lab.ports.current_fact_research import (
    CurrentFactResearchRequest,
    CurrentFactResearchResult,
    ResearchSource,
)
from ashare_lab.ports.idea_routing import UnboundIdeaStrategy
from tests.unit.application.test_idea_routing import (
    _compiler,  # pyright: ignore[reportPrivateUsage]
    _direct_idea_route,  # pyright: ignore[reportPrivateUsage]
    _RecordingIdeaRouter,  # pyright: ignore[reportPrivateUsage]
)

NOW = datetime(2026, 9, 4, tzinfo=UTC)
ORIGINAL = CompileInput(
    utterance="东方财富MACD金叉买入，不卖出",
    instrument_context="300059.SZ",
    as_of_date=date(2026, 9, 4),
)


class _Dialogue:
    def __init__(self, *, unavailable: bool = False) -> None:
        self.requests: list[ClarificationDialogueRequest] = []
        self.unavailable = unavailable

    async def assess(
        self, request: ClarificationDialogueRequest,
    ) -> ClarificationDialogueAssessment:
        self.requests.append(request)
        if self.unavailable:
            raise RuntimeError("fake reply transport failure")
        return ClarificationDialogueAssessment(
            reply_kind="off_topic", acknowledgement_id="light_redirect",
            natural_reply=("我听到了，但联网暂不可用，这是服务侧的问题；原条件保留。"
                           if request.diagnostic_code == "idea_research_unavailable" else
                           "我听到了，先聊聊这件事。原来的交易条件我会保留。"),
        )


class _Researcher:
    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode
        self.requests: list[CurrentFactResearchRequest] = []

    async def research(self, request: CurrentFactResearchRequest) -> CurrentFactResearchResult:
        self.requests.append(request)
        if self.mode == "error":
            raise RuntimeError("fake search transport failure")
        return CurrentFactResearchResult(
            provider="local_test_fixture", model="no_real_model", provider_response_id="fixture",
            query=request.query, purpose=request.purpose, as_of=request.as_of,
            summary="仅用于测试的来源摘要，不是真实联网验收。", facts=(),
            sources=(ResearchSource(
                source_id="fixture-source", title="Fixture source",
                url="https://example.test/fixture", publisher="Test fixture", published_at=None,
            ),) if self.mode == "ok" else (),
            unresolved_questions=(), retrieved_at=NOW,
            response_sha256="sha256:" + "a" * 64, search_call_count=1,
        )


async def _partial() -> tuple[StrategyCompiler, CompileOutcome, _RecordingIdeaRouter]:
    prior = await _compiler(generator=RuleBasedCandidateGenerator(), idea_router=None).compile(
        ORIGINAL,
    )
    assert prior.status is CompileStatus.NEEDS_CLARIFICATION
    assert prior.diagnostic_code == "exit_rule_not_recognized"
    assert prior.idea_route is not None
    assert all("MACD" in item.entry_summary for item in prior.idea_route.proposals)
    router = _RecordingIdeaRouter(_direct_idea_route())
    compiler = _compiler(generator=RuleBasedCandidateGenerator(), idea_router=router)
    compiler._clarification_dialogue_router = _Dialogue()  # pyright: ignore[reportPrivateUsage]
    return compiler, prior, router


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", [TurnIntent.CASUAL, TurnIntent.VIEWPOINT])
async def test_direct_emotional_aside_does_not_replace_pending_rule(intent: TurnIntent) -> None:
    compiler, prior, router = await _partial()
    answer = "我先吃点东西" if intent is TurnIntent.CASUAL else "我讨厌特朗普"
    turn = await compiler.answer_clarification(
        original_input=ORIGINAL, prior_outcome=prior, answer=answer,
        semantic_intent=intent,
    )
    evidence = {
        "intent": intent.value,
        "original": ORIGINAL.utterance,
        "after": turn.compile_input.utterance,
        "symbol": turn.compile_input.instrument_context,
        "diagnostic": turn.outcome.diagnostic_code,
        "revision_changed": turn.revision_changed,
        "idea_requests": [(r.utterance, r.semantic_intent) for r in router.requests],
    }
    print(evidence)
    assert turn.compile_input == ORIGINAL, evidence
    assert turn.outcome == prior, evidence
    assert not turn.revision_changed


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", [TurnIntent.CASUAL, TurnIntent.VIEWPOINT])
async def test_orchestrated_aside_keeps_verified_stock_and_pending_entry(
    intent: TurnIntent,
) -> None:
    compiler, prior, router = await _partial()
    state = DialogueState.project(
        draft_id=uuid4(), revision=1, compile_input=ORIGINAL, outcome=prior,
        created_at=NOW, recent_turns=(DialogueTurn(
            user_text=ORIGINAL.utterance, assistant_text="买入规则已保留，什么时候卖出？",
            intent="new_strategy", revision=1, created_at=NOW,
            verified_instrument=VerifiedInstrumentMemory(
                symbol="300059.SZ", source="initial_compile", verified_at=NOW,
                name="东方财富", evidence="东方财富",
            ),
        ),),
    )
    answer = "我先吃点东西" if intent is TurnIntent.CASUAL else "我讨厌特朗普"
    plan = await DialogueTurnOrchestrator(compiler).plan(
        state=state, answer=answer, semantic_intent=intent,
    )
    turn = plan.clarification_turn
    assert turn is not None
    evidence = {
        "intent": plan.intent.value, "after": turn.compile_input.utterance,
        "symbol": turn.compile_input.instrument_context,
        "diagnostic": turn.outcome.diagnostic_code,
        "revision_changed": turn.revision_changed,
        "lost_existing_options": turn.outcome.idea_route is None,
        "instrument_reuse_prompt": plan.pending_instrument_reuse is not None,
        "idea_request_count": len(router.requests),
    }
    print(evidence)
    assert turn.compile_input == ORIGINAL, evidence
    assert turn.outcome == prior, evidence
    assert not turn.revision_changed
    assert plan.pending_instrument_reuse is None


@pytest.mark.asyncio
@pytest.mark.parametrize("research_mode", ["ok", "error", "empty"])
async def test_researched_aside_preserves_state_and_later_supplement_recovers(
    research_mode: str,
) -> None:
    compiler, prior, ideas = await _partial()
    researcher = _Researcher(research_mode)
    dialogue = _Dialogue()
    compiler._current_fact_researcher = researcher  # pyright: ignore[reportPrivateUsage]
    compiler._clarification_dialogue_router = dialogue  # pyright: ignore[reportPrivateUsage]
    aside = await compiler.answer_clarification(
        original_input=ORIGINAL, prior_outcome=prior, answer="我讨厌特朗普",
        semantic_intent=TurnIntent.VIEWPOINT,
    )
    assert len(researcher.requests) == 1
    assert researcher.requests[0].query == "我讨厌特朗普"
    assert researcher.requests[0].instrument_context == "300059.SZ"
    assert researcher.requests[0].as_of.tzinfo is not None
    assert not ideas.requests
    assert aside.compile_input == ORIGINAL and aside.outcome == prior
    assert not aside.revision_changed and not aside.outcome.run_requested
    response = dialogue.requests[0]
    assert response.response_only and response.prior_utterance == ORIGINAL.utterance
    assert response.question != prior.clarification
    assert response.options == ()
    assert aside.suggestions
    assert "不是策略介绍" in response.context_summary
    assert "不要求复述已知规则" in response.context_summary
    assert "不要固定追加交易问题" in response.context_summary
    assert (response.research is not None) == (research_mode == "ok")
    if research_mode != "ok":
        assert "服务侧" in aside.assistant_message
    # The semantic-model path must now receive both unabridged turns, including
    # the superseded "不卖出". An offline keyword parser cannot stand in for
    # contextual interpretation; fixture only the model's resolved candidate.
    resolved = await RuleBasedCandidateGenerator().generate(replace(
        ORIGINAL, utterance="东方财富MACD金叉买入，MACD死叉卖出",
    ))
    model_generate = AsyncMock(return_value=resolved)
    compiler._generator.generate = model_generate  # pyright: ignore[reportPrivateUsage]
    recovered = await compiler.answer_clarification(
        original_input=aside.compile_input, prior_outcome=aside.outcome,
        answer="MACD死叉卖出", semantic_intent=TurnIntent.SUPPLEMENT,
    )
    assert recovered.outcome.status is CompileStatus.READY
    strategy = recovered.outcome.strategy
    assert strategy is not None and strategy.instrument.symbol == "300059.SZ"
    assert isinstance(strategy.entry, IndicatorCondition)
    assert strategy.entry.indicator_id == "technical.macd"
    assert strategy.entry.trigger == "golden_cross"
    assert len(strategy.exit.children) == 1
    exit_condition = strategy.exit.children[0]
    assert isinstance(exit_condition, IndicatorCondition)
    assert exit_condition.indicator_id == "technical.macd"
    assert exit_condition.trigger == "death_cross"
    assert exit_condition.params == strategy.entry.params
    assert not recovered.outcome.run_requested
    assert len(researcher.requests) == 1 and not ideas.requests
    model_generate.assert_awaited_once()
    interpreted = model_generate.call_args.args[0]
    assert ORIGINAL.utterance in interpreted.utterance
    assert "本轮补充：MACD死叉卖出" in interpreted.utterance


@pytest.mark.asyncio
async def test_reply_transport_failure_keeps_rules_and_safety_does_not_search() -> None:
    compiler, prior, _ = await _partial()
    researcher = _Researcher()
    compiler._current_fact_researcher = researcher  # pyright: ignore[reportPrivateUsage]
    compiler._clarification_dialogue_router = _Dialogue(  # pyright: ignore[reportPrivateUsage]
        unavailable=True,
    )
    aside = await compiler.answer_clarification(
        original_input=ORIGINAL, prior_outcome=prior, answer="我讨厌特朗普",
        semantic_intent=TurnIntent.VIEWPOINT,
    )
    assert aside.compile_input == ORIGINAL and aside.outcome == prior
    assert "回复未能生成" in aside.assistant_message
    assert len(researcher.requests) == 1
    compiler._clarification_dialogue_router = _Dialogue()  # pyright: ignore[reportPrivateUsage]
    safe = await compiler.answer_clarification(
        original_input=ORIGINAL, prior_outcome=prior, answer="我想死",
        semantic_intent=TurnIntent.SAFETY,
    )
    assert safe.compile_input == ORIGINAL and not safe.revision_changed
    assert len(researcher.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_edit", [False, True])
async def test_viewpoint_with_report_context_never_enters_strategy_editor(
    explicit_edit: bool,
) -> None:
    compiler, _, _ = await _partial()
    original = CompileInput(
        utterance="东方财富MACD金叉买入，死叉卖出", instrument_context="300059.SZ",
        as_of_date=date(2026, 9, 4), semantic_intent="new_strategy",
    )
    ready = await compiler.compile(original)
    assert ready.status is CompileStatus.READY
    state = replace(DialogueState.project(
        draft_id=uuid4(), revision=2, compile_input=original, outcome=ready,
        created_at=NOW, recent_turns=(),
    ), backtest_results=({"run_id": "fixture-only", "summary": {"round_trips": 3}},))
    editor = AsyncMock(side_effect=AssertionError("a viewpoint is not a strategy edit"))
    compiler.edit_current_strategy = editor
    orchestrator = DialogueTurnOrchestrator(compiler)
    assert await orchestrator.plan_strategy_edit(
        state=state, answer="我讨厌特朗普", semantic_intent=TurnIntent.VIEWPOINT,
        explicit_edit=explicit_edit,
    ) is None
    plan = await orchestrator.plan(
        state=state, answer="我讨厌特朗普", semantic_intent=TurnIntent.VIEWPOINT,
    )
    editor.assert_not_awaited()
    turn = plan.clarification_turn
    assert turn is not None and not turn.revision_changed
    assert turn.compile_input == original and turn.outcome == ready


@pytest.mark.asyncio
async def test_explicit_new_rules_still_replace_strategy_after_emotional_aside() -> None:
    compiler, prior, ideas = await _partial()
    # A fresh sentence resolves its explicit name independently; it must not
    # need the previous draft's instrument_context to identify the same stock.
    compiler._generator = HybridCandidateGenerator(  # pyright: ignore[reportPrivateUsage]
        deterministic=RuleBasedCandidateGenerator(), bounded_fallback=RuleBasedCandidateGenerator(),
        instrument_name_resolver=lambda name: {"东方财富": "300059.SZ"}[name],
    )
    researcher = _Researcher()
    compiler._current_fact_researcher = researcher  # pyright: ignore[reportPrivateUsage]
    state = DialogueState.project(
        draft_id=uuid4(), revision=1, compile_input=ORIGINAL, outcome=prior,
        created_at=NOW, recent_turns=(),
    )
    answer = "东方财富收盘价上穿20日均线买入，下穿20日均线卖出"
    plan = await DialogueTurnOrchestrator(compiler).plan(
        state=state, answer=answer, semantic_intent=TurnIntent.NEW_STRATEGY,
    )
    turn = plan.clarification_turn
    assert turn is not None and turn.revision_changed
    assert turn.outcome.status is CompileStatus.READY
    assert turn.compile_input.utterance == answer
    strategy = turn.outcome.strategy
    assert strategy is not None and isinstance(strategy.entry, IndicatorCondition)
    assert strategy.entry.indicator_id == "technical.ma"
    assert not researcher.requests and not ideas.requests


@pytest.mark.asyncio
async def test_first_explicit_request_for_new_directions_still_generates_ideas() -> None:
    compiler, _, ideas = await _partial()
    researcher = _Researcher()
    compiler._current_fact_researcher = researcher  # pyright: ignore[reportPrivateUsage]
    outcome = await compiler.compile(CompileInput(
        utterance="我讨厌特朗普的关税政策，给东方财富做三个近一年完整买卖方案，本金10万元。",
        instrument_context="300059.SZ", as_of_date=date(2026, 9, 4),
        semantic_intent="vague_strategy",
    ))
    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.idea_route is not None and len(outcome.idea_route.proposals) == 3
    assert len(ideas.requests) == 1
    assert ideas.requests[0].semantic_intent == "vague_strategy"
    assert not researcher.requests


def test_binding_real_idea_template_removes_stale_unbound_assumption() -> None:
    compiler = _compiler(generator=RuleBasedCandidateGenerator(), idea_router=None)
    strategy = _direct_idea_route().proposals[0].strategy
    assert strategy is not None
    template = UnboundIdeaStrategy.model_validate(
        strategy.model_dump(exclude={"instrument", "schema_version"}),
    )
    unbound = _to_proposal(_ProviderIdeaProposal(
        title="均线趋势", hypothesis="检查日线趋势规则。",
        entry_summary="收盘价上穿20日均线", exit_summary="收盘价下穿20日均线",
        suggested_utterance="收盘价上穿20日均线买入，下穿20日均线卖出，回测近一年。",
        strategy_template=template,
    ), instrument_symbol=None)
    stale = "尚未绑定证券；选定方向后还需用户补充具体 A 股。"
    assert stale in unbound.assumptions
    bound = compiler.bind_idea_proposal(ORIGINAL, unbound, "300059.SZ")
    assert bound is not None and bound.strategy is not None
    assert bound.strategy.instrument.symbol == "300059.SZ"
    assert bound.strategy.entry == template.entry and bound.strategy.exit == template.exit
    assert stale not in bound.assumptions, bound.assumptions
