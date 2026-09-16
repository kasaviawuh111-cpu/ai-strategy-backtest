"""Reuse the execution service's real-input preparation before accepting a strategy."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict, replace
from datetime import date
from typing import cast, get_args

from ashare_lab.adapters.market_data.mx_daily_history import (
    MxDailyHistoryBeforeListingError,
    MxDailyHistoryError,
    MxDailyHistoryFieldsMissingError,
)
from ashare_lab.adapters.market_data.mx_indicator_contract import UnsupportedSkillIndicatorError
from ashare_lab.adapters.market_data.mx_saas import (
    MxFailureReason,
    MxSaasProviderAuthError,
    MxSaasProviderDataError,
    MxSaasProviderError,
    MxSaasProviderNoDataError,
    MxSaasProviderUnavailableError,
    MxTool,
)
from ashare_lab.application.backtest_submission import (
    BacktestDataNotYetAvailableError,
    BacktestDateRangeError,
    BacktestRunConfig,
    EventDataUnavailableError,
    FinancialDataUnavailableError,
    resolve_execution_settings,
)
from ashare_lab.application.compile_strategy import CompileOutcome, CompileStatus
from ashare_lab.adapters.market_data.mx_grid_anchor import (
    MissingLatestGridQuoteError, bind_latest_grid_anchor,
)
from ashare_lab.domain.strategy.price_plans import GridPlan, GridParameters, GridSpecificationError
from ashare_lab.application.minute_grid_plan import MinuteGridCapabilityError
from ashare_lab.application.execution_feedback import execution_capability_feedback
from ashare_lab.domain.strategy.canonical import canonical_hash
from ashare_lab.application.skill_numeric_history import SkillNumericHistoryError
from ashare_lab.application.minute_replay_input import MinuteReplayDataError, MinuteReplayCoverageError
from ashare_lab.domain.signals.provider_runtime import (
    ProviderSignalRuntimeError,
    provider_condition_leaves,
)
from ashare_lab.domain.strategy import (
    AllCondition,
    AnyCondition,
    FirstOfExit,
    IndicatorCondition,
    NotCondition,
    StrategySpec,
    iter_event_conditions,
    iter_indicator_conditions,
)
from ashare_lab.ports.candidate_generation import CompileInput
from ashare_lab.ports.dialogue_progress import emit_progress
from ashare_lab.ports.request_context import current_request_id

from .container import ApiContainer
from .errors import ApiProblem
from .schemas import ErrorDetail

# Uvicorn configures this logger for local/container output; the module logger
# can otherwise silently discard the only evidence behind a public 503.
_LOGGER = logging.getLogger("uvicorn.error")
_TIMEOUT_SECONDS = 180.0
_SAFE_CALL_ID = re.compile(r"(?:finance|screen|indicator_history)_[0-9a-f]{32}")
_SAFE_DATA_REASONS = frozenset({
    "protocol_invalid_json", "protocol_invalid_payload", "protocol_tables_missing",
    "protocol_table_invalid", "protocol_raw_table_missing", "protocol_dates_missing",
    "provider_query_rejected", "provider_partial_result", "data_security_mismatch",
    "data_dates_mismatch", "data_values_invalid", "data_fields_missing",
    "data_history_unavailable", "data_field_binding_mismatch", "data_validation_failed",
    "data_unit_unconfirmed",
})
_HISTORY_FIELD_LABELS = {
    name: name for name in (
        "开盘价", "最高价", "最低价", "收盘价", "成交量", "成交额", "前收盘价",
        "交易状态", "是否为ST股票", "涨停价", "跌停价",
    )
}

_EXECUTION_DATA_REASONS = {
    "corporate_action_coverage_insufficient": "分红送转数据尚未覆盖指标预热期及完整回测区间。",
    "corporate_action_source_missing": "暂缺该股票的分红送转数据。",
    "corporate_action_acquisition_unavailable": "本次未能取得完整的分红送转数据，策略条件已保留。",
    "corporate_action_source_invalid": "分红送转数据尚未完成结构核对。",
    "corporate_action_prior_close_missing": "暂缺除权除息前一交易日的原始收盘价。",
    "corporate_action_raw_price_evidence_missing": "暂缺用于核对除权除息的未复权价格证据。",
    "corporate_action_reference_price_mismatch": "除权除息参考价与分红送转方案尚未核对一致。",
    "corporate_action_ex_date_factor_missing": "暂缺分红送转对应除权日的价格换算信息。",
    "corporate_action_terms_not_known_before_ex_date": "分红送转方案的公告时点尚未核对一致。",
    "market_calendar_source_missing": "暂缺市场交易日历。",
    "market_calendar_missing_next_settlement_session": "交易日历尚未覆盖下一结算交易日。",
}


def _source_indicator_locations(strategy: StrategySpec) -> list[tuple[str, IndicatorCondition]]:
    def walk(node: object, path: str) -> list[tuple[str, IndicatorCondition]]:
        if isinstance(node, IndicatorCondition):
            return [(path, node)]
        if isinstance(node, AllCondition | AnyCondition | FirstOfExit):
            return [item for index, child in enumerate(node.children)
                    for item in walk(child, f"{path}/children/{index}")]
        if isinstance(node, NotCondition):
            return walk(node.child, f"{path}/child")
        return []
    return walk(strategy.entry, "/entry") + walk(strategy.exit, "/exit")


def _condition_failure_details(
    strategy: StrategySpec, *, indicator_id: str | None, condition_path: str | None = None,
    warmup: bool = False,
) -> tuple[ErrorDetail, ...]:
    # Match the exact same technical exit projection used by preparation. It
    # removes position-aware exits and collapses a single technical child.
    from ashare_lab.application.skill_backtest_service import (
        _exit_condition,  # pyright: ignore[reportPrivateUsage]
    )

    locations: dict[str, IndicatorCondition] = {}
    for leg, condition in (("entry", strategy.entry), ("exit", _exit_condition(strategy))):
        if condition is None:
            continue
        try:
            leaves = provider_condition_leaves(condition)
        except ProviderSignalRuntimeError:
            continue
        locations.update((f"{leg}.{path}", leaf) for path, leaf in leaves)
    source_locations = _source_indicator_locations(strategy)
    fallback_location = "strategy"
    if warmup:
        matched: list[str] = []
        if isinstance(condition_path, str):
            leg = condition_path.split(".", 1)[0]
            fallback_location = leg if leg in {"entry", "exit"} else "strategy"
            leaf = locations.get(condition_path)
            if leaf is not None and (indicator_id is None or leaf.indicator_id == indicator_id):
                # Projection preserves leaf objects, not indices. Only expose a
                # source pointer if this exact runtime leaf has one source location.
                candidates = [path for path, item in source_locations
                              if item is leaf and (path.startswith(f"/{leg}/")
                                                   or path == f"/{leg}")]
                if len(candidates) == 1:
                    matched = candidates
    else:
        matched = [path for path, leaf in source_locations if leaf.indicator_id == indicator_id]
    message = (
        "这条条件在本次区间内尚无可用于后续交易日执行的有效历史指标值，可能尚未完成预热。"
        if warmup else "这条条件目前没有可执行的历史指标数据链路。"
    )
    return tuple(ErrorDetail(
        location=path, type="backtest_condition_unavailable", message=message,
    ) for path in matched) if matched else (ErrorDetail(
        location=fallback_location, type="backtest_condition_unavailable",
        message="当前策略的历史指标数据尚不满足本次回测要求。",
    ),)


def _missing_history_details(error: MxDailyHistoryFieldsMissingError) -> tuple[ErrorDetail, ...]:
    fields = tuple(dict.fromkeys(
        _HISTORY_FIELD_LABELS[field] for field in cast(tuple[object, ...], error.fields)
        if isinstance(field, str) and field in _HISTORY_FIELD_LABELS
    ))
    bounds = (
        f"取数区间 {error.start.isoformat()} 至 {error.end.isoformat()}："
        if type(error.start) is date and type(error.end) is date and error.start <= error.end
        else "本次历史行情取数："
    )
    message = bounds + (
        "未取得" + "、".join(fields) + "的完整历史数据。" if fields
        else "尚未取得本次所需的完整历史行情字段。"
    )
    return (ErrorDetail(location="market_history", type="backtest_data_missing", message=message),)


def _numeric_failure_context(
    strategy: StrategySpec, error: SkillNumericHistoryError,
) -> tuple[tuple[ErrorDetail, ...], dict[str, str]]:
    locations = _condition_failure_details(
        strategy, indicator_id=error.indicator_id,
        condition_path=error.condition_path, warmup=True,
    )
    source = dict(_source_indicator_locations(strategy))
    details: list[ErrorDetail] = []
    metadata: dict[str, str] = {}
    for location in locations:
        path = location.location
        if path is None:
            continue
        leaf = source.get(path)
        if leaf is None:
            continue
        queries = [value for key in ("metric_query", "left_metric_query", "right_metric_query")
                   if isinstance(value := leaf.params.get(key), str)]
        query = error.metric_query if error.metric_query in queries else " / ".join(queries)
        # Only bounded condition labels: no URL, control characters, arbitrary
        # exception fields or provider responses enter logs/public diagnostics.
        safe_label = r"[\w\u4e00-\u9fff ()（）%％./+-]{1,120}"
        query = query if re.fullmatch(safe_label, query) else "当前指标"
        unit = leaf.params.get("unit")
        unit = unit if isinstance(unit, str) and re.fullmatch(safe_label, unit) else None
        leg = "买入" if path.startswith("/entry") else "卖出"
        message = f"{leg}条件「{query}」：{error.message}"
        if unit is not None and error.code in {"unit_unconfirmed", "unit_mismatch"}:
            message += f" 当前条件使用的比较单位是「{unit}」，尚不能确认返回数值与它的对应关系。"
        details.append(ErrorDetail(
            location=path, type="backtest_condition_unavailable", message=message,
        ))
        metadata.update(condition_path=path, indicator_id=leaf.indicator_id,
                        metric_query=query)
        if unit is not None:
            metadata["comparison_unit"] = unit
    return tuple(details), metadata


async def preflight_backtest_strategy(
    *, strategy: StrategySpec, config: BacktestRunConfig, container: ApiContainer,
) -> BacktestRunConfig:
    """No run is created here. Original execution intent remains unchanged.

    Snapshot/non-preparable runtimes retain their existing submission path. The
    Skill service owns cache freshness and all data/indicator checks; this API
    boundary neither reimplements those checks nor requires a profitable signal.
    """
    prepare = getattr(container.backtest_submission, "prepare_candidate", None)
    if not callable(prepare):
        return config
    # Lazy import: the service also imports API result schemas during startup.
    from ashare_lab.application.skill_backtest_service import SkillCandidatePreparationError

    emit_progress("backtest_data_preparation", "正在检查回测区间、历史数据和指标是否可用。")
    failure: Exception | None = None
    try:
        try:
            async with asyncio.timeout(_TIMEOUT_SECONDS):
                for attempt in range(2):
                    try:
                        await cast(
                            Callable[[StrategySpec, BacktestRunConfig], Awaitable[object]], prepare,
                        )(strategy, config)
                        break
                    except MxSaasProviderUnavailableError as exc:
                        if attempt or exc.reason not in {"connect_timeout", "transport_error"}:
                            raise
                        # Only preparation is retried: no run exists yet. Reuse
                        # successful cached inputs, preserving the total deadline.
                        _LOGGER.info(
                            "backtest_preflight_recovery request_id=%s reason=%s attempt=1",
                            current_request_id(), exc.reason,
                        )
                        emit_progress(
                            "backtest_data_recovering",
                            "数据连接暂时中断，正在自动恢复；原策略不变，无需重复提交。",
                        )
                        await asyncio.sleep(1)
        except Exception as exc:
            # Capture only for the existing handlers below. Unknown exceptions
            # still propagate; do not log provider prose or change acceptance.
            failure = exc
            raise
    except MxDailyHistoryBeforeListingError as exc:
        problem = ApiProblem(
            status_code=422, code="skill_history_before_listing",
            available_start=strategy.backtest.start + (exc.listing_date - exc.start),
            message=(f"回测开始日期 {strategy.backtest.start.isoformat()} 早于该股票的上市日期 "
                     f"{exc.listing_date.isoformat()}。请选择上市日或之后的日期；本次未启动回测。"),
            details=(ErrorDetail(
                location="backtest.start", type="backtest_date_unavailable",
                message=(f"所选回测起点 {strategy.backtest.start.isoformat()} 早于已核实上市日期 "
                         f"{exc.listing_date.isoformat()}，该段没有上市后的交易历史。"),
            ),),
        )
    except GridSpecificationError as exc:
        problem = ApiProblem(
            status_code=422, code=exc.code, message=exc.safe_message,
            details=(ErrorDetail(location="trading_plan.parameters", type=exc.code,
                                 message=exc.safe_message),),
        )
    except MinuteGridCapabilityError as exc:
        code, message = execution_capability_feedback(str(exc))
        problem = ApiProblem(status_code=422, code=code, message=message)
    except SkillCandidatePreparationError as exc:
        if exc.code == "skill_indicator_history_not_ready":
            leg = "买入" if (exc.condition_path or "").startswith("entry.") else (
                "卖出" if (exc.condition_path or "").startswith("exit.") else "策略"
            )
            problem = ApiProblem(
                status_code=422, code=exc.code,
                message=(f"本次区间内，{leg}条件的指标还没有完成预热，或没有可用于后续交易日"
                         "执行的有效历史值。请延长回测区间或缩短指标周期；本次未启动回测。"),
                details=_condition_failure_details(
                    strategy, indicator_id=exc.indicator_id,
                    condition_path=exc.condition_path, warmup=True,
                ),
            )
        elif exc.code == "skill_history_no_execution_session":
            problem = ApiProblem(
                status_code=422, code=exc.code,
                message="本次区间没有足够交易日用于收盘判断并在后续交易日尝试成交。"
                        "请扩大回测区间；本次未启动回测。",
            )
        else:
            problem = _data_unavailable()
    except SkillNumericHistoryError as exc:
        permanent = exc.code in {"invalid_condition", "unit_mismatch", "non_daily_history"}
        details, _ = _numeric_failure_context(strategy, exc)
        problem = ApiProblem(
            status_code=422 if permanent else 503,
            code=f"skill_numeric_{exc.code}",
            message=exc.message + " 本次未启动回测。",
            details=details,
        )
    except UnsupportedSkillIndicatorError as exc:
        problem = ApiProblem(
            status_code=422, code="skill_indicator_unavailable",
            message="指标条件已识别，所需历史指标数据尚未接通。原规则已保留，本次未启动回测。",
            details=_condition_failure_details(strategy, indicator_id=exc.indicator_id),
        )
    except BacktestDateRangeError:
        problem = ApiProblem(
            status_code=422, code="backtest_date_range_invalid",
            message="回测日期范围无效，请选择 1990 年及以后的日期，且开始不能晚于结束。"
                    "本次未启动回测。",
        )
    except BacktestDataNotYetAvailableError:
        available_end = getattr(container.backtest_submission, "available_data_end", None)
        latest = available_end() if callable(available_end) else None
        message = (
            f"当前行情已更新至{latest.isoformat()}，请将回测结束日期改为这一天或更早，"
            "也可以等行情更新后再试。买卖规则已保留。"
            if isinstance(latest, date) else
            "所选结束日期的行情还未更新，请选择更早的日期，或稍后再试。买卖规则已保留。"
        )
        problem = ApiProblem(
            status_code=422, code="backtest_data_not_yet_available",
            message=message,
        )
    except (EventDataUnavailableError, FinancialDataUnavailableError):
        problem = ApiProblem(
            status_code=422, code="backtest_condition_data_unavailable",
            message="事件或财务条件已识别，所需历史数据尚未齐备。原规则已保留，本次未启动回测。",
        )
    except MinuteReplayCoverageError as exc:
        problem = ApiProblem(status_code=422, code="backtest_data_range_unavailable",
            available_start=exc.available_start, available_end=exc.available_end,
            message="当前已读取的分钟数据未覆盖原回测区间；原规则和日期已保留，尚未执行回测。")
    except TimeoutError:
        problem = ApiProblem(
            status_code=503, code="backtest_data_preparation_timeout",
            message="这次没能取到回测所需的行情，暂时无法计算结果。你的策略和设置已保留，可以稍后重试。",
        )
    except MxDailyHistoryFieldsMissingError as exc:
        problem = ApiProblem(
            status_code=503, code="skill_history_fields_missing",
            message="数据源没有返回本次所需的完整历史行情字段，暂时不能开始回测。"
                    "请稍后重新读取；已保存的策略不变。",
            details=_missing_history_details(exc),
        )
    except MxDailyHistoryError as exc:
        # Record the failing validation location, not raw provider data or query.
        origin = exc.__traceback__
        while origin is not None and origin.tb_next is not None:
            origin = origin.tb_next
        _LOGGER.warning(
            "history_validation_failed request_id=%s validator=%s line=%s",
            current_request_id(),
            origin.tb_frame.f_code.co_name if origin else "unknown",
            origin.tb_lineno if origin else 0,
        )
        problem = ApiProblem(
            status_code=503, code="skill_history_incomplete",
            message="历史行情的日期或数据不完整，暂时不能开始回测。"
                    "请稍后重新读取；已保存的策略不变。",
        )
    except (MxSaasProviderError, OSError) as exc:
        problem = _data_unavailable(exc)
    except ValueError as exc:
        # The service also raises ValueError for mismatched upstream fields.
        # Do not mislabel these as the user's language/strategy parse failure.
        problem = _data_unavailable(exc)
    else:
        emit_progress("backtest_data_ready", "历史数据初步检查已完成，启动回测时还会核对所需行情和执行条件。")
        # Submission marks these inputs as prepared internally. The worker can
        # reuse them without erasing the user's refresh request from its audit.
        return config
    _LOGGER.info(
        "backtest_preflight request_id=%s symbol=%s start=%s end=%s result=%s failure=%s",
        current_request_id(), strategy.instrument.symbol,
        strategy.backtest.start, strategy.backtest.end, problem.code,
        json.dumps(_safe_failure_metadata(failure, strategy, problem.code), sort_keys=True),
    )
    raise problem from None


def _safe_failure_metadata(
    failure: Exception | None, strategy: StrategySpec, problem_code: str,
) -> dict[str, str | int]:
    """Only application-owned labels/IDs; never exception messages or payloads."""
    metadata: dict[str, str | int] = {"error_class": type(failure).__name__}
    if isinstance(failure, MinuteReplayDataError) and str(failure) in _EXECUTION_DATA_REASONS:
        metadata["data_reason"] = str(failure)
    if isinstance(failure, MxSaasProviderError):
        if failure.reason in get_args(MxFailureReason):
            metadata["reason"] = cast(str, failure.reason)
        if failure.tool in get_args(MxTool):
            metadata["tool"] = failure.tool
        if isinstance(failure.call_id, str) and _SAFE_CALL_ID.fullmatch(failure.call_id):
            metadata["call_id"] = failure.call_id
        if type(failure.http_status) is int and 100 <= failure.http_status <= 599:
            metadata["http_status"] = failure.http_status
        if type(failure.attempts) is int and 1 <= failure.attempts <= 100:
            metadata["attempts"] = failure.attempts
    if isinstance(failure, MxSaasProviderDataError):
        reason = failure.data_reason
        if reason in _SAFE_DATA_REASONS:
            metadata["data_reason"] = reason
    if isinstance(failure, OSError) and type(failure.errno) is int:
        metadata["errno"] = failure.errno
    if isinstance(failure, SkillNumericHistoryError):
        _, condition_metadata = _numeric_failure_context(strategy, failure)
        metadata.update(condition_metadata)
    indicator_id = getattr(failure, "indicator_id", None)
    if isinstance(indicator_id, str) and indicator_id in {
        leaf.indicator_id for leaf in iter_indicator_conditions(strategy)
    }:
        metadata["indicator_id"] = indicator_id
    code = getattr(failure, "code", None)
    if isinstance(code, str) and (code == problem_code or f"skill_numeric_{code}" == problem_code):
        metadata["error_code"] = code
    return metadata


async def resolve_latest_grid_quote(
    params: GridParameters, symbol: str, provider: object,
) -> GridParameters:
    """Share the identified current-quote lookup across direct and idea routes."""
    query = getattr(provider, "query_finance", None)
    if not callable(query):
        raise MissingLatestGridQuoteError("latest quote provider unavailable")
    try:
        data = await asyncio.wait_for(query(
            query=f"查询{symbol}行情最新价（元），只要当前行情快照，不要历史序列。",
            indicators="最新价",
        ), timeout=45)
        return bind_latest_grid_anchor(params, symbol, data)
    except (MissingLatestGridQuoteError, MxSaasProviderNoDataError,
            MxSaasProviderUnavailableError):
        # A successful searchData response can still contain historical data.
        # The current screener is a second source, never a close-price fallback.
        screen = getattr(provider, "screen", None)
        if not callable(screen):
            raise
        emit_progress("grid_latest_quote_retry",
                      "查数结果未取得行情最新价，正在通过东方财富选股通道核对报价。")
        quote = await asyncio.wait_for(screen(
            query=f"{symbol}行情最新价", asset_type="A股",
        ), timeout=45)
        return bind_latest_grid_anchor(params, symbol, quote)


async def preflight_ready_outcome(
    *, outcome: CompileOutcome, container: ApiContainer,
    request: CompileInput | None = None,
) -> CompileOutcome:
    if outcome.status is not CompileStatus.READY or outcome.strategy is None:
        return outcome
    try:
        plan = outcome.strategy.trading_plan
        if isinstance(plan, GridPlan) and plan.parameters.anchor_mode == "previous_close":
            resolver = getattr(container.backtest_submission, "resolve_grid_anchor", None)
            if resolver is None:
                raise ApiProblem(status_code=503, code="grid_previous_close_unavailable",
                    message="历史昨收价解析服务不可用，原策略已保留。")
            try:
                strategy = await resolver(outcome.strategy)
            except (ValueError, RuntimeError, OSError) as exc:
                origin = exc.__traceback__
                while origin is not None and origin.tb_next is not None:
                    origin = origin.tb_next
                _LOGGER.warning(
                    "grid_anchor_preparation_failed request_id=%s exception_class=%s validator=%s line=%s",
                    current_request_id(), type(exc).__name__,
                    origin.tb_frame.f_code.co_name if origin else "unknown",
                    origin.tb_lineno if origin else 0,
                )
                if isinstance(exc, MxDailyHistoryError):
                    raise ApiProblem(status_code=503, code="skill_history_incomplete",
                        message="历史行情的交易状态或数据尚未通过校验，暂时不能开始回测；原策略已保留。") from exc
                raise ApiProblem(status_code=503, code="grid_previous_close_unavailable",
                    message="起始日昨收价尚未取得，不会使用最新价或开盘价替代。") from exc
            outcome = replace(outcome, strategy=strategy, strategy_hash=canonical_hash(strategy))
            plan = strategy.trading_plan
        if isinstance(plan, GridPlan) and plan.parameters.anchor_mode == "latest_price" and (
            plan.parameters.anchor_price is None
            or plan.parameters.anchor_quote_response_sha256 is None
        ):
            emit_progress("grid_latest_quote", "正在读取行情最新价并固定网格基准，保留原买卖间距。")
            provider = container.live_finance_data
            try:
                symbol = outcome.strategy.instrument.symbol
                params = await resolve_latest_grid_quote(plan.parameters, symbol, provider)
            except (ValueError, MxSaasProviderError, OSError, TimeoutError) as exc:
                _LOGGER.info("grid_latest_quote_unavailable error_class=%s", type(exc).__name__)
                raise ApiProblem(status_code=503, code="grid_latest_quote_unavailable",
                    message="行情最新价暂未取得，最新价基准和原买卖规则已保留；不会改用起始开盘价。") from None
            strategy = outcome.strategy.model_copy(update={
                "trading_plan": plan.model_copy(update={"parameters": params}),
            })
            outcome = replace(outcome, strategy=strategy, strategy_hash=canonical_hash(strategy))
        await preflight_backtest_strategy(
            strategy=outcome.strategy, container=container,
            config=BacktestRunConfig(
                **resolve_execution_settings(outcome.execution_settings).model_dump(
                    exclude_none=True,
                ),
                # A READY-card check is not submission of the refresh request.
                refresh_data=False,
            ),
        )
    except ApiProblem as exc:
        if exc.code in {"backtest_data_not_yet_available", "skill_history_before_listing",
                        "backtest_data_range_unavailable"}:
            source = getattr(container, "backtest_submission", None)
            boundary = getattr(source, "available_data_end", None)
            latest = boundary() if callable(boundary) else None
            if exc.available_end is not None:
                latest = min(latest, exc.available_end) if isinstance(latest, date) else exc.available_end
            original = outcome.strategy
            proposed_start = (max(original.backtest.start, exc.available_start)
                              if original is not None and exc.available_start else
                              original.backtest.start if original is not None else None)
            proposed_end = (min(original.backtest.end, latest)
                            if original is not None and isinstance(latest, date) else
                            original.backtest.end if original is not None else None)
            if (original is not None and proposed_start is not None and proposed_end is not None
                    and proposed_start <= proposed_end
                    and (proposed_start, proposed_end) != (original.backtest.start, original.backtest.end)):
                proposed = original.model_copy(update={"backtest": original.backtest.model_copy(
                    update={"start": proposed_start, "end": proposed_end})})
                try:
                    # A global import watermark alone is NOT coverage evidence.
                    # Verify this stock and every dependency over the proposed range.
                    await preflight_backtest_strategy(strategy=proposed, container=container,
                        config=BacktestRunConfig(**resolve_execution_settings(
                            outcome.execution_settings).model_dump(exclude_none=True), refresh_data=False))
                except ApiProblem:
                    pass  # Never offer an unverified smaller range.
                else:
                    return replace(outcome, status=CompileStatus.NEEDS_CLARIFICATION,
                        strategy=None, strategy_hash=None, run_requested=False, refresh_data=False,
                        revision_base_strategy=original,
                        suggested_strategy=proposed, suggested_strategy_hash=canonical_hash(proposed),
                        suggested_strategy_choice_id=None,
                        suggested_strategy_note="建议范围已预填；原范围保留，接受后才保存新范围。本次未启动回测。",
                        diagnostic_code="backtest_range_confirmation_required",
                        clarification=(f"这条策略目前已核实可用的数据范围为{proposed.backtest.start}至{proposed_end}。"
                            f"原回测范围为{original.backtest.start}至{original.backtest.end}；"
                            f"已为你预填建议范围{proposed.backtest.start}至{proposed_end}，是否接受？"
                            "买卖条件不变，确认前不会启动回测。"))
        if exc.code in {"minute_execution_unavailable", "grid_execution_unavailable",
                        "backtest_data_not_yet_available", "backtest_date_range_invalid"}:
            # Interpretation and editing remain valid without a running executor.
            # /prepare and /backtest-runs still enforce the actual execution gate.
            emit_progress("execution_unavailable", exc.message)
            suggestion = (outcome.clarification + " " if outcome.clarification
                          and outcome.clarification.startswith("已为你补充") else "")
            return replace(outcome, run_requested=False,
                           diagnostic_code=exc.code, clarification=suggestion + exc.message)
        search_codes = {
            "skill_numeric_history_unavailable_after_query_retry",
            "skill_numeric_invalid_history", "skill_numeric_non_daily_history",
            "backtest_condition_data_unavailable", "skill_indicator_unavailable",
        }
        recoverable = exc.code.startswith("skill_numeric_") or exc.code in search_codes | {
            "grid_geometry_conflict", "grid_nonpositive_level", "grid_parameters_invalid",
            "grid_parameters_unavailable", "execution_rule_unavailable",
            "grid_previous_close_unavailable",
            "grid_latest_quote_unavailable",
            "backtest_data_temporarily_unavailable", "backtest_data_preparation_timeout",
            "skill_history_fields_missing", "skill_history_incomplete",
            "skill_indicator_history_not_ready",
        }
        if request is None or not recoverable:
            raise
        # Numeric history failures need dated, typed observations. Public news
        # cannot repair that contract and must not replace a precise data error.
        if exc.code in search_codes and not exc.code.startswith("skill_numeric_"):
            skill_context = None
            if exc.code == "backtest_condition_data_unavailable" and container.live_finance_data:
                emit_progress("skill_condition_lookup", "正在查询原条件对应的事件或财务数据。")
                provider = container.live_finance_data
                lookup = getattr(provider, "query_current_finance", provider.query_finance)
                event_conditions = tuple(iter_event_conditions(outcome.strategy))
                event_names = [
                    definition.name_zh for condition in event_conditions
                    if (definition := container.coverage_catalog.resolve_event(
                        condition.event_code,
                    )) is not None
                ]
                indicators = (
                    "、".join(event_names) + "明细，公告日期、公告主体、事件方向和事件属性"
                    if event_names else None
                )
                query = (
                    f"查询{outcome.strategy.instrument.symbol}从"
                    f"{outcome.strategy.backtest.start}至{outcome.strategy.backtest.end}的"
                    f"{indicators}。返回明细记录，不要股价行情。"
                    if indicators else
                    f"查询{outcome.strategy.instrument.symbol}以下策略涉及的历史数据，"
                    f"不是执行交易：{request.utterance}。"
                    f"区间{outcome.strategy.backtest.start}至{outcome.strategy.backtest.end}；"
                    "返回原始日期、主体和字段。"
                )
                try:
                    data = await asyncio.wait_for(lookup(
                        query=query, indicators=indicators,
                    ), timeout=45.0)
                    table_text = json.dumps(data.tables, ensure_ascii=False, default=str)
                    event_content = not event_conditions or any(
                        term in table_text for term in ("公告", "event_tables", *event_names)
                    )
                    if data.tables and event_content:
                        encoded = json.dumps(asdict(data), ensure_ascii=False, default=str)
                        skill_context = encoded[:24_000]
                        if len(encoded) > 24_000:
                            skill_context += "\n[展示已截断，不得声称覆盖完整历史]"
                except Exception as lookup_error:
                    _LOGGER.info("condition Skill lookup unavailable type=%s",
                                 type(lookup_error).__name__)
            emit_progress(
                "research_fallback",
                "已取得相关数据，正在解释适用范围。" if skill_context else
                "所需历史数据尚未准备好，正在联网检索相关信息。",
            )
            try:
                recovered = await container.compiler.research_data_gap(
                    request=request, outcome=outcome,
                    **({"skill_data_context": skill_context} if skill_context else {}),
                )
            except Exception:
                recovered = None
            if recovered is not None:
                return replace(
                    recovered, revision_base_strategy=outcome.strategy,
                    pending_edit_inputs=(), edit_clarification_options=(),
                    pending_edit_run_requested=False, pending_edit_refresh_data=False,
                )
        # Parsing/review already accepted these rules. A preparation failure
        # must create the new revision, not throw away the answer and revive an
        # older pending question. Execution remains gated by normal preflight.
        reason = " ".join(detail.message for detail in exc.details) or exc.message
        message = "股票和买卖规则已识别并保留。" + reason
        if exc.details:
            message += " 本次未启动回测，无需重复确认已知条件。"
        emit_progress("backtest_data_not_ready", message)
        return replace(
            outcome, status=CompileStatus.NEEDS_CLARIFICATION,
            strategy=None, strategy_hash=None, revision_base_strategy=outcome.strategy,
            diagnostic_code=exc.code, clarification=message,
            idea_route=None, run_requested=False, refresh_data=False,
            pending_edit_inputs=(), edit_clarification_options=(),
            pending_edit_run_requested=False, pending_edit_refresh_data=False,
        )
    return outcome


def _data_unavailable(failure: Exception | None = None) -> ApiProblem:
    reason = "这次没能完成回测。"
    if isinstance(failure, MinuteReplayDataError):
        reason = _EXECUTION_DATA_REASONS.get(str(failure), reason)
    elif isinstance(failure, MxSaasProviderNoDataError):
        reason = "暂时没有这段时间的完整行情，无法计算回测结果。"
    elif isinstance(failure, MxSaasProviderDataError):
        issue = {
            "data_unit_unconfirmed": "有些指标的单位还无法确认",
            "data_security_mismatch": "取到的行情与所选股票不符",
            "data_fields_missing": "所需行情或指标有缺失",
            "data_field_binding_mismatch": "取到的指标与策略要求不符",
            "data_dates_mismatch": "取到的行情日期与所选区间不符",
            "data_history_unavailable": "这段时间的行情还不完整",
            "data_values_invalid": "取到的行情或指标存在异常数值",
        }.get(failure.data_reason, "取到的行情或指标还无法用于计算")
        reason = f"{issue}，这次无法生成回测结果。"
    elif isinstance(failure, MxSaasProviderError):
        reason = "这次没能取到回测所需的行情，暂时无法计算结果。"
    # Provider codes/tool names remain in structured diagnostics and logs.
    return ApiProblem(
        status_code=503, code="backtest_data_temporarily_unavailable",
        message=reason + " 你的策略和设置已保留，可以稍后重试。",
    )
