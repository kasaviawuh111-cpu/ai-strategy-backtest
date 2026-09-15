"""Parameterized grid research through the existing Eastmoney history channel."""

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from threading import BoundedSemaphore
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ashare_lab.adapters.market_data.mx_daily_history import (
    MxDailyHistoryBeforeListingError,
    MxDailyHistoryError,
)
from ashare_lab.adapters.market_data.mx_saas import (
    MxSaasProviderAuthError,
    MxSaasProviderError,
    MxSaasProviderUnavailableError,
)
from ashare_lab.application.backtest_submission import latest_stable_a_share_data_date
from ashare_lab.application.conditional_orders import ConditionParameters, run_conditional_backtest
from ashare_lab.application.grid_strategy import (
    GridParameters,
    GridSpecificationError,
    run_grid_backtest,
)
from ashare_lab.domain.market_data import normalize_a_share_instrument
from ashare_lab.domain.strategy import canonical_hash

from ..container import ApiContainer, get_container
from ..errors import ApiProblem

router = APIRouter(prefix="/api/v1", tags=["price-strategy-backtests"])
logger = logging.getLogger(__name__)
_capacity = BoundedSemaphore(2)


class PriceBacktestScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instrument_id: str = Field(min_length=6, max_length=12)
    start: date
    end: date

    @model_validator(mode="after")
    def valid_scope(self) -> "PriceBacktestScope":
        normalize_a_share_instrument(self.instrument_id)
        if self.start > self.end or self.start.year < 1990 or (self.end - self.start).days > 7300:
            raise ValueError("请使用1990年以后的有效日期，单次区间不超过20年")
        if self.end > latest_stable_a_share_data_date(datetime.now(UTC)):
            raise ValueError("结束日期须使用已完成交易日")
        return self


class GridRequest(PriceBacktestScope):
    parameters: GridParameters


class ConditionRequest(PriceBacktestScope):
    parameters: ConditionParameters


@router.post("/grid/backtests")
async def create_grid_backtest(
    body: GridRequest, container: Annotated[ApiContainer, Depends(get_container)],
) -> dict[str, object]:
    return await _run_backtest(body, container)


@router.post("/conditional/backtests")
async def create_conditional_backtest(
    body: ConditionRequest, container: Annotated[ApiContainer, Depends(get_container)],
) -> dict[str, object]:
    return await _run_backtest(body, container)


async def _run_backtest(
    body: GridRequest | ConditionRequest, container: ApiContainer,
) -> dict[str, object]:
    # Local import avoids the result-schema/service composition cycle.
    from ashare_lab.application.skill_backtest_service import SkillBacktestService

    service = container.backtest_submission
    scope = "conditional" if isinstance(body, ConditionRequest) else "grid"
    if not isinstance(service, SkillBacktestService):
        raise ApiProblem(status_code=503, code=f"{scope}_history_unavailable",
                         message="历史数据通道暂未就绪，已填写参数可以保留。")
    if not _capacity.acquire(blocking=False):
        raise ApiProblem(status_code=429, code=f"{scope}_busy",
                         message="当前请求较多，请稍候再试。")
    symbol = str(normalize_a_share_instrument(body.instrument_id))
    try:
        start = body.start - timedelta(days=14)
        try:
            history = await service.history.load(symbol, start, body.end)
        except MxDailyHistoryBeforeListingError:
            history = await service.history.load(symbol, body.start, body.end)
        if history.instrument_id != symbol:
            history = await service.history.load(symbol, body.start, body.end, force_refresh=True)
        if history.instrument_id != symbol:
            raise MxDailyHistoryError("grid_history_instrument_mismatch_after_refresh")
        if isinstance(body, ConditionRequest):
            result = await asyncio.to_thread(
                run_conditional_backtest, params=body.parameters, history=history,
                start=body.start, end=body.end,
            )
        else:
            result = await asyncio.to_thread(
                run_grid_backtest, params=body.parameters, history=history,
                start=body.start, end=body.end,
            )
        encoded = jsonable_encoder(result)
        encoded["request"] = body.model_dump(mode="json")
        encoded["result_hash"] = canonical_hash(encoded)
        return encoded
    except GridSpecificationError as exc:
        raise ApiProblem(status_code=422, code=f"{scope}_parameters_invalid",
                         message=str(exc)) from exc
    except MxSaasProviderUnavailableError as exc:
        logger.warning("grid_history_network_error error_class=%s", type(exc).__name__)
        raise ApiProblem(status_code=503, code=f"{scope}_history_network_error",
                         message="历史行情查询遇到网络异常，自动恢复后仍未取得完整数据。") from exc
    except MxDailyHistoryBeforeListingError as exc:
        raise ApiProblem(
            status_code=422, code=f"{scope}_before_listing",
            message=f"回测起始日期早于股票上市日期{exc.listing_date}，请调整区间。",
        ) from exc
    except MxSaasProviderAuthError as exc:
        logger.warning("grid_history_access_error error_class=%s", type(exc).__name__)
        raise ApiProblem(
            status_code=503, code=f"{scope}_history_access_unavailable",
            message="历史行情通道暂未取得访问授权，参数已保留，本次尚未计算收益。",
        ) from exc
    except (MxDailyHistoryError, MxSaasProviderError) as exc:
        logger.warning("grid_history_data_error error_class=%s", type(exc).__name__)
        raise ApiProblem(
            status_code=503, code=f"{scope}_history_data_incomplete",
            message="历史行情暂未返回完整数据，参数已保留，本次尚未计算收益。",
        ) from exc
    finally:
        _capacity.release()
