# Starlette's TestClient currently exposes partially unknown httpx method types to Pyright.
# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ashare_lab.adapters.language import RuleBasedCandidateGenerator
from ashare_lab.adapters.language.vibe_candidates import HybridCandidateGenerator
from ashare_lab.adapters.market_data.mx_saas import (
    MxSaasProviderAuthError,
    MxSaasProviderUnavailableError,
)
from ashare_lab.api import create_app
from ashare_lab.api.routes.strategy_drafts import _to_idea_route_payload
from ashare_lab.api.schemas import StrategyDraftResponse
from ashare_lab.application.compile_strategy import CompileOutcome, CompileStatus, StrategyCompiler
from ashare_lab.application.turn_intent import TurnIntent
from ashare_lab.domain.catalog import load_catalog_directory
from ashare_lab.domain.strategy import StrategySpec, canonical_hash
from ashare_lab.ports.candidate_generation import (
    CandidateAst,
    CandidateGroundingEvidence,
    CompileInput,
    IndicatorIntent,
)
from ashare_lab.ports.clarification_dialogue import (
    ClarificationDialogueAssessment,
    ClarificationDialogueRequest,
)
from ashare_lab.ports.current_fact_research import (
    CurrentFactResearchResult,
    ResearchFact,
    ResearchPurpose,
    ResearchSource,
)
from ashare_lab.ports.idea_routing import IdeaAssetMapping, IdeaProposal, IdeaRoute
from ashare_lab.ports.live_market_data import (
    LiveFinanceDataResult,
    LiveMarketDataProvenance,
    LiveMarketDataResult,
    LiveScreenedFinanceDataResult,
    LiveSecurityEntity,
)
from ashare_lab.ports.strategy_advice import StockRecommendation

ROOT = Path(__file__).resolve().parents[3]


def test_candidate_batch_can_select_multiple_independent_drafts(client: TestClient) -> None:
    batch = client.post("/api/v1/strategy-drafts", json={
        "utterance": "MACD死叉卖出", "instrument_context": "300059.SZ",
        "as_of_date": "2026-08-27",
    }).json()
    proposals = batch["idea_route"]["proposals"]
    assert len(proposals) >= 2
    url = (f"/api/v1/strategy-drafts/{batch['draft_id']}"
           f"/revisions/{batch['revision']}/clarification-answers")
    children = []
    for proposal in proposals[:2]:
        response = client.post(url, json={"answer": proposal["id"]})
        assert response.status_code == 200, response.text
        children.append(response.json()["draft"])
    assert len({batch["draft_id"], *(child["draft_id"] for child in children)}) == 3
    assert all(child["revision"] == 1 for child in children)
    assert all(child["strategy"] is not None for child in children)
    # Returning to the original batch is repeatable, not a stale revision bypass.
    again = client.post(url, json={"answer": proposals[0]["id"]})
    assert again.status_code == 200
    assert again.json()["draft"]["strategy_hash"] == children[0]["strategy_hash"]


@pytest.mark.parametrize("intent", [TurnIntent.CASUAL, TurnIntent.CANCEL, TurnIntent.UNKNOWN])
def test_report_edit_entry_preserves_strategy_for_model_non_edit_intent(intent: TurnIntent) -> None:
    app = create_app()
    with TestClient(app) as client:
        created = client.post("/api/v1/strategy-drafts", json={
            "utterance": "300059.SZ RSI低于30买入，RSI高于70卖出",
            "as_of_date": "2026-08-20",
        }).json()
        assert created["status"] == "ready"
        compiler = app.state.container.compiler
        compiler.classify_dialogue_intent = AsyncMock(return_value=intent)
        compiler.edit_current_strategy = AsyncMock(side_effect=AssertionError("not an edit"))
        compiler.compile = AsyncMock(side_effect=AssertionError("do not replace saved strategy"))
        compiler.compose_dialogue_response = AsyncMock(return_value="我们先聊聊，策略先不动。")
        response = client.post("/api/v1/strategy-drafts", headers={
            "X-Conversation-Parent-Draft-ID": created["draft_id"],
        }, json={"utterance": "我现在不想谈买卖了", "edit_current_strategy": True,
                 "as_of_date": "2026-08-20"})
        assert response.status_code == 201
        payload = response.json()
        for field in ("draft_id", "revision", "strategy", "strategy_hash", "status"):
            assert payload[field] == created[field]
        assert payload["assistant_message"] == "我们先聊聊，策略先不动。"
        assert not payload.get("run_requested")
        compiler.classify_dialogue_intent.assert_awaited_once()
        compiler.edit_current_strategy.assert_not_awaited()
        compiler.compile.assert_not_awaited()


@pytest.mark.parametrize("explicit_edit", [False, True])
def test_ready_parent_viewpoint_uses_support_without_editing(explicit_edit: bool) -> None:
    app = create_app()
    with TestClient(app) as client:
        created = client.post("/api/v1/strategy-drafts", json={
            "utterance": "300059.SZ RSI低于30买入，RSI高于70卖出",
            "as_of_date": "2026-08-20",
        }).json()
        assert created["status"] == "ready"
        compiler = app.state.container.compiler
        compiler.classify_dialogue_intent = AsyncMock(return_value=TurnIntent.VIEWPOINT)
        compiler.edit_current_strategy = AsyncMock(side_effect=AssertionError("not an edit"))
        compiler.compile = AsyncMock(side_effect=AssertionError("do not replace saved strategy"))
        compiler.viewpoint_support_turn = AsyncMock(wraps=compiler.viewpoint_support_turn)

        response = client.post("/api/v1/strategy-drafts", headers={
            "X-Conversation-Parent-Draft-ID": created["draft_id"],
        }, json={"utterance": "我对最近的新闻感到很失望", "edit_current_strategy": explicit_edit,
                 "as_of_date": "2026-08-20"})

        assert response.status_code == 201, response.text
        payload = response.json()
        for field in ("draft_id", "revision", "strategy", "strategy_hash", "status"):
            assert payload[field] == created[field]
        assert not payload.get("run_requested") and not payload.get("refresh_data")
        # This local fixture has no researcher; it must not invent search success.
        assert "联网暂未取得" in payload["assistant_message"]
        compiler.viewpoint_support_turn.assert_awaited_once()
        compiler.edit_current_strategy.assert_not_awaited()
        compiler.compile.assert_not_awaited()


class _UnexpectedFallback:
    async def generate(self, request: CompileInput) -> tuple[CandidateAst, ...]:
        raise AssertionError(f"bounded fallback must not run: {request!r}")


class _ExplodingCompiler:
    async def compile(self, _request: Any) -> Any:
        raise RuntimeError("internal secret must not leak")


class _RecordingDialogueRouter:
    def __init__(
        self, *, natural_reply: str = "我知道你是在开玩笑，这句先不作为策略条件。",
    ) -> None:
        self.natural_reply = natural_reply
        self.requests: list[ClarificationDialogueRequest] = []

    async def assess(
        self,
        request: ClarificationDialogueRequest,
    ) -> ClarificationDialogueAssessment | None:
        self.requests.append(request)
        return ClarificationDialogueAssessment(
            reply_kind="off_topic",
            acknowledgement_id="light_redirect",
            natural_reply=self.natural_reply,
            recommended_option_ids=tuple(reversed([item.id for item in request.options])),
        )


class _RecordingLiveData:
    def __init__(self) -> None:
        self.screen_calls: list[tuple[str, str]] = []
        self.finance_calls: list[tuple[str, str | None]] = []
        self.screen_finance_calls: list[tuple[str, str, str]] = []
        self.provenance = LiveMarketDataProvenance(
            response_sha256="sha256:" + "a" * 64,
            retrieved_at=datetime(2026, 9, 3, 10, 0, tzinfo=UTC),
            schema_version="test.live.v1",
        )

    async def screen(self, *, query: str, asset_type: str) -> LiveMarketDataResult:
        self.screen_calls.append((query, asset_type))
        return LiveMarketDataResult(
            provider="test_screener",
            query=query,
            asset_type=asset_type,
            columns=("证券代码", "证券简称"),
            rows=({"证券代码": "300059", "证券简称": "东方财富"},),
            provenance=self.provenance,
        )

    async def query_finance(
        self,
        *,
        query: str,
        indicators: str | None,
    ) -> LiveFinanceDataResult:
        self.finance_calls.append((query, indicators))
        return LiveFinanceDataResult(
            provider="test_finance",
            query=query,
            indicators=indicators,
            tables=(
                {
                    "title": "东方财富换手率",
                    "rawTable": {"headers": ["日期", "换手率(%)"], "data": [["2026-09-02", 2.37]]},
                },
            ),
            provenance=self.provenance,
        )

    async def screen_then_query_finance(
        self,
        *,
        screening_query: str,
        asset_type: str,
        indicators: str,
    ) -> LiveScreenedFinanceDataResult:
        self.screen_finance_calls.append((screening_query, asset_type, indicators))
        return LiveScreenedFinanceDataResult(
            screen=LiveMarketDataResult(
                provider="test_screener",
                query=screening_query,
                asset_type=asset_type,
                columns=("证券代码", "证券简称"),
                rows=({"证券代码": "300059", "证券简称": "东方财富"},),
                provenance=self.provenance,
            ),
            entities=(LiveSecurityEntity(code="300059", name="东方财富", asset_type="A股"),),
            batches=(
                LiveFinanceDataResult(
                    provider="test_finance",
                    query="查询东方财富(300059)",
                    indicators=indicators,
                    tables=(
                        {
                            "title": "东方财富归母净利润",
                            "rawTable": {"data": [["2025", "provider-raw-value"]]},
                        },
                    ),
                    provenance=self.provenance,
                ),
            ),
        )


class _FailingLiveFinanceData:
    def __init__(self, error: Exception | None) -> None:
        self.error = error
        self.provenance = LiveMarketDataProvenance(
            response_sha256="sha256:" + "b" * 64,
            retrieved_at=datetime(2026, 9, 3, 10, 0, tzinfo=UTC),
            schema_version="test.live.v1",
        )

    async def query_finance(
        self,
        *,
        query: str,
        indicators: str | None,
    ) -> LiveFinanceDataResult:
        if self.error is not None:
            raise self.error
        return LiveFinanceDataResult(
            provider="test_finance",
            query=query,
            indicators=indicators,
            tables=(),
            provenance=self.provenance,
        )


class _IdeaCompiler:
    async def compile(self, _request: Any) -> CompileOutcome:
        proposals = tuple(
            IdeaProposal(
                id=f"idea_{index:012x}",
                title=f"候选 {index}",
                hypothesis="仅用价格行为代理检验该观点。",
                entry_summary="MACD 金叉",
                exit_summary="MACD 死叉",
                suggested_utterance="MACD金叉买入，死叉卖出，回测近5年",
                capability_ids=("technical.macd",),
                assumptions=("不证明因果关系。",),
                confidence=0.75,
                instrument_symbol="300059.SZ",
            )
            for index in range(1, 3)
        )
        return CompileOutcome(
            status=CompileStatus.NEEDS_CLARIFICATION,
            clarification="请选择一个可回测的价格代理。",
            diagnostic_code="idea_guidance_required",
            idea_route=IdeaRoute(
                understanding="用户表达了一个政治态度。",
                hypothesis="相关不确定性可能与当前股票价格行为同期出现。",
                asset_mapping=IdeaAssetMapping(
                    instrument_symbol="300059.SZ",
                    rationale="只使用当前页面股票作为价格代理。",
                ),
                proposals=proposals,
                research=CurrentFactResearchResult(
                    provider="deepseek",
                    model="deepseek-v4-flash",
                    provider_response_id="resp_research_1",
                    query="分析东方财富并给我几个可回测策略",
                    purpose=ResearchPurpose.VIEWPOINT,
                    as_of=datetime(2026, 9, 4, 9, 0, tzinfo=UTC),
                    summary="公开资料显示公司近期披露了半年度报告。",
                    facts=(
                        ResearchFact(
                            statement="公司已披露半年度报告。",
                            fact_kind="reported_fact",
                            source_ids=("source_1",),
                            time_scope="2026年半年度",
                        ),
                    ),
                    sources=(
                        ResearchSource(
                            source_id="source_1",
                            title="东方财富半年度报告",
                            url="https://example.com/disclosure",
                            publisher="交易所",
                            published_at="2026-08-20",
                        ),
                    ),
                    unresolved_questions=(),
                    retrieved_at=datetime(2026, 9, 4, 9, 1, tzinfo=UTC),
                    response_sha256="1" * 64,
                    search_call_count=1,
                ),
            ),
        )


class _UnboundIdeaCompiler(_IdeaCompiler):
    async def compile(self, request: Any) -> CompileOutcome:
        outcome = await super().compile(request)
        assert outcome.idea_route is not None
        return replace(
            outcome,
            idea_route=replace(
                outcome.idea_route,
                asset_mapping=IdeaAssetMapping(
                    instrument_symbol=None,
                    relation="unbound",
                    rationale="尚未绑定证券；选择方向后仍需补充具体 A 股。",
                    evidence_status="instrument_required",
                ),
                proposals=tuple(
                    replace(proposal, instrument_symbol=None, capability_ids=())
                    for proposal in outcome.idea_route.proposals
                ),
            ),
        )


def test_ready_draft_returns_canonical_strategy_hash_and_provenance(
    client: TestClient,
    ready_request: dict[str, str],
) -> None:
    response = client.post("/api/v1/strategy-drafts", json=ready_request)
    payload: dict[str, Any] = response.json()

    assert response.status_code == 201
    assert response.headers["Idempotency-Replayed"] == "false"
    assert payload["revision"] == 1
    assert payload["status"] == "ready"
    assert payload["strategy"]["schema_version"] == "strategy.v1"
    assert payload["strategy"]["instrument"]["symbol"] == "300059.SZ"
    assert payload["strategy_hash"].startswith("sha256:")
    assert {item["path"] for item in payload["provenance"]} >= {
        "/instrument/symbol",
        "/backtest/start",
        "/backtest/end",
    }


def test_idea_guidance_is_typed_and_remains_non_executable() -> None:
    app = create_app()
    app.state.container = replace(app.state.container, compiler=_IdeaCompiler())
    with TestClient(app) as idea_client:
        response = idea_client.post(
            "/api/v1/strategy-drafts",
            json={
                "utterance": "我讨厌特朗普",
                "instrument_context": "300059.SZ",
                "as_of_date": "2026-08-30",
            },
        )
    payload: dict[str, Any] = response.json()

    assert response.status_code == 201
    assert payload["status"] == "needs_clarification"
    assert payload["diagnostic_code"] == "idea_guidance_required"
    assert payload["strategy"] is None
    assert payload["strategy_hash"] is None
    assert payload["idea_route"]["schema_version"] == "idea-route.v1"
    assert payload["idea_route"]["asset_mapping"] == {
        "instrument_symbol": "300059.SZ",
        "relation": "current_page_proxy",
        "rationale": "只使用当前页面股票作为价格代理。",
        "evidence_status": "host_context_only",
    }
    assert len(payload["idea_route"]["proposals"]) == 2
    assert all(
        item["suggested_utterance"] == "MACD金叉买入，死叉卖出，回测近5年"
        for item in payload["idea_route"]["proposals"]
    )
    assert all(
        item["instrument_symbol"] == "300059.SZ"
        for item in payload["idea_route"]["proposals"]
    )
    assert payload["idea_route"]["research"] == {
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "provider_response_id": "resp_research_1",
        "query": "分析东方财富并给我几个可回测策略",
        "purpose": "viewpoint",
        "as_of": "2026-09-04T09:00:00Z",
        "summary": "公开资料显示公司近期披露了半年度报告。",
        "facts": [
            {
                "statement": "公司已披露半年度报告。",
                "fact_kind": "reported_fact",
                "source_ids": ["source_1"],
                "time_scope": "2026年半年度",
            }
        ],
        "sources": [
            {
                "source_id": "source_1",
                "title": "东方财富半年度报告",
                "url": "https://example.com/disclosure",
                "publisher": "交易所",
                "published_at": "2026-08-20",
            }
        ],
        "unresolved_questions": [],
        "retrieved_at": "2026-09-04T09:01:00Z",
        "response_sha256": "1" * 64,
        "search_call_count": 1,
        "schema_version": "current-fact-research.v1",
    }
    with pytest.raises(ValidationError):
        StrategyDraftResponse.model_validate({**payload, "idea_route": None})
    with pytest.raises(ValidationError):
        StrategyDraftResponse.model_validate(
            {**payload, "diagnostic_code": "non_daily_timeframe_not_supported"}
        )
    StrategyDraftResponse.model_validate({**payload, "diagnostic_code": "strategy_rule_incomplete"})
    StrategyDraftResponse.model_validate(
        {**payload, "diagnostic_code": "ambiguous_cross_indicator"}
    )


def test_idea_route_empty_mapping_rationale_gets_safe_display_fallback() -> None:
    # The provider is allowed to omit display-only prose. The mapper must still
    # return a usable route instead of raising a Pydantic validation error.
    idea = IdeaRoute(
        understanding="保留当前策略方向。",
        hypothesis="待继续核对。",
        asset_mapping=IdeaAssetMapping(instrument_symbol=None, rationale=""),
        proposals=(),
    )
    payload = _to_idea_route_payload(idea)
    assert payload.asset_mapping.rationale == "保留当前标的和策略方向。"


def test_unbound_price_plan_parameters_survive_public_candidate_mapping() -> None:
    from datetime import date
    from ashare_lab.domain.strategy import BacktestConfig, CatalogRef, execution_for_price_plan
    from ashare_lab.domain.strategy.price_plans import ScheduledPlan, ScheduledParameters
    from ashare_lab.ports.idea_routing import UnboundIdeaStrategy

    plan = ScheduledPlan(parameters=ScheduledParameters(budget_cny=2000))
    template = UnboundIdeaStrategy(
        catalog=CatalogRef(catalog_id="cn_a.signals", release_version="2026.09.01"),
        trading_plan=plan, execution=execution_for_price_plan(plan),
        backtest=BacktestConfig(start=date(2025, 9, 11), end=date(2026, 9, 11), initial_cash_cny=1000000),
    )
    idea = IdeaRoute(
        understanding="每周定投", hypothesis="待选股的研究方案",
        asset_mapping=IdeaAssetMapping(instrument_symbol=None, relation="unbound"),
        proposals=(IdeaProposal(
            id="idea_0123456789ab", title="每周定投", hypothesis="定期买入",
            entry_summary="每周买入2000元", exit_summary="暂不设置卖出",
            suggested_utterance="每周买入2000元，回测近一年",
            capability_ids=("strategy.scheduled",), assumptions=(), confidence=.75,
            strategy_template=template,
        ),),
    )
    response = _to_idea_route_payload(idea).model_dump(mode="json")
    proposal = response["proposals"][0]
    assert proposal["strategy"] is None
    assert proposal["strategy_template"] == template.model_dump(mode="json")
    assert proposal["instrument_symbol"] is None


@pytest.mark.asyncio
async def test_unbound_viewpoint_guidance_is_retained_until_stock_pairing() -> None:
    app = create_app()
    app.state.container = replace(app.state.container, compiler=_UnboundIdeaCompiler())
    with TestClient(app) as idea_client:
        response = idea_client.post(
            "/api/v1/strategy-drafts",
            json={
                "utterance": "我讨厌特朗普",
                "instrument_context": None,
                "as_of_date": "2026-08-30",
            },
        )

    payload: dict[str, Any] = response.json()
    assert response.status_code == 201
    assert payload["diagnostic_code"] == "stock_pairing_pending"
    assert payload["strategy"] is None
    assert payload["strategy_hash"] is None
    assert payload["idea_route"] is None
    assert payload["instrument_suggestion"] is None
    assert not payload.get("run_requested")
    StrategyDraftResponse.model_validate(payload)
    # The public route hides unbound choices, without losing the planning state.
    stored = await app.state.container.drafts.latest_for_answer(
        draft_id=UUID(payload["draft_id"]), revision=payload["revision"],
    )
    assert stored.outcome.idea_route is not None
    internal_route = _to_idea_route_payload(stored.outcome.idea_route).model_dump(mode="json")
    assert internal_route["asset_mapping"] == {
        "instrument_symbol": None,
        "relation": "unbound",
        "rationale": "尚未绑定证券；选择方向后仍需补充具体 A 股。",
        "evidence_status": "instrument_required",
    }
    assert all(
        proposal["instrument_symbol"] is None and proposal["capability_ids"] == []
        for proposal in internal_route["proposals"]
    )
    # Empty capabilities are only valid for directions awaiting a stock.
    internal_route["proposals"][0]["instrument_symbol"] = "300059.SZ"
    with pytest.raises(ValidationError, match="bound proposals require"):
        StrategyDraftResponse.model_validate({
            **payload, "diagnostic_code": "idea_guidance_required", "idea_route": internal_route,
        })


def test_bare_cross_api_preserves_instrument_and_defaults_to_macd() -> None:
    generator = HybridCandidateGenerator(
        deterministic=RuleBasedCandidateGenerator(),
        bounded_fallback=_UnexpectedFallback(),
        instrument_name_resolver=lambda name: {"汤姆猫": "300459.SZ"}[name],
    )
    compiler = StrategyCompiler(
        generator=generator,
        catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals",
        release_version="2026.09.01",
    )
    app = create_app()
    app.state.container = replace(app.state.container, compiler=compiler)

    with TestClient(app) as grounded_client:
        response = grounded_client.post(
            "/api/v1/strategy-drafts",
            json={"utterance": "汤姆猫金叉买死叉卖", "as_of_date": "2026-08-20"},
        )

    assert response.status_code == 201
    payload: dict[str, Any] = response.json()
    assert payload["status"] == "ready"
    assert payload["diagnostic_code"] is None and payload["idea_route"] is None
    assert payload["strategy"]["instrument"]["symbol"] == "300459.SZ"
    assert payload["strategy"]["entry"]["indicator_id"] == "technical.macd"
    assert payload["strategy"]["exit"]["children"][0]["indicator_id"] == "technical.macd"
    assert "汤姆猫" in payload["candidate_grounding"]["matched_spans"]


def test_instrument_context_rejects_a_different_symbol_named_in_the_utterance(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/v1/strategy-drafts",
        json={
            "utterance": "贵州茅台600519.SS的MACD金叉买入，死叉卖出",
            "instrument_context": "300059.SZ",
            "as_of_date": "2026-08-30",
        },
    )
    payload: dict[str, Any] = response.json()

    assert response.status_code == 201
    assert payload["status"] == "unsupported"
    assert payload["diagnostic_code"] == "instrument_context_mismatch"
    assert payload["strategy"] is None


def test_report_term_count_and_fill_anchored_exit_survive_the_http_contract(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/v1/strategy-drafts",
        json={
            "utterance": "同花顺发年报提到ai次数超过5次的话就买入，3天后卖出",
            "instrument_context": "300033.SZ",
            "as_of_date": "2026-08-30",
        },
    )
    payload: dict[str, Any] = response.json()

    assert response.status_code == 201
    assert payload["status"] == "ready"
    assert payload["strategy"]["instrument"]["symbol"] == "300033.SZ"
    assert payload["strategy"]["entry"]["event_code"] == ("event.financial_results.annual_report")
    assert payload["strategy"]["entry"]["document_text"] == {
        "metric_id": "document.literal_mention_count",
        "metric_version": "1.0.0",
        "term": "ai",
        "normalization": "nfkc",
        "match_mode": "ascii_token",
        "case_sensitive": False,
        "comparator": "gt",
        "value": 5,
    }
    assert payload["strategy"]["exit"]["children"] == [
        {
            "type": "holding_period_exit",
            "sessions": 3,
            "anchor": "first_entry_fill",
            "count_mode": "subsequent_trading_sessions",
            "execution": "target_session_open_proxy",
        }
    ]


def test_boolean_connectives_change_the_strategy_shape_and_hash(client: TestClient) -> None:
    common = {
        "instrument_context": "300059.SZ",
        "as_of_date": "2026-08-27",
    }
    all_response = client.post(
        "/api/v1/strategy-drafts",
        json={
            **common,
            "utterance": "MACD金叉并且RSI低于30买入，MACD死叉卖出",
        },
    )
    any_response = client.post(
        "/api/v1/strategy-drafts",
        json={
            **common,
            "utterance": "MACD金叉或者RSI低于30买入，MACD死叉卖出",
        },
    )

    assert all_response.status_code == any_response.status_code == 201
    assert all_response.json()["status"] == any_response.json()["status"] == "ready"
    assert all_response.json()["strategy"]["entry"]["type"] == "all"
    assert any_response.json()["strategy"]["entry"]["type"] == "any"
    assert all_response.json()["strategy_hash"] != any_response.json()["strategy_hash"]


def test_simultaneous_profit_and_loss_exit_is_invalid_in_http_contract(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/v1/strategy-drafts",
        json={
            "utterance": "MACD金叉买入，止盈20%且止损5%卖出",
            "instrument_context": "300059.SZ",
            "as_of_date": "2026-08-27",
        },
    )
    payload: dict[str, Any] = response.json()

    assert response.status_code == 201
    assert payload["status"] == "invalid"
    assert payload["strategy"] is None
    assert payload["strategy_hash"] is None
    assert payload["diagnostic_code"] == "strategy_validation_failed:ValidationError"
    assert not payload.get("run_requested")


@pytest.mark.parametrize(
    ("utterance", "diagnostic_code"),
    [
        ("5分钟MACD金叉买入，5分钟死叉卖出", "non_daily_timeframe_not_supported"),
        ("30min MACD金叉买入，30min MACD死叉卖出", "non_daily_timeframe_not_supported"),
        (
            "MACD金叉当天收盘买入，死叉当天收盘卖出",
            "same_session_execution_not_supported",
        ),
        (
            "MACD金叉后下一交易日收盘买入，死叉后下一交易日收盘卖出",
            "execution_price_time_not_supported",
        ),
        (
            "MACD金叉后第二天收盘买入，MACD死叉卖出",
            "execution_price_time_not_supported",
        ),
        (
            "MACD金叉后第二个交易日收盘买入，MACD死叉卖出",
            "execution_price_time_not_supported",
        ),
        (
            "MACD金叉三天后买入，MACD死叉卖出",
            "execution_price_time_not_supported",
        ),
        (
            "MACD金叉后3个交易日买入，MACD死叉卖出",
            "execution_price_time_not_supported",
        ),
        (
            "MACD金叉买入，MACD死叉三天后卖出",
            "execution_price_time_not_supported",
        ),
        (
            "业绩预告亏损后买入，MACD死叉卖出",
            "event_attribute_filter_not_supported",
        ),
        (
            "业绩预告利润大于1000万元后买入，MACD死叉卖出",
            "event_attribute_filter_not_supported",
        ),
        (
            "定期报告净利润增长超过30%后买入，MACD死叉卖出",
            "event_attribute_filter_not_supported",
        ),
        (
            "一季报发布后买入，MACD死叉卖出",
            "event_report_period_filter_not_supported",
        ),
        (
            "2024年报发布后买入，MACD死叉卖出",
            "event_report_period_filter_not_supported",
        ),
        (
            "2024年度报告发布后买入，MACD死叉卖出",
            "event_report_period_filter_not_supported",
        ),
        (
            "今年年报发布后买入，MACD死叉卖出",
            "event_report_period_filter_not_supported",
        ),
        (
            "去年的年度报告发布后买入，MACD死叉卖出",
            "event_report_period_filter_not_supported",
        ),
        (
            "24年报发布后买入，MACD死叉卖出",
            "event_report_period_filter_not_supported",
        ),
        (
            "MACD在零轴上方金叉买入，MACD死叉卖出",
            "technical_qualifier_not_supported",
        ),
    ],
)
def test_http_draft_never_erases_explicit_unsupported_source_semantics(
    client: TestClient,
    utterance: str,
    diagnostic_code: str,
) -> None:
    response = client.post(
        "/api/v1/strategy-drafts",
        json={
            "utterance": utterance,
            "instrument_context": "300059.SZ",
            "as_of_date": "2026-08-30",
        },
    )
    payload: dict[str, Any] = response.json()

    assert response.status_code == 201
    assert payload["status"] == "unsupported"
    assert payload["diagnostic_code"] == diagnostic_code
    assert payload["strategy"] is None


def test_http_draft_preserves_supported_fill_anchored_holding_exit(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/v1/strategy-drafts",
        json={
            "utterance": "年报发布后买入，持有3个交易日卖出",
            "instrument_context": "300059.SZ",
            "as_of_date": "2026-08-30",
        },
    )
    payload: dict[str, Any] = response.json()

    assert response.status_code == 201
    assert payload["status"] == "ready"
    assert payload["strategy"]["exit"]["children"] == [
        {
            "type": "holding_period_exit",
            "sessions": 3,
            "anchor": "first_entry_fill",
            "count_mode": "subsequent_trading_sessions",
            "execution": "target_session_open_proxy",
        }
    ]


@pytest.mark.parametrize(
    "utterance",
    [
        "MACD买入，MACD卖出",
        "RSI买入，RSI卖出",
        "KDJ买入，KDJ卖出",
        "成交量买入，MACD死叉卖出",
    ],
)
def test_http_draft_clarifies_named_indicators_without_triggers(
    client: TestClient,
    utterance: str,
) -> None:
    response = client.post(
        "/api/v1/strategy-drafts",
        json={
            "utterance": utterance,
            "instrument_context": "300059.SZ",
            "as_of_date": "2026-08-30",
        },
    )
    payload: dict[str, Any] = response.json()

    assert response.status_code == 201
    assert payload["status"] == "needs_clarification"
    assert payload["diagnostic_code"] == "indicator_trigger_requires_clarification"
    assert payload["strategy"] is None
    assert payload["strategy_hash"] is None
    assert "触发" in payload["clarification"]
    assert not payload.get("run_requested")


def test_compiler_statuses_are_preserved_as_domain_results(client: TestClient) -> None:
    clarification = client.post(
        "/api/v1/strategy-drafts",
        json={"utterance": "MACD金叉买入，死叉卖出", "as_of_date": "2026-08-27"},
    )
    unsupported = client.post(
        "/api/v1/strategy-drafts",
        json={
            "utterance": "东方财富大跌反弹时买入",
            "instrument_context": "300059.SZ",
            "as_of_date": "2026-08-27",
        },
    )
    invalid = client.post(
        "/api/v1/strategy-drafts",
        json={
            "utterance": "   ",
            "instrument_context": "300059.SZ",
            "as_of_date": "2026-08-27",
        },
    )
    missing_exit = client.post(
        "/api/v1/strategy-drafts",
        json={
            "utterance": "年度报告发布后买入",
            "instrument_context": "300059.SZ",
            "as_of_date": "2026-08-27",
        },
    )
    incomplete_rule = client.post(
        "/api/v1/strategy-drafts",
        json={
            "utterance": "MACD",
            "instrument_context": "300059.SZ",
            "as_of_date": "2026-08-27",
        },
    )
    missing_entry = client.post(
        "/api/v1/strategy-drafts",
        json={
            "utterance": "MACD死叉卖出",
            "instrument_context": "300059.SZ",
            "as_of_date": "2026-08-27",
        },
    )
    unrecognized_entry = client.post(
        "/api/v1/strategy-drafts",
        json={
            "utterance": "随便买入，MACD死叉卖出",
            "instrument_context": "300059.SZ",
            "as_of_date": "2026-08-27",
        },
    )

    assert clarification.json()["status"] == "needs_clarification"
    assert clarification.json()["diagnostic_code"] == "instrument_required"
    assert unsupported.json()["status"] == "unsupported"
    assert unsupported.json()["diagnostic_code"] == "template_not_published/big_drop_rebound"
    assert invalid.json()["status"] == "invalid"
    assert invalid.json()["diagnostic_code"] == "empty_utterance"
    assert missing_exit.json()["status"] == "needs_clarification"
    assert missing_exit.json()["diagnostic_code"] == "exit_rule_not_recognized"
    assert "什么条件下卖出" in missing_exit.json()["clarification"]
    assert missing_exit.json()["idea_route"] is None
    assert missing_exit.json()["strategy"] is None
    assert incomplete_rule.json()["status"] == "needs_clarification"
    assert incomplete_rule.json()["diagnostic_code"] == "strategy_rule_incomplete"
    assert incomplete_rule.json()["clarification"] == "想怎么把它变成买卖规则？"
    assert len(incomplete_rule.json()["idea_route"]["proposals"]) >= 2
    assert incomplete_rule.json()["strategy"] is None
    assert missing_entry.json()["status"] == "needs_clarification"
    assert missing_entry.json()["diagnostic_code"] == "entry_rule_not_recognized"
    assert missing_entry.json()["clarification"] == "什么时候买？"
    assert len(missing_entry.json()["idea_route"]["proposals"]) == 3
    assert missing_entry.json()["strategy"] is None
    assert unrecognized_entry.json()["status"] == "needs_clarification"
    assert unrecognized_entry.json()["diagnostic_code"] == "entry_rule_not_recognized"
    assert unrecognized_entry.json()["clarification"] == "什么时候买？"
    assert len(unrecognized_entry.json()["idea_route"]["proposals"]) == 3
    assert unrecognized_entry.json()["strategy"] is None


def test_idempotency_key_replays_same_create_without_new_draft(
    client: TestClient,
    ready_request: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = {"Idempotency-Key": "draft-mobile-001"}
    first = client.post("/api/v1/strategy-drafts", json=ready_request, headers=headers)

    async def unexpected(*args, **kwargs):
        raise AssertionError("completed replay must not invoke language processing")

    monkeypatch.setattr(StrategyCompiler, "compile", unexpected)
    monkeypatch.setattr(StrategyCompiler, "classify_initial_intent", unexpected)
    monkeypatch.setattr(StrategyCompiler, "compose_ready_response", unexpected)
    second = client.post("/api/v1/strategy-drafts", json=ready_request, headers=headers)

    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert first.headers["Idempotency-Replayed"] == "false"
    assert second.headers["Idempotency-Replayed"] == "true"


def test_idempotency_key_rejects_different_payload(
    client: TestClient,
    ready_request: dict[str, str],
) -> None:
    headers = {"Idempotency-Key": "draft-mobile-002"}
    first = client.post("/api/v1/strategy-drafts", json=ready_request, headers=headers)
    changed = dict(ready_request, instrument_context="000001.SZ")
    second = client.post("/api/v1/strategy-drafts", json=changed, headers=headers)

    assert first.status_code == 201
    _assert_error(second, status_code=409, code="idempotency_key_conflict")


def test_revision_increments_and_is_itself_idempotent(
    client: TestClient,
    ready_request: dict[str, str],
) -> None:
    created = client.post("/api/v1/strategy-drafts", json=ready_request).json()
    url = f"/api/v1/strategy-drafts/{created['draft_id']}/revisions"
    strategy = deepcopy(created["strategy"])
    strategy["entry"]["params"] = {"fast": 10, "signal": 7, "slow": 30}
    strategy["exit"]["children"][0]["params"] = {"fast": 10, "signal": 7, "slow": 30}
    strategy["backtest"] = {
        "start": "2022-01-04",
        "end": "2026-08-06",
        "initial_cash_cny": 250_000,
    }
    changed = {"utterance": ready_request["utterance"], "strategy": strategy}
    headers = {"Idempotency-Key": "revision-001"}

    first = client.post(url, json=changed, headers=headers)
    replay = client.post(url, json=changed, headers=headers)

    assert first.status_code == replay.status_code == 201
    assert first.json()["revision"] == 2
    assert first.json()["draft_id"] == created["draft_id"]
    assert first.json()["strategy"] == strategy
    assert first.json()["strategy_hash"] == canonical_hash(StrategySpec.model_validate(strategy))
    assert first.json()["provenance"] == [{"path": "/", "source": "revision/request.strategy"}]
    assert first.json() == replay.json()
    assert replay.headers["Idempotency-Replayed"] == "true"


def test_clarification_answer_reuses_the_server_saved_sentence_and_creates_a_revision(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/v1/strategy-drafts",
        json={
            "utterance": "东方财富MACD金叉买入",
            "instrument_context": "300059.SZ",
            "as_of_date": "2026-08-20",
        },
    ).json()

    response = client.post(
        (
            f"/api/v1/strategy-drafts/{created['draft_id']}"
            f"/revisions/{created['revision']}/clarification-answers"
        ),
        json={"answer": "MACD死叉卖出"},
    )
    payload: dict[str, Any] = response.json()

    assert response.status_code == 200
    assert payload["reply_kind"] == "accepted"
    assert payload["assistant_message"].strip()
    assert payload["suggestions"] == []
    assert payload["draft"]["draft_id"] == created["draft_id"]
    assert payload["draft"]["revision"] == 2
    assert payload["draft"]["status"] == "ready"
    assert payload["draft"]["strategy"]["instrument"]["symbol"] == "300059.SZ"
    assert payload["draft"]["strategy"]["entry"]["indicator_id"] == "technical.macd"
    assert payload["draft"]["strategy"]["exit"]["children"][0]["indicator_id"] == ("technical.macd")


def test_data_lookup_does_not_consume_pending_strategy_and_preserves_query_wording() -> None:
    provider = _RecordingLiveData()
    app = create_app(live_market_data=provider, live_finance_data=provider)
    with TestClient(app) as dialogue_client:
        created = dialogue_client.post(
            "/api/v1/strategy-drafts",
            json={
                "utterance": "东方财富MACD金叉买入",
                "instrument_context": "300059.SZ",
                "as_of_date": "2026-08-20",
            },
        ).json()
        identity_calls = list(provider.finance_calls)
        query = dialogue_client.post(
            (
                f"/api/v1/strategy-drafts/{created['draft_id']}"
                f"/revisions/{created['revision']}/clarification-answers"
            ),
            json={"answer": "东方财富昨天的换手率是多少"},
        )
        query_calls = list(provider.finance_calls)
        completed = dialogue_client.post(
            (
                f"/api/v1/strategy-drafts/{created['draft_id']}"
                f"/revisions/{created['revision']}/clarification-answers"
            ),
            json={"answer": "MACD死叉卖出"},
        )

    payload: dict[str, Any] = query.json()
    assert query.status_code == 200
    assert payload["draft"]["revision"] == created["revision"]
    assert payload["draft"]["diagnostic_code"] == "exit_rule_not_recognized"
    assert payload["data"]["kind"] == "finance"
    assert payload["data"]["finance"]["provider"] == "test_finance"
    assert payload["data"]["finance"]["provenance"]["response_sha256"].startswith("sha256:")
    assert identity_calls == [("查询A股300059.SZ的证券代码和股票简称", "证券代码和股票简称")]
    assert query_calls == [*identity_calls, ("东方财富昨天的换手率是多少", "昨天的换手率")]
    assert provider.screen_calls == []
    # This fixture has no model data reviewer; return the actual table without
    # locally asserting that its date/metric satisfies the user's question.
    assert "数据已返回" in payload["assistant_message"]
    assert "2026-09-02" in payload["assistant_message"]
    assert "换手率(%)=2.37" in payload["assistant_message"]
    assert payload["data"]["finance"]["tables"][0]["rawTable"] == {
        "headers": ["日期", "换手率(%)"], "data": [["2026-09-02", 2.37]],
    }
    assert "全部满足" not in payload["assistant_message"]
    assert "test_finance" not in payload["assistant_message"]
    assert "不作为历史回测数据" not in payload["assistant_message"]
    assert completed.status_code == 200
    assert completed.json()["draft"]["status"] == "ready"


def test_entity_omitted_data_lookup_reuses_only_the_verified_instrument_context() -> None:
    provider = _RecordingLiveData()
    app = create_app(live_market_data=provider, live_finance_data=provider)
    with TestClient(app) as dialogue_client:
        created = dialogue_client.post(
            "/api/v1/strategy-drafts",
            json={
                "utterance": "东方财富MACD金叉买入",
                "instrument_context": "300059.SZ",
                "as_of_date": "2026-08-20",
            },
        ).json()
        identity_calls = list(provider.finance_calls)
        response = dialogue_client.post(
            (
                f"/api/v1/strategy-drafts/{created['draft_id']}"
                f"/revisions/{created['revision']}/clarification-answers"
            ),
            json={"answer": "昨天换手率多少"},
        )

    assert response.status_code == 200
    assert identity_calls == [("查询A股300059.SZ的证券代码和股票简称", "证券代码和股票简称")]
    assert provider.finance_calls == [*identity_calls, ("300059.SZ；昨天换手率多少", "昨天换手率")]
    assert response.json()["draft"]["revision"] == created["revision"]


def test_entity_omitted_data_lookup_without_verified_context_asks_for_stock() -> None:
    provider = _RecordingLiveData()
    app = create_app(live_market_data=provider, live_finance_data=provider)
    with TestClient(app) as dialogue_client:
        created = dialogue_client.post(
            "/api/v1/strategy-drafts",
            json={
                "utterance": "MACD金叉买入",
                "as_of_date": "2026-08-20",
            },
        ).json()
        response = dialogue_client.post(
            (
                f"/api/v1/strategy-drafts/{created['draft_id']}"
                f"/revisions/{created['revision']}/clarification-answers"
            ),
            json={"answer": "昨天换手率多少"},
        )

    payload: dict[str, Any] = response.json()
    assert response.status_code == 200
    assert provider.finance_calls == []
    assert "哪只股票" in payload["assistant_message"]
    assert payload.get("data") is None
    assert payload["draft"]["revision"] == created["revision"]


def test_discovery_query_with_metric_uses_screen_then_finance_without_revising_draft() -> None:
    provider = _RecordingLiveData()
    app = create_app(live_market_data=provider, live_finance_data=provider)
    with TestClient(app) as dialogue_client:
        created = dialogue_client.post(
            "/api/v1/strategy-drafts",
            json={
                "utterance": "东方财富MACD金叉买入",
                "instrument_context": "300059.SZ",
                "as_of_date": "2026-08-20",
            },
        ).json()
        identity_calls = list(provider.finance_calls)
        response = dialogue_client.post(
            (
                f"/api/v1/strategy-drafts/{created['draft_id']}"
                f"/revisions/{created['revision']}/clarification-answers"
            ),
            json={"answer": "上涨的股票；获取近10年的归母净利润"},
        )

    payload: dict[str, Any] = response.json()
    assert response.status_code == 200
    assert payload["draft"]["revision"] == created["revision"]
    assert payload["assistant_message"].strip()
    assert payload["draft"]["diagnostic_code"] == created["diagnostic_code"]
    assert payload["data"]["kind"] == "screened_finance"
    assert payload["data"]["historical_backtest_eligible"] is False
    assert provider.screen_finance_calls == [
        ("上涨的股票", "A股", "近10年的归母净利润")
    ]
    assert provider.screen_calls == []
    assert provider.finance_calls == identity_calls


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (MxSaasProviderAuthError("bad credential"), "授权失败"),
        (MxSaasProviderUnavailableError("temporary"), "未完成本次查询"),
        (None, "没有找到符合条件的结果"),
    ],
)
def test_data_lookup_distinguishes_auth_transient_and_empty_results(
    error: Exception | None,
    expected: str,
) -> None:
    provider = _FailingLiveFinanceData(error)
    app = create_app(live_finance_data=provider)
    with TestClient(app) as dialogue_client:
        created = dialogue_client.post(
            "/api/v1/strategy-drafts",
            json={
                "utterance": "东方财富MACD金叉买入",
                "instrument_context": "300059.SZ",
                "as_of_date": "2026-08-20",
            },
        ).json()
        response = dialogue_client.post(
            (
                f"/api/v1/strategy-drafts/{created['draft_id']}"
                f"/revisions/{created['revision']}/clarification-answers"
            ),
            json={"answer": "东方财富昨天的换手率是多少"},
        )

    payload: dict[str, Any] = response.json()
    assert response.status_code == 200
    assert payload["draft"]["revision"] == created["revision"]
    assert expected in payload["assistant_message"]
    for field in ("revision", "status", "diagnostic_code", "strategy", "idea_route"):
        assert payload["draft"][field] == created[field]
    if error is not None:
        assert payload["query_diagnostic_code"]
    assert "data" not in payload if error is not None else payload["data"]["kind"] == "finance"


def test_clarification_progress_to_missing_stock_retains_rules_when_screener_unconfigured(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/v1/strategy-drafts",
        json={
            "utterance": "MACD金叉买入",
            "as_of_date": "2026-08-20",
        },
    ).json()

    response = client.post(
        (
            f"/api/v1/strategy-drafts/{created['draft_id']}"
            f"/revisions/{created['revision']}/clarification-answers"
        ),
        json={"answer": "MACD死叉卖出"},
    )
    payload: dict[str, Any] = response.json()

    assert response.status_code == 200
    assert payload["reply_kind"] == "accepted"
    assert payload["draft"]["status"] == "needs_clarification"
    assert payload["draft"]["diagnostic_code"] == "instrument_required"
    assert "暂时无法帮你挑选股票" in payload["assistant_message"]
    assert "方案已保留" in payload["assistant_message"]
    assert payload["assistant_message"].count("输入想回测的股票") == 1
    assert payload["draft"]["revision"] == created["revision"] + 1
    assert payload["draft"]["strategy"] is None
    assert payload["draft"]["instrument_suggestion"] is None
    assert not payload["draft"].get("run_requested")
    assert "接下来只差：我只差" not in payload["assistant_message"]


def test_complete_strategy_answer_replaces_a_missing_instrument_turn(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/v1/strategy-drafts",
        json={
            "utterance": "MACD金叉买入，MACD死叉卖出",
            "as_of_date": "2026-08-20",
        },
    ).json()
    replacement = "300033.SZ RSI低于30买入，RSI高于70卖出，回测近1年"
    fresh = client.post(
        "/api/v1/strategy-drafts",
        json={"utterance": replacement, "as_of_date": "2026-08-20"},
    ).json()

    response = client.post(
        (
            f"/api/v1/strategy-drafts/{created['draft_id']}"
            f"/revisions/{created['revision']}/clarification-answers"
        ),
        json={"answer": replacement},
    )
    payload: dict[str, Any] = response.json()

    assert created["diagnostic_code"] == "instrument_required"
    assert response.status_code == 200
    assert payload["reply_kind"] == "accepted"
    assert payload["assistant_message"].strip()
    assert payload["draft"]["revision"] == 2
    assert payload["draft"]["status"] == "ready"
    assert payload["draft"]["strategy_hash"] == fresh["strategy_hash"]
    assert payload["draft"]["strategy"]["instrument"]["symbol"] == "300033.SZ"
    assert payload["draft"]["strategy"]["entry"]["indicator_id"] == "technical.rsi"
    assert payload["draft"]["strategy"]["exit"]["children"][0]["indicator_id"] == ("technical.rsi")
    assert "technical.macd" not in str(payload["draft"]["strategy"])


def test_complete_rule_with_same_missing_stock_diagnostic_replaces_old_rule(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/v1/strategy-drafts",
        json={
            "utterance": "MACD金叉买入，MACD死叉卖出",
            "as_of_date": "2026-08-20",
        },
    ).json()
    replacement = client.post(
        (
            f"/api/v1/strategy-drafts/{created['draft_id']}"
            f"/revisions/{created['revision']}/clarification-answers"
        ),
        json={"answer": "上涨5%卖出，跌2%买入"},
    ).json()

    assert replacement["reply_kind"] == "accepted"
    assert replacement["draft"]["revision"] == 2
    assert replacement["draft"]["diagnostic_code"] == "instrument_required"

    completed = client.post(
        (f"/api/v1/strategy-drafts/{created['draft_id']}/revisions/2/clarification-answers"),
        json={"answer": "300059.SZ"},
    ).json()

    assert completed["draft"]["status"] == "ready"
    assert completed["draft"]["strategy"]["entry"]["indicator_id"] == "price.return_pct"
    assert completed["draft"]["strategy"]["exit"]["children"][0]["indicator_id"] == (
        "price.return_pct"
    )
    assert "technical.macd" not in str(completed["draft"]["strategy"])


def test_instrument_only_answer_preserves_the_saved_rule(client: TestClient) -> None:
    created = client.post(
        "/api/v1/strategy-drafts",
        json={
            "utterance": "MACD金叉买入，MACD死叉卖出",
            "as_of_date": "2026-08-20",
        },
    ).json()

    response = client.post(
        (
            f"/api/v1/strategy-drafts/{created['draft_id']}"
            f"/revisions/{created['revision']}/clarification-answers"
        ),
        json={"answer": "300033.SZ"},
    )
    payload: dict[str, Any] = response.json()

    assert response.status_code == 200
    assert payload["reply_kind"] == "accepted"
    assert payload["draft"]["status"] == "ready"
    assert payload["draft"]["strategy"]["instrument"]["symbol"] == "300033.SZ"
    assert payload["draft"]["strategy"]["entry"]["indicator_id"] == "technical.macd"
    assert payload["draft"]["strategy"]["exit"]["children"][0]["indicator_id"] == ("technical.macd")


def test_complete_strategy_answer_replaces_a_missing_exit_turn(client: TestClient) -> None:
    created = client.post(
        "/api/v1/strategy-drafts",
        json={
            "utterance": "300059.SZ MACD金叉买入",
            "as_of_date": "2026-08-20",
        },
    ).json()
    replacement = "300059.SZ RSI低于30买入，RSI高于70卖出，回测近1年"
    fresh = client.post(
        "/api/v1/strategy-drafts",
        json={"utterance": replacement, "as_of_date": "2026-08-20"},
    ).json()

    response = client.post(
        (
            f"/api/v1/strategy-drafts/{created['draft_id']}"
            f"/revisions/{created['revision']}/clarification-answers"
        ),
        json={"answer": replacement},
    )
    payload: dict[str, Any] = response.json()

    assert created["diagnostic_code"] == "exit_rule_not_recognized"
    assert response.status_code == 200
    assert payload["reply_kind"] == "accepted"
    assert payload["assistant_message"].strip()
    assert payload["draft"]["status"] == "ready"
    assert payload["draft"]["strategy_hash"] == fresh["strategy_hash"]
    assert payload["draft"]["strategy"]["entry"]["indicator_id"] == "technical.rsi"
    assert payload["draft"]["strategy"]["exit"]["children"][0]["indicator_id"] == ("technical.rsi")
    assert "technical.macd" not in str(payload["draft"]["strategy"])


def test_named_partial_strategy_answer_moves_past_the_old_instrument_question() -> None:
    class InterpretedCandidates:
        """This HTTP test starts at the model-result boundary, not Chinese parsing."""

        def __init__(self) -> None:
            self.requests: list[CompileInput] = []

        async def generate(self, request: CompileInput) -> tuple[CandidateAst, ...]:
            if request.semantic_intent is None:
                # No optional exit suggestions are approved by this fixture;
                # the user's missing sell side must remain non-executable.
                return ()
            self.requests.append(request)
            assert request.semantic_intent == "new_strategy"
            if len(self.requests) == 1:
                assert request.utterance == "MACD金叉买入，MACD死叉卖出"
                return (CandidateAst(
                    instrument_symbol=None, confidence=0.95,
                    entry=(IndicatorIntent("technical.macd", "1.0.0", "golden_cross",
                                           (("fast", 12), ("slow", 26), ("signal", 9))),),
                    exit=(IndicatorIntent("technical.macd", "1.0.0", "death_cross",
                                          (("fast", 12), ("slow", 26), ("signal", 9))),),
                ),)
            assert request.utterance == "同花顺放量1.5倍买入"
            return (CandidateAst(
                instrument_symbol=None, instrument_name="同花顺", confidence=0.95,
                entry=(IndicatorIntent("volume.relative", "1.0.0", "gte_multiple",
                                       (("baseline_period", 20), ("consecutive_days", 3)), 1.5),),
                exit=(), unsupported_code="exit_rule_not_recognized",
                grounding_evidence=(CandidateGroundingEvidence(
                    path="/instrument/name", start=0, end=3, text="同花顺",
                ),),
            ),)

    interpreted = InterpretedCandidates()
    generator = HybridCandidateGenerator(
        deterministic=RuleBasedCandidateGenerator(),
        bounded_fallback=interpreted,
        instrument_name_resolver=lambda name: {"同花顺": "300033.SZ"}[name],
        model_first=True,
    )
    compiler = StrategyCompiler(
        generator=generator,
        catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals",
        release_version="2026.09.01",
    )
    compiler.classify_initial_intent = AsyncMock(return_value=TurnIntent.NEW_STRATEGY)
    compiler.classify_dialogue_intent = AsyncMock(return_value=TurnIntent.NEW_STRATEGY)
    app = create_app(compiler=compiler)
    with TestClient(app) as dialogue_client:
        created = dialogue_client.post(
            "/api/v1/strategy-drafts",
            json={
                "utterance": "MACD金叉买入，MACD死叉卖出",
                "as_of_date": "2026-08-20",
            },
        ).json()
        response = dialogue_client.post(
            (
                f"/api/v1/strategy-drafts/{created['draft_id']}"
                f"/revisions/{created['revision']}/clarification-answers"
            ),
            json={"answer": "同花顺放量1.5倍买入"},
        )
    payload: dict[str, Any] = response.json()

    assert response.status_code == 200
    assert payload["reply_kind"] == "accepted"
    assert payload["draft"]["revision"] == 2
    assert payload["draft"]["status"] == "needs_clarification"
    assert payload["draft"]["diagnostic_code"] == "exit_rule_not_recognized"
    assert payload["draft"]["strategy"] is None
    assert not payload["draft"].get("run_requested")
    assert len(interpreted.requests) == 2
    assert payload["draft"]["candidate_grounding"]["matched_spans"] == ["同花顺"]
    assert payload["draft"]["candidate_grounding"]["spans"] == [{
        "path": "/instrument/symbol", "start": 0, "end": 3, "text": "同花顺",
    }]
    assert "股票" not in payload["assistant_message"]
    assert "卖" in payload["assistant_message"]


def test_unresolved_clarification_stays_textual_and_returns_only_validated_choices(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/v1/strategy-drafts",
        json={
            "utterance": "东方财富MACD金叉买入",
            "instrument_context": "300059.SZ",
            "as_of_date": "2026-08-20",
        },
    ).json()

    response = client.post(
        (
            f"/api/v1/strategy-drafts/{created['draft_id']}"
            f"/revisions/{created['revision']}/clarification-answers"
        ),
        json={"answer": "我是你爸"},
    )
    payload: dict[str, Any] = response.json()

    assert response.status_code == 200
    assert payload["reply_kind"] == "clarification"
    assert payload["draft"]["revision"] == created["revision"]
    assert payload["draft"]["diagnostic_code"] == created["diagnostic_code"]
    assert "听到了" in payload["assistant_message"]
    assert "进场条件" in payload["assistant_message"]
    assert "什么时候卖" in payload["assistant_message"]
    assert 2 <= len(payload["suggestions"]) <= 3
    proposal_ids = {item["id"] for item in created["idea_route"]["proposals"]}
    assert {item["id"] for item in payload["suggestions"]}.issubset(proposal_ids)
    assert all(set(item) == {"id", "title", "preview"} for item in payload["suggestions"])


@pytest.mark.parametrize(
    ("answer", "expected_acknowledgement"),
    [
        ("我不想用MACD死叉卖出", "不想用"),
        ("你觉得MACD死叉卖出好吗", "询问"),
        ("比如MACD死叉卖出", "举例"),
    ],
)
def test_negation_question_and_example_never_become_an_executable_supplement(
    client: TestClient,
    answer: str,
    expected_acknowledgement: str,
) -> None:
    created = client.post(
        "/api/v1/strategy-drafts",
        json={
            "utterance": "东方财富MACD金叉买入",
            "instrument_context": "300059.SZ",
            "as_of_date": "2026-08-20",
        },
    ).json()

    response = client.post(
        (
            f"/api/v1/strategy-drafts/{created['draft_id']}"
            f"/revisions/{created['revision']}/clarification-answers"
        ),
        json={"answer": answer},
    )
    payload: dict[str, Any] = response.json()

    assert response.status_code == 200
    assert payload["reply_kind"] == "clarification"
    assert payload["draft"]["revision"] == 1
    assert payload["draft"]["status"] == "needs_clarification"
    assert payload["draft"]["diagnostic_code"] == "exit_rule_not_recognized"
    assert expected_acknowledgement in payload["assistant_message"]
    assert 2 <= len(payload["suggestions"]) <= 3


def test_unsupported_recompile_does_not_replace_the_valid_clarification_revision(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/v1/strategy-drafts",
        json={
            "utterance": "东方财富MACD金叉买入",
            "instrument_context": "300059.SZ",
            "as_of_date": "2026-08-20",
        },
    ).json()

    response = client.post(
        (
            f"/api/v1/strategy-drafts/{created['draft_id']}"
            f"/revisions/{created['revision']}/clarification-answers"
        ),
        json={"answer": "MACD在零轴上方死叉卖出"},
    )
    payload: dict[str, Any] = response.json()

    assert response.status_code == 200
    assert payload["reply_kind"] == "clarification"
    assert payload["draft"]["revision"] == 1
    assert payload["draft"]["diagnostic_code"] == "exit_rule_not_recognized"
    assert payload["draft"]["status"] == "needs_clarification"


def test_off_topic_model_reply_is_not_appended_or_applied_as_a_strategy_change() -> (
    None
):
    router = _RecordingDialogueRouter()
    compiler = StrategyCompiler(
        generator=HybridCandidateGenerator(
            deterministic=RuleBasedCandidateGenerator(),
            bounded_fallback=_UnexpectedFallback(),
        ),
        catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals",
        release_version="2026.09.01",
        clarification_dialogue_router=router,
    )
    app = create_app(compiler=compiler)
    with TestClient(app) as dialogue_client:
        created = dialogue_client.post(
            "/api/v1/strategy-drafts",
            json={
                "utterance": "东方财富MACD金叉买入",
                "instrument_context": "300059.SZ",
                "as_of_date": "2026-08-20",
            },
        ).json()
        unresolved = dialogue_client.post(
            (
                f"/api/v1/strategy-drafts/{created['draft_id']}"
                f"/revisions/{created['revision']}/clarification-answers"
            ),
            json={"answer": "你好，我是你爸"},
        ).json()

    assert len(router.requests) == 1
    assert router.requests[0].answer == "你好，我是你爸"
    assert router.requests[0].prior_utterance == "东方财富MACD金叉买入"
    assert unresolved["assistant_message"] == router.natural_reply
    assert unresolved["suggestions"] == []
    for field in ("draft_id", "revision", "status", "diagnostic_code", "strategy", "idea_route"):
        assert unresolved["draft"][field] == created[field]
    assert unresolved["draft"]["diagnostic_code"] == "exit_rule_not_recognized"
    assert not unresolved["draft"].get("run_requested")


def test_first_turn_conversation_uses_dialogue_provider_before_strategy_translation() -> None:
    router = _RecordingDialogueRouter()
    compiler = StrategyCompiler(
        generator=HybridCandidateGenerator(
            deterministic=RuleBasedCandidateGenerator(),
            bounded_fallback=_UnexpectedFallback(),
        ),
        catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals",
        release_version="2026.09.01",
        clarification_dialogue_router=router,
    )
    app = create_app(compiler=compiler)
    with TestClient(app) as dialogue_client:
        response = dialogue_client.post(
            "/api/v1/strategy-drafts",
            json={"utterance": "你好，我是你爸", "as_of_date": "2026-08-20"},
        )

    payload: dict[str, Any] = response.json()
    assert response.status_code == 201
    # Best-effort identity enrichment follows routing. It must neither replace
    # the conversation reply nor translate it into an executable strategy.
    assert len(router.requests) == 2
    assert not router.requests[0].identity_only
    assert router.requests[1].identity_only
    assert not router.requests[1].allow_data_query
    assert router.requests[0].answer == "你好，我是你爸"
    assert router.requests[0].prior_utterance == ""
    assert router.requests[0].options == ()
    assert payload["status"] == "needs_clarification"
    assert payload["diagnostic_code"] == "conversation_only"
    assert payload["strategy"] is None
    assert payload["strategy_hash"] is None
    assert not payload.get("run_requested")
    assert payload["clarification"].startswith("我知道你是在开玩笑")
    assert payload["clarification"] == "我知道你是在开玩笑，这句先不作为策略条件。"
    assert router.requests[0].question == ""
    assert "下面选" not in payload["clarification"]


def test_first_person_strategy_sentence_is_not_intercepted_as_small_talk() -> None:
    router = _RecordingDialogueRouter(natural_reply="已按你的金叉、死叉规则准备好，可以核对。")
    compiler = StrategyCompiler(
        generator=RuleBasedCandidateGenerator(),
        catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals",
        release_version="2026.09.01",
        clarification_dialogue_router=router,
    )
    app = create_app(compiler=compiler)
    with TestClient(app) as dialogue_client:
        response = dialogue_client.post(
            "/api/v1/strategy-drafts",
            json={
                "utterance": "我是想MACD金叉买入，死叉卖出",
                "instrument_context": "300059.SZ",
                "as_of_date": "2026-08-20",
            },
        )

    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "ready"
    assert len(router.requests) == 1
    request = router.requests[0]
    assert request.response_only
    assert request.diagnostic_code == "response_only"
    assert request.answer == "我是想MACD金叉买入，死叉卖出"
    assert payload["assistant_message"] == router.natural_reply
    assert "golden_cross" in str(payload["strategy"]["entry"])
    assert "death_cross" in str(payload["strategy"]["exit"])
    assert not payload.get("run_requested")


def test_valid_clarification_supplement_uses_response_model_without_changing_rules() -> None:
    # Even inconsistent response prose cannot replace the validated AST or authorize a run.
    router = _RecordingDialogueRouter(natural_reply="改成RSI策略，已经开始回测。")
    compiler = StrategyCompiler(
        generator=RuleBasedCandidateGenerator(),
        catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals",
        release_version="2026.09.01",
        clarification_dialogue_router=router,
    )
    app = create_app(compiler=compiler)
    with TestClient(app) as dialogue_client:
        created = dialogue_client.post(
            "/api/v1/strategy-drafts",
            json={
                "utterance": "东方财富MACD金叉买入",
                "instrument_context": "300059.SZ",
                "as_of_date": "2026-08-20",
            },
        ).json()
        response = dialogue_client.post(
            (
                f"/api/v1/strategy-drafts/{created['draft_id']}"
                f"/revisions/{created['revision']}/clarification-answers"
            ),
            json={"answer": "MACD死叉卖出"},
        )

    assert response.status_code == 200
    payload = response.json()
    draft = payload["draft"]
    assert draft["status"] == "ready"
    assert draft["revision"] == created["revision"] + 1
    assert len(router.requests) == 1
    request = router.requests[0]
    assert request.response_only
    assert request.answer == "MACD死叉卖出"
    assert request.options == ()
    assert "golden_cross" in str(draft["strategy"]["entry"])
    assert "death_cross" in str(draft["strategy"]["exit"])
    assert "RSI" not in str(draft["strategy"])
    serialized = StrategySpec.model_validate(draft["strategy"]).model_dump_json()
    assert serialized in request.context_summary
    assert not draft.get("run_requested")
    assert not payload.get("run_requested")


def test_clarification_answer_rejects_a_stale_or_missing_revision(client: TestClient) -> None:
    created = client.post(
        "/api/v1/strategy-drafts",
        json={
            "utterance": "东方财富MACD金叉买入",
            "instrument_context": "300059.SZ",
            "as_of_date": "2026-08-20",
        },
    ).json()
    url = (
        f"/api/v1/strategy-drafts/{created['draft_id']}"
        f"/revisions/{created['revision']}/clarification-answers"
    )
    assert client.post(url, json={"answer": "MACD死叉卖出"}).status_code == 200

    stale = client.post(url, json={"answer": "持有5个交易日卖出"})
    missing = client.post(
        f"/api/v1/strategy-drafts/{uuid4()}/revisions/1/clarification-answers",
        json={"answer": "MACD死叉卖出"},
    )

    _assert_error(stale, status_code=409, code="strategy_draft_revision_stale")
    _assert_error(missing, status_code=404, code="strategy_draft_not_found")


def test_revision_preserves_event_leaf_and_edited_exit_exactly(client: TestClient) -> None:
    request = {
        "utterance": "东方财富年报发布后买入，MACD死叉卖出",
        "instrument_context": "300059.SZ",
        "as_of_date": "2026-08-06",
    }
    created = client.post("/api/v1/strategy-drafts", json=request).json()
    strategy = deepcopy(created["strategy"])
    event_leaf = deepcopy(strategy["entry"])
    strategy["exit"]["children"][0]["params"] = {"fast": 8, "signal": 5, "slow": 21}
    strategy["backtest"] = {
        "start": "2023-01-03",
        "end": "2026-08-06",
        "initial_cash_cny": 180_000,
    }

    response = client.post(
        f"/api/v1/strategy-drafts/{created['draft_id']}/revisions",
        json={"utterance": request["utterance"], "strategy": strategy},
    )
    payload: dict[str, Any] = response.json()

    assert response.status_code == 201
    assert payload["revision"] == 2
    assert payload["strategy"] == strategy
    assert payload["strategy"]["entry"] == event_leaf
    assert payload["strategy_hash"] == canonical_hash(StrategySpec.model_validate(strategy))


def test_revision_rejects_strategy_outside_active_catalog(
    client: TestClient,
    ready_request: dict[str, str],
) -> None:
    created = client.post("/api/v1/strategy-drafts", json=ready_request).json()
    strategy = deepcopy(created["strategy"])
    strategy["entry"]["definition_version"] = "999.0.0"

    response = client.post(
        f"/api/v1/strategy-drafts/{created['draft_id']}/revisions",
        json={"utterance": ready_request["utterance"], "strategy": strategy},
    )

    _assert_error(response, status_code=422, code="strategy_revision_invalid")


def test_revision_rejects_a_share_code_suffix_mismatch(
    client: TestClient,
    ready_request: dict[str, str],
) -> None:
    created = client.post("/api/v1/strategy-drafts", json=ready_request).json()
    strategy = deepcopy(created["strategy"])
    strategy["instrument"]["symbol"] = "300059.SH"

    response = client.post(
        f"/api/v1/strategy-drafts/{created['draft_id']}/revisions",
        json={"utterance": ready_request["utterance"], "strategy": strategy},
    )

    _assert_error(response, status_code=422, code="strategy_revision_invalid")


@pytest.mark.parametrize("answer", ["用它", "我自己选股票", None])
def test_missing_stock_offers_screened_candidate_and_respects_choice(answer: str | None) -> None:
    class StockAdvisor:
        async def recommend_stocks(
            self, query: str, result: LiveMarketDataResult,
        ) -> tuple[StockRecommendation, ...]:
            assert "MACD金叉买入，MACD死叉卖出" in query
            assert result.rows == ({"证券代码": "300059", "证券简称": "东方财富"},)
            return (StockRecommendation("300059.SZ", "东方财富", "筛选结果包含该 A 股。"),)

        async def advise(self, request: Any) -> None:
            raise AssertionError("stock choice must not regenerate the user's rules")

    provider = _RecordingLiveData()
    with TestClient(create_app(
        live_market_data=provider, strategy_advisor=StockAdvisor() if answer else None,
    )) as local:
        created = local.post("/api/v1/strategy-drafts", json={
            "utterance": "MACD金叉买入，MACD死叉卖出", "as_of_date": "2026-09-04",
        }).json()
        if answer is None:
            # A screen row alone is not an audited recommendation.
            assert len(provider.screen_calls) == 1
            assert created["instrument_suggestion"] is None
            assert created["strategy"] is None
            assert not created.get("run_requested")
            assert "股票筛选结果还未完成核实" in created["clarification"]
            return
        assert created["instrument_suggestion"]["symbol"] == "300059.SZ"
        assert created["strategy"] is None
        assert not created.get("run_requested")
        response = local.post(
            f"/api/v1/strategy-drafts/{created['draft_id']}"
            f"/revisions/{created['revision']}/clarification-answers",
            json={"answer": answer},
        )
        assert response.status_code == 200
        draft = response.json()["draft"]
        assert len(provider.screen_calls) == 1
        if answer == "用它":
            assert draft["status"] == "ready"
            assert draft["strategy"]["instrument"]["symbol"] == "300059.SZ"
            assert draft["strategy"]["entry"]["indicator_id"] == "technical.macd"
            assert draft["strategy"]["entry"]["trigger"] == "golden_cross"
            assert draft["strategy"]["exit"]["op"] == "first_of"
            assert len(draft["strategy"]["exit"]["children"]) == 1
            exit_rule = draft["strategy"]["exit"]["children"][0]
            assert exit_rule["indicator_id"] == "technical.macd"
            assert exit_rule["trigger"] == "death_cross"
        else:
            assert draft["status"] == "needs_clarification"
            assert draft["instrument_suggestion"] is None
            assert draft["strategy"] is None
        assert not draft.get("run_requested")


def test_optimization_recovers_validated_strategy_after_draft_loss(
    client: TestClient, ready_request: dict[str, str],
) -> None:
    original = client.post("/api/v1/strategy-drafts", json=ready_request).json()
    missing_id = str(uuid4())
    recovered = client.post(
        f"/api/v1/strategy-drafts/{missing_id}/revisions",
        json={"utterance": ready_request["utterance"], "strategy": original["strategy"],
              "recover_if_missing": True},
    )
    assert recovered.status_code == 201
    body = recovered.json()
    assert body["status"] == "ready"
    assert body["draft_id"] != missing_id
    assert body["strategy"] == original["strategy"]
    # The recovered draft is a real saved revision, usable by subsequent edits.
    assert client.post(
        f"/api/v1/strategy-drafts/{body['draft_id']}/revisions",
        json={"strategy": body["strategy"]},
    ).json()["revision"] == 2
    invalid = deepcopy(original["strategy"])
    invalid["catalog"]["release_version"] = "not-active"
    assert client.post(
        f"/api/v1/strategy-drafts/{uuid4()}/revisions",
        json={"strategy": invalid, "recover_if_missing": True},
    ).status_code == 422


def test_missing_draft_and_invalid_request_use_uniform_error_envelope(
    client: TestClient,
    ready_request: dict[str, str],
) -> None:
    strategy = client.post("/api/v1/strategy-drafts", json=ready_request).json()["strategy"]
    missing = client.post(
        f"/api/v1/strategy-drafts/{uuid4()}/revisions",
        json={"utterance": ready_request["utterance"], "strategy": strategy},
        headers={"X-Request-ID": "contract-404"},
    )
    invalid = client.post(
        "/api/v1/strategy-drafts",
        json={"utterance": "x", "as_of_date": "not-a-date", "unexpected": True},
    )

    _assert_error(missing, status_code=404, code="strategy_draft_not_found")
    assert missing.json()["request_id"] == "contract-404"
    assert missing.headers["X-Request-ID"] == "contract-404"
    _assert_error(invalid, status_code=422, code="request_validation_failed")
    assert invalid.json()["error"]["details"]


def test_body_and_field_limits_are_enforced_before_compilation() -> None:
    with TestClient(create_app(max_body_bytes=256)) as limited_client:
        response = limited_client.post(
            "/api/v1/strategy-drafts",
            content=b"{" + b"x" * 300 + b"}",
            headers={"Content-Type": "application/json", "X-Request-ID": "too-big"},
        )
    _assert_error(response, status_code=413, code="request_body_too_large")
    assert response.json()["request_id"] == "too-big"

    with TestClient(create_app()) as normal_client:
        too_long = normal_client.post(
            "/api/v1/strategy-drafts",
            json={"utterance": "x" * 2_001, "as_of_date": "2026-08-27"},
        )
    _assert_error(too_long, status_code=422, code="request_validation_failed")


def test_unexpected_errors_are_sanitized_and_correlated() -> None:
    app = create_app()
    app.state.container = replace(app.state.container, compiler=_ExplodingCompiler())
    with TestClient(app, raise_server_exceptions=False) as failing_client:
        response = failing_client.post(
            "/api/v1/strategy-drafts",
            json={
                "utterance": "MACD金叉买入，死叉卖出",
                "instrument_context": "300059.SZ",
                "as_of_date": "2026-08-27",
            },
            headers={"X-Request-ID": "contract-500"},
        )

    _assert_error(response, status_code=500, code="internal_error")
    assert response.json()["request_id"] == "contract-500"
    assert "secret" not in response.text


def _assert_error(response: Any, *, status_code: int, code: str) -> None:
    assert response.status_code == status_code
    payload: dict[str, Any] = response.json()
    assert set(payload) == {"error", "request_id"}
    assert payload["error"]["code"] == code
    assert payload["request_id"]
    assert response.headers["X-Request-ID"] == payload["request_id"]
