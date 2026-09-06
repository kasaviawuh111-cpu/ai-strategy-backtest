# Starlette's TestClient currently exposes partially unknown httpx method types to Pyright.
# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

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
from ashare_lab.api.schemas import StrategyDraftResponse
from ashare_lab.application.compile_strategy import CompileOutcome, CompileStatus, StrategyCompiler
from ashare_lab.domain.catalog import load_catalog_directory
from ashare_lab.domain.strategy import StrategySpec, canonical_hash
from ashare_lab.ports.candidate_generation import CandidateAst, CompileInput
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

ROOT = Path(__file__).resolve().parents[3]


class _UnexpectedFallback:
    async def generate(self, request: CompileInput) -> tuple[CandidateAst, ...]:
        raise AssertionError(f"bounded fallback must not run: {request!r}")


class _ExplodingCompiler:
    async def compile(self, _request: Any) -> Any:
        raise RuntimeError("internal secret must not leak")


class _RecordingDialogueRouter:
    def __init__(self, *, explode: bool = False) -> None:
        self.explode = explode
        self.requests: list[ClarificationDialogueRequest] = []

    async def assess(
        self,
        request: ClarificationDialogueRequest,
    ) -> ClarificationDialogueAssessment | None:
        self.requests.append(request)
        if self.explode:
            raise AssertionError("valid supplement must not call dialogue provider")
        return ClarificationDialogueAssessment(
            reply_kind="off_topic",
            acknowledgement_id="light_redirect",
            natural_reply="我知道你是在开玩笑，这句先不作为策略条件。",
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


def test_unbound_viewpoint_guidance_is_exposed_without_guessing_a_stock() -> None:
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
    assert payload["diagnostic_code"] == "idea_guidance_required"
    assert payload["strategy"] is None
    assert payload["idea_route"]["asset_mapping"] == {
        "instrument_symbol": None,
        "relation": "unbound",
        "rationale": "尚未绑定证券；选择方向后仍需补充具体 A 股。",
        "evidence_status": "instrument_required",
    }
    assert all(
        proposal["instrument_symbol"] is None and proposal["capability_ids"] == []
        for proposal in payload["idea_route"]["proposals"]
    )
    StrategyDraftResponse.model_validate(payload)
    # Empty capabilities are only valid for directions awaiting a stock.
    payload["idea_route"]["proposals"][0]["instrument_symbol"] = "300059.SZ"
    with pytest.raises(ValidationError, match="bound proposals require"):
        StrategyDraftResponse.model_validate(payload)


def test_bare_cross_api_preserves_instrument_and_clarification_grounding() -> None:
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
    assert payload["status"] == "needs_clarification"
    assert payload["diagnostic_code"] == "ambiguous_cross_indicator"
    assert payload["idea_route"]["asset_mapping"]["instrument_symbol"] == "300459.SZ"
    assert payload["candidate_grounding"]["matched_spans"] == ["汤姆猫", "汤姆猫金叉"]
    assert [(item["path"], item["text"]) for item in payload["candidate_grounding"]["spans"]] == [
        ("/instrument/symbol", "汤姆猫"),
        ("/clarification", "汤姆猫金叉"),
    ]


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


def test_unsupported_position_aware_exit_and_is_explicit_in_http_contract(
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
    assert payload["status"] == "unsupported"
    assert payload["strategy"] is None
    assert payload["diagnostic_code"] == "position_aware_exit_and_not_supported"
    assert "同时满足才卖出" in payload["clarification"]


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
    assert payload["clarification"] is not None
    assert "不会替你补默认触发规则" in payload["clarification"]


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
) -> None:
    headers = {"Idempotency-Key": "draft-mobile-001"}
    first = client.post("/api/v1/strategy-drafts", json=ready_request, headers=headers)
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
    assert "完整" in payload["assistant_message"]
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
        query = dialogue_client.post(
            (
                f"/api/v1/strategy-drafts/{created['draft_id']}"
                f"/revisions/{created['revision']}/clarification-answers"
            ),
            json={"answer": "东方财富昨天的换手率是多少"},
        )
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
    assert provider.finance_calls == [("东方财富昨天的换手率是多少", "昨天的换手率")]
    assert provider.screen_calls == []
    assert "东方财富换手率" in payload["assistant_message"]
    assert "换手率 2.37%" in payload["assistant_message"]
    assert "2026-09-02" not in payload["assistant_message"]
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
        response = dialogue_client.post(
            (
                f"/api/v1/strategy-drafts/{created['draft_id']}"
                f"/revisions/{created['revision']}/clarification-answers"
            ),
            json={"answer": "昨天换手率多少"},
        )

    assert response.status_code == 200
    assert provider.finance_calls == [("300059.SZ；昨天换手率多少", "昨天换手率")]
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
    assert "股票名称或 6 位代码" in payload["assistant_message"]
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
    assert payload["data"]["kind"] == "screened_finance"
    assert payload["data"]["historical_backtest_eligible"] is False
    assert provider.screen_finance_calls == [
        ("上涨的股票", "A股", "近10年的归母净利润")
    ]
    assert provider.screen_calls == []
    assert provider.finance_calls == []


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (MxSaasProviderAuthError("bad credential"), "不是你的问题"),
        (MxSaasProviderUnavailableError("temporary"), "暂时连不上"),
        (None, "没有查到匹配数据"),
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
    assert "data" not in payload if error is not None else payload["data"]["kind"] == "finance"


def test_clarification_progress_to_missing_stock_asks_once_without_repeated_only_missing(
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
    assert payload["assistant_message"].count("请告诉我想回测哪一只 A 股") == 1
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
    assert "新规则" in payload["assistant_message"]
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
    assert "新规则" in payload["assistant_message"]
    assert payload["draft"]["status"] == "ready"
    assert payload["draft"]["strategy_hash"] == fresh["strategy_hash"]
    assert payload["draft"]["strategy"]["entry"]["indicator_id"] == "technical.rsi"
    assert payload["draft"]["strategy"]["exit"]["children"][0]["indicator_id"] == ("technical.rsi")
    assert "technical.macd" not in str(payload["draft"]["strategy"])


def test_named_partial_strategy_answer_moves_past_the_old_instrument_question() -> None:
    generator = HybridCandidateGenerator(
        deterministic=RuleBasedCandidateGenerator(),
        bounded_fallback=_UnexpectedFallback(),
        instrument_name_resolver=lambda name: {"同花顺": "300033.SZ"}[name],
    )
    compiler = StrategyCompiler(
        generator=generator,
        catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals",
        release_version="2026.09.01",
    )
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


def test_dialogue_provider_is_used_only_after_deterministic_recompile_keeps_same_diagnostic() -> (
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
    assert unresolved["assistant_message"].startswith("我知道你是在开玩笑，这句先不作为策略条件")
    assert "什么时候卖" in unresolved["assistant_message"]
    assert unresolved["assistant_message"].count("只差") <= 1
    assert "接下来只差：我只差" not in unresolved["assistant_message"]
    assert "。。" not in unresolved["assistant_message"]
    assert [item["id"] for item in unresolved["suggestions"]] == [
        item.id for item in reversed(router.requests[0].options)
    ]


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
    assert len(router.requests) == 1
    assert router.requests[0].answer == "你好，我是你爸"
    assert router.requests[0].prior_utterance == ""
    assert router.requests[0].options == ()
    assert payload["status"] == "needs_clarification"
    assert payload["diagnostic_code"] == "strategy_rule_incomplete"
    assert payload["clarification"].startswith("我知道你是在开玩笑")
    assert payload["clarification"] == "我知道你是在开玩笑，这句先不作为策略条件。"
    assert router.requests[0].question == ""
    assert "下面选" not in payload["clarification"]


def test_first_person_strategy_sentence_is_not_intercepted_as_small_talk() -> None:
    router = _RecordingDialogueRouter(explode=True)
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
    assert response.json()["status"] == "ready"
    assert router.requests == []


def test_valid_clarification_supplement_never_calls_dialogue_provider() -> None:
    router = _RecordingDialogueRouter(explode=True)
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
    assert response.json()["draft"]["status"] == "ready"
    assert router.requests == []


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


@pytest.mark.parametrize("answer", ["用它", "我自己选股票"])
def test_missing_stock_offers_screened_candidate_and_respects_choice(answer: str) -> None:
    provider = _RecordingLiveData()
    with TestClient(create_app(live_market_data=provider)) as local:
        created = local.post("/api/v1/strategy-drafts", json={
            "utterance": "MACD金叉买入，MACD死叉卖出", "as_of_date": "2026-09-04",
        }).json()
        assert created["instrument_suggestion"]["symbol"] == "300059.SZ"
        assert created["strategy"] is None
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
        else:
            assert draft["status"] == "needs_clarification"
            assert draft["instrument_suggestion"] is None
            assert draft["strategy"] is None


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
