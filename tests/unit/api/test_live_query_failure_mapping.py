"""Error-only regressions; no external provider, model, or backtest runs."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from fastapi import Response

from ashare_lab.adapters.language.vibe_candidates import CandidateTransportError
from ashare_lab.adapters.market_data.mx_saas import (
    MxSaasProviderAuthError,
    MxSaasProviderDataError,
    MxSaasProviderError,
    MxSaasProviderNoDataError,
    MxSaasProviderUnavailableError,
)
from ashare_lab.api import create_app
from ashare_lab.api.container import ApiContainer
from ashare_lab.api.errors import ApiProblem
from ashare_lab.api.routes.market_data import (
    query_live_finance_data,
    screen_live_market,
    screen_then_query_live_finance_data,
)
from ashare_lab.api.routes.strategy_drafts import (
    _answer_live_data_query,
    create_strategy_draft,
)
from ashare_lab.api.schemas import (
    LiveFinanceQueryRequest,
    LiveMarketScreenRequest,
    LiveScreenedFinanceQueryRequest,
    StrategyDraftRequest,
)
from ashare_lab.application.compile_strategy import CompileOutcome, CompileStatus
from ashare_lab.application.dialogue_state import DialogueState
from ashare_lab.domain.strategy import StrategySpec, canonical_hash
from ashare_lab.ports.candidate_generation import CompileInput
from ashare_lab.ports.live_market_data import (
    LiveFinanceDataResult,
    LiveMarketDataProvenance,
    LiveMarketDataResult,
    LiveScreenedFinanceDataResult,
    LiveSecurityEntity,
)
from ashare_lab.ports.strategy_advice import QueryDataReview

SECRET_SENTINEL = "NEVER_ECHO_RAW_RESPONSE_secret-token_402_429"
NOW = datetime(2026, 9, 6, tzinfo=UTC)
QUERY = "查询东方财富当前市盈率"
SCREEN_QUERY = "A股近一年涨幅前5只股票"


ERROR_CASES = (
    (MxSaasProviderAuthError(SECRET_SENTINEL), "authentication_failed", 503, "授权失败"),
    (MxSaasProviderUnavailableError(SECRET_SENTINEL, reason="read_timeout"),
     "read_timeout", 504, "等待响应超时"),
    (MxSaasProviderUnavailableError(SECRET_SENTINEL, reason="connect_timeout"),
     "connect_timeout", 504, "建立连接超时"),
    (MxSaasProviderUnavailableError(SECRET_SENTINEL, reason="transport_error"),
     "connection_failed", 503, "连接或传输失败"),
    (MxSaasProviderUnavailableError(SECRET_SENTINEL, reason="http_error", http_status=429),
     "rate_limited", 429, "限流"),
    (MxSaasProviderUnavailableError(SECRET_SENTINEL, reason="http_error", http_status=503),
     "service_unavailable", 502, "服务端"),
    (MxSaasProviderUnavailableError(SECRET_SENTINEL), "unavailable", 503, "服务未完成"),
    (MxSaasProviderUnavailableError(SECRET_SENTINEL, reason="http_error", http_status=402),
     "unavailable", 503, "服务未完成"),
    (MxSaasProviderDataError("business code 402 " + SECRET_SENTINEL, http_status=200),
     "invalid_response", 502, "数据"),
    (MxSaasProviderDataError("business code 429 " + SECRET_SENTINEL, http_status=200),
     "invalid_response", 502, "数据"),
    (MxSaasProviderNoDataError(SECRET_SENTINEL), "no_results", 404, "数据"),
)


class _Provider:
    def __init__(self, error: MxSaasProviderError | None, *, first_result: bool = False) -> None:
        self.error = error
        self.first_result = first_result
        self.calls = 0

    def _check(self) -> None:
        self.calls += 1
        if self.error is not None and not (self.first_result and self.calls == 1):
            raise self.error

    async def screen(self, *, query: str, asset_type: str) -> LiveMarketDataResult:
        self._check()
        return LiveMarketDataResult(
            "fixture", query, asset_type, ("代码", "名称"),
            ({"代码": "300059", "名称": "东方财富"},),
            LiveMarketDataProvenance("sha256:" + "a" * 64, NOW, "fixture.v1"),
        )

    async def query_finance(
        self, *, query: str, indicators: str | None,
    ) -> LiveFinanceDataResult:
        self._check()
        return LiveFinanceDataResult(
            "fixture", query, indicators,
            ({"entityName": "东方财富", "code": "300059.SZ", "rawTable": {
                "headers": ["证券简称", "市盈率"], "data": [["东方财富", 35.2]],
            }},), LiveMarketDataProvenance("sha256:" + "a" * 64, NOW, "fixture.v1"),
        )

    async def screen_then_query_finance(
        self, *, screening_query: str, asset_type: str, indicators: str,
    ) -> LiveScreenedFinanceDataResult:
        self._check()
        return LiveScreenedFinanceDataResult(
            await self.screen(query=screening_query, asset_type=asset_type),
            (LiveSecurityEntity("300059.SZ", "东方财富", "A股"),),
            (await self.query_finance(query=QUERY, indicators=indicators),),
        )


def _container(provider: _Provider) -> ApiContainer:
    return cast(ApiContainer, create_app(
        live_market_data=provider, live_finance_data=provider,
    ).state.container)


@pytest.mark.asyncio
@pytest.mark.parametrize(("error", "suffix", "status_code", "fragment"), ERROR_CASES)
@pytest.mark.parametrize("route", ("screen", "query", "screen-query"))
async def test_http_routes_use_safe_adapter_classification(
    error: MxSaasProviderError, suffix: str, status_code: int, fragment: str, route: str,
) -> None:
    container = _container(_Provider(error))
    with pytest.raises(ApiProblem) as caught:
        if route == "screen":
            await screen_live_market(
                LiveMarketScreenRequest(query=SCREEN_QUERY, asset_type="A股"), container,
            )
        elif route == "query":
            await query_live_finance_data(LiveFinanceQueryRequest(query=QUERY), container)
        else:
            await screen_then_query_live_finance_data(LiveScreenedFinanceQueryRequest(
                screening_query=SCREEN_QUERY, asset_type="A股", indicators="市盈率",
            ), container)
    problem = caught.value
    assert problem.status_code == status_code
    assert problem.code == f"live_market_data_{suffix}"
    if suffix != "no_results" or route != "screen":
        assert fragment in problem.message
    assert SECRET_SENTINEL not in problem.message
    if suffix in {"unavailable", "invalid_response"}:
        assert not any(word in problem.message for word in ("连接失败", "限流", "余额"))


@pytest.mark.asyncio
@pytest.mark.parametrize(("error", "suffix", "status_code", "fragment"), ERROR_CASES)
@pytest.mark.parametrize("query", (QUERY, SCREEN_QUERY))
async def test_new_query_draft_reports_diagnostic_without_rewriting_stored_outcome(
    error: MxSaasProviderError, suffix: str, status_code: int, fragment: str, query: str,
) -> None:
    container = _container(_Provider(error))
    result = await create_strategy_draft(
        body=StrategyDraftRequest(utterance=query, as_of_date=date(2026, 9, 6)),
        response=Response(), container=container,
    )
    assert result.diagnostic_code == f"live_market_data_{suffix}"
    assert fragment in result.assistant_message
    assert SECRET_SENTINEL not in result.assistant_message
    assert result.data is None
    stored = await container.drafts.load_latest_dialogue_state(draft_id=result.draft_id)
    assert stored.outcome.diagnostic_code == "data_query_only"
    assert stored.outcome.strategy is None
    assert stored.revision == result.revision == 1


async def _stored_strategy(container: ApiContainer) -> DialogueState:
    example = Path(__file__).resolve().parents[3] / (
        "contracts/examples/strategy.macd-volume.daily.v1.json"
    )
    strategy = StrategySpec.model_validate(json.loads(example.read_text()))
    result = await container.drafts.create(
        outcome=CompileOutcome(
            status=CompileStatus.READY, strategy=strategy,
            strategy_hash=canonical_hash(strategy), diagnostic_code=None,
            run_requested=True, refresh_data=True,
        ),
        compile_input=CompileInput("已确认规则", date(2026, 9, 6), "300059.SZ"),
        request_hash="sha256:" + "d" * 64, idempotency_key=str(uuid4()),
    )
    return await container.drafts.load_latest_dialogue_state(draft_id=result.value.draft_id)


class _RetryAdvisor:
    async def review_query_result(self, **kwargs: object) -> QueryDataReview:
        return QueryDataReview(False, (), "A股近一年涨幅前5且非ST", "补充查询口径")


@pytest.mark.asyncio
async def test_refetch_timeout_preserves_first_table_and_pending_strategy() -> None:
    provider = _Provider(MxSaasProviderUnavailableError(
        SECRET_SENTINEL, reason="read_timeout",
    ), first_result=True)
    container = replace(_container(provider), strategy_advisor=_RetryAdvisor())
    state = await _stored_strategy(container)
    before = state.outcome
    response = await _answer_live_data_query(
        draft_id=state.draft_id, answer=SCREEN_QUERY, state=state,
        response=Response(), container=container,
    )
    assert response.draft.diagnostic_code == "live_market_data_read_timeout"
    assert response.draft.strategy == before.strategy
    assert response.draft.strategy_hash == before.strategy_hash
    assert response.draft.revision == state.revision
    assert response.draft.run_requested == before.run_requested
    assert response.draft.refresh_data == before.refresh_data
    assert response.data.screen.rows == ({"代码": "300059", "名称": "东方财富"},)
    assert "查询结果已保留" in response.assistant_message
    assert provider.calls == 2
    stored = await container.drafts.load_latest_dialogue_state(draft_id=state.draft_id)
    assert stored.outcome == before
    assert stored.revision == state.revision


class _FailedModel:
    async def review_query_result(self, **kwargs: object) -> None:
        raise CandidateTransportError(SECRET_SENTINEL, failure_kind="rate_limited", http_status=429)


@pytest.mark.asyncio
@pytest.mark.parametrize("query", (QUERY, SCREEN_QUERY))
async def test_classified_model_review_failure_keeps_data_and_strategy(query: str) -> None:
    container = replace(_container(_Provider(None)), strategy_advisor=_FailedModel())
    state = await _stored_strategy(container)
    response = await _answer_live_data_query(
        draft_id=state.draft_id, answer=query, state=state,
        response=Response(), container=container,
    )
    assert response.draft.diagnostic_code == "candidate_provider_rate_limited"
    assert response.draft.strategy == state.outcome.strategy
    assert response.draft.strategy_hash == state.outcome.strategy_hash
    assert response.data is not None
    assert "模型服务请求频率受限" in response.assistant_message
    assert "查询结果与刚才的策略已保留" in response.assistant_message
    assert SECRET_SENTINEL not in response.assistant_message


class _AdviceFailedModel:
    async def review_query_result(self, **kwargs: object) -> QueryDataReview:
        return QueryDataReview(True, ("fixture",), None, "fixture success")

    async def advise(self, request: object) -> None:
        raise CandidateTransportError(SECRET_SENTINEL, failure_kind="timeout")


@pytest.mark.asyncio
async def test_classified_model_answer_failure_keeps_verified_finance_table() -> None:
    container = replace(_container(_Provider(None)), strategy_advisor=_AdviceFailedModel())
    state = await _stored_strategy(container)
    response = await _answer_live_data_query(
        draft_id=state.draft_id, answer=QUERY, state=state,
        response=Response(), container=container,
    )
    assert response.draft.diagnostic_code == "candidate_provider_timeout"
    assert response.data.finance.tables[0]["rawTable"]["data"] == [["东方财富", 35.2]]
    assert response.draft.strategy == state.outcome.strategy
    assert response.draft.run_requested == state.outcome.run_requested
    assert SECRET_SENTINEL not in response.assistant_message


@pytest.mark.asyncio
async def test_classified_model_composed_answer_failure_keeps_screen_and_finance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _container(_Provider(None))
    state = await _stored_strategy(container)

    async def fail(**kwargs: object) -> str:
        raise CandidateTransportError(SECRET_SENTINEL, failure_kind="service_unavailable")

    monkeypatch.setattr(container.compiler, "compose_dialogue_response", fail)
    response = await _answer_live_data_query(
        draft_id=state.draft_id, answer=SCREEN_QUERY + "，获取近10年的归母净利润", state=state,
        response=Response(), container=container,
    )
    assert response.draft.diagnostic_code == "candidate_provider_service_unavailable"
    assert response.data.screened_finance.screen.rows
    assert response.data.screened_finance.batches[0].tables
    assert response.draft.strategy == state.outcome.strategy
    assert response.draft.run_requested == state.outcome.run_requested
    assert SECRET_SENTINEL not in response.assistant_message
