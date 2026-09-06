"""Controlled orchestration fixtures, excluded from real-input acceptance counts."""

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
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
from ashare_lab.application.compile_strategy import CompileStatus, StrategyCompiler
from ashare_lab.domain.catalog import load_catalog_directory
from ashare_lab.ports.candidate_generation import (
    CandidateAst,
    CandidateGroundingEvidence,
    CompileInput,
)
from ashare_lab.ports.idea_routing import IdeaAssetMapping, IdeaProposal, IdeaRoute
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
@pytest.mark.parametrize("with_template", [True, False])
async def test_complete_rules_are_preserved_across_ranked_stock_choices(
    with_template: bool,
) -> None:
    request = CompileInput(
        utterance="收盘价上穿20日均线买入，收盘价下穿20日均线卖出，回测近一年",
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
        async def screen(self, *, query: str, asset_type: str) -> LiveMarketDataResult:
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
    template = pending.selected_idea_proposal.strategy_template
    assert template is not None
    if not with_template:
        pending = replace(pending, selected_idea_proposal=None)
    offered, default_stock = await _offer_missing_instrument(
        outcome=pending, compile_input=request, state=None,
        container=cast(ApiContainer, SimpleNamespace(
            compiler=compiler, live_market_data=Data(), strategy_advisor=Advisor(),
        )),
    )
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
            result, proposals, supplemental_results, previous_requests,
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
