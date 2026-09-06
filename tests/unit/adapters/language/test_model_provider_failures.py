"""Classified failures stay failures; no paid providers are contacted."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

import pytest

from ashare_lab.adapters.language.independent_web_research import QueryPlanningCurrentFactResearcher
from ashare_lab.adapters.language.vibe_backtest_review import VibeBacktestReviewAdvisor
from ashare_lab.adapters.language.vibe_candidates import (
    CandidateCapabilityMatrix,
    CandidateFailureKind,
    CandidateProviderIdentityView,
    CandidateTransportError,
    CandidateTransportRequest,
    CandidateTransportResponse,
    VibeBoundedCandidateGenerator,
    build_candidate_capability_matrix,
)
from ashare_lab.adapters.language.vibe_clarification import VibeClarificationDialogueRouter
from ashare_lab.adapters.language.vibe_ideas import VibeIdeaRouter
from ashare_lab.adapters.language.vibe_strategy_advice import VibeVerifiedFactStrategyAdvisor
from ashare_lab.adapters.language.vibe_strategy_editing import VibeStrategyEditor
from ashare_lab.domain.catalog import load_catalog_directory, load_coverage_catalog_directory
from ashare_lab.domain.strategy import StrategySpec
from ashare_lab.ports.backtest_review import BacktestReviewRequest
from ashare_lab.ports.candidate_generation import CompileInput
from ashare_lab.ports.clarification_dialogue import ClarificationDialogueRequest
from ashare_lab.ports.current_fact_research import (
    CurrentFactResearcher,
    CurrentFactResearchRequest,
    ResearchPurpose,
)
from ashare_lab.ports.idea_routing import IdeaProposal
from ashare_lab.ports.live_market_data import LiveMarketDataProvenance, LiveMarketDataResult
from ashare_lab.ports.strategy_advice import VerifiedFactStrategyAdviceRequest
from ashare_lab.ports.strategy_editing import StrategyEditRequest

ROOT = Path(__file__).parents[4]
NOW = datetime(2026, 9, 5, tzinfo=UTC)
IDENTITY = CandidateProviderIdentityView("deepseek", "fixture", "v1", "v1")


class _FailingTransport:
    def __init__(self, error: CandidateTransportError) -> None:
        self.error = error
        self.calls = 0

    async def generate_json(self, request: CandidateTransportRequest) -> CandidateTransportResponse:
        self.calls += 1
        raise self.error


@pytest.fixture(scope="module")
def matrix() -> CandidateCapabilityMatrix:
    return build_candidate_capability_matrix(
        load_catalog_directory(ROOT / "catalogs"),
        load_coverage_catalog_directory(ROOT / "catalogs" / "coverage"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "status"), [
    ("authentication_failed", 401), ("insufficient_balance", 402),
])
@pytest.mark.parametrize("entry", [
    "compile", "clarify", "ideas", "edit", "review", "advice", "query_review",
    "recommend", "pair", "research_planner",
])
async def test_known_failure_is_not_missing_conditions_and_never_retried(
    matrix: CandidateCapabilityMatrix, kind: CandidateFailureKind, status: int, entry: str,
) -> None:
    error = CandidateTransportError("private fixture", failure_kind=kind, http_status=status)
    transport = _FailingTransport(error)
    repair = _FailingTransport(error)
    strategy = StrategySpec.model_validate_json(
        (ROOT / "contracts/examples/strategy.macd-volume.daily.v1.json").read_text(),
    )
    compile_input = CompileInput(
        utterance="低买高卖", instrument_context="300059.SZ", as_of_date=date(2026, 9, 5),
    )
    advisor = VibeVerifiedFactStrategyAdvisor(
        transport, capability_matrix=matrix, provider_identity=IDENTITY,
    )
    screen = LiveMarketDataResult(
        provider="fixture", query="fixture", asset_type="A股", columns=("代码", "名称"),
        rows=({"代码": "300059", "名称": "东方财富"},),
        provenance=LiveMarketDataProvenance("fixture", NOW, "v1"),
    )
    proposal = IdeaProposal(
        id="idea_fixture", title="趋势", hypothesis="fixture", entry_summary="上穿20日线",
        exit_summary="下穿20日线", suggested_utterance="上穿20日线买入，下穿20日线卖出",
        capability_ids=(), assumptions=(), confidence=1,
    )
    calls: dict[str, Callable[[], Awaitable[object]]] = {
        "compile": lambda: VibeBoundedCandidateGenerator(
            transport, capability_matrix=matrix, repair_invalid_output=True,
        ).generate(compile_input),
        "clarify": lambda: VibeClarificationDialogueRouter(
            transport, capability_matrix=matrix,
        ).assess(ClarificationDialogueRequest(
            answer="用10天退出", prior_utterance="金叉买入", question="何时卖出？",
            diagnostic_code="exit_rule_not_recognized", context_summary="fixture", options=(),
        )),
        "ideas": lambda: VibeIdeaRouter(
            transport, capability_matrix=matrix, repair_transport=repair,
        ).route(compile_input),
        "edit": lambda: VibeStrategyEditor(
            transport, capability_matrix=matrix, provider_identity=IDENTITY,
        ).edit(StrategyEditRequest(
            answer="改成持有10天退出", prior_utterance="fixture", strategy=strategy,
            as_of_date=NOW.date(),
        )),
        "review": lambda: VibeBacktestReviewAdvisor(
            transport, capability_matrix=matrix, provider_identity=IDENTITY,
        ).review(BacktestReviewRequest(
            run_id="run:fixture", instrument_symbol="300059.SZ", as_of_date=NOW.date(),
            strategy_payload=strategy.model_dump(mode="json"),
            result_facts={"summary": {"tradeCount": 14, "totalReturn": -0.17}},
            evidence_grade="limited", evidence_reasons=("fixture",),
        )),
        "advice": lambda: advisor.advise(VerifiedFactStrategyAdviceRequest(
            original_utterance="如何构建策略", instrument_symbol="300059.SZ",
            as_of_date=NOW.date(), verified_facts=("现价19元",),
        )),
        "query_review": lambda: advisor.review_query_result(
            question="查询现价", data_snapshot={"columns": ["现价"], "rows": [{"现价": 19}]},
        ),
        "recommend": lambda: advisor.recommend_stocks("fixture", screen),
        "pair": lambda: advisor.pair_stock_strategies("fixture", screen, (proposal,)),
        "research_planner": lambda: QueryPlanningCurrentFactResearcher(
            cast(CurrentFactResearcher, object()), transport,
        ).research(CurrentFactResearchRequest("fixture", ResearchPurpose.CURRENT_FACT, NOW)),
    }
    with pytest.raises(CandidateTransportError) as caught:
        await calls[entry]()
    assert caught.value is error
    assert transport.calls == 1 and repair.calls == 0


def test_unknown_legacy_failure_keeps_compatibility() -> None:
    error = CandidateTransportError("legacy", timed_out=True)
    assert not error.is_classified and error.timed_out and error.http_status is None
    assert error.public_code == "candidate_provider_timeout"


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "status"), [
    ("authentication_failed", 401), ("insufficient_balance", 402),
])
async def test_idea_research_preserves_planner_failure_without_followup_calls(
    matrix: CandidateCapabilityMatrix, kind: CandidateFailureKind, status: int,
) -> None:
    error = CandidateTransportError("private fixture", failure_kind=kind, http_status=status)
    planner_transport = _FailingTransport(error)
    idea_transport = _FailingTransport(error)
    repair_transport = _FailingTransport(error)
    planner = QueryPlanningCurrentFactResearcher(
        cast(CurrentFactResearcher, object()), planner_transport,
    )
    router = VibeIdeaRouter(
        idea_transport, capability_matrix=matrix, researcher=planner,
        repair_transport=repair_transport,
    )
    with pytest.raises(CandidateTransportError) as caught:
        await router.route(CompileInput(
            utterance="特朗普最近的关税政策对A股有什么影响", as_of_date=NOW.date(),
        ))
    assert caught.value is error
    assert caught.value.failure_kind == kind and caught.value.http_status == status
    assert planner_transport.calls == 1
    assert idea_transport.calls == 0 and repair_transport.calls == 0
