"""Response-only API wiring tests; fixtures are not live-model acceptance."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import cast
from uuid import uuid4

import pytest

from ashare_lab.adapters.market_data.mx_saas import (
    MxSaasProviderAuthError,
    MxSaasProviderNoDataError,
    MxSaasProviderUnavailableError,
)
from ashare_lab.api import create_app
from ashare_lab.api.container import ApiContainer
from ashare_lab.api.routes.strategy_drafts import _resolve_live_data_query
from ashare_lab.application.compile_strategy import CompileOutcome, CompileStatus
from ashare_lab.application.dialogue_state import DialogueState
from ashare_lab.ports.candidate_generation import CompileInput
from ashare_lab.ports.clarification_dialogue import (
    ClarificationDialogueAssessment,
    ClarificationDialogueRequest,
)
from ashare_lab.ports.live_market_data import (
    LiveFinanceDataResult,
    LiveMarketDataProvenance,
    LiveMarketDataResult,
    LiveScreenedFinanceDataResult,
    LiveSecurityEntity,
)
from ashare_lab.ports.strategy_advice import (
    QueryDataReview,
    StockRecommendation,
    VerifiedFactStrategyAdvice,
    VerifiedFactStrategyAdviceRequest,
)

NOW = datetime(2026, 9, 6, 7, 0, tzinfo=UTC)
MODEL_REPLY = "这是模型针对本轮查询组织的完整回复。"


class _Dialogue:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.requests: list[ClarificationDialogueRequest] = []

    async def assess(
        self, request: ClarificationDialogueRequest,
    ) -> ClarificationDialogueAssessment | None:
        self.requests.append(request)
        if not self.available:
            return None
        return ClarificationDialogueAssessment(
            reply_kind="unclear", acknowledgement_id="ask_rephrase", natural_reply=MODEL_REPLY,
        )


class _Provider:
    def __init__(self, *, with_batches: bool = True) -> None:
        self.with_batches = with_batches
        self.calls: list[str] = []
        self.provenance = LiveMarketDataProvenance("sha256:" + "a" * 64, NOW, "fixture.v1")
        self.rows = tuple(
            {"证券代码": code, "证券简称": name, "收盘价(元) 2026-09-04": value}
            for code, name, value in (
                ("300059", "东方财富", 20), ("300033", "同花顺", 30),
                ("000001", "平安银行", 10), ("002594", "比亚迪", 100),
            )
        )

    def screen_result(self, query: str) -> LiveMarketDataResult:
        return LiveMarketDataResult(
            "fixture", query, "A股", tuple(self.rows[0]), self.rows, self.provenance,
        )

    async def screen(self, *, query: str, asset_type: str) -> LiveMarketDataResult:
        assert asset_type == "A股"
        self.calls.append("screen")
        return self.screen_result(query)

    async def query_finance(
        self, *, query: str, indicators: str | None,
    ) -> LiveFinanceDataResult:
        raise AssertionError("no extra data query is authorized by response composition")

    async def screen_then_query_finance(
        self, *, screening_query: str, asset_type: str, indicators: str,
    ) -> LiveScreenedFinanceDataResult:
        self.calls.append("screen_then_query_finance")
        batch = LiveFinanceDataResult(
            "fixture", screening_query, indicators,
            ({"entityName": "东方财富", "rawTable": {
                "headers": ["证券简称", "数据日期", "归母净利润(亿元)"],
                "data": [["东方财富", "2025-12-31", 42]],
            }},), self.provenance,
        )
        return LiveScreenedFinanceDataResult(
            self.screen_result(screening_query),
            tuple(LiveSecurityEntity(row["证券代码"], row["证券简称"], asset_type)
                  for row in self.rows),
            (batch,) if self.with_batches else (),
        )


class _Advisor:
    def __init__(self, reviews: tuple[QueryDataReview | None, ...] | None = None) -> None:
        self.reviews = list(reviews) if reviews is not None else None
        self.review_calls: list[dict[str, object]] = []
        self.recommendation_calls: list[tuple[str, LiveMarketDataResult]] = []

    async def review_query_result(
        self, *, question: str, data_snapshot: Mapping[str, object],
        previous_queries: tuple[str, ...] = (), remaining_data_rounds: int = 1,
    ) -> QueryDataReview | None:
        self.review_calls.append({
            "question": question, "data_snapshot": data_snapshot,
            "previous_queries": previous_queries, "remaining_data_rounds": remaining_data_rounds,
        })
        if self.reviews is not None:
            assert self.reviews, "query review exceeded its fixture budget"
            return self.reviews.pop(0)
        return QueryDataReview(
            satisfied=True, evidence=("组件夹具的口径核对结果。",),
            retry_query=None, message=MODEL_REPLY,
        )

    async def advise(self, request: VerifiedFactStrategyAdviceRequest) -> None:
        raise AssertionError("a screening response must not generate trading strategies")

    async def recommend_stocks(
        self, query: str, result: LiveMarketDataResult,
    ) -> tuple[StockRecommendation, ...]:
        self.recommendation_calls.append((query, result))
        return tuple(
            StockRecommendation(symbol, name, reason)
            for symbol, name, reason in (
                ("002594.SZ", "比亚迪", "本次比较的第一只。"),
                ("000001.SZ", "平安银行", "本次比较的第二只。"),
                ("300033.SZ", "同花顺", "本次比较的第三只。"),
                ("300059.SZ", "东方财富", "第四只不得进入推荐摘要。"),
            )
        )


def _state() -> DialogueState:
    return DialogueState.project(
        draft_id=uuid4(), revision=1,
        compile_input=CompileInput("当前查数会话", date(2026, 9, 5)),
        outcome=CompileOutcome(
            status=CompileStatus.NEEDS_CLARIFICATION, diagnostic_code="data_query_only",
        ),
        created_at=NOW, recent_turns=(),
    )


def _container(
    monkeypatch: pytest.MonkeyPatch, provider: _Provider, dialogue: _Dialogue,
    advisor: _Advisor | None = None,
) -> ApiContainer:
    container = cast(ApiContainer, create_app(
        live_market_data=provider, live_finance_data=provider,
        strategy_advisor=advisor if advisor is not None else _Advisor(),
    ).state.container)
    monkeypatch.setattr(container.compiler, "_clarification_dialogue_router", dialogue)
    return container


@pytest.mark.asyncio
@pytest.mark.parametrize(("answer", "with_batches", "expected_kind", "expected_call"), [
    ("A股近一年涨幅前5只股票", True, "screen", "screen"),
    ("帮我推荐成交活跃的A股股票", True, "screen", "screen"),
    ("A股近一年涨幅前5只股票，获取近10年的归母净利润", True,
     "screened_finance", "screen_then_query_finance"),
    ("A股近一年涨幅前5只股票，获取近10年的归母净利润", False,
     "screened_finance", "screen_then_query_finance"),
])
async def test_success_uses_whole_model_reply_and_bounded_real_result_context(
    monkeypatch: pytest.MonkeyPatch, answer: str, with_batches: bool,
    expected_kind: str, expected_call: str,
) -> None:
    provider, dialogue, advisor = _Provider(with_batches=with_batches), _Dialogue(), _Advisor()
    state = _state()

    message, data, idea = await _resolve_live_data_query(
        answer=answer, state=state,
        container=_container(monkeypatch, provider, dialogue, advisor),
    )

    assert message == MODEL_REPLY
    assert data is not None and data.kind == expected_kind and idea is None
    assert provider.calls == [expected_call]
    assert state.outcome.strategy is None and not state.outcome.run_requested
    if expected_kind == "screen" and "推荐" not in answer:
        assert not dialogue.requests
        assert len(advisor.review_calls) == 1
        assert advisor.review_calls[0]["question"] == answer
        snapshot = cast(Mapping[str, object], advisor.review_calls[0]["data_snapshot"])
        assert snapshot["columns"] == list(provider.rows[0])
        assert snapshot["rows"] == list(provider.rows)
        assert "2026-09-04" in str(snapshot)
        assert data.screen is not None and data.screen.rows == provider.rows
        return
    assert len(dialogue.requests) == 1
    request = dialogue.requests[0]
    assert request.response_only and not request.allow_data_query
    assert request.answer == answer
    assert "不得新增" in request.context_summary
    assert "不是回测结果" in request.context_summary
    if "推荐" in answer:
        assert len(advisor.review_calls) == 1
        assert request.verified_instruments == (
            ("002594.SZ", "比亚迪"), ("000001.SZ", "平安银行"), ("300033.SZ", "同花顺"),
        )
        assert data.screen is not None
        assert [row["代码"] for row in data.screen.rows] == [
            "002594.SZ", "000001.SZ", "300033.SZ",
        ]
        assert "本次比较的第一只" in request.context_summary
        assert "第四只不得进入" not in request.context_summary
        assert "东方财富" not in request.context_summary
    else:
        assert not advisor.review_calls
        assert request.verified_instruments == (
            ("300059", "东方财富"), ("300033", "同花顺"), ("000001", "平安银行"),
        )
        assert data.screened_finance is not None
        assert len(data.screened_finance.entities) == 4
        assert len(data.screened_finance.batches) == int(with_batches)
        if with_batches:
            assert "2025-12-31" in request.context_summary
            assert "42" in request.context_summary


@pytest.mark.asyncio
async def test_unavailable_recommendation_reply_keeps_complete_recommended_screen_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, dialogue = _Provider(), _Dialogue(available=False)
    answer = "帮我推荐成交活跃的A股股票"
    message, data, idea = await _resolve_live_data_query(
        answer=answer, state=_state(),
        container=_container(monkeypatch, provider, dialogue),
    )
    assert "模型" in message and "重试" in message
    assert "查到了" not in message
    assert data is not None and data.screen is not None
    assert data.screen.query == answer
    assert data.screen.columns == ("股票", "代码", "选择理由")
    assert data.screen.rows == (
        {"股票": "比亚迪", "代码": "002594.SZ", "选择理由": "本次比较的第一只。"},
        {"股票": "平安银行", "代码": "000001.SZ", "选择理由": "本次比较的第二只。"},
        {"股票": "同花顺", "代码": "300033.SZ", "选择理由": "本次比较的第三只。"},
    )
    assert data.screen.provenance.response_sha256 == provider.provenance.response_sha256
    assert idea is None and provider.calls == ["screen"]
    assert len(dialogue.requests) == 1


@pytest.mark.asyncio
async def test_missing_stock_is_a_model_question_without_calling_data_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, dialogue = _Provider(), _Dialogue()
    state = replace(_state(), compile_input=CompileInput("查数", date(2026, 9, 5)))
    message, data, idea = await _resolve_live_data_query(
        answer="它的换手率是多少", state=state,
        container=_container(monkeypatch, provider, dialogue),
    )
    assert message == MODEL_REPLY and data is None and idea is None
    assert provider.calls == [] and len(dialogue.requests) == 1
    assert dialogue.requests[0].response_only
    assert dialogue.requests[0].verified_instruments == ()
    assert "股票" in dialogue.requests[0].question


class _RetryProvider(_Provider):
    def __init__(
        self, *, screens: tuple[LiveMarketDataResult | Exception, ...] = (),
        finances: tuple[LiveFinanceDataResult | Exception, ...] = (),
    ) -> None:
        super().__init__()
        self.screens = list(screens)
        self.finances = list(finances)
        self.screen_queries: list[str] = []
        self.finance_queries: list[str] = []

    async def screen(self, *, query: str, asset_type: str) -> LiveMarketDataResult:
        assert asset_type == "A股"
        self.calls.append("screen")
        self.screen_queries.append(query)
        assert self.screens, "screening exceeded its fixture budget"
        result = self.screens.pop(0)
        if isinstance(result, Exception):
            raise result
        return replace(result, query=query)

    async def query_finance(
        self, *, query: str, indicators: str | None,
    ) -> LiveFinanceDataResult:
        self.calls.append("query_finance")
        self.finance_queries.append(query)
        assert self.finances, "finance lookup exceeded its fixture budget"
        result = self.finances.pop(0)
        if isinstance(result, Exception):
            raise result
        return replace(result, query=query, indicators=indicators)


def _period_screen(*, annual: bool) -> LiveMarketDataResult:
    provider = _Provider()
    field = "区间涨跌幅(%) 2025-09-05至2026-09-04" if annual else "涨跌幅(%) 2026-09-04"
    return replace(
        provider.screen_result("夹具查询"), columns=("证券代码", "证券简称", field),
        rows=tuple({"证券代码": row["证券代码"], "证券简称": row["证券简称"],
                    field: 20 + index if annual else 1 + index}
                   for index, row in enumerate(provider.rows)),
        provenance=replace(
            provider.provenance, response_sha256="sha256:" + ("b" if annual else "a") * 64,
        ),
    )


def _review(
    *, satisfied: bool, retry: str | None = None, message: str = MODEL_REPLY,
) -> QueryDataReview:
    return QueryDataReview(
        satisfied=satisfied, evidence=("返回字段明确标注了数据日期或区间。",),
        retry_query=retry, message=message,
    )


@pytest.mark.asyncio
async def test_screen_retries_the_full_universe_when_daily_data_does_not_answer_the_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer = "A股近一年涨幅前5只股票"
    retry = (
        "从全部A股按2025-09-05至2026-09-04区间涨跌幅降序选前5只，"
        "返回证券代码、证券简称和区间涨跌幅"
    )
    first, last = _period_screen(annual=False), _period_screen(annual=True)
    first_amount = "区间成交额(元) 2026-01-01至2026-09-04"
    last_amount = "成交额(元) 2026-09-04"
    first = replace(first, columns=(*first.columns, first_amount),
                    rows=tuple({**row, first_amount: 987654321000} for row in first.rows),
                    provider_metadata={"amount_period": "range", "amount_unit": "CNY"})
    last = replace(last, columns=(*last.columns, last_amount),
                   rows=tuple({**row, last_amount: 123456789 + index}
                              for index, row in enumerate(last.rows)),
                   provider_metadata={"data_date": "2026-09-04",
                                      "amount_period": "single_day", "amount_unit": "CNY"})
    provider = _RetryProvider(screens=(first, last))
    advisor = _Advisor((_review(satisfied=False, retry=retry), _review(satisfied=True)))
    dialogue, state = _Dialogue(), _state()

    message, data, idea = await _resolve_live_data_query(
        answer=answer, state=state, container=_container(monkeypatch, provider, dialogue, advisor),
    )

    assert message == MODEL_REPLY and idea is None
    assert provider.calls == ["screen", "screen"]
    assert provider.screen_queries == [answer, retry]
    assert all("A股" in query for query in provider.screen_queries)
    assert [call["remaining_data_rounds"] for call in advisor.review_calls] == [1, 0]
    assert all(call["question"] == answer for call in advisor.review_calls)
    assert advisor.review_calls[1]["previous_queries"]
    assert "2026-09-04" in str(advisor.review_calls[0]["data_snapshot"])
    assert "2025-09-05至2026-09-04" in str(advisor.review_calls[1]["data_snapshot"])
    final_snapshot = cast(Mapping[str, object], advisor.review_calls[1]["data_snapshot"])
    assert final_snapshot["columns"] == list(last.columns)
    assert final_snapshot["rows"] == list(last.rows)
    assert final_snapshot["provider_metadata"] == last.provider_metadata
    assert final_snapshot["retrieved_at"] == last.provenance.retrieved_at.isoformat()
    assert "123456789" in str(final_snapshot) and "987654321000" not in str(final_snapshot)
    assert first_amount not in str(final_snapshot)
    assert data is not None and data.screen is not None
    assert data.screen.query == retry and data.screen.columns == last.columns
    assert data.screen.rows == last.rows
    assert not dialogue.requests and not advisor.recommendation_calls
    assert state.outcome.strategy is None and not state.outcome.run_requested


@pytest.mark.asyncio
async def test_unsatisfied_second_screen_returns_review_without_recommendation_or_third_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer = "帮我推荐近一年涨幅领先的A股股票"
    retry = "从全部A股查询近一年区间涨跌幅并按降序返回前5只股票"
    first, last = _period_screen(annual=False), _period_screen(annual=False)
    stop_message = "两次返回仍只有单日涨幅，无法回答近一年表现；已保留这次实际查询表。"
    advisor = _Advisor((
        _review(satisfied=False, retry=retry),
        _review(satisfied=False, retry="继续从全部A股查询近一年涨幅", message=stop_message),
    ))
    provider, dialogue = _RetryProvider(screens=(first, last)), _Dialogue()

    message, data, idea = await _resolve_live_data_query(
        answer=answer, state=_state(),
        container=_container(monkeypatch, provider, dialogue, advisor),
    )

    assert message == stop_message and idea is None
    assert provider.screen_queries == [answer, retry]
    assert [call["remaining_data_rounds"] for call in advisor.review_calls] == [1, 0]
    assert data is not None and data.screen is not None and data.screen.query == retry
    assert data.screen.rows == last.rows
    assert not advisor.recommendation_calls and not dialogue.requests


@pytest.mark.asyncio
async def test_unavailable_query_review_preserves_data_without_an_extra_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, dialogue, advisor = _Provider(), _Dialogue(), _Advisor((None,))
    message, data, idea = await _resolve_live_data_query(
        answer="A股近一年涨幅前5只股票", state=_state(),
        container=_container(monkeypatch, provider, dialogue, advisor),
    )
    assert "数据已返回" in message and "收盘价(元) 2026-09-04：20" in message
    assert "满足" not in message and "未完成" not in message
    assert data is not None and data.screen is not None and data.screen.rows == provider.rows
    assert idea is None and provider.calls == ["screen"]
    assert len(advisor.review_calls) == 1
    assert not advisor.recommendation_calls and not dialogue.requests


@pytest.mark.asyncio
async def test_retry_provider_failure_names_the_screening_step_and_keeps_the_first_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer = "A股近一年涨幅前5只股票"
    first = _period_screen(annual=False)
    retry = "查询全部A股近一年区间涨跌幅前5名"
    provider = _RetryProvider(screens=(
        first, MxSaasProviderUnavailableError("fixture unavailable"),
    ))
    dialogue, advisor = _Dialogue(), _Advisor((_review(satisfied=False, retry=retry),))
    message, data, idea = await _resolve_live_data_query(
        answer=answer, state=_state(),
        container=_container(monkeypatch, provider, dialogue, advisor),
    )
    assert "东方财富选股 Skill" in message and "服务未完成" in message
    assert data is not None and data.screen is not None
    assert data.screen.query == answer and data.screen.rows == first.rows
    assert provider.screen_queries == [answer, retry] and idea is None
    assert len(advisor.review_calls) == 1
    assert not advisor.recommendation_calls and not dialogue.requests


@pytest.mark.asyncio
async def test_known_stock_finance_retries_finance_only_without_generating_a_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer = "东方财富300059.SZ近一年日均换手率是多少"
    retry = "查询东方财富300059.SZ在2025-09-05至2026-09-04的日均换手率，返回区间和百分比单位"
    provenance = _Provider().provenance
    first = LiveFinanceDataResult("fixture", answer, None, ({"entityName": "东方财富", "rawTable": {
        "headers": ["证券代码", "证券简称", "换手率(%) 2026-09-04"],
        "data": [["300059.SZ", "东方财富", 2.1]],
    }},), provenance)
    last = replace(first, tables=({"entityName": "东方财富", "rawTable": {
        "headers": ["证券代码", "证券简称", "日均换手率(%) 2025-09-05至2026-09-04"],
        "data": [["300059.SZ", "东方财富", 3.2]],
    }},))
    provider = _RetryProvider(finances=(first, last))
    dialogue = _Dialogue()
    advisor = _Advisor((_review(satisfied=False, retry=retry), _review(satisfied=True)))
    state = _state()

    message, data, idea = await _resolve_live_data_query(
        answer=answer, state=state, container=_container(monkeypatch, provider, dialogue, advisor),
    )

    assert message == MODEL_REPLY and idea is None
    assert provider.calls == ["query_finance", "query_finance"]
    assert provider.finance_queries == [answer, retry] and not provider.screen_queries
    assert [call["remaining_data_rounds"] for call in advisor.review_calls] == [1, 0]
    assert data is not None and data.finance is not None
    assert data.finance.query == retry and data.finance.tables == last.tables
    assert "3.2" in str(advisor.review_calls[-1]["data_snapshot"])
    assert not dialogue.requests and not advisor.recommendation_calls
    assert state.outcome.strategy is None and not state.outcome.run_requested


@pytest.mark.asyncio
@pytest.mark.parametrize("first_tool", ["finance", "screen"])
async def test_alternate_skill_keeps_original_scope_and_actual_response_type(
    monkeypatch: pytest.MonkeyPatch, first_tool: str,
) -> None:
    answer = "东方财富300059.SZ前20日最高收盘价是多少" if first_tool == "finance" else (
        "A股近一年涨幅前5只股票"
    )
    source = _Provider()
    screen = replace(source.screen_result(answer), rows=(source.rows[0],))
    finance = LiveFinanceDataResult(
        "actual_finance", answer, None,
        ({"entityName": "东方财富", "code": "300059.SZ", "rawTable": {
            "headers": ["前20日最高收盘价(元)"], "data": [[23.75]],
        }},), replace(source.provenance, response_sha256="sha256:" + "b" * 64),
    )
    failure = MxSaasProviderNoDataError("first Skill returned no rows")
    provider = _RetryProvider(
        screens=(screen,) if first_tool == "finance" else (failure,),
        finances=(failure,) if first_tool == "finance" else (finance,),
    )
    # Optional review unavailable after recovery: show real data, never discard it
    # and never use another model to generate strategies as a prerequisite.
    advisor, dialogue, state = _Advisor((None,)), _Dialogue(), _state()
    message, data, idea = await _resolve_live_data_query(
        answer=answer, state=state,
        container=_container(monkeypatch, provider, dialogue, advisor),
    )
    assert provider.calls == (
        ["query_finance", "screen"] if first_tool == "finance"
        else ["screen", "query_finance"]
    )
    assert provider.screen_queries == provider.finance_queries == [answer]
    assert data is not None and idea is None
    assert data.kind == ("screen" if first_tool == "finance" else "finance")
    if data.screen is not None:
        assert data.screen.rows == screen.rows
        assert data.screen.provenance.response_sha256 == screen.provenance.response_sha256
    else:
        assert data.finance is not None and data.finance.tables == finance.tables
        assert data.finance.provenance.response_sha256 == finance.provenance.response_sha256
    assert "数据已返回" in message and "全部满足" not in message
    assert advisor.review_calls[0]["remaining_data_rounds"] == 0
    assert not dialogue.requests and not advisor.recommendation_calls
    assert state.outcome.strategy is None and not state.outcome.run_requested


@pytest.mark.asyncio
@pytest.mark.parametrize("alternate_available", [True, False])
async def test_finance_empty_table_body_uses_one_alternate_query(
    monkeypatch: pytest.MonkeyPatch, alternate_available: bool,
) -> None:
    answer = "东方财富300059.SZ最新价是多少"
    source = _Provider()
    empty = LiveFinanceDataResult(
        "eastmoney_mx_finance_data", answer, None,
        ({"entityName": "东方财富", "rawTable": {
            "headers": ["最新价(元)"], "data": [],
        }},), source.provenance,
    )
    actual_row = {"证券代码": "300059", "证券简称": "东方财富", "最新价(元)": 20}
    screen = replace(source.screen_result(answer), columns=tuple(actual_row), rows=(actual_row,))
    provider = _RetryProvider(
        finances=(empty,),
        screens=(screen if alternate_available else MxSaasProviderNoDataError("empty"),),
    )
    advisor, dialogue, state = _Advisor((None,)), _Dialogue(), _state()
    message, data, idea = await _resolve_live_data_query(
        answer=answer, state=state,
        container=_container(monkeypatch, provider, dialogue, advisor),
    )
    assert provider.calls == ["query_finance", "screen"]
    assert provider.screen_queries == provider.finance_queries == [answer]
    assert idea is None and not dialogue.requests and not advisor.recommendation_calls
    assert state.outcome.strategy is None and not state.outcome.run_requested
    if alternate_available:
        assert data is not None and data.screen is not None
        assert data.screen.rows == screen.rows
        assert data.screen.provenance.response_sha256 == screen.provenance.response_sha256
        assert "数据已返回" in message and "全部满足" not in message
        assert advisor.review_calls[0]["remaining_data_rounds"] == 0
    else:
        assert data is None and not advisor.review_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("first_tool", ["finance", "screen"])
async def test_authorization_failure_never_tries_the_other_skill(
    monkeypatch: pytest.MonkeyPatch, first_tool: str,
) -> None:
    error = MxSaasProviderAuthError("SECRET_NEVER_ECHO")
    provider = _RetryProvider(
        screens=(error,) if first_tool == "screen" else (),
        finances=(error,) if first_tool == "finance" else (),
    )
    answer = "东方财富最新价" if first_tool == "finance" else "A股涨幅前5只股票"
    advisor, dialogue = _Advisor(), _Dialogue()
    message, data, idea = await _resolve_live_data_query(
        answer=answer, state=_state(),
        container=_container(monkeypatch, provider, dialogue, advisor),
    )
    assert provider.calls == ["query_finance" if first_tool == "finance" else "screen"]
    assert data is None and idea is None and "授权" in message
    assert "SECRET" not in message and not advisor.review_calls


@pytest.mark.asyncio
async def test_other_skill_recovery_does_not_claim_the_requested_period_is_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer = "东方财富300059.SZ近一年日均换手率是多少"
    provider = _RetryProvider(
        finances=(MxSaasProviderUnavailableError("fixture timeout"),),
        screens=(_period_screen(annual=False),),
    )
    gap = "服务返回的是单日数据；本次实际返回表已保留，尚无所需的近一年日均换手率。"
    advisor = _Advisor((_review(satisfied=False, retry="不应再发出请求", message=gap),))
    message, data, idea = await _resolve_live_data_query(
        answer=answer, state=_state(),
        container=_container(monkeypatch, provider, _Dialogue(), advisor),
    )
    assert message == gap and data is not None and data.screen is not None and idea is None
    assert provider.calls == ["query_finance", "screen"]
    assert advisor.review_calls[0]["remaining_data_rounds"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("returned_identity", ["maotai", "multiple", "none"])
async def test_mixed_lookup_uses_current_security_not_the_old_strategy_stock(
    monkeypatch: pytest.MonkeyPatch, returned_identity: str,
) -> None:
    class MixedAdvisor(_Advisor):
        def __init__(self) -> None:
            super().__init__((replace(_review(satisfied=True), strategy_requested=True),))
            self.advice_requests: list[VerifiedFactStrategyAdviceRequest] = []

        async def advise(
            self, request: VerifiedFactStrategyAdviceRequest,
        ) -> VerifiedFactStrategyAdvice:
            self.advice_requests.append(request)
            return VerifiedFactStrategyAdvice(
                analysis="当前数据已用于本轮明确请求的策略分析。", hypothesis="", proposals=(),
                provider="fixture", model="fixture", prompt_version="fixture.v1",
                schema_version="fixture.v1",
            )

    table: dict[str, object] = {"rawTable": {
        "headers": ["最新价(元)"], "data": [[1500]],
    }}
    if returned_identity == "maotai":
        table.update({"code": "600519.SH", "entityName": "贵州茅台"})
    elif returned_identity == "multiple":
        table["rawTable"] = {
            "headers": ["证券代码", "证券简称", "最新价(元)"],
            "data": [["600519", "贵州茅台", 1500], ["300059", "东方财富", 20]],
        }
    question = "查询贵州茅台最新价，并分析两种策略" if returned_identity == "maotai" else (
        "查询贵州茅台和东方财富最新价，并分析策略" if returned_identity == "multiple" else
        "它的最新价是多少，并分析两种策略"
    )
    provider = _RetryProvider(finances=(LiveFinanceDataResult(
        "fixture", question, None, (table,), _Provider().provenance,
    ),))
    advisor, dialogue = MixedAdvisor(), _Dialogue()
    state = replace(_state(), compile_input=CompileInput(
        "东方财富原策略", date(2026, 9, 5), "300059.SZ",
    ))
    message, data, idea = await _resolve_live_data_query(
        answer=question, state=state,
        container=_container(monkeypatch, provider, dialogue, advisor),
    )
    assert data is not None and data.finance is not None and idea is None
    assert state.verified_instrument_context == "300059.SZ"
    assert provider.calls == ["query_finance"]
    if returned_identity == "multiple":
        assert not advisor.advice_requests
        assert "数据查询已完成" in message and "策略建议暂未生成" in message
    else:
        assert len(advisor.advice_requests) == 1
        assert advisor.advice_requests[0].instrument_symbol == (
            "600519.SH" if returned_identity == "maotai" else "300059.SZ"
        )
