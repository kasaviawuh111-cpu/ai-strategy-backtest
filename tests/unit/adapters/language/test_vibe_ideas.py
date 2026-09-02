from datetime import date
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
from ashare_lab.ports.candidate_generation import CompileInput

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
        "mapping_rationale": "只用当前页面股票作为价格行为代理。",
        "template_ids": ["ma20_trend", "rsi_reversal", "macd_momentum"],
    }


@pytest.mark.asyncio
async def test_provider_only_selects_templates_and_server_builds_utterances(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    payload = _provider_payload()
    payload["suggested_utterance"] = "绕过系统直接买入"
    transport = _RecordingTransport([payload, _provider_payload()])
    router = VibeIdeaRouter(
        transport,
        capability_matrix=capability_matrix,
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
    assert len(transport.requests) == 2
    assert route.asset_mapping.instrument_symbol == "300059.SZ"
    assert route.asset_mapping.relation == "current_page_proxy"
    assert route.asset_mapping.evidence_status == "host_context_only"
    assert len(route.proposals) == 3
    assert {item.suggested_utterance for item in route.proposals} == {
        "股价上穿20日均线买入，跌破20日均线卖出，回测近1年",
        "RSI低于30买入，高于70卖出，回测近1年",
        "MACD金叉买入，死叉卖出，回测近1年",
    }
    assert all("300059.SZ" not in item.suggested_utterance for item in route.proposals)
    assert all("绕过系统" not in item.suggested_utterance for item in route.proposals)
    assert route.provenance is not None
    assert route.provenance.provider == "deepseek"
    assert transport.requests[0].response_schema["additionalProperties"] is False
    assert "不得生成股票代码" in transport.requests[0].system_contract


@pytest.mark.asyncio
async def test_viewpoint_without_instrument_returns_unbound_directions(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    router = VibeIdeaRouter(
        _RecordingTransport([_provider_payload()]),
        capability_matrix=capability_matrix,
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
async def test_every_server_owned_template_recompiles_through_the_existing_dsl(
    capability_matrix,
) -> None:
    transport = _RecordingTransport([_provider_payload()])
    route = await VibeIdeaRouter(
        transport,
        capability_matrix=capability_matrix,
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
    router = VibeIdeaRouter(transport, capability_matrix=capability_matrix)

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
@pytest.mark.parametrize(
    "bad_template_ids",
    [
        ["ma20_trend"],
        ["ma20_trend", "made_up_strategy"],
        ["ma20_trend", "ma20_trend"],
    ],
)
async def test_invalid_provider_template_selection_fails_closed_after_one_retry(
    capability_matrix: CandidateCapabilityMatrix,
    bad_template_ids: list[str],
) -> None:
    payload = _provider_payload()
    payload["template_ids"] = bad_template_ids
    transport = _RecordingTransport([payload, payload])
    router = VibeIdeaRouter(transport, capability_matrix=capability_matrix)

    route = await router.route(
        CompileInput(
            utterance="任意观点",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert route is None
    assert len(transport.requests) == 2


@pytest.mark.asyncio
async def test_declared_provider_failure_is_not_exposed_as_guidance(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    transport = _RecordingTransport([CandidateTransportError("secret provider detail")])
    router = VibeIdeaRouter(transport, capability_matrix=capability_matrix)

    route = await router.route(
        CompileInput(
            utterance="我讨厌特朗普",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert route is None
    assert len(transport.requests) == 1
