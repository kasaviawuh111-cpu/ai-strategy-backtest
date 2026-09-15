"""Controlled orchestration fixtures, excluded from real-input acceptance counts."""

import logging
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from ashare_lab.adapters.language import RuleBasedCandidateGenerator
from ashare_lab.adapters.market_data.mx_saas import MxSaasProviderNoDataError
from ashare_lab.api.container import ApiContainer
from ashare_lab.api.routes.strategy_drafts import (
    _offer_missing_instrument,  # pyright: ignore[reportPrivateUsage]
    _pair_stock_strategies_with_data,  # pyright: ignore[reportPrivateUsage]
    _to_response,  # pyright: ignore[reportPrivateUsage]
)
from ashare_lab.api.store import StoredDraftRevision
from ashare_lab.application.compile_strategy import CompileOutcome, CompileStatus, StrategyCompiler
from ashare_lab.application.dialogue_state import DialogueState, VerifiedInstrumentMemory
from ashare_lab.application.dialogue_turn import DialogueTurnOrchestrator
from ashare_lab.domain.catalog import load_catalog_directory
from ashare_lab.domain.strategy import IndicatorCondition
from ashare_lab.ports.candidate_generation import (
    CandidateAst,
    CandidateGroundingEvidence,
    CompileInput,
)
from ashare_lab.ports.clarification_dialogue import (
    ClarificationDialogueAssessment,
    ClarificationDialogueRequest,
)
from ashare_lab.ports.idea_routing import (
    IdeaAssetMapping,
    IdeaProposal,
    IdeaRoute,
    UnboundIdeaStrategy,
)
from ashare_lab.ports.live_market_data import (
    LiveFinanceDataResult,
    LiveMarketDataProvenance,
    LiveMarketDataResult,
)
from ashare_lab.ports.strategy_advice import (
    StockRecommendation,
    StockStrategyDataRequest,
    StockStrategyPair,
    StockStrategyPairing,
)


@pytest.mark.asyncio
async def test_empty_research_route_keeps_answer_without_stock_pairing() -> None:
    outcome = CompileOutcome(
        status=CompileStatus.NEEDS_CLARIFICATION,
        diagnostic_code="capability_research_fallback",
        clarification="已查到部分资料，但缺少连续板块数据，暂不能执行原规则。",
        idea_route=replace(_route(), proposals=()),
    )
    preserved, stock = await _offer_missing_instrument(
        outcome=outcome,
        compile_input=CompileInput(utterance="金融科技板块强势股策略", as_of_date=date(2026, 9, 8)),
        state=None, container=cast(ApiContainer, SimpleNamespace()),
    )
    assert preserved is outcome
    assert stock is None


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["new_strategy", "vague_strategy", "casual"])
async def test_missing_stock_provider_keeps_vague_templates_without_auto_execution(intent: str) -> None:
    outcome = CompileOutcome(
        status=CompileStatus.NEEDS_CLARIFICATION,
        diagnostic_code="idea_guidance_required",
        clarification="先给策略方向，再确认股票。",
        idea_route=_route(),
    )
    preserved, stock = await _offer_missing_instrument(
        outcome=outcome,
        compile_input=CompileInput(
            utterance="定期投点钱进去，省得总盯盘",
            as_of_date=date(2026, 9, 8), semantic_intent=intent,
        ),
        state=None,
        # Any attempted screening would fail because no provider is present.
        container=cast(ApiContainer, SimpleNamespace(live_market_data=None)),
    )
    assert preserved.idea_route is outcome.idea_route
    assert not preserved.run_requested
    assert stock is None


@pytest.mark.asyncio
@pytest.mark.parametrize("with_template", [True, False])
@pytest.mark.parametrize("scoped_retry", [True, False])
async def test_complete_rules_are_preserved_across_ranked_stock_choices(
    with_template: bool, scoped_retry: bool,
) -> None:
    request = CompileInput(
        utterance=("收盘价上穿20日均线买入，收盘价下穿20日均线卖出，回测近一年"
                   + ("，只选金融科技板块股票" if scoped_retry else "")),
        as_of_date=date(2026, 9, 5),
    )

    class Generator:
        calls = 0

        async def generate(self, request: CompileInput) -> tuple[CandidateAst, ...]:
            self.calls += 1
            parsed = (await RuleBasedCandidateGenerator().generate(request))[0]
            parts = request.utterance.split("，")
            return (replace(parsed, grounding_evidence=tuple(
                CandidateGroundingEvidence(
                    path=f"/{leg}/0", start=request.utterance.index(text),
                    end=request.utterance.index(text) + len(text), text=text,
                ) for leg, text in zip(("entry", "exit"), parts[:2], strict=True)
            )),)

    screen = replace(_screen(), rows=(*_screen().rows, {"代码": "600183", "名称": "生益科技"}))

    class Data:
        calls = 0

        async def screen(self, *, query: str, asset_type: str) -> LiveMarketDataResult:
            self.calls += 1
            assert request.utterance in query
            if scoped_retry:
                assert "只选金融科技板块股票" in query
                if self.calls == 1:
                    raise MxSaasProviderNoDataError("fixture retry within original sector")
                assert "不放宽原选股范围" in query
            return screen

    class Advisor:
        async def recommend_stocks(
            self, query: str, result: LiveMarketDataResult,
        ) -> tuple[StockRecommendation, ...]:
            assert result is screen
            return (
                StockRecommendation("600183.SH", "生益科技", "模型比较理由甲。"),
                StockRecommendation("300059.SZ", "东方财富", "模型比较理由乙。"),
                StockRecommendation("600519.SH", "贵州茅台", "模型比较理由丙。"),
            )

    generator = Generator()
    compiler = StrategyCompiler(
        generator=generator,
        catalog=load_catalog_directory(Path(__file__).parents[3] / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01",
    )
    pending = await compiler.compile(request)
    assert pending.status is CompileStatus.NEEDS_CLARIFICATION
    assert pending.selected_idea_proposal is not None
    # A real absence verdict keeps the existing stock-offer flow, not merely
    # an instrument_required flag caused by a parser omission.
    identity_router = Mock(assess=AsyncMock(return_value=ClarificationDialogueAssessment(
        reply_kind="preference", acknowledgement_id="respect_preference",
        natural_reply="本轮没有指定股票。", instrument_selected=False,
    )))
    compiler._clarification_dialogue_router = identity_router
    effective, recovered = await compiler.recover_unsupported_identity(request, pending)
    assert effective is request and recovered is pending
    identity_router.assess.assert_awaited_once()
    compiler._clarification_dialogue_router = None
    template = pending.selected_idea_proposal.strategy_template
    assert template is not None
    if with_template:
        # Selecting a direction must not revoke "I will provide my own stock".
        route = replace(_route(), proposals=tuple(
            replace(item, strategy_template=template) for item in _route().proposals
        ))
        selected = await compiler.answer_clarification(
            original_input=request,
            prior_outcome=CompileOutcome(
                status=CompileStatus.NEEDS_CLARIFICATION,
                diagnostic_code="idea_guidance_required", idea_route=route,
                instrument_suggestion_declined=True,
            ),
            answer=route.proposals[0].id,
        )
        assert selected.outcome.diagnostic_code == "instrument_required"
        assert selected.outcome.instrument_suggestion_declined
        skipped, suggested = await _offer_missing_instrument(
            outcome=selected.outcome, compile_input=selected.compile_input, state=None,
            # No provider attributes: any attempted recommendation would fail this test.
            container=cast(ApiContainer, SimpleNamespace()),
        )
        assert skipped is selected.outcome and suggested is None
        own_stock = compiler.bind_selected_idea(
            replace(selected.compile_input, instrument_context="300059.SZ"), skipped,
        )
        assert own_stock is not None and own_stock.status is CompileStatus.READY
        assert own_stock.instrument_suggestion_declined
    if not with_template:
        pending = replace(pending, selected_idea_proposal=None)
    data = Data()
    offered, default_stock = await _offer_missing_instrument(
        outcome=pending, compile_input=request, state=None,
        container=cast(ApiContainer, SimpleNamespace(
            compiler=compiler, live_market_data=data, strategy_advisor=Advisor(),
        )),
    )
    assert data.calls == (2 if scoped_retry else 1)
    assert offered.strategy is None
    response = _to_response(StoredDraftRevision(
        draft_id=uuid4(), revision=1, outcome=offered, compile_input=request,
        created_at=datetime.now(UTC), pending_instrument_reuse=default_stock,
    ))
    assert [item.symbol for item in response.instrument_suggestions] == [
        "600183.SH", "300059.SZ", "600519.SH",
    ]
    assert all(item.source == "eastmoney_mx_screener" for item in response.instrument_suggestions)
    assert all(item.retrieved_at == screen.provenance.retrieved_at
               for item in response.instrument_suggestions)
    if not with_template:
        assert default_stock is not None and default_stock.symbol == "600183.SH"
        assert response.diagnostic_code == "instrument_reuse_confirmation"
        return
    assert default_stock is None
    assert offered.idea_route is not None
    choices = offered.idea_route.proposals
    assert [item.instrument_symbol for item in choices] == ["600183.SH", "300059.SZ", "600519.SH"]
    assert len({item.id for item in choices}) == 3
    assert all(item.strategy_template == template for item in choices)
    assert all(item.strategy == template.bind(item.instrument_symbol or "") for item in choices)
    chosen = compiler.bind_selected_idea(
        replace(request, instrument_context="002594.SZ"), offered,
    )
    assert chosen is not None and chosen.status is CompileStatus.READY
    assert chosen.strategy == template.bind("002594.SZ")
    assert generator.calls == 1


@dataclass(frozen=True)
class _PairingCall:
    screen: LiveMarketDataResult
    proposals: tuple[IdeaProposal, ...]
    understanding: str
    supplements: tuple[LiveFinanceDataResult, ...]
    previous: tuple[StockStrategyDataRequest, ...]
    remaining: int
    feedback: tuple[str, ...]


class _Advisor:
    def __init__(self, responses: tuple[StockStrategyPairing, ...]) -> None:
        self.responses = responses
        self.calls: list[_PairingCall] = []

    async def pair_stock_strategies(
        self, utterance: str, result: LiveMarketDataResult,
        proposals: tuple[IdeaProposal, ...], understanding: str = "", *,
        supplemental_results: tuple[LiveFinanceDataResult, ...] = (),
        previous_requests: tuple[StockStrategyDataRequest, ...] = (),
        remaining_data_rounds: int = 2, data_feedback: tuple[str, ...] = (),
    ) -> StockStrategyPairing:
        assert utterance == "我是吕芳"
        index = len(self.calls)
        self.calls.append(_PairingCall(
            result, proposals, understanding, supplemental_results, previous_requests,
            remaining_data_rounds, data_feedback,
        ))
        assert index < len(self.responses), "Unexpected extra pairing model request"
        return self.responses[index]


class _Data:
    def __init__(
        self, responses: tuple[LiveFinanceDataResult | MxSaasProviderNoDataError, ...],
    ) -> None:
        self.responses = responses
        self.finance_calls: list[tuple[str, str | None]] = []
        self.screen_calls = 0

    async def query_finance(
        self, *, query: str, indicators: str | None,
    ) -> LiveFinanceDataResult:
        index = len(self.finance_calls)
        self.finance_calls.append((query, indicators))
        assert index < len(self.responses), "Unexpected extra finance query"
        result = self.responses[index]
        if isinstance(result, MxSaasProviderNoDataError):
            raise result
        return result

    async def screen(self, *, query: str, asset_type: str) -> LiveMarketDataResult:
        self.screen_calls += 1
        raise AssertionError("Data enrichment must not re-screen the universe")


def _screen() -> LiveMarketDataResult:
    return LiveMarketDataResult(
        provider="eastmoney_mx_screener", query="fixture candidates", asset_type="A股",
        columns=("代码", "名称"), rows=(
            {"代码": "300059", "名称": "东方财富"},
            {"代码": "600519", "名称": "贵州茅台"},
        ),
        provenance=LiveMarketDataProvenance(
            response_sha256="fixture-not-live", schema_version="fixture",
            retrieved_at=datetime(2026, 9, 5, tzinfo=UTC),
        ),
    )


def _route() -> IdeaRoute:
    return IdeaRoute(
        understanding="先比较两个趋势方向。", hypothesis="fixture",
        asset_mapping=IdeaAssetMapping(instrument_symbol=None, relation="unbound"),
        proposals=tuple(IdeaProposal(
            id=f"idea_{index}", title=f"趋势方向{index}", hypothesis="fixture",
            entry_summary="上穿20日均线", exit_summary="下穿20日均线",
            suggested_utterance="上穿20日均线买入，下穿20日均线卖出",
            capability_ids=(), assumptions=(), confidence=1,
        ) for index in range(2)),
    )


@pytest.mark.asyncio
async def test_failed_template_binding_response_preserves_options_and_can_recover(
    caplog: pytest.LogCaptureFixture,
) -> None:
    original = CompileInput(
        utterance="收盘价上穿20日均线买入，收盘价下穿20日均线卖出，回测近一年",
        as_of_date=date(2026, 9, 5),
    )

    class Generator:
        calls = 0

        async def generate(self, request: CompileInput) -> tuple[CandidateAst, ...]:
            self.calls += 1
            assert self.calls == 1, "A retained template must not be generated again"
            return await RuleBasedCandidateGenerator().generate(request)

    generator = Generator()
    compiler = StrategyCompiler(
        generator=generator, catalog=load_catalog_directory(Path(__file__).parents[3] / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01",
    )
    initial = await compiler.compile(replace(original, instrument_context="300059.SZ"))
    assert initial.strategy is not None
    template = UnboundIdeaStrategy.model_validate(
        initial.strategy.model_dump(exclude={"instrument", "schema_version"}),
    )
    pending = CompileOutcome(
        status=CompileStatus.NEEDS_CLARIFICATION, diagnostic_code="idea_guidance_required",
        idea_route=replace(_route(), proposals=tuple(
            replace(item, strategy_template=template) for item in _route().proposals
        )),
        stock_recommendations=(StockRecommendation(
            symbol="300059.SZ", name="东方财富", reason="本次筛选候选",
            source="eastmoney_mx_screener", retrieved_at=datetime.now(UTC),
        ),),
    )
    confirmed = compiler.bind_selected_idea(
        replace(original, instrument_context="300059.SZ"), pending,
    )
    assert confirmed is not None and confirmed.idea_route is not None
    assert not confirmed.stock_recommendations
    assert not confirmed.run_requested
    assert len(confirmed.idea_route.proposals) == 2
    assert all(item.strategy == template.bind("300059.SZ")
               for item in confirmed.idea_route.proposals)
    assert isinstance(template.entry, IndicatorCondition)
    invalid = template.model_copy(update={
        "entry": template.entry.model_copy(update={"trigger": "golden_cross"}),
    })
    route = replace(
        _route(),
        asset_mapping=replace(_route().asset_mapping, rationale="先选策略，再确认回测股票。"),
        proposals=(
            replace(_route().proposals[0], id="idea_000000000000", strategy_template=invalid),
            replace(_route().proposals[1], id="idea_000000000001", strategy_template=template),
        ),
    )
    prior = CompileOutcome(
        status=CompileStatus.NEEDS_CLARIFICATION, diagnostic_code="idea_guidance_required",
        idea_route=route, run_requested=True, refresh_data=True,
    )
    request = replace(original, instrument_context="300059.SZ")
    failed = compiler.bind_selected_idea(request, prior)
    assert failed is not None
    state = DialogueState.project(
        draft_id=uuid4(), revision=2, compile_input=request, outcome=failed,
        created_at=datetime.now(UTC), recent_turns=(),
    )
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        response = _to_response(state)
        assert response.diagnostic_code == "idea_guidance_execution_invalid"
        assert response.idea_route is not None and len(response.idea_route.proposals) == 2
        assert not response.run_requested and not response.refresh_data
        plan = await DialogueTurnOrchestrator(compiler).plan(
            state=state, answer="idea_000000000001",
        )
        assert plan.clarification_turn is not None
        turn = plan.clarification_turn
        ready = _to_response(replace(
            state, revision=3, compile_input=turn.compile_input, outcome=turn.outcome,
        ))
    assert ready.status is CompileStatus.READY
    assert ready.strategy is not None and ready.strategy.instrument.symbol == "300059.SZ"
    assert ready.strategy.entry == template.entry and ready.strategy.exit == template.exit
    assert not ready.run_requested and not ready.refresh_data
    messages = [record.message for record in caplog.records
                if record.message.startswith("strategy_draft_outcome ")]
    assert len(messages) == 2
    assert (
        "status=needs_clarification diagnostic_code=idea_guidance_execution_invalid" in messages[0]
    )
    assert "status=ready diagnostic_code=none" in messages[1]
    assert all(original.utterance not in message for message in messages)
    assert generator.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("remembered", [True, False])
async def test_unselected_persona_directions_survive_stock_reply_composition(
    remembered: bool,
) -> None:
    route = replace(_route(), understanding="借秦始皇的果断劲儿，先给出可修改的趋势方案。")
    original = CompileInput(utterance="我是秦始皇", as_of_date=date(2026, 9, 5))
    pending = CompileOutcome(
        status=CompileStatus.NEEDS_CLARIFICATION, diagnostic_code="idea_guidance_required",
        clarification=route.understanding, idea_route=route,
    )
    requests: list[ClarificationDialogueRequest] = []
    reply = "借秦始皇的果断劲儿，先给你可修改的趋势方案；东方财富也可以作为待测样本。"

    class Dialogue:
        async def assess(
            self, request: ClarificationDialogueRequest,
        ) -> ClarificationDialogueAssessment:
            requests.append(request)
            return ClarificationDialogueAssessment(
                reply_kind="unclear", acknowledgement_id="ask_rephrase", natural_reply=reply,
            )

    class Data:
        async def screen(self, *, query: str, asset_type: str) -> LiveMarketDataResult:
            assert not remembered
            return _screen()

    compiler = StrategyCompiler(
        generator=RuleBasedCandidateGenerator(),
        catalog=load_catalog_directory(Path(__file__).parents[3] / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01",
        clarification_dialogue_router=Dialogue(),
    )
    state = DialogueState.project(
        draft_id=uuid4(), revision=1, compile_input=original, outcome=pending,
        created_at=datetime(2026, 9, 5, tzinfo=UTC), recent_turns=(),
        pending_instrument_reuse=VerifiedInstrumentMemory(
            symbol="300059.SZ", name="东方财富", source="draft_create_compile",
            verified_at=datetime(2026, 9, 5, tzinfo=UTC), evidence="东方财富",
        ),
    ) if remembered else None
    offered, candidate = await _offer_missing_instrument(
        outcome=pending, compile_input=original, state=state,
        container=cast(ApiContainer, SimpleNamespace(
            compiler=compiler, live_market_data=Data(), strategy_advisor=None,
        )),
    )
    if not remembered:
        assert requests == []
        assert candidate is None and offered.stock_recommendations == ()
        assert offered.idea_route is route
        return
    assert len(requests) == 1
    submitted = requests[0]
    assert submitted.answer == original.utterance and submitted.question == ""
    assert route.understanding in submitted.context_summary
    assert all(item.title in submitted.context_summary for item in route.proposals)
    assert "用户尚未选择任何策略" in submitted.context_summary
    assert "用户已选策略：" not in submitted.context_summary
    assert "身份信息不证明均线、量价或买入信号" in submitted.context_summary
    assert offered.clarification == reply and offered.idea_route is route
    assert offered.selected_idea_proposal is None and not offered.run_requested
    assert candidate is not None


def _need(field: str, *, symbol: str = "300059.SZ") -> StockStrategyPairing:
    return StockStrategyPairing(
        introduction="还需补充一些信息。", pairs=(),
        data_request=StockStrategyDataRequest(
            symbols=(symbol,), fields=(field,), message=f"我再查一下{field}，继续比较。",
        ),
    )


def _matched() -> StockStrategyPairing:
    return StockStrategyPairing(introduction="可以试试这些组合。", pairs=(
        StockStrategyPair("idea_1", "600519.SH", "贵州茅台", "fixture reason"),
        StockStrategyPair("idea_0", "300059.SZ", "东方财富", "fixture reason"),
    ))


@pytest.mark.asyncio
@pytest.mark.parametrize("understanding", [
    "借秦始皇的果断劲儿，先给出可修改的趋势方案。",
    "保留低估值偏好，历史估值条件尚未纳入回测，先给可修改的日线反转方案。",
])
@pytest.mark.parametrize("intent", ["new_strategy", "vague_strategy", "casual"])
async def test_pairing_keeps_original_direction_understanding(understanding: str, intent: str) -> None:
    route = replace(_route(), understanding=understanding)
    pairing = _matched()

    class Compiler:
        def bind_idea_proposal(
            self, request: CompileInput, proposal: IdeaProposal, symbol: str,
        ) -> IdeaProposal:
            return replace(proposal, instrument_symbol=symbol)

    class Advisor:
        async def pair_stock_strategies(
            self, utterance: str, result: LiveMarketDataResult,
            proposals: tuple[IdeaProposal, ...], original_understanding: str, **kwargs: object,
        ) -> StockStrategyPairing:
            assert original_understanding == understanding
            assert proposals is route.proposals
            return pairing

    class Data:
        async def screen(self, *, query: str, asset_type: str) -> LiveMarketDataResult:
            return _screen()

    pending = CompileOutcome(
        status=CompileStatus.NEEDS_CLARIFICATION, diagnostic_code="idea_guidance_required",
        idea_route=route, clarification=understanding,
    )
    offered, candidate = await _offer_missing_instrument(
        outcome=pending, compile_input=CompileInput(
            utterance="策略灵感", as_of_date=date(2026, 9, 5), semantic_intent=intent,
        ), state=None, container=cast(ApiContainer, SimpleNamespace(
            compiler=Compiler(), live_market_data=Data(), strategy_advisor=Advisor(),
        )),
    )
    assert candidate is None and offered.idea_route is not None
    assert offered.idea_route.understanding == pairing.introduction
    assert offered.clarification == offered.idea_route.understanding
    assert len(offered.idea_route.proposals) == 2
    assert offered.strategy is None and not offered.run_requested


def _finance(field: str) -> LiveFinanceDataResult:
    # Provider-shaped rawTable/table with distinct raw and display units.
    return LiveFinanceDataResult(
        provider="eastmoney_mx_finance_data", query=f"fixture query {field}", indicators=field,
        tables=({
            "title": "东方财富指标查询", "entityCode": "300059.SZ",
            "rawTable": {
                "headers": ["证券代码", "日期", "成交额(元)"],
                "data": [["300059.SZ", "2026-09-04", 1_230_000_000]],
            },
            "table": {
                "headers": ["证券代码", "日期", "成交额(亿元)"],
                "data": [["300059.SZ", "2026-09-04", "12.30"]],
            },
        },),
        provenance=_screen().provenance,
    )


async def _run(
    advisor: _Advisor, data: _Data, screen: LiveMarketDataResult, route: IdeaRoute,
) -> tuple[StockStrategyPairing | None, str | None]:
    result = await _pair_stock_strategies_with_data(
        advisor=advisor, result=screen, route=route,
        compile_input=CompileInput(utterance="我是吕芳", as_of_date=date(2026, 9, 5)),
        container=cast(ApiContainer, SimpleNamespace(
            live_finance_data=data, live_market_data=data,
        )),
    )
    assert data.screen_calls == 0
    assert all(call.screen is screen and call.proposals is route.proposals
               for call in advisor.calls)
    assert all(call.understanding == route.understanding for call in advisor.calls)
    return result


@pytest.mark.asyncio
async def test_missing_field_is_queried_and_original_finance_tables_reach_next_pairing() -> None:
    needed, matched = _need("最近交易日成交额"), _matched()
    extra = _finance("最近交易日成交额")
    advisor, data = _Advisor((needed, matched)), _Data((extra,))
    result, error = await _run(advisor, data, _screen(), _route())
    assert result is matched and error is None
    assert len(data.finance_calls) == 1
    query, indicators = data.finance_calls[0]
    assert "300059.SZ" in query and "不要选股" in query
    assert indicators == "最近交易日成交额"
    assert [call.remaining for call in advisor.calls] == [2, 1]
    assert advisor.calls[0].supplements == ()
    assert advisor.calls[1].supplements == (extra,)
    assert advisor.calls[1].supplements[0] is extra
    assert advisor.calls[1].previous == (needed.data_request,)
    assert "已返回原始表格" in advisor.calls[1].feedback[0]


@pytest.mark.asyncio
async def test_oversized_enrichment_requests_narrower_lookup_without_claiming_no_match() -> None:
    needed, matched = _need("最近交易日成交额"), _matched()
    extra = replace(_finance("最近交易日成交额"), tables=({"data": "x" * 100_001},))
    advisor, data = _Advisor((needed, matched)), _Data((extra,))
    result, error = await _run(advisor, data, _screen(), _route())
    assert result is matched and error is None
    assert advisor.calls[1].supplements == ()
    assert "数据过大" in advisor.calls[1].feedback[0]
    assert "不能据此判断股票无关联" in advisor.calls[1].feedback[0]
    assert "最新可用一条" in data.finance_calls[0][0]


@pytest.mark.asyncio
async def test_pairing_uses_recovery_without_replacing_candidates_or_source() -> None:
    needed, matched = _need("最近交易日成交额"), _matched()
    extra = replace(_finance("最近交易日成交额"), provider="eastmoney_mx_screener")

    class RecoveringData(_Data):
        async def query_current_finance(
            self, *, query: str, indicators: str | None, asset_type: str = "A股",
        ) -> LiveFinanceDataResult:
            assert asset_type == "A股"
            self.finance_calls.append((query, indicators))
            return extra

        async def query_finance(
            self, *, query: str, indicators: str | None,
        ) -> LiveFinanceDataResult:
            raise AssertionError("Pairing bypassed the recovering lookup")

    advisor, data = _Advisor((needed, matched)), RecoveringData(())
    result, error = await _run(advisor, data, _screen(), _route())
    assert result is matched and error is None
    assert len(data.finance_calls) == 1
    assert "300059.SZ" in data.finance_calls[0][0]
    assert data.finance_calls[0][1] == "最近交易日成交额"
    assert advisor.calls[1].supplements == (extra,)
    assert advisor.calls[1].supplements[0].provider == "eastmoney_mx_screener"
    assert advisor.calls[1].previous == (needed.data_request,)


@pytest.mark.asyncio
async def test_no_data_is_feedback_then_different_model_fields_can_succeed() -> None:
    first, second, matched = _need("最近交易日换手率"), _need("最近交易日成交额"), _matched()
    extra = _finance("最近交易日成交额")
    advisor = _Advisor((first, second, matched))
    data = _Data((MxSaasProviderNoDataError("fixture no data"), extra))
    result, error = await _run(advisor, data, _screen(), _route())
    assert result is matched and error is None
    assert [indicators for _, indicators in data.finance_calls] == [
        "最近交易日换手率", "最近交易日成交额",
    ]
    assert [call.remaining for call in advisor.calls] == [2, 1, 0]
    assert advisor.calls[1].supplements == ()
    assert advisor.calls[1].previous == (first.data_request,)
    assert "没有返回数据" in advisor.calls[1].feedback[0]
    assert advisor.calls[2].supplements == (extra,)
    assert advisor.calls[2].previous == (first.data_request, second.data_request)
    assert len(advisor.calls[2].feedback) == 2


@pytest.mark.asyncio
async def test_lookup_budget_stops_after_two_finance_and_three_pairing_calls() -> None:
    advisor = _Advisor((_need("成交额"), _need("换手率"), _need("近20日涨跌幅")))
    data = _Data((_finance("成交额"), _finance("换手率")))
    result, error = await _run(advisor, data, _screen(), _route())
    assert result is None and error is not None
    assert "补查请求未通过校验" in error
    assert len(data.finance_calls) == 2
    assert [call.remaining for call in advisor.calls] == [2, 1, 0]


@pytest.mark.asyncio
async def test_unknown_requested_symbol_is_rejected_before_finance_lookup() -> None:
    advisor = _Advisor((_need("成交额", symbol="600000.SH"),))
    data = _Data(())
    result, error = await _run(advisor, data, _screen(), _route())
    assert result is None and error is not None
    assert "补查请求未通过校验" in error
    assert len(advisor.calls) == 1
    assert data.finance_calls == []

@pytest.mark.asyncio
@pytest.mark.parametrize("transport_failure", [False, True])
async def test_rejected_matching_never_resurrects_raw_stock_choices(transport_failure) -> None:
    screen = _screen()
    route = _route()
    class Data:
        async def screen(self, **kwargs):
            return screen
    class Advisor:
        async def pair_stock_strategies(self, *args, **kwargs):
            if transport_failure:
                from ashare_lab.adapters.language.vibe_candidates import CandidateTransportError
                raise CandidateTransportError("fixture", failure_kind="connection_failed")
            return None
    outcome = CompileOutcome(status=CompileStatus.NEEDS_CLARIFICATION,
                             diagnostic_code="idea_guidance_required", idea_route=route)
    offered, default = await _offer_missing_instrument(
        outcome=outcome,
        compile_input=CompileInput(utterance="给我一个策略", as_of_date=date(2026, 9, 5)),
        state=None, container=cast(ApiContainer, SimpleNamespace(
            live_market_data=Data(), strategy_advisor=Advisor(),
        )),
    )
    assert default is None and offered.idea_route is route
    assert offered.strategy is None and not offered.run_requested
    assert offered.stock_recommendations == ()
    assert "保留" in offered.clarification


@pytest.mark.asyncio
@pytest.mark.parametrize("topic", ["山竹", "榴莲", "咖啡"])
@pytest.mark.parametrize("matched", [True, False])
async def test_parent_industry_research_is_bounded_and_disclosed(topic, matched):
    class Data:
        calls = []
        async def screen(self, **kwargs):
            self.calls.append(kwargs['query'])
            return _screen()
    class Advisor:
        calls = 0
        async def plan_industry_expansion(self, utterance):
            from ashare_lab.ports.strategy_advice import IndustryExpansion
            assert topic in utterance
            return IndustryExpansion('水果种植业', 'A股主营业务涉及水果种植业，返回代码、简称、主营业务、行业，最多10只')
        async def pair_stock_strategies(self, utterance, *args, **kwargs):
            self.calls += 1
            if self.calls == 1 or not matched:
                return StockStrategyPairing('', ())
            assert '所属行业' in utterance and topic in utterance
            assert '禁止扩展' in utterance
            return _matched()
    class Compiler:
        def bind_idea_proposal(self, request, proposal, symbol):
            return replace(proposal, instrument_symbol=symbol)
    data, advisor = Data(), Advisor()
    route = _route()
    outcome = CompileOutcome(status=CompileStatus.NEEDS_CLARIFICATION,
                             diagnostic_code='idea_guidance_required', idea_route=route)
    offered, default = await _offer_missing_instrument(
        outcome=outcome, compile_input=CompileInput(utterance=f'{topic}相关股票策略', as_of_date=date(2026,9,5)),
        state=None, container=cast(ApiContainer, SimpleNamespace(live_market_data=data,
            strategy_advisor=advisor, compiler=Compiler())),
    )
    assert len(data.calls) == advisor.calls == 2
    assert topic not in data.calls[1] and '水果种植业' in data.calls[1]
    assert default is None and not offered.run_requested
    if matched:
        assert offered.clarification == _matched().introduction
        assert len(offered.idea_route.proposals) == 2
    else:
        assert offered.stock_recommendations == ()
        assert offered.idea_route is route


@pytest.mark.asyncio
async def test_followup_receives_previously_displayed_stock_chips():
    from ashare_lab.application.turn_intent import TurnIntent
    captured = []
    class Dialogue:
        async def assess(self, request):
            captured.append(request)
            return None
    compiler = StrategyCompiler(generator=RuleBasedCandidateGenerator(),
        catalog=load_catalog_directory(Path(__file__).parents[3] / 'catalogs'),
        catalog_id='cn_a.signals', release_version='2026.09.01', clarification_dialogue_router=Dialogue())
    prior = CompileOutcome(status=CompileStatus.NEEDS_CLARIFICATION,
        diagnostic_code='idea_guidance_required', idea_route=_route(),
        stock_recommendations=(StockRecommendation('300059.SZ', '东方财富', '旧候选理由'),))
    await compiler._assess_clarification_dialogue(
        CompileInput(utterance='山竹相关股票策略', as_of_date=date(2026,9,5)), prior,
        '为什么这只是相关股票？', TurnIntent.DATA_QUERY, ())
    assert '东方财富（300059.SZ）' in captured[0].context_summary
    assert '旧候选理由' in captured[0].context_summary
    assert '不证明主题关联' in captured[0].context_summary
