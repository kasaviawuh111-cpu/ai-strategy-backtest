"""R11/R12: model meaning checks must not be overridden by Chinese word lists.

All provider responses and review verdicts are explicit fixtures. These tests
exercise public adapter methods, never a live provider or an executable run.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import cast

import pytest

from ashare_lab.adapters.language.openai_compatible import (
    DisabledCandidateJsonTransport,
    OpenAICompatibleCandidateTransport,
)
from ashare_lab.adapters.language.vibe_backtest_review import VibeBacktestReviewAdvisor
from ashare_lab.adapters.language.vibe_candidates import (
    CandidateCapabilityMatrix,
    CandidateProviderIdentityView,
    CandidateTransportRequest,
    CandidateTransportResponse,
    VibeBoundedCandidateGenerator,
    build_candidate_capability_matrix,
)
from ashare_lab.adapters.language.vibe_clarification import VibeClarificationDialogueRouter
from ashare_lab.adapters.language.vibe_ideas import VibeIdeaRouter
from ashare_lab.adapters.language.vibe_strategy_advice import VibeVerifiedFactStrategyAdvisor
from ashare_lab.api.app import build_hybrid_candidate_compiler
from ashare_lab.domain.catalog import load_catalog_directory, load_coverage_catalog_directory
from ashare_lab.domain.strategy import (
    BacktestConfig,
    CatalogRef,
    FirstOfExit,
    IndicatorCondition,
    Instrument,
    StrategySpec,
)
from ashare_lab.ports.backtest_review import BacktestReviewRequest
from ashare_lab.ports.candidate_generation import CompileInput
from ashare_lab.ports.strategy_advice import VerifiedFactStrategyAdviceRequest

ROOT = Path(__file__).parents[4]
AS_OF = date(2026, 9, 7)
CATALOG_REF = CatalogRef(catalog_id="cn_a.signals", release_version="2026.09.01")
IDENTITY = CandidateProviderIdentityView(
    provider="fixture", model="fixture", prompt_version="test", schema_version="test",
)
_DISPLAY_VERDICT_FIELDS = {"facts", "state_and_authority", "user_intent_and_tone"}


@pytest.fixture(scope="module")
def matrix() -> CandidateCapabilityMatrix:
    return build_candidate_capability_matrix(
        load_catalog_directory(ROOT / "catalogs"),
        load_coverage_catalog_directory(ROOT / "catalogs" / "coverage"),
    )


class _ReviewedTransport:
    """Schema-routed fixtures: a provider call cannot accidentally pass as review."""

    def __init__(
        self, payload: dict[str, object], *, accepted: bool = True,
        candidate_difference: bool = False,
    ) -> None:
        self.payload = payload
        self.accepted = accepted
        self.candidate_difference = candidate_difference
        self.requests: list[CandidateTransportRequest] = []
        self.display_reviews: list[CandidateTransportRequest] = []

    async def generate_json(
        self, request: CandidateTransportRequest,
    ) -> CandidateTransportResponse:
        self.requests.append(request)
        properties = request.response_schema.get("properties", {})
        if isinstance(properties, Mapping) and properties.keys() >= _DISPLAY_VERDICT_FIELDS:
            self.display_reviews.append(request)
            return {
                "facts": "supported",
                "state_and_authority": "supported" if self.accepted else "unsupported",
                "user_intent_and_tone": "supported",
            }
        if request.response_schema_name == "strategy_semantic_review":
            return {
                "instrument": "equivalent", "requested_bar_interval": "unspecified",
                "requirements": [{
                    "status": "represented", "candidate_path": "/entry",
                    "source_quote": request.utterance,
                    "requested_meaning": "测试买入条件", "candidate_meaning": "测试买入条件",
                }],
                "differences": ([{
                    "candidate_path": "/entry/0/trigger", "source_quote": "MACD金叉买入",
                    "requested_meaning": "金叉进场", "candidate_meaning": "其他方向进场",
                }] if self.candidate_difference else []),
            }
        return cast(CandidateTransportResponse, self.payload)

    def assert_reviewed(self, displayed: str, *, trusted_fragment: str) -> None:
        # The exact final prose and server facts/DSL must reach the reviewer.
        # An unconditional pass or a replacement keyword list cannot satisfy this.
        assert self.display_reviews, "generated text never reached independent semantic review"
        payload = json.dumps(
            self.display_reviews[-1].user_payload, ensure_ascii=False, default=str,
        )
        assert displayed in payload
        assert trusted_fragment in payload


def _strategy(threshold: int = 30) -> StrategySpec:
    return StrategySpec(
        catalog=CATALOG_REF, instrument=Instrument(symbol="300059.SZ"),
        entry=IndicatorCondition(
            indicator_id="technical.rsi", definition_version="1.0.0",
            params={"period": 14}, trigger="below", value=threshold,
        ),
        exit=FirstOfExit(children=(IndicatorCondition(
            indicator_id="technical.rsi", definition_version="1.0.0",
            params={"period": 14}, trigger="above", value=70,
        ),)),
        backtest=BacktestConfig(start=date(2025, 9, 7), end=AS_OF, initial_cash_cny=1_000_000),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [True, False], ids=["synonyms", "unsafe-promise"])
async def test_complete_idea_dsl_uses_semantics_not_entry_exit_word_list(
    matrix: CandidateCapabilityMatrix, accepted: bool,
) -> None:
    explanation = ("参数只是假设，历史检验不代表未来表现。" if accepted
                   else "照这个方案操作，未来一定能盈利。")
    proposals = [{
        "title": f"RSI阈值{threshold}", "hypothesis": explanation,
        "entry_summary": f"RSI低于{threshold}时入场", "exit_summary": "RSI高于70时离场",
        "suggested_utterance": (
            f"RSI低于{threshold}时入场，RSI高于70时离场，回放最近一年。"
        ),
        "strategy": _strategy(threshold).model_dump(mode="json"),
    } for threshold in (30, 25)]
    transport = _ReviewedTransport({
        "understanding": "先给出可编辑的RSI策略方向。", "hypothesis": "比较不同入场阈值。",
        "proposals": proposals,
    }, accepted=accepted)
    result = await VibeIdeaRouter(
        transport, capability_matrix=matrix, strategy_catalog=CATALOG_REF,
        model_semantic_review=True,
    ).route(CompileInput(
        utterance="给我两套RSI策略方向", instrument_context="300059.SZ", as_of_date=AS_OF,
        idea_inspiration="比较两套RSI策略方向",
    ))
    assert (result is not None) is accepted
    transport.assert_reviewed(explanation, trusted_fragment="technical.rsi")
    if result is not None:
        assert [item.strategy for item in result.proposals] == [_strategy(30), _strategy(25)]


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [True, False], ids=["open-close-synonyms", "unsafe-promise"])
async def test_fact_advice_prose_uses_review_then_existing_strategy_compiler(
    matrix: CandidateCapabilityMatrix, accepted: bool,
) -> None:
    analysis = ("当前数据只是一个切片，可比较不同历史规则。" if accepted
                else "采用下面规则，未来一定能盈利。")
    sentence = "14日RSI低于30时开仓，14日RSI高于70时平仓，回测近一年。"
    transport = _ReviewedTransport({
        "analysis": analysis, "hypothesis": "阈值是待检验的参数。",
        "proposals": [{
            "title": "RSI反转", "hypothesis": "检验反转是否持续。",
            "entry_summary": "RSI低于30", "exit_summary": "RSI高于70",
            "suggested_utterance": sentence,
        }],
    }, accepted=accepted)
    result = await VibeVerifiedFactStrategyAdvisor(
        transport, capability_matrix=matrix, provider_identity=IDENTITY,
        model_semantic_review=True,
    ).advise(VerifiedFactStrategyAdviceRequest(
        original_utterance="东方财富现价，再给一个策略方向", instrument_symbol="300059.SZ",
        as_of_date=AS_OF, verified_facts=("东方财富：最新价=19.15",),
    ))
    assert (result is not None) is accepted
    transport.assert_reviewed(analysis, trusted_fragment="19.15")
    if result is not None:
        assert result.proposals[0].suggested_utterance == sentence


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [True, False], ids=["negated-backtest", "false-execution"])
async def test_query_reply_distinguishes_negation_from_false_execution(
    matrix: CandidateCapabilityMatrix, accepted: bool,
) -> None:
    message = ("这是当前行情，不是回测结果。" if accepted
               else "本次回测已经完成，可以直接下单。")
    transport = _ReviewedTransport({
        "satisfied": True, "evidence": ["当前行情"], "retry_query": None, "message": message,
    }, accepted=accepted)
    result = await VibeVerifiedFactStrategyAdvisor(
        transport, capability_matrix=matrix, provider_identity=IDENTITY,
        model_semantic_review=True,
    ).review_query_result(
        question="这次查到的是当前行情吗？", data_snapshot={"数据类型": "当前行情"},
    )
    assert (result is not None) is accepted
    transport.assert_reviewed(message, trusted_fragment="数据类型")
    if result is not None:
        assert result.message == message and result.satisfied


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "accepted", [True, False], ids=["negated-return-promise", "unsafe-promise"],
)
async def test_backtest_review_distinguishes_warning_from_return_promise(
    matrix: CandidateCapabilityMatrix, accepted: bool,
) -> None:
    analysis = ("历史表现无法保证未来一定能盈利。" if accepted
                else "采用调整后的规则，未来一定能盈利。")
    proposals = [{
        "title": f"比较RSI阈值{threshold}", "diagnosis": "入场阈值仍需比较。",
        "change_dimension": "entry", "expected_effect": "检验入场节奏是否变化。",
        "tradeoff": "触发次数和交易成本可能改变。",
        "suggested_utterance": (
            f"14日RSI低于{threshold}时入场，高于70时离场，回放最近一年。"
        ),
        "strategy": _strategy(threshold).model_dump(mode="json"),
    } for threshold in (25, 20)]
    transport = _ReviewedTransport({
        "analysis": analysis, "conclusion": "继续比较调整后的实际回测结果。",
        "proposals": proposals,
    }, accepted=accepted)
    result = await VibeBacktestReviewAdvisor(
        transport, capability_matrix=matrix, provider_identity=IDENTITY,
        model_semantic_review=True,
    ).review(BacktestReviewRequest(
        run_id="run:fixture-only", instrument_symbol="300059.SZ", as_of_date=AS_OF,
        strategy_payload=_strategy().model_dump(mode="json"),
        result_facts={"summary": {"tradeCount": 14, "totalReturn": -0.17}},
        evidence_grade="limited", evidence_reasons=("完整交易仅14次",),
    ))
    assert (result is not None) is accepted
    transport.assert_reviewed(analysis, trusted_fragment="totalReturn")
    if result is not None:
        assert result.analysis == analysis
        assert result.proposals[0].strategy == _strategy(25)


def _candidate_payload(*, confidence: float, invalid_parameter: bool = False) -> dict[str, object]:
    entry, exit_text = "MACD金叉买入", "MACD死叉卖出"
    return {"candidates": [{
        "entry": [{
            "kind": "indicator", "indicator_id": "technical.macd", "definition_version": "1.0.0",
            "trigger": "golden_cross", "params": {"fast": -1 if invalid_parameter else 12,
                                                     "slow": 26, "signal": 9},
        }],
        "exit": [{
            "kind": "indicator", "indicator_id": "technical.macd", "definition_version": "1.0.0",
            "trigger": "death_cross", "params": {"fast": 12, "slow": 26, "signal": 9},
        }],
        "entry_spans": [{"start": 0, "end": len(entry), "text": entry}],
        "exit_spans": [{"start": len(entry) + 1, "end": len(entry) + 1 + len(exit_text),
                        "text": exit_text}],
        "confidence": confidence,
        "defaulted_fields": [f"/{side}/0/params/{parameter}" for side in ("entry", "exit")
                             for parameter in ("fast", "slow", "signal")],
    }]}


@pytest.mark.asyncio
@pytest.mark.parametrize("difference", [False, True], ids=["equivalent", "concrete-difference"])
async def test_low_self_score_cannot_erase_a_semantically_reviewed_legal_candidate(
    matrix: CandidateCapabilityMatrix, difference: bool,
) -> None:
    transport = _ReviewedTransport(
        _candidate_payload(confidence=0.41), candidate_difference=difference,
    )
    candidates = await VibeBoundedCandidateGenerator(
        transport, capability_matrix=matrix, model_semantic_review=True,
    ).generate(CompileInput(
        utterance="MACD金叉买入，MACD死叉卖出", instrument_context="300059.SZ", as_of_date=AS_OF,
        semantic_intent="new_strategy",
    ))
    assert [item.response_schema_name for item in transport.requests] == [
        "ashare_bounded_strategy_candidates", "strategy_semantic_review",
    ]
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.entry and candidate.exit
    assert candidate.unsupported_code == ("semantic_confirmation_required" if difference else None)
    assert bool(candidate.semantic_review_issues) is difference


@pytest.mark.asyncio
async def test_positive_semantic_fixture_cannot_approve_illegal_catalog_parameter(
    matrix: CandidateCapabilityMatrix,
) -> None:
    transport = _ReviewedTransport(_candidate_payload(confidence=0.95, invalid_parameter=True))
    candidates = await VibeBoundedCandidateGenerator(
        transport, capability_matrix=matrix, model_semantic_review=True,
    ).generate(CompileInput(
        utterance="MACD金叉买入，MACD死叉卖出", instrument_context="300059.SZ", as_of_date=AS_OF,
        semantic_intent="new_strategy",
    ))
    assert candidates[0].unsupported_code == "candidate_provider_invalid_output"
    assert not candidates[0].entry and not candidates[0].exit
    assert all(item.response_schema_name != "strategy_semantic_review"
               for item in transport.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize(("answer", "intent"), [
    ("我讨厌特朗普的关税政策", "viewpoint"),
    ("旧方案先放一边，按特朗普的关税政策另起几套方向", "vague_strategy"),
    ("我不喜欢他的政策。旧方案不用了，新方案按RSI低于30进场，高于70离场", "new_strategy"),
    ("我不喜欢他的政策，把现有卖出条件改成RSI高于65，其他保留", "supplement"),
])
async def test_contextual_intent_contract_keeps_emotion_separate_from_strategy_authority(
    matrix: CandidateCapabilityMatrix, answer: str, intent: str,
) -> None:
    transport = _ReviewedTransport({"intent": intent})
    context = {"strategy": _strategy().model_dump(mode="json"), "pending_question": None}
    result = await VibeClarificationDialogueRouter(
        transport, capability_matrix=matrix, model_semantic_review=True,
    ).classify_initial(answer, AS_OF, context=context)
    assert result == intent
    request = transport.requests[0]
    assert request.response_schema_name == "contextual_dialogue_intent"
    assert request.user_payload == {"utterance": answer, "context": context}
    assert "单纯人物好恶、观点或情绪插话归viewpoint" in request.system_contract
    assert "不修改已有AST" in request.system_contract
    assert "new_strategy或vague_strategy" in request.system_contract
    assert "明确调整现有条件归supplement" in request.system_contract


@pytest.mark.asyncio
@pytest.mark.parametrize("real_planner", [False, True], ids=["fake-legacy", "planner-only"])
async def test_factory_reviews_real_planner_when_candidate_provider_is_disabled(
    matrix: CandidateCapabilityMatrix, monkeypatch: pytest.MonkeyPatch, real_planner: bool,
) -> None:
    class IdentifiedFixture(_ReviewedTransport):
        identity = IDENTITY

    fixture = IdentifiedFixture({
        "understanding": "比较两套可编辑的RSI方案。", "hypothesis": "检验不同入场阈值。",
        "proposals": [{
            "title": f"RSI阈值{threshold}", "hypothesis": "这是未运行的研究假设。",
            "entry_summary": f"RSI低于{threshold}买入", "exit_summary": "RSI高于70卖出",
            "suggested_utterance": f"RSI低于{threshold}买入，RSI高于70卖出，回测近一年。",
            "strategy": _strategy(threshold).model_dump(mode="json"),
        } for threshold in (30, 25)],
    })

    async def intercepted_generate(
        _transport: OpenAICompatibleCandidateTransport, request: CandidateTransportRequest,
    ) -> CandidateTransportResponse:
        return await fixture.generate_json(request)

    monkeypatch.setattr(OpenAICompatibleCandidateTransport, "generate_json", intercepted_generate)
    planner = (OpenAICompatibleCandidateTransport(
        endpoint="https://provider.invalid/chat/completions", provider="fixture-live",
        model="fixture", prompt_version="test", schema_version="test",
    ) if real_planner else fixture)
    compiler = build_hybrid_candidate_compiler(
        load_catalog_directory(ROOT / "catalogs"),
        candidate_transport=DisabledCandidateJsonTransport(
            prompt_version="test", schema_version="test",
        ),
        idea_transport=planner, capability_matrix=matrix, idea_direct_dsl=True,
        backtest_anchor_date=AS_OF,
    )
    outcome = await compiler.compile(CompileInput(
        utterance="给我两套RSI方案", instrument_context="300059.SZ", as_of_date=AS_OF,
        semantic_intent="vague_strategy",
    ))
    assert outcome.idea_route is not None and len(outcome.idea_route.proposals) == 2
    assert outcome.strategy is None and not outcome.run_requested
    assert [request.response_schema_name for request in fixture.requests] == [
        "strategy_ideas", *(["dialogue_reply_semantic_review"] if real_planner else []),
    ]
