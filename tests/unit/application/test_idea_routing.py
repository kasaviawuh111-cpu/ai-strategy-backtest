from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ashare_lab.adapters.language import HybridCandidateGenerator, RuleBasedCandidateGenerator
from ashare_lab.adapters.language.vibe_candidates import (
    CandidateTransportRequest,
    CandidateTransportResponse,
    build_candidate_capability_matrix,
)
from ashare_lab.adapters.language.vibe_clarification import VibeClarificationDialogueRouter
from ashare_lab.application.compile_strategy import (
    CompileOutcome,
    CompileStatus,
    StrategyCompiler,
    _proposal_has_real_opening_inventory,
)
from ashare_lab.application.dialogue_state import (
    DialogueState,
    DialogueTurn,
    VerifiedInstrumentMemory,
)
from ashare_lab.application.dialogue_turn import DialogueTurnOrchestrator
from ashare_lab.application.turn_intent import TurnIntent, classify_clarification_turn
from ashare_lab.domain.catalog import load_catalog_directory, load_coverage_catalog_directory
from ashare_lab.domain.financials.models import FinancialMetricId, FinancialUnit
from ashare_lab.domain.strategy import (
    AllCondition,
    BacktestConfig,
    CatalogRef,
    FinancialConditionV1,
    FirstOfExit,
    IndicatorCondition,
    Instrument,
    StrategySpec,
    canonical_hash,
)
from ashare_lab.ports.candidate_generation import (
    CandidateAst,
    CandidateGroundingEvidence,
    CompileInput,
    IndicatorIntent,
)
from ashare_lab.ports.clarification_dialogue import (
    ClarificationDialogueAssessment,
    ClarificationDialogueRequest,
    ClarificationDialogueTurn,
)
from ashare_lab.ports.execution_settings import ExecutionSettingsPatch
from ashare_lab.ports.idea_routing import (
    IdeaAssetMapping,
    IdeaGenerationError,
    IdeaProposal,
    IdeaRoute,
    IdeaRouteProvenance,
    IdeaStockSelectionUnavailableError,
    UnboundIdeaStrategy,
)

ROOT = Path(__file__).parents[3]


class _UnsupportedGenerator:
    def __init__(self, code: str, *, count: int = 1) -> None:
        self.code = code
        self.count = count
        self.requests: list[CompileInput] = []

    async def generate(self, request: CompileInput) -> tuple[CandidateAst, ...]:
        self.requests.append(request)
        if request.utterance in _STRATEGY_UTTERANCES:
            return await RuleBasedCandidateGenerator().generate(request)
        return tuple(
            CandidateAst(
                instrument_symbol=request.instrument_context,
                entry=(),
                exit=(),
                confidence=0.0,
                unsupported_code=self.code,
            )
            for _ in range(self.count)
        )


class _AmbiguousProposalGenerator:
    def __init__(self, ambiguous_utterance: str) -> None:
        self.ambiguous_utterance = ambiguous_utterance

    async def generate(self, request: CompileInput) -> tuple[CandidateAst, ...]:
        rule_based = RuleBasedCandidateGenerator()
        primary = await rule_based.generate(request)
        if request.utterance != self.ambiguous_utterance:
            return primary
        alternative = await rule_based.generate(
            CompileInput(
                utterance="RSI低于30买入，RSI高于70卖出，回测近1年",
                instrument_context=request.instrument_context,
                as_of_date=request.as_of_date,
            )
        )
        return (*primary, *alternative)


@pytest.mark.asyncio
@pytest.mark.parametrize("valid_cross", [True, False])
async def test_bare_cross_idea_guidance_keeps_only_default_macd(valid_cross: bool) -> None:
    route = _direct_idea_route()
    proposals = []
    for period, item in zip((12, 10, 8), route.proposals, strict=True):
        strategy = item.strategy
        assert strategy is not None
        if valid_cross:
            entry = IndicatorCondition(indicator_id="technical.macd", definition_version="1.0.0",
                params={"fast": period, "slow": 26, "signal": 9}, trigger="golden_cross")
            strategy = strategy.model_copy(update={"entry": entry, "exit": FirstOfExit(
                children=(entry.model_copy(update={"trigger": "death_cross"}),),
            )})
        proposals.append(replace(item, strategy=strategy))
    router = _RecordingIdeaRouter(replace(route, proposals=tuple(proposals)))
    outcome = await _compiler(generator=_NeverCalledGenerator(), idea_router=router).compile(
        CompileInput(utterance="东方财富金叉买死叉卖", instrument_context="300059.SZ",
                     as_of_date=date(2026, 9, 7), semantic_intent="vague_strategy"),
    )
    assert len(router.requests) == 1
    assert router.requests[0].utterance == "东方财富金叉买死叉卖"
    assert "不得改成RSI" in router.requests[0].idea_context[-1]
    assert (outcome.idea_route is not None) == valid_cross
    assert outcome.suggested_strategy is None and not outcome.run_requested


class _RecordingIdeaRouter:
    def __init__(self, result: IdeaRoute | None) -> None:
        self.result = result
        self.requests: list[CompileInput] = []

    async def route(self, request: CompileInput) -> IdeaRoute | None:
        self.requests.append(request)
        return self.result


class _NeverCalledGenerator:
    async def generate(self, request: CompileInput) -> tuple[CandidateAst, ...]:
        raise AssertionError(f"direct DSL must not be reparsed: {request.utterance}")


class _RecordingRuleBasedGenerator:
    def __init__(self) -> None:
        self.requests: list[CompileInput] = []

    async def generate(self, request: CompileInput) -> tuple[CandidateAst, ...]:
        self.requests.append(request)
        return await RuleBasedCandidateGenerator().generate(request)


_STRATEGY_PROPOSALS = (
    (
        "趋势确认",
        "股价上穿 20 日均线",
        "股价跌破 20 日均线",
        "股价上穿20日均线买入，股价跌破20日均线卖出，回测近1年",
    ),
    (
        "超跌反转",
        "RSI 低于 30",
        "RSI 高于 70",
        "RSI低于30买入，RSI高于70卖出，回测近1年",
    ),
    (
        "动量转强",
        "MACD 金叉",
        "MACD 死叉",
        "MACD金叉买入，MACD死叉卖出，回测近1年",
    ),
)
_STRATEGY_UTTERANCES = frozenset(item[3] for item in _STRATEGY_PROPOSALS)


def _idea_route(
    *,
    proposal_count: int = 3,
    instrument_symbol: str | None = "300059.SZ",
) -> IdeaRoute:
    proposals = tuple(
        IdeaProposal(
            id=f"idea_{index:012x}",
            title=title,
            hypothesis="只用价格代理检验该观点。",
            entry_summary=entry_summary,
            exit_summary=exit_summary,
            suggested_utterance=suggested_utterance,
            capability_ids=("provider.claim.must.be.replaced",),
            assumptions=("不证明因果关系。",),
            confidence=0.75,
            instrument_symbol=instrument_symbol,
        )
        for index, (title, entry_summary, exit_summary, suggested_utterance) in enumerate(
            _STRATEGY_PROPOSALS[:proposal_count],
            start=1,
        )
    )
    return IdeaRoute(
        understanding="用户表达了一个政治态度。",
        hypothesis="相关不确定性可能与当前股票的价格行为同期出现。",
        asset_mapping=IdeaAssetMapping(
            instrument_symbol=instrument_symbol,
            relation="current_page_proxy" if instrument_symbol is not None else "unbound",
            rationale=(
                "只使用当前股票页作为价格代理。"
                if instrument_symbol is not None
                else "尚未绑定证券；选择方向后仍需补充具体 A 股。"
            ),
            evidence_status=(
                "host_context_only" if instrument_symbol is not None else "instrument_required"
            ),
        ),
        proposals=proposals,
    )
def _compiler(
    *,
    generator: object,
    idea_router: _RecordingIdeaRouter | None,
    backtest_anchor_date: date | None = None,
) -> StrategyCompiler:
    return StrategyCompiler(
        generator=generator,  # type: ignore[arg-type]
        idea_router=idea_router,
        catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals",
        release_version="2026.09.01",
        backtest_anchor_date=backtest_anchor_date,
    )


def _direct_idea_route() -> IdeaRoute:
    route = _idea_route(instrument_symbol="300059.SZ")
    proposals = []
    for period, proposal in zip((20, 30, 60), route.proposals, strict=True):
        strategy = StrategySpec(
            catalog=CatalogRef(
                catalog_id="cn_a.signals",
                release_version="2026.09.01",
            ),
            instrument=Instrument(symbol="300059.SZ"),
            entry=IndicatorCondition(
                indicator_id="technical.ma",
                definition_version="1.0.0",
                params={"period": period, "price_field": "close"},
                trigger="price_crosses_above",
            ),
            exit=FirstOfExit(
                children=(
                    IndicatorCondition(
                        indicator_id="technical.ma",
                        definition_version="1.0.0",
                        params={"period": period, "price_field": "close"},
                        trigger="price_crosses_below",
                    ),
                )
            ),
            backtest=BacktestConfig(
                start=date(2025, 9, 4),
                end=date(2026, 9, 4),
                initial_cash_cny=100_000,
            ),
        )
        proposals.append(replace(proposal, strategy=strategy))
    return replace(
        route,
        proposals=tuple(proposals),
        provenance=IdeaRouteProvenance(
            source="bounded_provider",
            provider="deepseek",
            model="deepseek-v4-pro",
            prompt_version="idea-route.prompt.v4",
            schema_version="idea-route-provider.v3",
            capability_projection_version="candidate-capabilities.v1",
            capability_projection_hash="sha256:" + "1" * 64,
            upstream_pattern_commit="1ee7df16af6eed8831014fa16ec0a9cb2d35f4e7",
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("utterance", "code", "count"), [
    ("估值过低的股票反转买", "candidate_provider_low_confidence", 1),
    ("估值过低的股票反转买", "candidate_provider_low_confidence", 2),
    ("东方财富均线交叉买，反之卖", "candidate_provider_low_confidence", 1),
])
async def test_unresolved_intention_prefers_editable_model_ideas(
    utterance: str, code: str, count: int,
) -> None:
    generator = _UnsupportedGenerator(code, count=count)
    route = replace(
        _direct_idea_route(),
        understanding="先按模型建议给出可编辑的日线规则；低估值保留为选股偏好。",
    )
    router = _RecordingIdeaRouter(route)
    compiler = _compiler(generator=generator, idea_router=router)
    original = CompileInput(
        utterance=utterance, instrument_context="300059.SZ", as_of_date=date(2026, 9, 4),
    )
    outcome = await compiler.compile(original)
    assert len(router.requests) == 1
    assert router.requests[0] == replace(original, idea_inspiration=original.utterance)
    assert len(generator.requests) == (0 if "均线交叉" in utterance else 1)
    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    # Neither input asks for intraday T trading; the pending choice is which
    # editable idea to use, not a missing T-trade instruction.
    assert outcome.diagnostic_code == "idea_guidance_required"
    assert outcome.clarification == route.understanding
    assert outcome.idea_route is not None and len(outcome.idea_route.proposals) == 3
    assert all(proposal.strategy is not None for proposal in outcome.idea_route.proposals)
    assert outcome.strategy is None and not outcome.run_requested


@pytest.mark.asyncio
async def test_unbound_low_confidence_ideas_keep_model_explanation() -> None:
    original = CompileInput(utterance="估值过低的股票反转买", as_of_date=date(2026, 9, 4))
    route = replace(
        _idea_route(instrument_symbol=None),
        understanding="先给出日线反转建议，参数可以修改；低估值仅保留为选股偏好。",
    )
    router = _RecordingIdeaRouter(route)
    generator = _UnsupportedGenerator("candidate_provider_low_confidence")
    outcome = await _compiler(generator=generator, idea_router=router).compile(original)
    assert outcome.idea_route is not None
    assert outcome.clarification == route.understanding
    assert len(generator.requests) == len(router.requests) == 1
    assert router.requests[0].utterance == original.utterance
    assert router.requests[0].idea_inspiration == original.utterance
    assert outcome.strategy is None and not outcome.run_requested


@pytest.mark.asyncio
async def test_low_confidence_mixed_with_invalid_candidates_does_not_rewrite() -> None:
    class MixedGenerator(_UnsupportedGenerator):
        async def generate(self, request: CompileInput) -> tuple[CandidateAst, ...]:
            candidates = await super().generate(request)
            return (*candidates, replace(
                candidates[0], unsupported_code="candidate_provider_invalid_output",
            ))

    original = CompileInput(
        utterance="估值过低的股票反转买", instrument_context="300059.SZ",
        as_of_date=date(2026, 9, 4),
    )
    router = _RecordingIdeaRouter(_direct_idea_route())
    generator = MixedGenerator("candidate_provider_low_confidence")
    outcome = await _compiler(generator=generator, idea_router=router).compile(original)
    assert outcome.diagnostic_code == "candidate_batch_no_valid_strategy"
    assert outcome.idea_route is None
    assert generator.requests == [original]
    assert router.requests == []
    assert {item.diagnostic_code for item in outcome.candidate_rejections} == {
        "candidate_provider_low_confidence", "candidate_provider_invalid_output",
    }
    assert outcome.strategy is None and not outcome.run_requested


@pytest.mark.asyncio
async def test_suggested_defaults_cannot_introduce_unavailable_historical_valuation() -> None:
    route = _direct_idea_route()
    proposals = []
    for proposal in route.proposals:
        assert proposal.strategy is not None
        proposals.append(replace(proposal, strategy=proposal.strategy.model_copy(update={
            "entry": FinancialConditionV1(
                metric_id=FinancialMetricId.PE, comparator="lt",
                value=Decimal(15), unit=FinancialUnit.TIMES,
            ),
        })))
    router = _RecordingIdeaRouter(replace(route, proposals=tuple(proposals)))
    outcome = await _compiler(
        generator=_UnsupportedGenerator("candidate_provider_low_confidence"),
        idea_router=router,
    ).compile(CompileInput(
        utterance="估值过低的股票反转买", instrument_context="300059.SZ",
        as_of_date=date(2026, 9, 4),
    ))
    assert len(router.requests) == 1
    assert outcome.diagnostic_code == "idea_guidance_execution_invalid"
    assert outcome.idea_route is None and outcome.strategy is None
    assert not outcome.run_requested


@pytest.mark.asyncio
@pytest.mark.parametrize("fixed_side", ["entry", "exit"])
@pytest.mark.parametrize("bound", [True, False])
async def test_partial_rules_keep_stock_and_reject_changes_to_explicit_side(
    fixed_side: str, bound: bool,
) -> None:
    """Schema/application regression only; real-model acceptance runs separately."""
    route = _direct_idea_route()
    baseline = route.proposals[0].strategy
    assert baseline is not None
    proposals = list(route.proposals)
    second = proposals[1].strategy
    assert second is not None
    proposals[1] = replace(proposals[1], strategy=second.model_copy(update={
        fixed_side: getattr(baseline, fixed_side),
    }))
    if not bound:
        proposals = [replace(proposal, instrument_symbol=None, strategy=None,
            strategy_template=UnboundIdeaStrategy.model_validate(
                proposal.strategy.model_dump(exclude={"instrument", "schema_version"}),
            )) for proposal in proposals]
        route = replace(route, asset_mapping=_idea_route(instrument_symbol=None).asset_mapping)
    router = _RecordingIdeaRouter(replace(route, proposals=tuple(proposals)))
    known_rule = IndicatorIntent(
        indicator_id="technical.ma", definition_version="1.0.0",
        params=(("period", 20), ("price_field", "close")),
        trigger="price_crosses_above" if fixed_side == "entry" else "price_crosses_below",
    )
    candidate = CandidateAst(
        instrument_symbol="300059.SZ" if bound else None, confidence=1,
        entry=(known_rule,) if fixed_side == "entry" else (),
        exit=(known_rule,) if fixed_side == "exit" else (),
        unsupported_code="exit_rule_not_recognized" if fixed_side == "entry"
        else "entry_rule_not_recognized",
        grounding_evidence=(CandidateGroundingEvidence(
            path="/instrument/symbol", start=0, end=4, text="东方财富",
        ),) if bound else (),
    )

    class PartialGenerator:
        async def generate(self, request: CompileInput) -> tuple[CandidateAst, ...]:
            return (candidate,)

    compiler = _compiler(generator=PartialGenerator(), idea_router=router)
    rule = "上穿20日均线买入" if fixed_side == "entry" else "跌破20日均线卖出"
    outcome = await compiler.compile(CompileInput(
        utterance=f"{'东方财富' if bound else ''}{rule}，另一边还没想好", as_of_date=date(2026, 9, 4),
    ))
    assert router.requests[0].instrument_context == ("300059.SZ" if bound else None)
    assert any(f"{fixed_side}=" in context and "固定条件" in context
               for context in router.requests[0].idea_context)
    assert outcome.idea_route is not None
    assert len(outcome.idea_route.proposals) == 2
    assert all(getattr(p.strategy or p.strategy_template, fixed_side) == getattr(baseline, fixed_side)
               for p in outcome.idea_route.proposals)
    assert all((p.strategy is not None and p.strategy.instrument.symbol == "300059.SZ")
               if bound else p.strategy is None and p.instrument_symbol is None
               for p in outcome.idea_route.proposals)


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["casual", "unknown", "safety"])
async def test_model_initial_intent_is_not_reinterpreted_by_keyword_routes(intent: str) -> None:
    requests: list[ClarificationDialogueRequest] = []

    class Dialogue:
        async def assess(
            self, request: ClarificationDialogueRequest,
        ) -> ClarificationDialogueAssessment:
            requests.append(request)
            return ClarificationDialogueAssessment(
                reply_kind="off_topic", acknowledgement_id="light_redirect",
                natural_reply="我听到了，你愿意多说一点吗？",
            )

    ideas = _RecordingIdeaRouter(_idea_route(instrument_symbol=None))
    compiler = StrategyCompiler(
        generator=_NeverCalledGenerator(), catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01",
        clarification_dialogue_router=Dialogue(), idea_router=ideas,
    )
    outcome = await compiler.compile(CompileInput(
        utterance="今天的价格让我不知道该怎么办", as_of_date=date(2026, 9, 4),
        semantic_intent=intent,
    ))
    assert outcome.strategy is None and not outcome.run_requested
    assert ideas.requests == []
    assert len(requests) == 1
    assert requests[0].diagnostic_code == (
        "safety_support" if intent == "safety" else "conversation_only"
    )
    assert outcome.clarification == "我听到了，你愿意多说一点吗？"


@pytest.mark.asyncio
async def test_chat_weather_and_persona_keep_model_replies_and_can_start_fresh_ideas() -> None:
    """Application-routing fixture only; this does not claim real-model acceptance."""
    assessments: list[ClarificationDialogueRequest] = []
    opening_reply = "这个玩笑我接住了。想聊些什么？"
    weather_reply = "你想了解哪里的天气？"

    class Dialogue:
        async def assess(
            self, request: ClarificationDialogueRequest,
        ) -> ClarificationDialogueAssessment:
            assessments.append(request)
            if request.answer == "我是秦始皇":
                return ClarificationDialogueAssessment(
                    reply_kind="preference", acknowledgement_id="respect_preference",
                    natural_reply="可以试试果断进出的短线风格。",
                    strategy_inspiration="将人物比喻转成有明确退出条件的短线策略方向。",
                )
            return ClarificationDialogueAssessment(
                reply_kind="off_topic", acknowledgement_id="light_redirect",
                natural_reply=opening_reply if request.answer == "我是你爹" else weather_reply,
            )

    ideas = _RecordingIdeaRouter(_idea_route(instrument_symbol=None))
    compiler = StrategyCompiler(
        generator=_NeverCalledGenerator(), catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01",
        clarification_dialogue_router=Dialogue(), idea_router=ideas,
    )
    initial_input = CompileInput(utterance="我是你爹", as_of_date=date(2026, 9, 4))
    initial = await compiler.compile(initial_input)
    assert initial.diagnostic_code == "conversation_only"
    assert initial.clarification == opening_reply
    assert ideas.requests == []
    recent = (ClarificationDialogueTurn(
        user_text=initial_input.utterance, assistant_text=opening_reply,
        intent="casual", revision=1, created_at=datetime(2026, 9, 4, tzinfo=UTC),
    ),)
    weather_text = "好吧明天下雨吗"
    assert classify_clarification_turn(weather_text) is TurnIntent.UNKNOWN
    weather = await compiler.answer_clarification(
        original_input=initial_input, prior_outcome=initial,
        answer=weather_text, recent_turns=recent,
    )
    assert weather.assistant_message == weather_reply
    assert weather.outcome is initial and weather.compile_input is initial_input
    assert not weather.revision_changed and ideas.requests == []
    recent = (*recent, ClarificationDialogueTurn(
        user_text=weather_text, assistant_text=weather.assistant_message,
        intent="unknown", revision=1, created_at=datetime(2026, 9, 4, tzinfo=UTC),
    ))
    assert classify_clarification_turn("我是秦始皇") is TurnIntent.CASUAL
    persona = await compiler.answer_clarification(
        original_input=weather.compile_input, prior_outcome=weather.outcome,
        answer="我是秦始皇", recent_turns=recent,
    )
    assert len(assessments) == 3 and all(item.question == "" for item in assessments)
    assert assessments[-1].recent_turns == recent
    assert persona.revision_changed
    assert persona.compile_input.utterance == "我是秦始皇"
    assert persona.outcome.idea_route is not None
    assert len(persona.outcome.idea_route.proposals) == 3
    assert len(ideas.requests) == 1 and ideas.requests[0].utterance == "我是秦始皇"
    assert opening_reply not in persona.assistant_message
    assert weather_reply not in persona.assistant_message


@pytest.mark.asyncio
async def test_contextual_dialogue_model_failure_has_no_template_or_revision_change() -> None:
    """The explicit unavailable fixture cannot synthesize an old-question reply."""
    assessments: list[ClarificationDialogueRequest] = []

    class UnavailableDialogue:
        async def assess(self, request: ClarificationDialogueRequest) -> None:
            assessments.append(request)
            return None

    compiler = StrategyCompiler(
        generator=_NeverCalledGenerator(), catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01",
        clarification_dialogue_router=UnavailableDialogue(),
    )
    original = CompileInput(utterance="试试短线", as_of_date=date(2026, 9, 4))
    pending = CompileOutcome(
        status=CompileStatus.NEEDS_CLARIFICATION, diagnostic_code="strategy_rule_incomplete",
        clarification="旧问题：什么时候买入和卖出？",
    )
    turn = await compiler.answer_clarification(
        original_input=original, prior_outcome=pending, answer="好吧明天下雨吗",
    )
    assert len(assessments) == 1
    assert turn.assistant_message == "对话模型这次未能返回有效回复，请稍后重试。"
    assert turn.outcome is pending and turn.compile_input is original
    assert not turn.revision_changed and turn.suggestions == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("utterance", ["我是秦始皇", "随便想个方向"])
async def test_repaired_initial_assessment_continues_once_to_editable_ideas(utterance: str) -> None:
    class Transport:
        calls = 0

        async def generate_json(
            self, request: CandidateTransportRequest,
        ) -> CandidateTransportResponse:
            self.calls += 1
            return {
                "reply_kind": "preference",
                "acknowledgement_id": "light_redirect" if self.calls == 1
                else "respect_preference",
                "natural_reply": "先沿着你的表达整理可修改的交易方向。",
                "strategy_inspiration": "探索有明确退出约束的交易风格。",
            }

    transport = Transport()
    catalog = load_catalog_directory(ROOT / "catalogs")
    ideas = _RecordingIdeaRouter(_idea_route(instrument_symbol=None))
    compiler = StrategyCompiler(
        generator=_NeverCalledGenerator(), catalog=catalog,
        catalog_id="cn_a.signals", release_version="2026.09.01", idea_router=ideas,
        clarification_dialogue_router=VibeClarificationDialogueRouter(
            transport, capability_matrix=build_candidate_capability_matrix(
                catalog, load_coverage_catalog_directory(ROOT / "catalogs" / "coverage"),
            ),
        ),
    )
    outcome = await compiler.compile(CompileInput(
        utterance=utterance, as_of_date=date(2026, 9, 4),
    ))
    assert transport.calls == 2 and len(ideas.requests) == 1
    assert ideas.requests[0].utterance == utterance
    assert outcome.diagnostic_code == "idea_guidance_required"
    assert outcome.idea_route is not None and len(outcome.idea_route.proposals) == 3
    assert outcome.strategy is None and not outcome.run_requested


@pytest.mark.asyncio
async def test_model_can_turn_persona_into_unbound_strategy_directions() -> None:
    class Dialogue:
        async def assess(
            self, request: ClarificationDialogueRequest,
        ) -> ClarificationDialogueAssessment:
            return ClarificationDialogueAssessment(
                reply_kind="preference", acknowledgement_id="respect_preference",
                natural_reply="可以把这种比喻作为策略灵感。",
                strategy_inspiration="如果想表达更积极的短线风格，可探索有风险退出的动量策略。",
            )

    route = _RecordingIdeaRouter(_idea_route(instrument_symbol=None))
    compiler = StrategyCompiler(
        generator=_NeverCalledGenerator(), catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01",
        clarification_dialogue_router=Dialogue(), idea_router=route,
    )
    outcome = await compiler.compile(CompileInput(
        utterance="我是个急性子", as_of_date=date(2026, 9, 4),
    ))
    assert outcome.diagnostic_code == "idea_guidance_required"
    assert outcome.idea_route is not None
    assert len(outcome.idea_route.proposals) == 3
    assert outcome.strategy is None
    assert route.requests[0].idea_inspiration is not None
    assert route.requests[0].instrument_context is None


@pytest.mark.asyncio
@pytest.mark.parametrize("answer,selected", [
    ("好吧，东方财富怎么交易", False),
    ("用东方财富该如何操作", False),
    ("先不管新闻，股票用东方财富，我想试追趋势。", True),
])
async def test_contextual_idea_followup_generates_once(answer: str, selected: bool) -> None:
    assessments: list[ClarificationDialogueRequest] = []

    class Dialogue:
        async def assess(
            self, request: ClarificationDialogueRequest,
        ) -> ClarificationDialogueAssessment:
            assessments.append(request)
            return ClarificationDialogueAssessment(
                reply_kind="question", acknowledgement_id="answer_question",
                natural_reply="可以把刚才的灵感转成待检验的策略。",
                strategy_inspiration="承接先前的风格灵感，提供三种有退出约束的交易假设。",
                instrument_name="东方财富", instrument_selected=selected,
            )

    route = _RecordingIdeaRouter(_direct_idea_route())
    compiler = StrategyCompiler(
        generator=_NeverCalledGenerator(), catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01",
        clarification_dialogue_router=Dialogue(), idea_router=route,
        instrument_name_resolver=lambda name: {"东方财富": "300059.SZ"}[name],
    )
    recent = (ClarificationDialogueTurn(
        user_text="我是个急性子", assistant_text="可探索三种有明确退出的风格。",
        intent="casual", revision=1, created_at=datetime(2026, 9, 4, tzinfo=UTC),
    ),)
    turn = await compiler.answer_clarification(
        original_input=CompileInput(utterance="我是个急性子", as_of_date=date(2026, 9, 4)),
        prior_outcome=CompileOutcome(
            status=CompileStatus.NEEDS_CLARIFICATION, diagnostic_code="idea_guidance_required",
            clarification="请补充股票并选择方向。", idea_route=_idea_route(instrument_symbol=None),
        ),
        answer=answer, recent_turns=recent,
    )
    assert len(assessments) == len(route.requests) == 1
    assert assessments[0].recent_turns == recent
    assert route.requests[0].instrument_context == "300059.SZ"
    assert "我是个急性子" in route.requests[0].idea_context[0]
    assert turn.revision_changed
    assert turn.outcome.idea_route is not None
    assert len(turn.outcome.idea_route.proposals) == 3
    assert all(p.instrument_symbol == "300059.SZ" for p in turn.outcome.idea_route.proposals)


@pytest.mark.asyncio
async def test_question_about_prior_idea_returns_without_recompiling_or_researching() -> None:
    assessments: list[ClarificationDialogueRequest] = []

    class Dialogue:
        async def assess(
            self, request: ClarificationDialogueRequest,
        ) -> ClarificationDialogueAssessment:
            assessments.append(request)
            return ClarificationDialogueAssessment(
                reply_kind="question", acknowledgement_id="answer_question",
                natural_reply="上次只取得搜索摘要，还不能据此确认近期事件。",
            )

    ideas = _RecordingIdeaRouter(None)
    compiler = StrategyCompiler(
        generator=_NeverCalledGenerator(), catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01",
        clarification_dialogue_router=Dialogue(), idea_router=ideas,
    )
    original = CompileInput(utterance="我讨厌特朗普", as_of_date=date(2026, 9, 5))
    prior = CompileOutcome(
        status=CompileStatus.NEEDS_CLARIFICATION, diagnostic_code="idea_guidance_required",
        clarification="你想试哪个方向？", idea_route=_idea_route(instrument_symbol=None),
    )
    turn = await compiler.answer_clarification(
        original_input=original, prior_outcome=prior,
        answer="你说的近期事情有出处吗？只给两条最相关的。",
    )
    assert len(assessments) == 1 and ideas.requests == []
    assert turn.outcome is prior and turn.compile_input is original
    assert not turn.revision_changed and not turn.suggestions
    assert turn.assistant_message == "上次只取得搜索摘要，还不能据此确认近期事件。"


@pytest.mark.asyncio
async def test_direct_idea_dsl_is_catalog_gated_and_selected_without_reparse(monkeypatch: pytest.MonkeyPatch) -> None:
    compiler = _compiler(
        generator=_NeverCalledGenerator(),
        idea_router=_RecordingIdeaRouter(_direct_idea_route()),
        backtest_anchor_date=date(2026, 9, 4),
    )
    original = CompileInput(
        utterance="低买高卖，给我三个近一年策略，本金10万元",
        instrument_context="300059.SZ",
        # The browser may hold an older display date; executable validation
        # must use the same trusted server anchor as the initial compile.
        as_of_date=date(2026, 8, 6),
    )

    guidance = await compiler.compile(original)
    assert guidance.status is CompileStatus.NEEDS_CLARIFICATION
    assert guidance.idea_route is not None
    assert all(item.strategy is not None for item in guidance.idea_route.proposals)

    async def no_acknowledgement_model(*args, **kwargs):
        raise AssertionError("Selecting an existing option must not depend on acknowledgement generation")

    monkeypatch.setattr(compiler, "compose_ready_response", no_acknowledgement_model)
    monkeypatch.setattr(compiler, "compose_dialogue_response", no_acknowledgement_model)

    selected = await compiler.answer_clarification(
        original_input=original,
        prior_outcome=guidance,
        answer="1",
    )
    assert selected.outcome.status is CompileStatus.READY
    assert selected.outcome.strategy is not None
    assert selected.outcome.strategy.backtest.initial_cash_cny == 100_000
    assert selected.outcome.candidate_provenance is not None
    assert selected.outcome.candidate_provenance.provider == "deepseek"
    assert selected.outcome.candidate_provenance.candidate_rank == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_rule", [None, "catalog", "indicator", "trigger", "period", "depth"],
)
async def test_unbound_template_binds_stock_without_reinterpreting_model_rules(
    invalid_rule: str | None,
) -> None:
    direct_route = _direct_idea_route()
    proposals = []
    for proposal in direct_route.proposals:
        assert proposal.strategy is not None
        template = UnboundIdeaStrategy.model_validate(
            proposal.strategy.model_dump(exclude={"instrument", "schema_version"})
        )
        if invalid_rule == "catalog":
            template = template.model_copy(update={"catalog": CatalogRef(
                catalog_id="unknown.catalog", release_version="2026.09.01",
            )})
        elif invalid_rule == "depth":
            leaf = template.entry
            deep = leaf
            for _ in range(8):
                deep = AllCondition(children=(leaf, deep))
            template = template.model_copy(update={"entry": deep})
        elif invalid_rule is not None:
            assert isinstance(template.entry, IndicatorCondition)
            change = {
                "indicator": {"indicator_id": "technical.unknown_indicator"},
                "trigger": {"trigger": "golden_cross"},
                "period": {"params": {"period": 0, "price_field": "close"}},
            }[invalid_rule]
            template = template.model_copy(update={
                "entry": template.entry.model_copy(update=change),
            })
        proposals.append(replace(
            proposal, instrument_symbol=None, strategy=None, strategy_template=template,
        ))
    route = replace(
        direct_route, asset_mapping=_idea_route(instrument_symbol=None).asset_mapping,
        proposals=tuple(proposals),
    )
    generator = _RecordingRuleBasedGenerator()
    compiler = _compiler(
        generator=generator, idea_router=_RecordingIdeaRouter(route),
        backtest_anchor_date=date(2026, 9, 4),
    )
    original = CompileInput(utterance="低买高卖", as_of_date=date(2026, 9, 4))
    guidance = await compiler.compile(original)
    if invalid_rule is not None:
        assert guidance.status is CompileStatus.NEEDS_CLARIFICATION
        assert guidance.diagnostic_code == "idea_guidance_execution_invalid"
        assert guidance.idea_route is None
        assert guidance.strategy is None
        assert [item.utterance for item in generator.requests] == [original.utterance]
        return
    assert guidance.idea_route is not None
    assert all(item.capability_ids == ("technical.ma",)
               for item in guidance.idea_route.proposals)
    selected = await compiler.answer_clarification(
        original_input=original, prior_outcome=guidance, answer="1",
    )
    assert selected.outcome.diagnostic_code == "instrument_required"
    assert selected.outcome.strategy is None
    state = DialogueState.project(
        draft_id=uuid4(), revision=2, compile_input=selected.compile_input,
        outcome=selected.outcome, created_at=datetime(2026, 9, 4, tzinfo=UTC), recent_turns=(),
    )
    plan = await DialogueTurnOrchestrator(compiler).plan(state=state, answer="600519")
    assert plan.clarification_turn is not None
    outcome = plan.clarification_turn.outcome
    # Identity preflight may inspect the original request; the model's direct
    # DSL must never be converted back to natural language and reinterpreted.
    assert [item.utterance for item in generator.requests] == [original.utterance]
    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert outcome.strategy.instrument.symbol == "600519.SH"
    template = proposals[0].strategy_template
    assert template is not None
    assert outcome.strategy.entry == template.entry
    assert outcome.strategy.exit == template.exit
    assert outcome.strategy.backtest == template.backtest
    assert outcome.strategy.execution == template.execution
    assert outcome.candidate_provenance == selected.outcome.candidate_provenance


@pytest.mark.asyncio
async def test_unbound_idea_gate_keeps_valid_templates_without_changing_their_rules() -> None:
    route = _direct_idea_route()
    proposals = []
    for index, proposal in enumerate(route.proposals):
        assert proposal.strategy is not None
        template = UnboundIdeaStrategy.model_validate(
            proposal.strategy.model_dump(exclude={"instrument", "schema_version"}),
        )
        if index == 0:
            assert isinstance(template.entry, IndicatorCondition)
            template = template.model_copy(update={
                "entry": template.entry.model_copy(update={"trigger": "golden_cross"}),
            })
        proposals.append(replace(
            proposal, instrument_symbol=None, strategy=None, strategy_template=template,
        ))
    route = replace(
        route, asset_mapping=_idea_route(instrument_symbol=None).asset_mapping,
        proposals=tuple(proposals),
    )
    compiler = _compiler(
        generator=_RecordingRuleBasedGenerator(), idea_router=_RecordingIdeaRouter(route),
    )
    outcome = await compiler.compile(
        CompileInput(utterance="低买高卖", as_of_date=date(2026, 9, 4)),
    )
    assert outcome.diagnostic_code == "idea_guidance_required"
    assert outcome.idea_route is not None
    assert [item.id for item in outcome.idea_route.proposals] == [
        item.id for item in proposals[1:]
    ]
    assert [item.strategy_template for item in outcome.idea_route.proposals] == [
        item.strategy_template for item in proposals[1:]
    ]
    assert all(item.instrument_symbol is None and item.strategy is None
               for item in outcome.idea_route.proposals)


@pytest.mark.asyncio
@pytest.mark.parametrize("already_selected", [True, False])
async def test_stored_idea_binding_failure_preserves_choice_and_stock(
    already_selected: bool,
) -> None:
    """Old stored templates can fail binding without becoming a new model request."""
    route = _direct_idea_route()
    proposals = []
    for index, proposal in enumerate(route.proposals):
        assert proposal.strategy is not None
        template = UnboundIdeaStrategy.model_validate(
            proposal.strategy.model_dump(exclude={"instrument", "schema_version"}),
        )
        assert isinstance(template.entry, IndicatorCondition)
        if index < 2:
            template = template.model_copy(update={
                "entry": template.entry.model_copy(update={"trigger": "golden_cross"}),
            })
        proposals.append(replace(
            proposal, instrument_symbol=None, strategy=None, strategy_template=template,
        ))
    route = replace(
        route, asset_mapping=_idea_route(instrument_symbol=None).asset_mapping,
        proposals=tuple(proposals),
    )
    prior = CompileOutcome(
        status=CompileStatus.NEEDS_CLARIFICATION,
        diagnostic_code="instrument_required" if already_selected else "idea_guidance_required",
        selected_idea_proposal=proposals[0] if already_selected else None,
        idea_route=None if already_selected else route,
        execution_settings=ExecutionSettingsPatch(slippage_bps=12),
        run_requested=True,
        refresh_data=True,
        pending_edit_run_requested=True,
        pending_edit_refresh_data=True,
    )
    original = CompileInput(utterance="低买高卖", as_of_date=date(2026, 9, 4))
    state = DialogueState.project(
        draft_id=uuid4(), revision=2, compile_input=original,
        outcome=prior, created_at=datetime(2026, 9, 4, tzinfo=UTC), recent_turns=(),
    )
    generator = _RecordingRuleBasedGenerator()
    compiler = _compiler(generator=generator, idea_router=_RecordingIdeaRouter(None))
    for _ in range(2):
        plan = await DialogueTurnOrchestrator(compiler).plan(state=state, answer="300059")
        assert plan.clarification_turn is not None
        turn = plan.clarification_turn
        assert turn.outcome.diagnostic_code == "idea_guidance_execution_invalid"
        assert turn.outcome.status is CompileStatus.NEEDS_CLARIFICATION
        assert turn.outcome.strategy is None
        assert turn.outcome.selected_idea_proposal == prior.selected_idea_proposal
        assert turn.outcome.idea_route == prior.idea_route
        assert turn.outcome.execution_settings == prior.execution_settings
        assert not turn.outcome.run_requested
        assert not turn.outcome.refresh_data
        assert not turn.outcome.pending_edit_run_requested
        assert not turn.outcome.pending_edit_refresh_data
        assert turn.compile_input.instrument_context == "300059.SZ"
        assert "已保留" in (turn.outcome.clarification or "")
        state = replace(state, compile_input=turn.compile_input, outcome=turn.outcome)
    if not already_selected:
        # One retained option is valid: selecting it must bind its exact DSL,
        # even though the previous group binding had fewer than two valid choices.
        recovered = await DialogueTurnOrchestrator(compiler).plan(
            state=state, answer=proposals[-1].id,
        )
        assert recovered.clarification_turn is not None
        result = recovered.clarification_turn.outcome
        assert result.status is CompileStatus.READY
        assert result.strategy is not None
        assert result.strategy.instrument.symbol == "300059.SZ"
        assert result.strategy.entry == proposals[-1].strategy_template.entry
        assert result.strategy.exit == proposals[-1].strategy_template.exit
        assert not result.run_requested
        assert not result.refresh_data
    assert generator.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize(("trigger", "supplied_days", "valid"), [
    ("gt_multiple", None, True),
    ("gte_multiple", None, True),
    ("lte_multiple", None, True),
    ("consecutive_gte_multiple", None, False),
    ("gt_multiple", 7, True),
    ("consecutive_gte_multiple", 7, True),
    ("gte_multiple", 0, True),
])
async def test_direct_ideas_fill_only_inactive_relative_volume_days(
    bound: bool, trigger: str, supplied_days: int | None, valid: bool,
) -> None:
    params = {"baseline_period": 20}
    if supplied_days is not None:
        params["consecutive_days"] = supplied_days
    volume = IndicatorCondition(
        indicator_id="volume.relative", definition_version="1.0.0",
        params=params, trigger=trigger, value=1.5,
    )
    route = _direct_idea_route()
    proposals = []
    for proposal in route.proposals:
        assert proposal.strategy is not None
        spec = proposal.strategy.model_copy(update={
            "entry": AllCondition(children=(proposal.strategy.entry, volume)),
        })
        proposals.append(replace(
            proposal, instrument_symbol="300059.SZ" if bound else None,
            strategy=spec if bound else None,
            strategy_hash=canonical_hash(spec) if bound else None,
            strategy_template=UnboundIdeaStrategy.model_validate(
                spec.model_dump(exclude={"instrument", "schema_version"}),
            ),
        ))
    route = replace(route, proposals=tuple(proposals))
    compiler = _compiler(
        generator=_RecordingRuleBasedGenerator(), idea_router=_RecordingIdeaRouter(route),
    )
    request = CompileInput(
        utterance="低买高卖", instrument_context="300059.SZ" if bound else None,
        as_of_date=date(2026, 9, 4),
    )
    outcome = await compiler.compile(request)
    assert volume.params == params  # Normalization never mutates the provider payload.
    if not valid:
        assert outcome.diagnostic_code == "idea_guidance_execution_invalid"
        assert outcome.idea_route is None
        return
    assert outcome.idea_route is not None
    choice = outcome.idea_route.proposals[0]
    rules = choice.strategy if bound else choice.strategy_template
    assert rules is not None and isinstance(rules.entry, AllCondition)
    expected_params = (
        params if trigger == "consecutive_gte_multiple"
        else {name: value for name, value in params.items() if name != "consecutive_days"}
    )
    assert rules.entry.children[1].params == expected_params
    assert rules.entry.children[1].trigger == trigger
    if not bound:
        ready = compiler.bind_selected_idea(
            replace(request, instrument_context="300059.SZ"),
            replace(outcome, selected_idea_proposal=choice),
        )
        assert ready is not None and ready.strategy is not None
        assert ready.strategy.entry == rules.entry
    else:
        assert choice.strategy_hash == canonical_hash(rules)


def test_selected_candidate_date_rejection_explains_data_boundary_without_rewriting() -> None:
    compiler = _compiler(generator=_NeverCalledGenerator(), idea_router=None,
                         backtest_anchor_date=date(2026, 9, 11))
    proposal = _direct_idea_route().proposals[0]
    assert proposal.strategy is not None
    strategy = proposal.strategy.model_copy(update={
        "backtest": proposal.strategy.backtest.model_copy(update={"end": date(2026, 9, 13)}),
    })
    proposal = replace(proposal, strategy=strategy, strategy_hash=canonical_hash(strategy))
    prior = CompileOutcome(status=CompileStatus.NEEDS_CLARIFICATION,
                           selected_idea_proposal=proposal)
    result = compiler._compile_selected_idea_strategy(
        request=CompileInput(utterance="选第一套", instrument_context="300059.SZ",
                             as_of_date=date(2026, 9, 13)),
        prior_outcome=prior, proposal=proposal,
    )
    assert result.strategy is None
    assert result.selected_idea_proposal == proposal
    assert "数据尚未更新" in result.clarification
    assert "2026-09-11" in result.clarification
    assert result.selected_idea_proposal.strategy.backtest.end == date(2026, 9, 13)
    assert result.revision_base_strategy == strategy
    template = UnboundIdeaStrategy.model_validate(
        strategy.model_dump(exclude={"instrument", "schema_version"})
    )
    unbound = replace(proposal, strategy=None, strategy_template=template,
                      instrument_symbol=None, strategy_hash=None)
    bound_rejection = compiler.bind_selected_idea(
        CompileInput(utterance="东方财富300059.SZ", instrument_context="300059.SZ",
                     as_of_date=date(2026, 9, 13)),
        replace(prior, selected_idea_proposal=unbound),
    )
    assert bound_rejection is not None and bound_rejection.strategy is None
    assert bound_rejection.revision_base_strategy == template.bind("300059.SZ")
    assert bound_rejection.selected_idea_proposal == unbound


def _paired_template_route(compiler: StrategyCompiler) -> IdeaRoute:
    """Contract fixtures only: no model or live screening is called here."""
    direct_route = _direct_idea_route()
    proposals = []
    for proposal, symbol, name in zip(
        direct_route.proposals,
        ("300059.SZ", "600519.SH", "000001.SZ"),
        ("东方财富", "贵州茅台", "平安银行"), strict=True,
    ):
        assert proposal.strategy is not None
        template = UnboundIdeaStrategy.model_validate(
            proposal.strategy.model_dump(exclude={"instrument", "schema_version"})
        )
        bound = compiler.bind_idea_proposal(
            CompileInput(utterance="我是秦始皇", as_of_date=date(2026, 9, 4)),
            replace(proposal, strategy_template=template, instrument_name=name,
                    pairing_reason="测试模型提供的配对理由"), symbol,
        )
        assert bound is not None
        proposals.append(bound)
    return replace(direct_route, proposals=tuple(proposals))


@pytest.mark.parametrize("symbol,valid", [("600519", True), ("not-a-symbol", False)])
def test_public_idea_binding_validates_without_selecting(symbol: str, valid: bool) -> None:
    compiler = _compiler(
        generator=_NeverCalledGenerator(), idea_router=None,
        backtest_anchor_date=date(2026, 9, 4),
    )
    proposal = _paired_template_route(compiler).proposals[1]
    result = compiler.bind_idea_proposal(
        CompileInput(utterance="我是秦始皇", instrument_context="300059.SZ",
                     as_of_date=date(2026, 8, 6)), proposal, symbol,
    )
    if not valid:
        assert result is None
        return
    assert result is not None
    assert result.instrument_symbol == "600519.SH"
    assert result.instrument_name == "贵州茅台"
    assert result.pairing_reason == proposal.pairing_reason
    assert result.capability_ids == ("technical.ma",)
    assert result.strategy_hash
    assert result.strategy_template == proposal.strategy_template
    assert result.strategy is not None
    assert result.strategy.entry == proposal.strategy_template.entry


@pytest.mark.asyncio
async def test_own_stock_detaches_pairs_then_binds_same_three_templates() -> None:
    class Dialogue:
        def __init__(self) -> None:
            self.requests: list[ClarificationDialogueRequest] = []

        async def assess(
            self, request: ClarificationDialogueRequest,
        ) -> ClarificationDialogueAssessment:
            self.requests.append(request)
            return ClarificationDialogueAssessment(
                reply_kind="question", acknowledgement_id="answer_question",
                natural_reply="当然，三个方向先留着。你想换成哪只股票？",
            )

    dialogue = Dialogue()
    idea_router = _RecordingIdeaRouter(None)
    compiler = StrategyCompiler(
        generator=_NeverCalledGenerator(), idea_router=idea_router,
        clarification_dialogue_router=dialogue,
        catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01",
        backtest_anchor_date=date(2026, 9, 4),
    )
    route = _paired_template_route(compiler)
    first = route.proposals[0]
    prior = CompileOutcome(
        status=CompileStatus.NEEDS_CLARIFICATION, diagnostic_code="idea_guidance_required",
        idea_route=route, strategy=first.strategy, strategy_hash=first.strategy_hash,
        selected_idea_proposal=first, revision_base_strategy=first.strategy,
        pending_idea_instrument="300059.SZ",
        suggested_strategy=first.strategy, suggested_strategy_hash=first.strategy_hash,
        suggested_strategy_choice_id="previous_choice", suggested_strategy_note="old binding",
    )
    now = datetime(2026, 9, 4, tzinfo=UTC)
    memory = VerifiedInstrumentMemory(
        symbol="300059.SZ", name="东方财富", source="fixture", verified_at=now,
    )
    turns = tuple(DialogueTurn(
        user_text=f"历史第 {index} 轮", assistant_text="已有回复", intent="unknown",
        revision=index, created_at=now, verified_instrument=memory,
    ) for index in range(1, 21))
    state = DialogueState.project(
        draft_id=uuid4(), revision=20,
        compile_input=CompileInput(utterance="我是秦始皇", instrument_context="300059.SZ",
                                   as_of_date=date(2026, 9, 4)),
        outcome=prior, created_at=now, recent_turns=turns,
        pending_instrument_reuse=memory,
    )
    orchestrator = DialogueTurnOrchestrator(compiler)
    plan = await orchestrator.plan(state=state, answer="我自己选股票")
    assert plan.clarification_turn is not None
    turn = plan.clarification_turn
    detached = turn.outcome
    assert turn.assistant_message == "当然，三个方向先留着。你想换成哪只股票？"
    assert turn.compile_input.instrument_context is None
    assert plan.pending_instrument_reuse is None
    assert plan.verified_instrument is None
    assert detached.status is CompileStatus.NEEDS_CLARIFICATION
    assert detached.diagnostic_code == "idea_guidance_required"
    assert detached.instrument_suggestion_declined
    assert detached.strategy is detached.strategy_hash is None
    assert detached.revision_base_strategy is detached.selected_idea_proposal is None
    assert detached.pending_idea_instrument is None
    assert detached.suggested_strategy is detached.suggested_strategy_hash is None
    assert detached.suggested_strategy_choice_id is detached.suggested_strategy_note is None
    assert detached.idea_route is not None
    assert detached.idea_route.asset_mapping.instrument_symbol is None
    assert len(detached.idea_route.proposals) == 3
    for old, new in zip(route.proposals, detached.idea_route.proposals, strict=True):
        assert (new.id, new.strategy_template, new.entry_summary, new.exit_summary) == (
            old.id, old.strategy_template, old.entry_summary, old.exit_summary,
        )
        assert new.instrument_symbol is new.instrument_name is new.pairing_reason is None
        assert new.strategy is new.strategy_hash is None
        assert not new.capability_ids
    assert len(dialogue.requests) == 1
    assert dialogue.requests[0].response_only
    assert len(dialogue.requests[0].recent_turns) == 20
    assert tuple(item.user_text for item in dialogue.requests[0].recent_turns) == tuple(
        item.user_text for item in turns
    )
    own_state = replace(
        state, compile_input=turn.compile_input, outcome=detached,
        pending_instrument_reuse=plan.pending_instrument_reuse,
    )
    assert own_state.recent_turns == turns
    assert own_state.verified_instrument_context is None
    own_plan = await orchestrator.plan(state=own_state, answer="600036")
    assert own_plan.clarification_turn is not None
    own_turn = own_plan.clarification_turn
    assert own_turn.outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert own_turn.outcome.idea_route is not None
    assert len(own_turn.outcome.idea_route.proposals) == 3
    assert all(
        item.instrument_symbol == "600036.SH"
        and item.instrument_name is None and item.pairing_reason is None
        for item in own_turn.outcome.idea_route.proposals
    )
    chosen = own_turn.outcome.idea_route.proposals[1]
    selected = await orchestrator.plan(
        state=replace(own_state, compile_input=own_turn.compile_input, outcome=own_turn.outcome),
        answer=chosen.id,
    )
    assert selected.clarification_turn is not None
    ready = selected.clarification_turn.outcome
    assert ready.status is CompileStatus.READY
    assert ready.strategy is not None
    assert ready.strategy.instrument.symbol == "600036.SH"
    assert chosen.strategy_template is not None
    assert ready.strategy.entry == chosen.strategy_template.entry
    assert ready.strategy.exit == chosen.strategy_template.exit
    assert ready.strategy.backtest == chosen.strategy_template.backtest
    assert idea_router.requests == []
    # Also cover direct symbol changes without first pressing the own-stock action.
    rebound = compiler.bind_selected_idea(
        replace(state.compile_input, instrument_context="600036.SH"),
        replace(prior, selected_idea_proposal=None),
    )
    assert rebound is not None and rebound.idea_route is not None
    assert all(item.instrument_name is None and item.pairing_reason is None
               for item in rebound.idea_route.proposals)


@pytest.mark.asyncio
@pytest.mark.parametrize("resolved", [True, False])
async def test_model_stock_selection_binds_existing_templates_before_acknowledging(
    resolved: bool,
) -> None:
    class Dialogue:
        async def assess(
            self, request: ClarificationDialogueRequest,
        ) -> ClarificationDialogueAssessment:
            return ClarificationDialogueAssessment(
                reply_kind="preference", acknowledgement_id="respect_preference",
                natural_reply="生益科技已接上，你想用哪个策略方向？",
                instrument_name="生益科技", instrument_selected=True,
            )

    def resolve_name(name: str) -> str:
        assert name == "生益科技"
        if not resolved:
            raise LookupError(name)
        return "600183.SH"

    ideas = _RecordingIdeaRouter(None)
    compiler = StrategyCompiler(
        generator=_NeverCalledGenerator(), idea_router=ideas,
        clarification_dialogue_router=Dialogue(), instrument_name_resolver=resolve_name,
        catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01",
        backtest_anchor_date=date(2026, 9, 4),
    )
    route = _paired_template_route(compiler)
    route = replace(route, proposals=tuple(replace(
        p, strategy=None, strategy_hash=None, instrument_symbol=None,
        instrument_name=None, pairing_reason=None,
    ) for p in route.proposals), asset_mapping=replace(
        route.asset_mapping, instrument_symbol=None,
    ))
    original = CompileInput(utterance="低买高卖", as_of_date=date(2026, 9, 4))
    prior = CompileOutcome(
        status=CompileStatus.NEEDS_CLARIFICATION, diagnostic_code="idea_guidance_required",
        idea_route=route, clarification="你想用哪只股票？",
    )
    state = DialogueState.project(
        draft_id=uuid4(), revision=2, compile_input=original, outcome=prior,
        created_at=datetime(2026, 9, 4, tzinfo=UTC), recent_turns=(),
    )
    plan = await DialogueTurnOrchestrator(compiler).plan(state=state, answer="用生益科技。")
    turn = plan.clarification_turn
    assert turn is not None
    assert ideas.requests == []
    if not resolved:
        assert not turn.revision_changed
        assert turn.outcome is prior
        assert "已接上" not in turn.assistant_message
        assert plan.verified_instrument is None
        return
    assert turn.revision_changed
    assert turn.compile_input.instrument_context == "600183.SH"
    assert turn.assistant_message == "生益科技已接上，你想用哪个策略方向？"
    assert turn.outcome.pending_idea_instrument == "600183.SH"
    assert plan.verified_instrument is not None
    assert plan.verified_instrument.symbol == "600183.SH"
    assert plan.verified_instrument.name == "生益科技"
    assert turn.outcome.idea_route is not None
    for old, new in zip(route.proposals, turn.outcome.idea_route.proposals, strict=True):
        assert new.instrument_symbol == "600183.SH"
        assert new.instrument_name == "生益科技"
        assert new.id == old.id and new.strategy_template == old.strategy_template
        assert new.strategy is not None
    # Supplying complete rules after choosing a stock completes the pending idea.
    # It must not discard that confirmed binding and ask to select another stock.
    complete_compiler = _compiler(generator=_RecordingRuleBasedGenerator(), idea_router=None)
    completed = await DialogueTurnOrchestrator(complete_compiler).plan(
        state=replace(state, compile_input=turn.compile_input, outcome=turn.outcome),
        answer="就按14日RSI低于30买、高于70卖，最近一年。",
    )
    assert completed.clarification_turn is not None
    ready = completed.clarification_turn.outcome
    assert ready.status is CompileStatus.READY
    assert ready.strategy is not None and ready.strategy.instrument.symbol == "600183.SH"


@pytest.mark.asyncio
async def test_model_option_selection_reuses_exact_displayed_strategy() -> None:
    class Dialogue:
        async def assess(
            self, request: ClarificationDialogueRequest,
        ) -> ClarificationDialogueAssessment:
            return ClarificationDialogueAssessment(
                reply_kind="preference", acknowledgement_id="respect_preference",
                natural_reply="选好了第二个组合，可以先核对这份规则。",
                selected_option_id=request.options[1].id,
            )

    compiler = StrategyCompiler(
        generator=_NeverCalledGenerator(), idea_router=_RecordingIdeaRouter(None),
        clarification_dialogue_router=Dialogue(),
        catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01",
        backtest_anchor_date=date(2026, 9, 4),
    )
    route = _paired_template_route(compiler)
    prior = CompileOutcome(
        status=CompileStatus.NEEDS_CLARIFICATION, diagnostic_code="idea_guidance_required",
        idea_route=route, clarification="你想用哪个组合？",
    )
    turn = await compiler.answer_clarification(
        original_input=CompileInput(utterance="我是秦始皇", as_of_date=date(2026, 9, 4)),
        prior_outcome=prior, answer="就用第二个组合吧。",
    )
    assert turn.outcome.status is CompileStatus.READY
    assert turn.outcome.strategy == route.proposals[1].strategy
    assert turn.outcome.run_requested is False
    assert turn.assistant_message == "选好了第二个组合，可以先核对这份规则。"


@pytest.mark.asyncio
async def test_model_stock_and_selected_option_keep_ready_binding_without_reparse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Dialogue:
        def __init__(self) -> None:
            self.requests: list[ClarificationDialogueRequest] = []

        async def assess(
            self, request: ClarificationDialogueRequest,
        ) -> ClarificationDialogueAssessment:
            self.requests.append(request)
            return ClarificationDialogueAssessment(
                reply_kind="preference", acknowledgement_id="respect_preference",
                natural_reply="已将生益科技接到刚才选定的方案，可以核对规则。",
                instrument_name="生益科技", instrument_selected=True,
                selected_option_id=request.options[0].id,
            )

    resolved_names: list[str] = []

    def resolve_name(name: str) -> str:
        resolved_names.append(name)
        assert name == "生益科技"
        return "600183.SH"

    dialogue = Dialogue()
    ideas = _RecordingIdeaRouter(None)
    compiler = StrategyCompiler(
        generator=_NeverCalledGenerator(), idea_router=ideas,
        clarification_dialogue_router=dialogue, instrument_name_resolver=resolve_name,
        catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01",
        backtest_anchor_date=date(2026, 9, 4),
    )
    route = _paired_template_route(compiler)
    route = replace(route, proposals=tuple(replace(
        proposal, strategy=None, strategy_hash=None, instrument_symbol=None,
        instrument_name=None, pairing_reason=None,
    ) for proposal in route.proposals), asset_mapping=replace(
        route.asset_mapping, instrument_symbol=None,
    ))
    selected = route.proposals[0]
    assert selected.strategy_template is not None
    prior = CompileOutcome(
        status=CompileStatus.NEEDS_CLARIFICATION, diagnostic_code="instrument_required",
        selected_idea_proposal=selected, idea_route=route, clarification="想用哪只股票？",
    )
    original_bind = compiler.bind_selected_idea
    ready_bindings: list[CompileOutcome] = []

    def record_ready_binding(
        request: CompileInput, prior_outcome: CompileOutcome,
    ) -> CompileOutcome:
        bound = original_bind(request, prior_outcome)
        assert bound is not None and bound.status is CompileStatus.READY
        assert bound.idea_route is None
        ready_bindings.append(bound)
        return bound

    monkeypatch.setattr(compiler, "bind_selected_idea", record_ready_binding)
    turn = await compiler.answer_clarification(
        original_input=CompileInput(utterance="低买高卖", as_of_date=date(2026, 9, 4)),
        prior_outcome=prior, answer="用生益科技和刚才选定的方案。",
    )

    assert len(ready_bindings) == 1
    assert len(dialogue.requests) == 1
    assert resolved_names == ["生益科技"]
    assert ideas.requests == []
    assert turn.revision_changed
    assert turn.outcome.status is CompileStatus.READY
    assert turn.outcome.strategy == selected.strategy_template.bind("600183.SH")
    assert turn.outcome.run_requested is False
    assert turn.assistant_message == "已将生益科技接到刚才选定的方案，可以核对规则。"


@pytest.mark.asyncio
async def test_unbound_model_proposal_waits_for_instrument_before_recompile() -> None:
    route = replace(
        _idea_route(instrument_symbol=None),
        provenance=_direct_idea_route().provenance,
    )
    generator = _RecordingRuleBasedGenerator()
    compiler = _compiler(generator=generator, idea_router=None)
    original = CompileInput(
        utterance="我看好保险股，给我三个可回测方向",
        instrument_context=None,
        as_of_date=date(2026, 8, 30),
    )
    prior = CompileOutcome(
        status=CompileStatus.NEEDS_CLARIFICATION,
        clarification="请先选一个方向，再补充标的。",
        diagnostic_code="idea_guidance_required",
        idea_route=route,
    )

    selected = await compiler.answer_clarification(
        original_input=original,
        prior_outcome=prior,
        answer="1",
    )

    expected_utterance = route.proposals[0].suggested_utterance
    assert generator.requests == []
    assert selected.outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert selected.outcome.diagnostic_code == "instrument_required"
    assert selected.compile_input.utterance == expected_utterance
    assert selected.compile_input.instrument_context is None
    assert selected.outcome.candidate_provenance is not None
    assert selected.outcome.candidate_provenance.provider == "deepseek"

    supplemented = await compiler.answer_clarification(
        original_input=selected.compile_input,
        prior_outcome=selected.outcome,
        answer="601318",
    )

    assert supplemented.outcome.status is CompileStatus.READY
    assert supplemented.outcome.strategy is not None
    assert supplemented.outcome.strategy.instrument.symbol == "601318.SH"
    assert supplemented.compile_input.utterance == f"601318，{expected_utterance}"
    assert [request.utterance for request in generator.requests] == [
        "601318",
        f"601318，{expected_utterance}",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("utterance", "semantic_intent"),
    (
        ("我讨厌特朗普", "viewpoint"),
        ("我讨厌特朗普的关税政策，想在东方财富（300059）上验证策略，"
         "给我三个近一年可回测的完整买卖方案，本金10万元。", "vague_strategy"),
        ("我看好国产算力", "viewpoint"),
        ("降息可能让成长股更受欢迎吗", "viewpoint"),
        ("这家公司管理层让我不放心", "viewpoint"),
        ("AI会不会是泡沫", "viewpoint"),
    ),
)
async def test_broad_everyday_viewpoints_are_guided_before_strict_translation(
    utterance: str,
    semantic_intent: Literal["viewpoint", "vague_strategy"],
) -> None:
    # This verifies routing after model intent classification, not a second
    # lexical classifier or fallback from an invalid strategy-model response.
    generator = _UnsupportedGenerator("candidate_provider_invalid_output")
    idea_router = _RecordingIdeaRouter(_idea_route())
    compiler = _compiler(generator=generator, idea_router=idea_router)

    outcome = await compiler.compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
            semantic_intent=semantic_intent,
        )
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "idea_guidance_required"
    assert outcome.idea_route is not None
    assert {item.utterance for item in generator.requests} == _STRATEGY_UTTERANCES
    assert all(
        proposal.capability_ids != ("provider.claim.must.be.replaced",)
        for proposal in outcome.idea_route.proposals
    )
    assert len(idea_router.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("utterance", ("今天天气怎么样", "晚上吃什么"))
async def test_obvious_lifestyle_question_never_reaches_idea_or_strategy_provider(
    utterance: str,
) -> None:
    generator = _UnsupportedGenerator("candidate_provider_invalid_output")
    idea_router = _RecordingIdeaRouter(_idea_route())
    compiler = _compiler(generator=generator, idea_router=idea_router)

    outcome = await compiler.compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "conversation_only"
    assert generator.requests == []
    assert idea_router.requests == []


@pytest.mark.asyncio
async def test_explicit_code_in_viewpoint_is_bound_locally_without_name_lookup() -> None:
    idea_router = _RecordingIdeaRouter(_idea_route(instrument_symbol="300059.SZ"))
    compiler = _compiler(
        generator=_UnsupportedGenerator("candidate_provider_invalid_output"),
        idea_router=idea_router,
    )

    outcome = await compiler.compile(
        CompileInput(
            utterance="300059怎么样，给我三个策略",
            instrument_context=None,
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "idea_guidance_required"
    assert idea_router.requests[0].instrument_context == "300059.SZ"
    assert outcome.candidate_grounding[0].text == "300059"


@pytest.mark.asyncio
async def test_analysis_first_company_name_uses_authoritative_resolver() -> None:
    resolved_names: list[str] = []

    def resolver(name: str) -> str:
        resolved_names.append(name)
        return {"东方财富": "300059.SZ"}[name]

    idea_router = _RecordingIdeaRouter(_idea_route(instrument_symbol="300059.SZ"))
    compiler = StrategyCompiler(
        generator=_UnsupportedGenerator("no_supported_signal_recognized"),
        idea_router=idea_router,
        instrument_name_resolver=resolver,
        catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals",
        release_version="2026.09.01",
    )

    outcome = await compiler.compile(
        CompileInput(
            utterance="分析东方财富的相关公开信息，给我几个可回测策略",
            instrument_context=None,
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "idea_guidance_required"
    assert resolved_names == ["东方财富"]
    assert idea_router.requests[0].instrument_context == "300059.SZ"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("diagnostic_code", "candidate_count"),
    [
        ("no_supported_signal_recognized", 1),
        ("no_supported_signal_recognized", 3),
    ],
)
async def test_literal_miss_batch_routes_to_non_executable_guidance(
    diagnostic_code: str,
    candidate_count: int,
) -> None:
    generator = _UnsupportedGenerator(diagnostic_code, count=candidate_count)
    idea_router = _RecordingIdeaRouter(_idea_route())
    compiler = _compiler(generator=generator, idea_router=idea_router)

    outcome = await compiler.compile(
        CompileInput(
            utterance="我想买入这只股票，但你帮我决定什么时候卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "idea_guidance_required"
    assert outcome.idea_route is not None
    assert len(generator.requests) == 4
    assert len(idea_router.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("utterance", "semantic_intent"), [
    ("这个观点成立时买入，不成立时卖出", None),
    ("贵州茅台，收盘价创前20日新高且成交量超过前20日均量1.5倍买入；"
     "从持仓后最高收盘价回撤8%或持有满40个交易日卖出。", "new_strategy"),
    ("东方财富发布大股东增持公告后次日买入，持有30个交易日卖出", "new_strategy"),
])
async def test_invalid_literal_candidate_stays_fail_closed_without_idea_rewrite(
    utterance: str, semantic_intent: str | None,
) -> None:
    idea_router = _RecordingIdeaRouter(_idea_route())
    generator = _UnsupportedGenerator("candidate_provider_invalid_output")
    compiler = _compiler(
        generator=generator,
        idea_router=idea_router,
    )
    request = CompileInput(
        utterance=utterance,
        instrument_context="300059.SZ",
        as_of_date=date(2026, 8, 30),
        semantic_intent=semantic_intent,
    )
    outcome = await compiler.compile(request)

    assert outcome.status is CompileStatus.UNSUPPORTED
    assert outcome.diagnostic_code == "candidate_provider_invalid_output"
    assert outcome.idea_route is None
    assert generator.requests == [request]
    assert idea_router.requests == []
    assert outcome.strategy is None and not outcome.run_requested


@pytest.mark.asyncio
async def test_idea_guidance_rejects_explicit_failed_candidate_before_any_model_call() -> None:
    router = _RecordingIdeaRouter(_idea_route())
    compiler = _compiler(generator=_NeverCalledGenerator(), idea_router=router)
    candidate = CandidateAst(
        instrument_symbol=None, entry=(), exit=(), confidence=0.0,
        unsupported_code="candidate_provider_invalid_output",
    )
    outcome = await compiler._compile_idea_guidance(  # pyright: ignore[reportPrivateUsage]
        CompileInput(utterance="需要保留的原规则", as_of_date=date(2026, 8, 30)),
        known_candidate=candidate, propose_defaults=True,
    )
    assert outcome is not None
    assert outcome.diagnostic_code == "candidate_provider_invalid_output"
    assert outcome.strategy is None and outcome.idea_route is None and not outcome.run_requested
    assert router.requests == []


@pytest.mark.asyncio
async def test_complete_strategy_keeps_the_existing_fast_path() -> None:
    idea_router = _RecordingIdeaRouter(_idea_route())
    compiler = _compiler(
        generator=RuleBasedCandidateGenerator(),
        idea_router=idea_router,
    )

    outcome = await compiler.compile(
        CompileInput(
            utterance="MACD金叉买入，死叉卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert outcome.idea_route is None
    assert idea_router.requests == []


@pytest.mark.asyncio
async def test_vague_strategy_prefers_model_idea_route_before_local_guidance() -> None:
    idea_router = _RecordingIdeaRouter(_idea_route())
    compiler = _compiler(
        generator=RuleBasedCandidateGenerator(),
        idea_router=idea_router,
    )

    outcome = await compiler.compile(
        CompileInput(
            utterance="低买高卖",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "idea_guidance_required"
    assert outcome.idea_route is not None
    assert outcome.idea_route is not idea_router.result
    assert [item.capability_ids for item in outcome.idea_route.proposals] == [
        ("technical.ma",),
        ("technical.rsi",),
        ("technical.macd",),
    ]
    assert len(idea_router.requests) == 1


@pytest.mark.asyncio
async def test_explicit_unsupported_semantics_never_enter_idea_routing() -> None:
    idea_router = _RecordingIdeaRouter(_idea_route())
    compiler = _compiler(
        generator=RuleBasedCandidateGenerator(),
        idea_router=idea_router,
    )

    outcome = await compiler.compile(
        CompileInput(
            utterance="5分钟MACD金叉买入，5分钟死叉卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.UNSUPPORTED
    assert outcome.diagnostic_code == "non_daily_timeframe_not_supported"
    assert outcome.idea_route is None
    assert idea_router.requests == []


@pytest.mark.asyncio
async def test_view_without_stock_keeps_model_directions_pending_confirmation() -> None:
    idea_router = _RecordingIdeaRouter(_idea_route(instrument_symbol=None))
    compiler = _compiler(
        generator=_UnsupportedGenerator("no_supported_signal_recognized"),
        idea_router=idea_router,
    )

    outcome = await compiler.compile(
        CompileInput(
            utterance="我讨厌特朗普",
            instrument_context=None,
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "idea_guidance_required"
    assert outcome.idea_route is not None
    assert outcome.idea_route.asset_mapping.evidence_status == "instrument_required"
    assert all(item.strategy is None for item in outcome.idea_route.proposals)
    assert outcome.strategy is None
    assert outcome.clarification == outcome.idea_route.understanding
    assert len(idea_router.requests) == 1


@pytest.mark.asyncio
async def test_ambiguous_latin_preference_never_enters_investment_idea_routing() -> None:
    idea_router = _RecordingIdeaRouter(_idea_route(instrument_symbol=None))
    compiler = _compiler(
        generator=_UnsupportedGenerator("no_supported_signal_recognized"),
        idea_router=idea_router,
    )

    outcome = await compiler.compile(
        CompileInput(
            utterance="我讨厌wash",
            instrument_context=None,
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "conversation_only"
    assert outcome.idea_route is None
    assert idea_router.requests == []


@pytest.mark.asyncio
async def test_selected_viewpoint_direction_keeps_the_resolved_stock_context() -> None:
    researched_route = _idea_route()
    researched_route = replace(
        researched_route,
        asset_mapping=IdeaAssetMapping(
            instrument_symbol=None,
            relation="unbound",
            rationale="每张卡片分别绑定服务端已核验标的。",
            evidence_status="instrument_required",
        ),
    )
    idea_router = _RecordingIdeaRouter(researched_route)
    compiler = _compiler(
        generator=RuleBasedCandidateGenerator(),
        idea_router=idea_router,
    )
    original = CompileInput(
        utterance="我看好东方财富",
        instrument_context=None,
        as_of_date=date(2026, 8, 30),
    )
    prior = await compiler.compile(original)

    turn = await compiler.answer_clarification(
        original_input=original,
        prior_outcome=prior,
        answer="1",
    )

    assert turn.outcome.status is CompileStatus.READY
    assert turn.outcome.strategy is not None
    assert turn.outcome.strategy.instrument.symbol == "300059.SZ"


@pytest.mark.asyncio
@pytest.mark.parametrize("utterance,name,symbol", [
    ("东方财富短线策略", "东方财富", "300059.SZ"),
    ("帮我给东方财富设计短线方向", "东方财富", "300059.SZ"),
    ("贵州茅台我想低买高卖", "贵州茅台", "600519.SH"),
    ("dfcf短线策略", "dfcf", "300059.SZ"),
    ("300059.SZ 短线策略", "300059.SZ", "300059.SZ"),
])
async def test_model_named_stock_identity_binds_vague_ideas_without_chinese_reparse(
    utterance: str, name: str, symbol: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[ClarificationDialogueRequest] = []
    resolved_names: list[str] = []

    class Dialogue:
        async def assess(
            self, request: ClarificationDialogueRequest,
        ) -> ClarificationDialogueAssessment:
            requests.append(request)
            return ClarificationDialogueAssessment(
                reply_kind="preference", acknowledgement_id="respect_preference",
                natural_reply="已提取用户指定的股票。",
                instrument_name=name, instrument_selected=True,
            )

    def resolver(value: str) -> str:
        resolved_names.append(value)
        return {name: symbol}[value]

    def no_keyword_extraction(_: str) -> None:
        raise AssertionError("model identity must not be reinterpreted by Chinese keywords")

    monkeypatch.setattr(
        "ashare_lab.application.compile_strategy._idea_guidance_instrument_mention",
        no_keyword_extraction,
    )
    original_route = _direct_idea_route()
    proposals: list[IdeaProposal] = []
    for proposal in original_route.proposals:
        assert proposal.strategy is not None
        proposals.append(replace(
            proposal, instrument_symbol=symbol,
            strategy=proposal.strategy.model_copy(update={"instrument": Instrument(symbol=symbol)}),
        ))
    ideas = _RecordingIdeaRouter(replace(
        original_route, proposals=tuple(proposals),
        asset_mapping=replace(original_route.asset_mapping, instrument_symbol=symbol),
    ))
    compiler = StrategyCompiler(
        generator=_NeverCalledGenerator(), catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01", idea_router=ideas,
        clarification_dialogue_router=Dialogue(), instrument_name_resolver=resolver,
    )
    outcome = await compiler.compile(CompileInput(
        utterance=utterance, as_of_date=date(2026, 9, 4), semantic_intent="vague_strategy",
    ))
    assert len(requests) == 1 and requests[0].identity_only
    assert requests[0].answer == utterance
    assert resolved_names == ([] if name == symbol else [name])
    assert len(ideas.requests) == 1 and ideas.requests[0].instrument_context == symbol
    assert outcome.idea_route is not None and len(outcome.idea_route.proposals) == 3
    assert all(item.instrument_symbol == symbol for item in outcome.idea_route.proposals)
    assert outcome.candidate_grounding == (CandidateGroundingEvidence(
        path="/instrument/symbol", start=utterance.index(name),
        end=utterance.index(name) + len(name), text=name,
    ),)
    assert not outcome.run_requested


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,diagnostic", [
    ("model_none", "idea_instrument_extraction_unavailable"),
    ("model_timeout", "idea_instrument_extraction_unavailable"),
    ("inferred_span", "idea_instrument_extraction_unavailable"),
    ("invalid_identity", "idea_instrument_extraction_unavailable"),
    ("resolver_missing", "instrument_unconfirmed"),
    ("resolver_invalid", "instrument_unconfirmed"),
    ("resolver_timeout", "instrument_resolution_unavailable"),
])
async def test_named_stock_identity_failure_never_falls_back_to_market_recommendations(
    failure: str, diagnostic: str,
) -> None:
    class Dialogue:
        async def assess(
            self, request: ClarificationDialogueRequest,
        ) -> ClarificationDialogueAssessment | None:
            if failure == "model_none":
                return None
            if failure == "model_timeout":
                raise TimeoutError("identity service timeout")
            return ClarificationDialogueAssessment(
                reply_kind="preference", acknowledgement_id="respect_preference",
                natural_reply="已提取股票身份。",
                instrument_name="贵州茅台" if failure == "inferred_span" else "东方财富",
                instrument_selected=failure != "invalid_identity",
            )

    def resolver(_: str) -> str:
        if failure == "resolver_timeout":
            raise TimeoutError("instrument service timeout")
        if failure == "resolver_missing":
            raise LookupError("no verified identity")
        return "not-a-security" if failure == "resolver_invalid" else "300059.SZ"

    ideas = _RecordingIdeaRouter(_idea_route(instrument_symbol=None))
    compiler = StrategyCompiler(
        generator=_NeverCalledGenerator(), catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01", idea_router=ideas,
        clarification_dialogue_router=Dialogue(), instrument_name_resolver=resolver,
    )
    outcome = await compiler.compile(CompileInput(
        utterance="东方财富短线策略", as_of_date=date(2026, 9, 4), semantic_intent="vague_strategy",
    ))
    assert outcome.diagnostic_code == diagnostic
    assert outcome.idea_route is None and outcome.strategy is None
    assert not outcome.instrument_suggestion_declined and not outcome.run_requested
    assert ideas.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ambiguous", [False, True])
async def test_model_identity_distinguishes_no_stock_from_ambiguous_stocks(ambiguous: bool) -> None:
    reply = "你想使用东方财富还是贵州茅台？"

    class Dialogue:
        async def assess(
            self, request: ClarificationDialogueRequest,
        ) -> ClarificationDialogueAssessment:
            return ClarificationDialogueAssessment(
                reply_kind="unclear" if ambiguous else "preference",
                acknowledgement_id="ask_rephrase" if ambiguous else "respect_preference",
                natural_reply=reply if ambiguous else "未指定股票。",
            )

    def no_resolver(_: str) -> str:
        raise AssertionError("unselected identity must not call the resolver")

    ideas = _RecordingIdeaRouter(_idea_route(instrument_symbol=None))
    compiler = StrategyCompiler(
        generator=_NeverCalledGenerator(), catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01", idea_router=ideas,
        clarification_dialogue_router=Dialogue(), instrument_name_resolver=no_resolver,
    )
    outcome = await compiler.compile(CompileInput(
        utterance="东方财富或者贵州茅台，短线策略" if ambiguous else "短线策略",
        as_of_date=date(2026, 9, 4), semantic_intent="vague_strategy",
    ))
    if ambiguous:
        assert outcome.idea_route is None and ideas.requests == []
        assert outcome.clarification == reply and not outcome.instrument_suggestion_declined
    else:
        assert outcome.idea_route is not None and len(ideas.requests) == 1
        assert ideas.requests[0].instrument_context is None
        assert not outcome.instrument_suggestion_declined


@pytest.mark.asyncio
async def test_dialogue_inspiration_named_stock_lookup_failure_stays_bound_to_user_intent() -> None:
    class Dialogue:
        async def assess(
            self, request: ClarificationDialogueRequest,
        ) -> ClarificationDialogueAssessment:
            assert not request.identity_only
            return ClarificationDialogueAssessment(
                reply_kind="question", acknowledgement_id="answer_question",
                natural_reply="可以围绕这只股票设计可编辑的短线方向。",
                strategy_inspiration="给出可编辑的短线策略。", instrument_name="东方财富",
            )

    def resolver(_: str) -> str:
        raise TimeoutError("instrument service timeout")

    ideas = _RecordingIdeaRouter(_idea_route(instrument_symbol=None))
    compiler = StrategyCompiler(
        generator=_NeverCalledGenerator(), catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01", idea_router=ideas,
        clarification_dialogue_router=Dialogue(), instrument_name_resolver=resolver,
    )
    outcome = await compiler.compile(CompileInput(
        utterance="东方财富怎么玩短线", as_of_date=date(2026, 9, 4), semantic_intent="unknown",
    ))
    assert outcome.diagnostic_code == "instrument_resolution_unavailable"
    assert outcome.idea_route is None and not outcome.instrument_suggestion_declined
    assert ideas.requests == []


@pytest.mark.asyncio
async def test_stock_first_guidance_prompt_keeps_the_resolved_stock_context() -> None:
    resolved_names: list[str] = []

    def resolver(name: str) -> str:
        resolved_names.append(name)
        return {"同花顺": "300033.SZ"}[name]

    idea_router = _RecordingIdeaRouter(_idea_route(instrument_symbol="300033.SZ"))
    compiler = StrategyCompiler(
        generator=RuleBasedCandidateGenerator(),
        idea_router=idea_router,
        instrument_name_resolver=resolver,
        catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals",
        release_version="2026.09.01",
    )
    original = CompileInput(
        utterance="同花顺咋样，给我三个策略",
        instrument_context=None,
        as_of_date=date(2026, 8, 30),
    )
    prior = await compiler.compile(original)

    assert prior.status is CompileStatus.NEEDS_CLARIFICATION
    assert prior.idea_route is not None
    assert prior.idea_route.asset_mapping.instrument_symbol == "300033.SZ"
    assert resolved_names == ["同花顺"]

    turn = await compiler.answer_clarification(
        original_input=original,
        prior_outcome=prior,
        answer="1",
    )

    assert turn.outcome.status is CompileStatus.READY
    assert turn.outcome.strategy is not None
    assert turn.outcome.strategy.instrument.symbol == "300033.SZ"


@pytest.mark.asyncio
@pytest.mark.parametrize("utterance", [
    "同花顺低买高卖", "同花顺 我想低买高卖", "同花顺，我想要低买高卖",
    "同花顺帮我做低买高卖", "同花顺这只股票，我希望低买高卖",
])
async def test_stock_first_low_buy_high_sell_prompt_keeps_the_resolved_stock_context(
    utterance: str,
) -> None:
    def resolver(name: str) -> str:
        return {"同花顺": "300033.SZ"}[name]

    idea_router = _RecordingIdeaRouter(_idea_route(instrument_symbol="300033.SZ"))
    compiler = StrategyCompiler(
        generator=RuleBasedCandidateGenerator(),
        idea_router=idea_router,
        instrument_name_resolver=resolver,
        catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals",
        release_version="2026.09.01",
    )

    outcome = await compiler.compile(
        CompileInput(
            utterance=utterance,
            instrument_context=None,
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "idea_guidance_required"
    assert outcome.idea_route is not None
    assert outcome.idea_route.asset_mapping.instrument_symbol == "300033.SZ"
    assert idea_router.requests[0].instrument_context == "300033.SZ"
    assert outcome.suggested_strategy is not None
    assert outcome.suggested_strategy_hash is not None
    assert outcome.suggested_strategy_choice_id == "idea_000000000001"
    assert outcome.suggested_strategy.instrument.symbol == "300033.SZ"
    assert outcome.idea_route.proposals[0].capability_ids == ("technical.ma",)


@pytest.mark.asyncio
async def test_broad_viewpoint_stays_choice_only_without_a_provisional_strategy() -> None:
    compiler = _compiler(
        generator=RuleBasedCandidateGenerator(),
        idea_router=_RecordingIdeaRouter(_idea_route()),
    )

    outcome = await compiler.compile(
        CompileInput(
            utterance="我看好东方财富后面要涨",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.idea_route is not None
    assert outcome.suggested_strategy is None
    assert outcome.suggested_strategy_hash is None


@pytest.mark.parametrize("utterance,name", [
    ("贵州茅台 我想低买高卖", "贵州茅台"),
    ("贵州茅台，我想要低买高卖", "贵州茅台"),
    ("东方财富这只股票，我希望低买高卖", "东方财富"),
    ("同花顺帮我做低买高卖", "同花顺"),
])
def test_stock_first_intent_filler_preserves_exact_name_span(utterance: str, name: str) -> None:
    from ashare_lab.application.compile_strategy import _idea_guidance_instrument_mention

    mention = _idea_guidance_instrument_mention(utterance)
    assert mention is not None
    assert mention.name == name
    assert utterance[mention.start:mention.end] == name


@pytest.mark.asyncio
async def test_imprecise_stock_bound_source_semantics_falls_back_to_guidance() -> None:
    def resolver(name: str) -> str:
        return {"同花顺": "300033.SZ"}[name]

    generator = HybridCandidateGenerator(
        deterministic=RuleBasedCandidateGenerator(),
        bounded_fallback=_UnsupportedGenerator("candidate_provider_invalid_output"),
        instrument_name_resolver=resolver,
    )
    idea_router = _RecordingIdeaRouter(_idea_route(instrument_symbol="300033.SZ"))
    compiler = StrategyCompiler(
        generator=generator,
        idea_router=idea_router,
        instrument_name_resolver=resolver,
        catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals",
        release_version="2026.09.01",
    )

    outcome = await compiler.compile(
        CompileInput(
            utterance="同花顺放量买入，破位不买",
            instrument_context=None,
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "idea_guidance_required"
    assert outcome.idea_route is not None
    assert outcome.idea_route.asset_mapping.instrument_symbol == "300033.SZ"
    assert idea_router.requests[0].instrument_context == "300033.SZ"


@pytest.mark.asyncio
async def test_negative_theme_viewpoint_is_not_misread_as_a_company_name() -> None:
    resolved_names: list[str] = []

    def resolver(name: str) -> str:
        resolved_names.append(name)
        raise LookupError(name)

    idea_router = _RecordingIdeaRouter(_idea_route(instrument_symbol=None))
    compiler = StrategyCompiler(
        generator=_UnsupportedGenerator("no_supported_signal_recognized"),
        idea_router=idea_router,
        instrument_name_resolver=resolver,
        catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals",
        release_version="2026.09.01",
    )

    outcome = await compiler.compile(
        CompileInput(
            utterance="我不喜欢新能源",
            instrument_context=None,
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.diagnostic_code == "idea_guidance_required"
    assert outcome.idea_route is not None
    assert outcome.idea_route.asset_mapping.instrument_symbol is None
    assert resolved_names == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "diagnostic", "message"),
    [
        (IdeaGenerationError("transport"), "idea_guidance_model_unavailable", "连接暂时失败"),
        (IdeaGenerationError("schema"), "idea_guidance_schema_invalid", "自动修复后仍未完成"),
        (IdeaGenerationError("explanation"), "idea_guidance_explanation_unverified",
         "说明与事实依据或交易规则仍未核对通过"),
        (IdeaGenerationError("execution"), "idea_guidance_execution_invalid", "未通过执行校验"),
        (IdeaStockSelectionUnavailableError("private upstream detail", stage="planning"),
         "idea_stock_selection_unavailable", "本次未发起选股或回测"),
        (IdeaStockSelectionUnavailableError("private upstream detail"),
         "idea_stock_selection_unavailable", "选股未能取得可用结果"),
        (IdeaStockSelectionUnavailableError("private SQL", reason="provider_sql_error", attempts=3),
         "idea_stock_selection_unavailable", "选股服务暂时不稳定。我已自动重试 2 次"),
        (IdeaStockSelectionUnavailableError("private key", reason="authentication_failed"),
         "idea_stock_selection_unavailable", "选股 Key 的授权未通过"),
        (IdeaStockSelectionUnavailableError("private data", reason="data_no_results", attempts=3),
         "idea_stock_selection_unavailable", "仍未返回匹配数据"),
    ],
)
async def test_idea_generation_failures_explain_the_actual_stage(
    failure: IdeaGenerationError | IdeaStockSelectionUnavailableError,
    diagnostic: str, message: str,
) -> None:
    class FailingRouter(_RecordingIdeaRouter):
        async def route(self, request: CompileInput) -> IdeaRoute | None:
            raise failure

    compiler = _compiler(
        generator=_UnsupportedGenerator("no_supported_signal_recognized"),
        idea_router=FailingRouter(None),
    )
    outcome = await compiler.compile(CompileInput(
        utterance="我讨厌特朗普", instrument_context="300059.SZ", as_of_date=date(2026, 8, 30),
    ))
    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == diagnostic
    assert message in (outcome.clarification or "")
    assert "private upstream" not in (outcome.clarification or "")
    assert outcome.strategy is None and outcome.idea_route is None


@pytest.mark.asyncio
async def test_malformed_vague_t_guidance_preserves_the_trade_intent_and_asks_only_for_terms() -> None:
    class FailingRouter(_RecordingIdeaRouter):
        async def route(self, request: CompileInput) -> IdeaRoute | None:
            raise IdeaGenerationError("schema")

    compiler = _compiler(
        generator=_UnsupportedGenerator("no_supported_signal_recognized"),
        idea_router=FailingRouter(None),
    )
    outcome = await compiler.compile(CompileInput(
        utterance="东方财富留点底仓做T",
        instrument_context="300059.SZ",
        as_of_date=date(2026, 8, 30),
    ))
    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "t_trade_details_required"
    assert "保留底仓的做T计划" in (outcome.clarification or "")
    assert "当日新买" in (outcome.clarification or "")
    assert any(item.path == "/trading_plan/kind" and item.text == "留点底仓做T"
               for item in outcome.candidate_grounding)


def test_t_guidance_requires_actual_opening_inventory_not_only_a_bottom_label() -> None:
    proposal = _idea_route().proposals[0]
    def shaped(opening: int, minimum: int, initial: int):
        return replace(proposal, strategy=SimpleNamespace(
            trading_plan=SimpleNamespace(parameters=SimpleNamespace(
                opening_shares=opening, min_shares=minimum, initial_shares=initial,
            )),
        ))
    assert not _proposal_has_real_opening_inventory(shaped(0, 1000, 0))
    assert not _proposal_has_real_opening_inventory(shaped(500, 1000, 0))
    assert not _proposal_has_real_opening_inventory(shaped(1000, 500, 100))
    assert _proposal_has_real_opening_inventory(shaped(1000, 500, 0))


@pytest.mark.asyncio
async def test_empty_guidance_batch_is_not_exposed() -> None:
    idea_router = _RecordingIdeaRouter(_idea_route(proposal_count=0))
    compiler = _compiler(
        generator=_UnsupportedGenerator("no_supported_signal_recognized"),
        idea_router=idea_router,
    )

    outcome = await compiler.compile(
        CompileInput(
            utterance="我讨厌特朗普",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "idea_guidance_execution_invalid"
    assert outcome.idea_route is None


@pytest.mark.asyncio
async def test_invalid_proposal_is_dropped_but_two_compiler_gated_choices_remain() -> None:
    route = _idea_route()
    invalid = replace(
        route.proposals[2],
        suggested_utterance="凭感觉买入，心情不好卖出，回测近1年",
    )
    compiler = _compiler(
        generator=RuleBasedCandidateGenerator(),
        idea_router=_RecordingIdeaRouter(replace(route, proposals=(*route.proposals[:2], invalid))),
    )

    outcome = await compiler.compile(
        CompileInput(
            utterance="低买高卖",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.diagnostic_code == "idea_guidance_required"
    assert outcome.idea_route is not None
    assert len(outcome.idea_route.proposals) == 2
    assert [item.capability_ids for item in outcome.idea_route.proposals] == [
        ("technical.ma",),
        ("technical.rsi",),
    ]


@pytest.mark.asyncio
async def test_strategy_hash_deduplication_preserves_one_valid_choice() -> None:
    route = _idea_route(proposal_count=2)
    duplicate = replace(
        route.proposals[1],
        suggested_utterance=route.proposals[0].suggested_utterance,
    )
    compiler = _compiler(
        generator=RuleBasedCandidateGenerator(),
        idea_router=_RecordingIdeaRouter(replace(route, proposals=(route.proposals[0], duplicate))),
    )

    outcome = await compiler.compile(
        CompileInput(
            utterance="低买高卖",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.diagnostic_code == "idea_guidance_required"
    assert outcome.idea_route is not None
    assert len(outcome.idea_route.proposals) == 1
    retained = outcome.idea_route.proposals[0]
    assert retained.id == route.proposals[0].id
    assert outcome.suggested_strategy is not None and outcome.suggested_strategy_hash
    assert outcome.suggested_strategy_choice_id == retained.id
    assert not outcome.run_requested


@pytest.mark.asyncio
async def test_ambiguous_compiler_translation_is_not_exposed_as_one_proposal() -> None:
    route = _idea_route()
    ambiguous = route.proposals[0].suggested_utterance
    compiler = _compiler(
        generator=_AmbiguousProposalGenerator(ambiguous),
        idea_router=_RecordingIdeaRouter(route),
    )

    outcome = await compiler.compile(
        CompileInput(
            utterance="低买高卖",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.idea_route is not None
    assert len(outcome.idea_route.proposals) == 2
    assert all(item.suggested_utterance != ambiguous for item in outcome.idea_route.proposals)


@pytest.mark.asyncio
async def test_proposal_cannot_replace_the_current_instrument_context() -> None:
    route = _idea_route()
    conflicting = replace(route.proposals[2], instrument_symbol="300033.SZ")
    compiler = _compiler(
        generator=RuleBasedCandidateGenerator(),
        idea_router=_RecordingIdeaRouter(
            replace(route, proposals=(*route.proposals[:2], conflicting))
        ),
    )

    outcome = await compiler.compile(
        CompileInput(
            utterance="低买高卖",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.idea_route is not None
    assert len(outcome.idea_route.proposals) == 2
    assert all(
        item.instrument_symbol == "300059.SZ" for item in outcome.idea_route.proposals
    )


@pytest.mark.asyncio
async def test_capability_ids_include_server_parsed_position_exit_kind() -> None:
    route = _idea_route()
    risk_managed = replace(
        route.proposals[0],
        suggested_utterance="MACD金叉买入，止盈20%卖出，回测近1年",
    )
    compiler = _compiler(
        generator=RuleBasedCandidateGenerator(),
        idea_router=_RecordingIdeaRouter(
            replace(route, proposals=(risk_managed, *route.proposals[1:]))
        ),
    )

    outcome = await compiler.compile(
        CompileInput(
            utterance="低买高卖",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.idea_route is not None
    assert outcome.idea_route.proposals[0].capability_ids == (
        "technical.macd",
        "strategy.take_profit",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "utterance",
    (
        "低买高卖",
        "RSI低于30买入，暂不卖出",
        "RSI低于30买入",
        "RSI高于70卖出",
        "OBV变化时买入，跌破20日均线卖出",
    ),
)
async def test_hybrid_clarification_paths_fail_closed_when_plan_deep_fails(
    utterance: str,
) -> None:
    idea_router = _RecordingIdeaRouter(None)
    compiler = _compiler(
        generator=RuleBasedCandidateGenerator(),
        idea_router=idea_router,
    )

    outcome = await compiler.compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "idea_guidance_model_unavailable"
    assert outcome.idea_route is None
    assert "原样重试" in (outcome.clarification or "")
    assert len(idea_router.requests) == 1


@pytest.mark.asyncio
async def test_isolated_compiler_without_idea_router_keeps_local_guidance() -> None:
    compiler = _compiler(
        generator=RuleBasedCandidateGenerator(),
        idea_router=None,
    )

    outcome = await compiler.compile(
        CompileInput(
            utterance="RSI低于30买入",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "exit_rule_not_recognized"
    assert outcome.idea_route is not None
    assert outcome.idea_route.provenance is None
