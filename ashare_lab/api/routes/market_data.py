"""Current-market discovery endpoints, isolated from historical backtests."""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends, status

from ashare_lab.adapters.market_data.mx_saas import (
    MxSaasProviderAuthError,
    MxSaasProviderDataError,
    MxSaasProviderError,
    MxSaasProviderNoDataError,
    MxSaasProviderUnavailableError,
)
from ashare_lab.ports.live_market_data import (
    LiveFinanceDataResult,
    LiveMarketDataResult,
    LiveScreenedFinanceData,
)

from ..container import ApiContainer, get_container
from ..errors import ApiProblem
from ..schemas import (
    LiveFinanceQueryRequest,
    LiveFinanceQueryResponse,
    LiveMarketProvenancePayload,
    LiveMarketScreenRequest,
    LiveMarketScreenResponse,
    LiveScreenedFinanceQueryRequest,
    LiveScreenedFinanceQueryResponse,
    LiveSecurityEntityPayload,
    error_response_docs,
)

router = APIRouter(prefix="/api/v1/market", tags=["live-market-data"])
Container = Annotated[ApiContainer, Depends(get_container)]


@router.post(
    "/screen",
    response_model=LiveMarketScreenResponse,
    status_code=status.HTTP_200_OK,
    operation_id="screenLiveMarket",
    responses=error_response_docs(404, 422, 429, 502, 503, 504),
)
async def screen_live_market(
    body: LiveMarketScreenRequest,
    container: Container,
) -> LiveMarketScreenResponse:
    provider = container.live_market_data
    if provider is None:
        raise ApiProblem(
            status_code=503,
            code="live_market_data_unavailable",
            message="东方财富选股 Skill 尚未配置，暂时无法选股。",
        )
    try:
        result = await provider.screen(query=body.query, asset_type=body.asset_type)
    except (MxSaasProviderAuthError, MxSaasProviderUnavailableError) as exc:
        raise live_market_data_problem(exc, skill_name="东方财富选股 Skill") from exc
    except MxSaasProviderNoDataError as exc:
        raise ApiProblem(
            status_code=404,
            code="live_market_data_no_results",
            message="东方财富选股 Skill 未找到符合本次条件的股票。",
        ) from exc
    except MxSaasProviderDataError as exc:
        raise ApiProblem(
            status_code=502,
            code="live_market_data_invalid_response",
            message="东方财富选股 Skill 返回的数据暂时无法解析，本次未生成选股结果。",
        ) from exc
    return _screen_response(result)


@router.post(
    "/query",
    response_model=LiveFinanceQueryResponse,
    status_code=status.HTTP_200_OK,
    operation_id="queryLiveFinanceData",
    responses=error_response_docs(404, 422, 429, 502, 503, 504),
)
async def query_live_finance_data(
    body: LiveFinanceQueryRequest,
    container: Container,
) -> LiveFinanceQueryResponse:
    provider = container.live_finance_data
    if provider is None:
        raise ApiProblem(
            status_code=503,
            code="live_market_data_unavailable",
            message="东方财富查数 Skill 尚未配置，暂时无法查询数据。",
        )
    try:
        result = await provider.query_finance(
            query=body.query,
            indicators=body.indicators,
        )
    except (MxSaasProviderAuthError, MxSaasProviderUnavailableError) as exc:
        raise live_market_data_problem(exc, skill_name="东方财富查数 Skill") from exc
    except MxSaasProviderNoDataError as exc:
        raise ApiProblem(
            status_code=404,
            code="live_market_data_no_results",
            message="东方财富查数 Skill 未返回本次查询的匹配数据。",
        ) from exc
    except MxSaasProviderDataError as exc:
        raise ApiProblem(
            status_code=502,
            code="live_market_data_invalid_response",
            message="东方财富查数 Skill 返回的数据暂时无法解析，本次查询未完成。",
        ) from exc
    return _finance_response(result)


@router.post(
    "/screen-query",
    response_model=LiveScreenedFinanceQueryResponse,
    status_code=status.HTTP_200_OK,
    operation_id="screenThenQueryLiveFinanceData",
    responses=error_response_docs(404, 422, 429, 502, 503, 504),
)
async def screen_then_query_live_finance_data(
    body: LiveScreenedFinanceQueryRequest,
    container: Container,
) -> LiveScreenedFinanceQueryResponse:
    """Compose current screening with batched current lookup.

    The provider credential remains server-side.  The result is explicitly not
    a historical point-in-time data source and cannot be submitted to backtest.
    """

    provider = container.live_market_data
    operation = getattr(provider, "screen_then_query_finance", None)
    if provider is None or not callable(operation):
        raise ApiProblem(
            status_code=503,
            code="live_market_data_unavailable",
            message="东方财富选股/查数流程尚未配置，暂时无法完成本次查询。",
        )
    screened_provider = cast(LiveScreenedFinanceData, provider)
    try:
        result = await screened_provider.screen_then_query_finance(
            screening_query=body.screening_query,
            asset_type=body.asset_type,
            indicators=body.indicators,
        )
    except (MxSaasProviderAuthError, MxSaasProviderUnavailableError) as exc:
        raise live_market_data_problem(exc, skill_name="东方财富选股/查数流程") from exc
    except MxSaasProviderNoDataError as exc:
        raise ApiProblem(
            status_code=404,
            code="live_market_data_no_results",
            message="东方财富选股/查数流程未返回本次查询的匹配数据。",
        ) from exc
    except MxSaasProviderDataError as exc:
        raise ApiProblem(
            status_code=502,
            code="live_market_data_invalid_response",
            message="东方财富选股/查数流程返回的数据暂时无法解析，本次查询未完成。",
        ) from exc
    return LiveScreenedFinanceQueryResponse(
        screen=_screen_response(result.screen),
        entities=tuple(
            LiveSecurityEntityPayload(
                code=entity.code,
                name=entity.name,
                asset_type=entity.asset_type,
            )
            for entity in result.entities
        ),
        batches=tuple(_finance_response(batch) for batch in result.batches),
    )


def live_market_data_problem(error: MxSaasProviderError, *, skill_name: str) -> ApiProblem:
    """Classify only adapter-owned metadata, never provider response text or business codes."""
    code, http_status, detail = "unavailable", 503, "服务未完成本次查询，请稍后重试。"
    if isinstance(error, MxSaasProviderAuthError):
        code, detail = "authentication_failed", "授权失败，本次查询未完成。"
    elif isinstance(error, MxSaasProviderNoDataError):
        code, http_status, detail = "no_results", 404, "未返回本次查询的匹配数据。"
    elif isinstance(error, MxSaasProviderDataError):
        code, http_status = "invalid_response", 502
        detail = "返回的数据暂时无法使用，本次查询未完成。"
    elif error.reason == "read_timeout":
        code, http_status, detail = "read_timeout", 504, "等待响应超时，本次查询未完成。"
    elif error.reason == "connect_timeout":
        code, http_status, detail = "connect_timeout", 504, "建立连接超时，本次查询未完成。"
    elif error.reason == "transport_error":
        code, detail = "connection_failed", "连接或传输失败，本次查询未完成。"
    elif error.reason == "http_error" and error.http_status == 429:
        code, http_status, detail = "rate_limited", 429, "请求受到限流，请稍后重试。"
    elif (
        error.reason == "http_error" and error.http_status is not None
        and 500 <= error.http_status <= 599
    ):
        code, http_status, detail = "service_unavailable", 502, "服务端暂时异常，请稍后重试。"
    return ApiProblem(
        status_code=http_status, code=f"live_market_data_{code}", message=f"{skill_name}{detail}",
    )


def _screen_response(result: LiveMarketDataResult) -> LiveMarketScreenResponse:
    return LiveMarketScreenResponse(
        provider=result.provider,
        query=result.query,
        asset_type=result.asset_type,
        columns=result.columns,
        rows=tuple(dict(item) for item in result.rows),
        provenance=LiveMarketProvenancePayload(
            response_sha256=result.provenance.response_sha256,
            retrieved_at=result.provenance.retrieved_at,
            schema_version=result.provenance.schema_version,
        ),
    )


def _finance_response(result: LiveFinanceDataResult) -> LiveFinanceQueryResponse:
    return LiveFinanceQueryResponse(
        provider=result.provider,
        query=result.query,
        indicators=result.indicators,
        tables=tuple(dict(item) for item in result.tables),
        provenance=LiveMarketProvenancePayload(
            response_sha256=result.provenance.response_sha256,
            retrieved_at=result.provenance.retrieved_at,
            schema_version=result.provenance.schema_version,
        ),
    )
