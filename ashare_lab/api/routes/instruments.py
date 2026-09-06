"""A-share autocomplete, separate from model-driven stock recommendations."""

import logging
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Query, Request

from ashare_lab.adapters.market_data.eastmoney_instrument_search import (
    EastmoneyInstrumentSearch,
    InstrumentSearchInvalid,
    InstrumentSearchResult,
    InstrumentSearchUnavailable,
)

from ..errors import ApiProblem
from ..schemas import error_response_docs

router = APIRouter(prefix="/api/v1/market", tags=["live-market-data"])
logger = logging.getLogger(__name__)


def get_instrument_search(request: Request) -> EastmoneyInstrumentSearch:
    service = getattr(request.app.state, "instrument_search", None)
    if service is None:
        service = EastmoneyInstrumentSearch()
        request.app.state.instrument_search = service
    return cast(EastmoneyInstrumentSearch, service)


@router.get(
    "/instruments", response_model=InstrumentSearchResult, operation_id="searchAShareInstruments",
    responses=error_response_docs(422, 502, 503),
)
async def search_instruments(
    query: Annotated[str, Query(min_length=1, max_length=32)],
    service: Annotated[EastmoneyInstrumentSearch, Depends(get_instrument_search)],
    limit: Annotated[int, Query(ge=1, le=20)] = 8,
) -> InstrumentSearchResult:
    try:
        return await service.search(query, limit=limit)
    except ValueError as exc:
        raise ApiProblem(status_code=422, code="instrument_search_query_invalid",
                         message="请输入股票名称、代码或简拼。") from exc
    except InstrumentSearchUnavailable as exc:
        raise ApiProblem(status_code=503, code="instrument_search_unavailable",
                         message="东方财富股票搜索暂时连接失败，输入已保留，请重试。") from exc
    except InstrumentSearchInvalid as exc:
        logger.warning("A-share autocomplete rejected provider response: %s", exc)
        raise ApiProblem(status_code=502, code="instrument_search_invalid",
                         message="东方财富股票搜索返回的名称或代码未通过核对，请重试。") from exc
