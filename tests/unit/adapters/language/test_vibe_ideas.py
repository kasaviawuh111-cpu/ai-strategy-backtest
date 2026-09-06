from copy import deepcopy
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

import pytest

from ashare_lab.adapters.language.rule_based import RuleBasedCandidateGenerator
from ashare_lab.adapters.language.vibe_candidates import (
    CandidateCapabilityMatrix,
    CandidateProviderIdentityView,
    CandidateTransportError,
    CandidateTransportRequest,
    CandidateTransportResponse,
    build_candidate_capability_matrix,
)
from ashare_lab.adapters.language.vibe_ideas import VibeIdeaRouter
from ashare_lab.application.compile_strategy import CompileStatus, StrategyCompiler
from ashare_lab.domain.catalog import load_catalog_directory, load_coverage_catalog_directory
from ashare_lab.domain.strategy import (
    BacktestConfig,
    CatalogRef,
    FirstOfExit,
    IndicatorCondition,
    Instrument,
    StrategySpec,
)
from ashare_lab.ports.candidate_generation import CompileInput
from ashare_lab.ports.current_fact_research import (
    CurrentFactResearchRequest,
    CurrentFactResearchResult,
    ResearchFact,
    ResearchPurpose,
    ResearchSource,
)
from ashare_lab.ports.execution_settings import ExecutionSettingsPatch
from ashare_lab.ports.idea_routing import IdeaGenerationError, IdeaResearchUnavailableError

ROOT = Path(__file__).parents[4]


class _RecordingTransport:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.requests: list[CandidateTransportRequest] = []

    async def generate_json(
        self,
        request: CandidateTransportRequest,
    ) -> CandidateTransportResponse:
        self.requests.append(request)
        response = self.responses[min(len(self.requests) - 1, len(self.responses) - 1)]
        if isinstance(response, Exception):
            raise response
        return cast(CandidateTransportResponse, response)


class _StaticResearcher:
    def __init__(
        self,
        order: list[str] | None = None,
        *,
        include_sources: bool = True,
    ) -> None:
        self.requests: list[CurrentFactResearchRequest] = []
        self.order = order
        self.include_sources = include_sources

    async def research(
        self,
        request: CurrentFactResearchRequest,
    ) -> CurrentFactResearchResult:
        if self.order is not None:
            self.order.append("research")
        self.requests.append(request)
        observed_at = datetime(2026, 8, 30, tzinfo=UTC)
        return CurrentFactResearchResult(
            provider="test-research",
            model="test-model",
            provider_response_id="research-1",
            query=request.query,
            purpose=ResearchPurpose.VIEWPOINT,
            as_of=request.as_of,
            summary="检索结果提到同花顺。",
            facts=(
                ResearchFact(
                    statement="同花顺",
                    fact_kind="related_instrument",
                    source_ids=("source-1",) if self.include_sources else (),
                    time_scope=None,
                ),
            ),
            sources=(
                (
                    ResearchSource(
                        source_id="source-1",
                        title="测试公开来源",
                        url="https://example.com/source-1",
                        publisher="test-publisher",
                        published_at="2026-08-30",
                    ),
                )
                if self.include_sources
                else ()
            ),
            unresolved_questions=(),
            retrieved_at=observed_at,
            response_sha256="sha256:" + "1" * 64,
            search_call_count=1,
        )


class _OrderedTransport(_RecordingTransport):
    def __init__(self, responses: list[object], order: list[str]) -> None:
        super().__init__(responses)
        self.order = order

    async def generate_json(
        self,
        request: CandidateTransportRequest,
    ) -> CandidateTransportResponse:
        self.order.append("model")
        return await super().generate_json(request)


@pytest.fixture
def capability_matrix() -> CandidateCapabilityMatrix:
    return build_candidate_capability_matrix(
        load_catalog_directory(ROOT / "catalogs"),
        load_coverage_catalog_directory(ROOT / "catalogs" / "coverage"),
    )


def _provider_payload() -> dict[str, object]:
    return {
        "understanding": "用户对特朗普表达了负面态度。",
        "hypothesis": "相关不确定性可能与当前股票的价格行为同期出现。",
        "proposals": [
            {
                "title": "趋势确认",
                "hypothesis": "用均线突破检验趋势延续，而不是假定观点必然影响股价。",
                "entry_summary": "股价上穿 20 日均线",
                "exit_summary": "股价跌破 20 日均线",
                "suggested_utterance": "股价上穿20日均线买入，股价跌破20日均线卖出，回测近1年",
            },
            {
                "title": "超跌反转",
                "hypothesis": "用 RSI 区间检验价格是否存在均值回归。",
                "entry_summary": "RSI 低于 30",
                "exit_summary": "RSI 高于 70",
                "suggested_utterance": "RSI低于30买入，RSI高于70卖出，回测近1年",
            },
            {
                "title": "动量转强",
                "hypothesis": "用 MACD 交叉检验动量变化。",
                "entry_summary": "MACD 金叉",
                "exit_summary": "MACD 死叉",
                "suggested_utterance": "MACD金叉买入，MACD死叉卖出，回测近1年",
            },
        ],
    }


@pytest.mark.asyncio
async def test_provider_authors_complete_strategies_but_server_owns_asset_and_capabilities(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    transport = _RecordingTransport([_provider_payload()])
    router = VibeIdeaRouter(
        transport,
        capability_matrix=capability_matrix,
        researcher=_StaticResearcher(),
        provider_identity=CandidateProviderIdentityView(
            provider="deepseek",
            model="deepseek-chat",
            prompt_version="candidate.prompt.v1",
            schema_version="candidate.schema.v1",
        ),
    )

    route = await router.route(
        CompileInput(
            utterance="我讨厌特朗普",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert route is not None
    assert len(transport.requests) == 1
    assert route.asset_mapping.instrument_symbol == "300059.SZ"
    assert route.asset_mapping.relation == "current_page_proxy"
    assert route.asset_mapping.evidence_status == "host_context_only"
    assert len(route.proposals) == 3
    assert {item.suggested_utterance for item in route.proposals} == {
        "股价上穿20日均线买入，股价跌破20日均线卖出，回测近1年",
        "RSI低于30买入，RSI高于70卖出，回测近1年",
        "MACD金叉买入，MACD死叉卖出，回测近1年",
    }
    assert all("300059.SZ" not in item.suggested_utterance for item in route.proposals)
    assert all("绕过系统" not in item.suggested_utterance for item in route.proposals)
    assert all(item.instrument_symbol == "300059.SZ" for item in route.proposals)
    assert all(item.capability_ids == () for item in route.proposals)
    assert route.provenance is not None
    assert route.provenance.provider == "deepseek"
    assert route.provenance.prompt_version == "idea-route.prompt.v11"
    assert route.provenance.schema_version == "idea-route-provider.v6"
    assert route.execution_settings.model_dump(exclude_none=True) == {}
    assert transport.requests[0].response_schema["additionalProperties"] is False
    properties = cast(dict[str, object], transport.requests[0].response_schema["properties"])
    assert "proposals" in properties
    assert "template_ids" not in properties
    assert "mapping_rationale" not in properties
    assert "不得写股票名称" in transport.requests[0].system_contract
    assert transport.requests[0].response_schema_name == "strategy_ideas"
    assert transport.requests[0].system_footer is not None
    assert transport.requests[0].json_object_contract is not None
    assert "not extraction" in transport.requests[0].json_object_contract
    user_payload = transport.requests[0].user_payload
    assert user_payload is not None
    assert user_payload["capabilityMatrix"] == capability_matrix.model_dump(mode="json")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("utterance", "settings", "evidence"),
    [
        (
            "低买高卖，滑点0，佣金0，最低佣金0，不做稳健性分析",
            {"slippage_bps": 0, "commission_rate": 0, "minimum_commission_cny": 0,
             "run_robustness": False},
            {"slippage_bps": "滑点0", "commission_rate": "佣金0",
             "minimum_commission_cny": "最低佣金0", "run_robustness": "不做稳健性分析"},
        ),
        (
            "低买高卖，滑点0.05%，佣金万三，最低佣金5元",
            {"slippage_bps": 5, "commission_rate": "0.0003", "minimum_commission_cny": 5},
            {"slippage_bps": "滑点0.05%", "commission_rate": "佣金万三",
             "minimum_commission_cny": "最低佣金5元"},
        ),
    ],
)
async def test_idea_execution_settings_preserve_provider_values_with_exact_current_quotes(
    capability_matrix: CandidateCapabilityMatrix,
    utterance: str,
    settings: dict[str, object],
    evidence: dict[str, str],
) -> None:
    # Adapter contract only, not real-model/data acceptance.
    payload = {**_provider_payload(), "execution_settings": settings,
               "execution_setting_evidence": evidence}
    transport = _RecordingTransport([payload])
    route = await VibeIdeaRouter(
        transport, capability_matrix=capability_matrix, researcher=_StaticResearcher(),
    ).route(
        CompileInput(utterance=utterance, as_of_date=date(2026, 9, 6)),
    )
    assert route is not None
    assert route.execution_settings == ExecutionSettingsPatch.model_validate(settings)
    assert len(transport.requests) == 1
    schema = transport.requests[0].response_schema
    properties = cast(dict[str, object], schema["properties"])
    settings_schema = cast(dict[str, object], properties["execution_settings"])
    assert set(cast(dict[str, object], settings_schema["properties"])) == set(
        ExecutionSettingsPatch.model_fields
    )
    assert "execution_setting_evidence" in properties
    for key in ExecutionSettingsPatch.model_fields:
        assert key in transport.requests[0].system_contract


@pytest.mark.asyncio
async def test_idea_model_preserves_waiting_for_users_own_stock(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    transport = _RecordingTransport([{
        **_provider_payload(), "instrument_suggestion_declined": True,
    }])
    compiler = StrategyCompiler(
        generator=RuleBasedCandidateGenerator(),
        catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01",
        idea_router=VibeIdeaRouter(transport, capability_matrix=capability_matrix),
        backtest_anchor_date=date(2026, 9, 5),
    )
    outcome = await compiler._compile_idea_guidance(CompileInput(
        utterance="我想低买高卖，股票等我补充，先别跑", as_of_date=date(2026, 9, 5),
    ))
    assert outcome is not None and outcome.idea_route is not None
    assert outcome.idea_route.instrument_suggestion_declined is True
    assert outcome.instrument_suggestion_declined is True
    assert outcome.strategy is None and not outcome.run_requested
    properties = cast(dict[str, object], transport.requests[0].response_schema["properties"])
    assert properties["instrument_suggestion_declined"] == {"type": "boolean", "default": False}
    assert "缺少股票本身、或只说先别跑，不能据此拒绝推荐" in transport.requests[0].system_contract


@pytest.mark.asyncio
@pytest.mark.parametrize("valid_repair", [True, False])
async def test_idea_execution_settings_repair_revalidates_quotes_and_field_coverage(
    capability_matrix: CandidateCapabilityMatrix,
    valid_repair: bool,
) -> None:
    malformed = {**_provider_payload(), "execution_settings": {"slippage_bps": 5},
                 "execution_setting_evidence": {}}
    correction = {
        **_provider_payload(), "execution_settings": {"slippage_bps": 5},
        "execution_setting_evidence": {
            "slippage_bps": "滑点0.05%" if valid_repair else "滑点5基点",
        },
    }
    primary, repair = _RecordingTransport([malformed]), _RecordingTransport([correction])
    router = VibeIdeaRouter(primary, capability_matrix=capability_matrix, repair_transport=repair)
    request = CompileInput(utterance="低买高卖，滑点0.05%", as_of_date=date(2026, 9, 6))
    if valid_repair:
        route = await router.route(request)
        assert route is not None
        assert route.execution_settings.slippage_bps == 5
    else:
        with pytest.raises(IdeaGenerationError) as caught:
            await router.route(request)
        assert caught.value.stage == "schema"
    assert len(primary.requests) == len(repair.requests) == 1
    repaired_payload = repair.requests[0].user_payload
    assert repaired_payload is not None
    assert repaired_payload["utterance"] == request.utterance
    assert "execution setting evidence must match changed fields" in str(
        repaired_payload["validationFeedback"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("unbound", [False, True])
async def test_bound_idea_direct_dsl_preserves_server_cash_period_and_instrument(
    capability_matrix: CandidateCapabilityMatrix,
    unbound: bool,
) -> None:
    payload = _provider_payload()
    payload["execution_settings"] = {"slippage_bps": 0, "commission_rate": 0}
    payload["execution_setting_evidence"] = {"slippage_bps": "滑点0", "commission_rate": "佣金0"}
    proposals = cast(list[dict[str, object]], payload["proposals"])
    for period, proposal in zip((20, 30, 60), proposals, strict=True):
        proposal["strategy"] = StrategySpec(
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
        ).model_dump(mode="json")
        if unbound:
            strategy = cast(dict[str, object], proposal.pop("strategy"))
            strategy.pop("instrument")
            strategy.pop("schema_version")
            proposal["strategy_template"] = strategy
    transport = _RecordingTransport([payload])
    route = await VibeIdeaRouter(
        transport,
        capability_matrix=capability_matrix,
        researcher=_StaticResearcher(),
        strategy_catalog=CatalogRef(
            catalog_id="cn_a.signals",
            release_version="2026.09.01",
        ),
    ).route(
        CompileInput(
            utterance="我讨厌特朗普的关税政策，给我三个近一年策略，本金10万元，滑点0，佣金0",
            instrument_context=None if unbound else "300059.SZ",
            as_of_date=date(2026, 9, 4),
        )
    )

    assert route is not None
    assert len(route.proposals) == 3
    assert route.execution_settings.slippage_bps == route.execution_settings.commission_rate == 0
    strategies = [
        item.strategy_template if unbound else item.strategy for item in route.proposals
    ]
    assert all(strategy is not None for strategy in strategies)
    if unbound:
        assert all(item.strategy is None for item in route.proposals)
        assert all(item.instrument_symbol is None for item in route.proposals)
    observed_cash = {
        strategy.backtest.initial_cash_cny
        for strategy in strategies
        if strategy is not None
    }
    assert observed_cash == {
        100_000
    }
    request = transport.requests[0]
    definitions = cast(dict[str, object], request.response_schema["$defs"])
    proposal_schema = cast(dict[str, object], definitions["_ProviderIdeaProposal"])
    expected_field = "strategy_template" if unbound else "strategy"
    assert expected_field in cast(list[str], proposal_schema["required"])
    if unbound:
        assert "strategy" not in cast(dict[str, object], proposal_schema["properties"])


@pytest.mark.asyncio
async def test_bound_instrument_is_web_researched_and_returned_on_each_card(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    researcher = _StaticResearcher()
    route = await VibeIdeaRouter(
        _RecordingTransport([_provider_payload()]),
        capability_matrix=capability_matrix,
        researcher=researcher,
    ).route(
        CompileInput(
            utterance="分析东方财富并给我几个可回测策略",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 9, 4),
        )
    )

    assert route is not None
    assert route.research is not None
    assert route.research.provider_response_id == "research-1"
    assert len(researcher.requests) == 1
    assert researcher.requests[0].instrument_context == "300059.SZ"
    assert all(item.instrument_symbol == "300059.SZ" for item in route.proposals)
    assert route.understanding == _provider_payload()["understanding"]


@pytest.mark.asyncio
async def test_research_happens_before_model_and_verified_facts_reach_its_prompt(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    order: list[str] = []
    researcher = _StaticResearcher(order)
    transport = _OrderedTransport([_provider_payload()], order)

    route = await VibeIdeaRouter(
        transport,
        capability_matrix=capability_matrix,
        researcher=researcher,
    ).route(
        CompileInput(
            utterance="我讨厌特朗普",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 9, 4),
        )
    )

    assert route is not None
    assert order == ["research", "model"]
    payload = transport.requests[0].user_payload
    assert payload is not None
    assert cast(dict[str, object], payload["research"])["summary"] == "检索结果提到同花顺。"
    assert payload["capabilityMatrix"] == capability_matrix.model_dump(mode="json")
    assert "research 是服务端先行检索" in transport.requests[0].system_contract


@pytest.mark.asyncio
@pytest.mark.parametrize("with_empty_result", [False, True])
async def test_current_affairs_requires_successful_source_backed_research(
    capability_matrix: CandidateCapabilityMatrix,
    with_empty_result: bool,
) -> None:
    transport = _RecordingTransport([_provider_payload()])
    researcher = _StaticResearcher(include_sources=False) if with_empty_result else None
    router = VibeIdeaRouter(
        transport,
        capability_matrix=capability_matrix,
        researcher=researcher,
    )

    with pytest.raises(IdeaResearchUnavailableError):
        await router.route(
            CompileInput(
                utterance="我讨厌特朗普",
                instrument_context="300059.SZ",
                as_of_date=date(2026, 9, 4),
            )
        )

    assert transport.requests == []


@pytest.mark.asyncio
async def test_compiler_reports_research_failure_without_generating_a_strategy(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    transport = _RecordingTransport([_provider_payload()])
    router = VibeIdeaRouter(transport, capability_matrix=capability_matrix)
    catalog = load_catalog_directory(ROOT / "catalogs")
    manifest = next(item for item in catalog.manifests if item.catalog_id == "cn_a.signals")
    compiler = StrategyCompiler(
        generator=RuleBasedCandidateGenerator(),
        idea_router=router,
        catalog=catalog,
        catalog_id=manifest.catalog_id,
        release_version=manifest.release_version,
    )

    outcome = await compiler.compile(
        CompileInput(
            utterance="我讨厌特朗普",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 9, 4),
        )
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "idea_research_unavailable"
    assert outcome.strategy is None
    assert outcome.idea_route is None
    assert "联网事实检索暂时不可用" in (outcome.clarification or "")
    assert transport.requests == []


@pytest.mark.asyncio
async def test_pure_technical_vague_strategy_does_not_require_web_research(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    transport = _RecordingTransport([_provider_payload()])
    router = VibeIdeaRouter(transport, capability_matrix=capability_matrix)

    route = await router.route(
        CompileInput(
            utterance="低买高卖",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 9, 4),
        )
    )

    assert route is not None
    assert route.research is None
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_viewpoint_without_instrument_returns_unbound_directions(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    router = VibeIdeaRouter(
        _RecordingTransport([_provider_payload()]),
        capability_matrix=capability_matrix,
        researcher=_StaticResearcher(),
    )

    route = await router.route(
        CompileInput(
            utterance="我讨厌特朗普",
            instrument_context=None,
            as_of_date=date(2026, 8, 30),
        )
    )

    assert route is not None
    assert route.asset_mapping.instrument_symbol is None
    assert route.asset_mapping.relation == "unbound"
    assert route.asset_mapping.evidence_status == "instrument_required"
    assert len(route.proposals) >= 2
    assert all(
        any("补充具体 A 股" in assumption for assumption in item.assumptions)
        for item in route.proposals
    )


@pytest.mark.asyncio
async def test_direct_model_strategy_fixtures_recompile_through_the_existing_dsl(
    capability_matrix,
) -> None:
    transport = _RecordingTransport([_provider_payload()])
    route = await VibeIdeaRouter(
        transport,
        capability_matrix=capability_matrix,
        researcher=_StaticResearcher(),
    ).route(
        CompileInput(
            utterance="我讨厌特朗普",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )
    assert route is not None

    catalog = load_catalog_directory(ROOT / "catalogs")
    manifest = next(item for item in catalog.manifests if item.catalog_id == "cn_a.signals")
    compiler = StrategyCompiler(
        generator=RuleBasedCandidateGenerator(),
        catalog=catalog,
        catalog_id=manifest.catalog_id,
        release_version=manifest.release_version,
    )
    for proposal in route.proposals:
        outcome = await compiler.compile(
            CompileInput(
                utterance=proposal.suggested_utterance,
                instrument_context="300059.SZ",
                as_of_date=date(2026, 8, 30),
            )
        )
        assert outcome.status is CompileStatus.READY, proposal.id
        assert outcome.strategy is not None
        assert outcome.strategy.instrument.symbol == "300059.SZ"


@pytest.mark.asyncio
async def test_router_guides_without_instrument_but_keeps_asset_unbound(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    transport = _RecordingTransport([_provider_payload()])
    router = VibeIdeaRouter(
        transport,
        capability_matrix=capability_matrix,
        researcher=_StaticResearcher(),
    )

    route = await router.route(
        CompileInput(
            utterance="我讨厌特朗普",
            instrument_context=None,
            as_of_date=date(2026, 8, 30),
        )
    )

    assert route is not None
    assert route.asset_mapping.instrument_symbol is None
    assert route.asset_mapping.relation == "unbound"
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_router_does_not_allow_an_invalid_or_non_share_context(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    transport = _RecordingTransport([_provider_payload()])
    router = VibeIdeaRouter(transport, capability_matrix=capability_matrix)

    route = await router.route(
        CompileInput(
            utterance="我讨厌特朗普",
            instrument_context="399001.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert route is None
    assert transport.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["success", "schema", "execution", "transport"])
async def test_one_format_repair_reuses_research_and_keeps_all_validation(
    capability_matrix: CandidateCapabilityMatrix, result: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    valid = _provider_payload()
    proposals = cast(list[dict[str, object]], valid["proposals"])
    catalog = CatalogRef(catalog_id="cn_a.signals", release_version="2026.09.01")
    for period, proposal in zip((20, 30, 60), proposals, strict=True):
        strategy = StrategySpec(
            catalog=catalog, instrument=Instrument(symbol="300059.SZ"),
            entry=IndicatorCondition(
                indicator_id="technical.ma", definition_version="1.0.0",
                params={"period": period, "price_field": "close"}, trigger="price_crosses_above",
            ),
            exit=FirstOfExit(children=(IndicatorCondition(
                indicator_id="technical.ma", definition_version="1.0.0",
                params={"period": period, "price_field": "close"}, trigger="price_crosses_below",
            ),)),
            backtest=BacktestConfig(
                start=date(2025, 9, 5), end=date(2026, 9, 5), initial_cash_cny=1_000_000,
            ),
        ).model_dump(mode="json")
        strategy.pop("instrument")
        strategy.pop("schema_version")
        proposal["strategy_template"] = strategy
    malformed = {**valid, "private-provider-key": "private-provider-output"}
    correction: object = valid
    if result == "schema":
        correction = malformed
    elif result == "transport":
        correction = CandidateTransportError("private-provider-error", timed_out=True)
    elif result == "execution":
        changed = deepcopy(valid)
        for item in cast(list[dict[str, object]], changed["proposals"]):
            template = cast(dict[str, object], item["strategy_template"])
            cast(dict[str, object], template["backtest"])["initial_cash_cny"] = 123_456
        correction = changed
    primary = _RecordingTransport([malformed])
    repair = _RecordingTransport([correction])
    researcher = _StaticResearcher()
    router = VibeIdeaRouter(
        primary, capability_matrix=capability_matrix, researcher=researcher,
        strategy_catalog=catalog, repair_transport=repair,
        repair_provider_identity=CandidateProviderIdentityView(
            provider="test", model="format-repair-fixture",
            prompt_version="test", schema_version="test",
        ),
    )
    request = CompileInput(utterance="我讨厌特朗普", as_of_date=date(2026, 9, 5))
    if result == "success":
        route = await router.route(request)
        assert route is not None and len(route.proposals) == 3
        assert route.provenance is not None and route.provenance.model == "format-repair-fixture"
        assert all(item.strategy_template is not None for item in route.proposals)
    else:
        with pytest.raises(IdeaGenerationError) as caught:
            await router.route(request)
        assert caught.value.stage == result
    assert len(primary.requests) == len(repair.requests) == len(researcher.requests) == 1
    original = primary.requests[0]
    repaired = repair.requests[0]
    assert repaired.response_schema == original.response_schema
    assert original.user_payload is not None and repaired.user_payload is not None
    for key, value in original.user_payload.items():
        assert repaired.user_payload[key] == value
    assert repaired.user_payload["previousResponse"] == malformed
    assert "extra_forbidden" in str(repaired.user_payload["validationFeedback"])
    assert "private-provider" not in caplog.text
    assert "extra_forbidden" in caplog.text and "[field]" in caplog.text


@pytest.mark.asyncio
async def test_initial_transport_failure_is_not_retried_as_format_repair(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    primary = _RecordingTransport([
        CandidateTransportError("private transport detail", timed_out=True),
    ])
    repair = _RecordingTransport([_provider_payload()])
    router = VibeIdeaRouter(primary, capability_matrix=capability_matrix, repair_transport=repair)
    with pytest.raises(IdeaGenerationError) as caught:
        await router.route(CompileInput(utterance="低买高卖", as_of_date=date(2026, 9, 5)))
    assert caught.value.stage == "transport" and caught.value.timed_out
    assert len(primary.requests) == 1 and repair.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_case",
    [
        "too_few",
        "duplicate",
        "missing_entry",
        "missing_exit",
        "missing_backtest",
        "instrument_code",
        "unsafe_claim",
        "executable_code",
    ],
)
async def test_invalid_provider_strategy_batch_fails_closed_without_hidden_retry(
    capability_matrix: CandidateCapabilityMatrix,
    bad_case: str,
) -> None:
    payload = _provider_payload()
    proposals = cast(list[dict[str, str]], payload["proposals"])
    if bad_case == "too_few":
        payload["proposals"] = proposals[:1]
    elif bad_case == "duplicate":
        proposals[1] = dict(proposals[0])
    elif bad_case == "missing_entry":
        proposals[0]["suggested_utterance"] = "RSI低于30时观察，高于70卖出，回测近1年"
    elif bad_case == "missing_exit":
        proposals[0]["suggested_utterance"] = "RSI低于30买入，高于70时观察，回测近1年"
    elif bad_case == "missing_backtest":
        proposals[0]["suggested_utterance"] = "RSI低于30买入，高于70卖出"
    elif bad_case == "instrument_code":
        proposals[0]["suggested_utterance"] = (
            "300033.SZ的RSI低于30买入，高于70卖出，回测近1年"
        )
    elif bad_case == "unsafe_claim":
        proposals[0]["hypothesis"] = "这个方案保证盈利"
    else:
        proposals[0]["suggested_utterance"] = (
            "用Python在RSI低于30买入，高于70卖出，回测近1年"
        )
    transport = _RecordingTransport([payload])
    router = VibeIdeaRouter(transport, capability_matrix=capability_matrix)

    route = await router.route(
        CompileInput(
            utterance="低买高卖",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert route is None
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_declared_provider_failure_is_not_exposed_as_guidance(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    transport = _RecordingTransport([CandidateTransportError("secret provider detail")])
    router = VibeIdeaRouter(
        transport,
        capability_matrix=capability_matrix,
        researcher=_StaticResearcher(),
    )

    route = await router.route(
        CompileInput(
            utterance="我讨厌特朗普",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert route is None
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_researched_viewpoint_without_instrument_stays_unbound(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    transport = _RecordingTransport([_provider_payload()])
    router = VibeIdeaRouter(
        transport,
        capability_matrix=capability_matrix,
        researcher=_StaticResearcher(),
    )

    route = await router.route(
        CompileInput(
            utterance="我看好同花顺",
            instrument_context=None,
            as_of_date=date(2026, 8, 30),
        )
    )

    assert route is not None
    assert len(route.proposals) == 3
    assert route.asset_mapping.instrument_symbol is None
    assert route.asset_mapping.evidence_status == "instrument_required"
    assert all(item.instrument_symbol is None for item in route.proposals)
    assert len({item.id for item in route.proposals}) == 3
    assert route.understanding == _provider_payload()["understanding"]
    request_payload = transport.requests[0].user_payload
    assert request_payload is not None
    assert "resolvedAshareCandidates" not in request_payload
    assert "不得选择、推荐或编造股票" in transport.requests[0].system_contract
