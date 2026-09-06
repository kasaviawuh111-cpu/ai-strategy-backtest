"""Queue first, then load Eastmoney Skill data and run the research simulator.

No snapshot preparation subprocess, alternate quote source, or corporate-action
ledger participates in this path. The existing run store and HTTP result contract
are reused; one result digest identifies the report presented to the review model.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable
from dataclasses import fields, replace
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import ClassVar, TypeVar, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

from ashare_lab.adapters.jobs.threaded import ThreadBacktestJobQueue
from ashare_lab.adapters.market_data.mx_daily_history import (
    MxDailyHistory,
    MxDailyHistoryBeforeListingError,
    MxDailyHistoryClient,
    MxDailyHistoryError,
    MxDailyHistoryFieldsMissingError,
)
from ashare_lab.adapters.market_data.mx_indicator_contract import (
    ACTIVE_SKILL_INDICATORS,
    UNSUPPORTED_SKILL_INDICATOR_REASONS,
    UnsupportedSkillIndicatorError,
    build_indicator_contract,
)
from ashare_lab.adapters.market_data.mx_saas import (
    MxRetryProgress,
    MxSaasProviderAuthError,
    MxSaasProviderError,
    MxSaasProviderNoDataError,
    observe_mx_retries,
)
from ashare_lab.adapters.market_data.provider_indicator_cache import (
    FileCachedHistoricalIndicatorData,
)
from ashare_lab.adapters.persistence.backtest_runs import BacktestRunConflictError
from ashare_lab.api.result_schemas import BacktestResultBundle
from ashare_lab.application.backtest_submission import (
    BacktestDataNotYetAvailableError,
    BacktestRunConfig,
    EventDataUnavailableError,
    FinancialDataUnavailableError,
    _effective_warmup_calendar_days,  # pyright: ignore[reportPrivateUsage]
    latest_stable_a_share_data_date,
    validate_a_share_backtest_range,
)
from ashare_lab.application.result_views import (
    RESULT_HASH_SCHEMA_VERSION,
    calculate_result_bundle_hash,
)
from ashare_lab.application.skill_backtest import SkillBacktestResult, run_skill_backtest
from ashare_lab.domain.execution import CapacityMode, LimitHandling
from ashare_lab.domain.market_data import DailyBar, PriceBasis
from ashare_lab.domain.shared import InstrumentId, Price, Quantity, RunId
from ashare_lab.domain.signals import (
    SignalEvidence,
    SignalFact,
    binding_for_provider_condition,
    provider_condition_leaves,
)
from ashare_lab.domain.signals.provider_runtime import (
    combine_condition_timelines,
    evaluate_provider_indicator_aligned,
)
from ashare_lab.domain.signals.runtime import SignalRuntime
from ashare_lab.domain.strategy import (
    AnyCondition,
    HoldingPeriodExit,
    PositionReturnExit,
    StrategySpec,
    TrailingDrawdownExit,
    canonical_hash,
    canonical_json,
    strategy_requires_events,
    strategy_requires_financials,
)
from ashare_lab.domain.strategy.models import Condition, IndicatorCondition
from ashare_lab.ports.backtest_runs import (
    BacktestJobState,
    BacktestQueueFullError,
    BacktestRunRecord,
    BacktestRunStore,
    CreateRunResult,
)
from ashare_lab.ports.provider_indicator_data import (
    HistoricalIndicatorData,
    ProviderIndicatorSeries,
)

logger = logging.getLogger(__name__)
_T = TypeVar("_T")
_PROFILE = "eastmoney_skill_research.v1"
# Explicit formulas over Skill OHLCV, NOT provider-supplied indicator fields.
# Keep the provider contract strict; do not use this as a generic fallback.
SKILL_DERIVED_INDICATORS = {
    "technical.ma": "N个有效交易日价格的简单均值（含当日）；穿越比较前后两日价格与均线",
    "price.rolling_high": "当前价格 > 前N个有效交易日最高价格（不含当日）",
    "volume.relative": "当日成交量 / 前N个有效交易日平均成交量（不含当日）",
}


class _Cancelled(Exception):
    pass


class SkillBacktestService:
    available_indicator_ids = ACTIVE_SKILL_INDICATORS | SKILL_DERIVED_INDICATORS.keys()
    indicator_unavailable_reasons: ClassVar[dict[str, str]] = {
        key: value
        for key, value in UNSUPPORTED_SKILL_INDICATOR_REASONS.items()
        if key not in SKILL_DERIVED_INDICATORS
    }

    def __init__(
        self,
        *,
        history: MxDailyHistoryClient,
        indicators: HistoricalIndicatorData,
        store: BacktestRunStore,
        max_workers: int = 1,
        max_pending: int | None = None,
    ) -> None:
        self.history = history
        self.indicators = indicators
        self.store = store
        self.queue = ThreadBacktestJobQueue(
            self.execute,
            max_workers=max_workers,
            max_pending=max_pending,
        )

    def shutdown(self) -> None:
        self.queue.shutdown()

    def submit(self, strategy: StrategySpec, config: BacktestRunConfig) -> CreateRunResult:
        validate_a_share_backtest_range(strategy.backtest.start, strategy.backtest.end)
        # Do not silently reinterpret an unsupported fundamental/event condition
        # as a technical strategy. Such histories still need their Skill adapter.
        if strategy_requires_events(strategy):
            raise EventDataUnavailableError("eastmoney_skill_event_history_not_connected")
        if strategy_requires_financials(strategy):
            raise FinancialDataUnavailableError("eastmoney_skill_financial_history_not_connected")
        now = datetime.now(UTC)
        if strategy.backtest.end > latest_stable_a_share_data_date(now):
            raise BacktestDataNotYetAvailableError("backtest end exceeds completed daily data")
        for condition in (strategy.entry, _exit_condition(strategy)):
            if condition is not None:
                for _, leaf in provider_condition_leaves(condition):
                    binding = binding_for_provider_condition(leaf)
                    if leaf.indicator_id in SKILL_DERIVED_INDICATORS:
                        continue
                    try:
                        build_indicator_contract(
                            leaf.indicator_id, binding.provider_indicator_name, binding.value_names
                        )
                    except UnsupportedSkillIndicatorError:
                        raise
                    except ValueError as exc:
                        raise UnsupportedSkillIndicatorError(
                            leaf.indicator_id, "该指标的请求参数尚未对应到已验证的 Skill 字段。"
                        ) from exc
        config_payload = {field.name: getattr(config, field.name) for field in fields(config)}
        run_id = RunId(f"run:{uuid4().hex}")
        record = BacktestRunRecord(
            run_id=run_id,
            fingerprint=canonical_hash(
                {
                    "profile": _PROFILE,
                    "run_id": str(run_id),
                    "strategy": strategy.model_dump(mode="json"),
                    "config": json.loads(json.dumps(config_payload, default=str)),
                }
            ),
            strategy_json=canonical_json(strategy),
            config_json=json.dumps(config_payload, default=str),
            manifest_json=json.dumps(
                {"profile": _PROFILE, "provider": "eastmoney_mx_finance_data"}
            ),
            state=BacktestJobState.QUEUED,
            progress_percent=0,
            progress_label="已排队，等待东方财富 Skill 取数",
            created_at=now,
            updated_at=now,
        )
        created = self.store.create_or_get(record)
        if not created.replayed:
            try:
                self.queue.enqueue(run_id)
            except Exception as exc:
                queue_full = isinstance(exc, BacktestQueueFullError)
                self.store.transition(
                    run_id,
                    expected=(BacktestJobState.QUEUED,),
                    target=BacktestJobState.FAILED,
                    progress_percent=0,
                    progress_label="回测队列已满，请稍后重试" if queue_full else "回测排队失败",
                    error_code="backtest_queue_full" if queue_full else "queue_unavailable",
                )
                raise
        return created

    def execute(self, run_id: RunId) -> BacktestRunRecord:
        record = self._get(run_id)
        if record.state.is_terminal:
            return record
        try:
            self._stage(run_id, BacktestJobState.RUNNING_DATA, 10, "东方财富 Skill 获取历史数据")
            strategy = StrategySpec.model_validate_json(record.strategy_json)
            config = _config_from_json(record.config_json)
            warmup = _effective_warmup_calendar_days(strategy, config)
            history = asyncio.run(self._load_history(run_id, strategy, config, warmup))
            self._stage(
                run_id, BacktestJobState.RUNNING_SIGNAL, 45, "获取东方财富指标并判断策略条件"
            )
            entry, exit_signals, indicator_series = asyncio.run(
                self._with_retry_progress(
                    run_id, self._signals(strategy, history, force_refresh=config.refresh_data)
                )
            )
            self._stage(run_id, BacktestJobState.RUNNING_EXECUTION, 70, "模拟交易并计算收益")
            result = run_skill_backtest(
                strategy=strategy,
                history=history,
                entry_timeline=entry,
                exit_timeline=exit_signals,
                config=config,
            )
            stressed = None
            if config.run_robustness:
                stressed = run_skill_backtest(
                    strategy=strategy,
                    history=history,
                    entry_timeline=entry,
                    exit_timeline=exit_signals,
                    config=replace(
                        config,
                        slippage_bps=min(
                            Decimal("1000"),
                            max(
                                config.slippage_bps * 2,
                                config.slippage_bps + 5,
                            ),
                        ),
                        run_robustness=False,
                    ),
                )
            self._stage(
                run_id, BacktestJobState.RUNNING_REPORT, 95, "生成可供真实模型分析的回测结果"
            )
            bundle = _result_bundle(
                run_id, strategy, config, history, indicator_series, result, stressed
            )
            return self._stage(
                run_id,
                BacktestJobState.SUCCEEDED,
                100,
                "回测完成，可请求 AI 分析与优化",
                result_json=bundle.model_dump_json(by_alias=True),
            )
        except _Cancelled:
            return self.store.transition(
                run_id,
                expected=(BacktestJobState.CANCEL_REQUESTED,),
                target=BacktestJobState.CANCELLED,
                progress_percent=self._get(run_id).progress_percent,
                progress_label="已取消",
            )
        except Exception as exc:
            current = self._get(run_id)
            if current.state.is_terminal:
                return current
            # Provider exception messages can contain request metadata; do not
            # expose them or credentials in HTTP errors or logs.
            error_code = f"skill_{type(exc).__name__}"[:128]
            progress_label = f"{current.progress_label}失败：{type(exc).__name__}"
            if isinstance(exc, MxSaasProviderError):
                tool = exc.tool if exc.tool in {"selectSecurity", "searchData"} else "unknown"
                reason = (
                    exc.reason
                    if exc.reason
                    in {
                        "read_timeout",
                        "connect_timeout",
                        "transport_error",
                        "http_error",
                    }
                    else None
                )
                status = exc.http_status
                if type(status) is not int or not 100 <= status <= 599:
                    status = None
                source = {
                    "selectSecurity": "东方财富选股 Skill",
                    "searchData": "东方财富查数 Skill",
                }.get(
                    tool,
                    (
                        "东方财富查数 Skill"
                        if "获取东方财富指标" in current.progress_label
                        else "东方财富选股/查数流程"
                    ),
                )
                if isinstance(exc, MxSaasProviderAuthError):
                    error_code = "skill_mx_auth_failed"
                    progress_label = f"{source}授权失败，本次取数未完成。"
                elif reason == "read_timeout":
                    error_code = "skill_mx_read_timeout"
                    progress_label = f"等待{source}响应超时，本次取数未完成，可以重试。"
                elif reason == "connect_timeout":
                    error_code = "skill_mx_connect_timeout"
                    progress_label = f"连接{source}超时，本次取数未完成，可以重试。"
                elif reason == "transport_error":
                    error_code = "skill_mx_transport_error"
                    progress_label = f"与{source}的连接未完成或中断，本次取数未完成，可以重试。"
                elif reason == "http_error":
                    error_code = "skill_mx_http_error"
                    status_label = f"（HTTP {status}）" if status is not None else ""
                    failure = "请求暂时受限" if status == 429 else "返回服务异常"
                    progress_label = (
                        f"{source}{failure}{status_label}，本次取数未完成，请稍后重试。"
                    )
                elif isinstance(exc, MxSaasProviderNoDataError):
                    error_code = "skill_mx_no_data"
                    progress_label = f"{source}未返回本次回测所需的数据。"
                else:
                    # Keep legacy/unclassified provider errors on their existing
                    # UI mapping; do not manufacture a precise transport cause.
                    error_code = f"skill_{type(exc).__name__}"[:128]
                logger.warning(
                    "Skill backtest failed: run=%s tool=%s reason=%s http_status=%s code=%s "
                    "transport_kind=%s attempts=%s call_id=%s",
                    run_id,
                    tool,
                    reason,
                    status,
                    error_code,
                    exc.transport_kind,
                    exc.attempts,
                    exc.call_id,
                )
            else:
                logger.warning(
                    "Skill backtest failed: run=%s exception=%s",
                    run_id,
                    type(exc).__name__,
                )
            if isinstance(exc, MxDailyHistoryFieldsMissingError):
                # These names come only from the adapter's requested field list,
                # never from provider prose or request/authorization metadata.
                error_code = "skill_history_fields_missing"
                fetch_range = (
                    f"{exc.start.isoformat()} 至 {exc.end.isoformat()}"
                    if exc.start is not None and exc.end is not None
                    else "本次区间"
                )
                progress_label = (
                    f"查询 {fetch_range} 的历史数据时，东方财富未返回"
                    + "、".join(exc.fields)
                    + "。本次回测未完成，原区间和规则已保留；可修改区间或稍后重新读取。"
                )
            elif isinstance(exc, MxDailyHistoryBeforeListingError):
                error_code = "skill_history_before_listing"
                progress_label = (
                    f"这只股票于 {exc.listing_date.isoformat()} 上市，"
                    f"你选择的区间从 {strategy.backtest.start.isoformat()} 开始，包含上市前日期。"
                    "请修改回测区间；买卖规则和成交设置已保留，不会自动缩短区间。"
                )
            elif isinstance(exc, MxDailyHistoryError):
                # Only fixed, application-owned messages are classified. Never
                # expose provider text, row values or a raw exception payload.
                date_errors = {
                    "MX session, raw and adjusted daily dates must align one-to-one",
                    "MX provider limits must cover every trading session and no suspended session",
                    "MX history response contains duplicate dates",
                    "MX history response contains dates outside the request",
                    "MX history response has ambiguous historical date axes",
                    "MX history chunk boundary coverage is unconfirmed",
                    "MX history chunks omitted the overlap session",
                    "MX history chunks disagree on an overlap session",
                }
                error_code = (
                    "skill_history_dates_mismatch" if str(exc) in date_errors
                    else "skill_history_validation_failed"
                )
                progress_label = (
                    "历史行情、复权价或涨跌停数据的日期未对齐，本次回测已停止。"
                    if error_code == "skill_history_dates_mismatch"
                    else "历史数据未通过完整性校验，本次回测已停止。"
                ) + "原规则和设置已保留；可以重新读取，或修改回测区间。"
            logger.warning("Skill failure classified: run=%s code=%s", run_id, error_code)
            return self.store.transition(
                run_id,
                expected=(current.state,),
                target=BacktestJobState.FAILED,
                progress_percent=current.progress_percent,
                progress_label=progress_label,
                error_code=error_code,
            )

    async def _load_history(
        self, run_id: RunId, strategy: StrategySpec, config: BacktestRunConfig, warmup: int,
    ) -> MxDailyHistory:
        try:
            return await self._with_retry_progress(run_id, self.history.load(
                instrument_id=strategy.instrument.symbol,
                start=strategy.backtest.start - timedelta(days=warmup),
                end=strategy.backtest.end,
                force_refresh=config.refresh_data,
            ))
        except MxDailyHistoryBeforeListingError as exc:
            # Only the technical warmup may be shortened, never the user's
            # requested backtest. Unknown/not-yet-ready indicator values remain
            # unknown and cannot generate an entry signal.
            if strategy.backtest.start < exc.listing_date:
                raise
            self._stage(run_id, BacktestJobState.RUNNING_DATA, 10,
                        "预热数据早于上市日，正在读取上市后数据；指标就绪后才判断信号，回测区间不变。")
            return await self._with_retry_progress(run_id, self.history.load(
                instrument_id=strategy.instrument.symbol,
                start=exc.listing_date,
                end=strategy.backtest.end,
                force_refresh=config.refresh_data,
            ))

    async def _with_retry_progress(self, run_id: RunId, request: Awaitable[_T]) -> _T:
        pending: set[str] = set()

        def update(event: MxRetryProgress) -> None:
            current = self._get(run_id)
            if current.state is BacktestJobState.CANCEL_REQUESTED:
                raise _Cancelled
            if current.state not in {
                BacktestJobState.RUNNING_DATA,
                BacktestJobState.RUNNING_SIGNAL,
            }:
                return
            if event.recovered:
                pending.discard(event.call_id)
                if pending:
                    return
                label = "数据请求已恢复，正在继续读取。"
            else:
                pending.add(event.call_id)
                if event.data_incomplete:
                    label = "历史数据缺少必要字段，正在补查一次；原方案已保留，无需重新提交。"
                else:
                    label = (
                        f"数据获取遇到临时问题，正在自动重试（{event.retry_number}/{event.max_retries}）。"
                        "原方案已保留，无需重新提交。"
                    )
            try:
                self.store.transition(
                    run_id,
                    expected=(current.state,),
                    target=current.state,
                    progress_percent=current.progress_percent,
                    progress_label=label,
                    expected_version=current.version,
                )
            except BacktestRunConflictError:
                # A simultaneous cancellation/state change wins over display-only
                # progress; never turn that race into a backtest failure.
                if self._get(run_id).state is BacktestJobState.CANCEL_REQUESTED:
                    raise _Cancelled from None

        with observe_mx_retries(update):
            return await request

    async def _signals(
        self,
        strategy: StrategySpec,
        history: MxDailyHistory,
        *,
        force_refresh: bool = False,
    ) -> tuple[
        tuple[SignalFact | None, ...],
        tuple[SignalFact | None, ...],
        tuple[ProviderIndicatorSeries, ...],
    ]:
        requests: dict[str, asyncio.Task[ProviderIndicatorSeries]] = {}
        gate = asyncio.Semaphore(2)
        session_dates = tuple(row.session_date for row in history.rows)

        async def fetch(condition: Condition | None) -> tuple[SignalFact | None, ...]:
            if condition is None:
                return (None,) * len(history.rows)
            timelines: dict[str, tuple[SignalFact | None, ...]] = {}
            for path, leaf in provider_condition_leaves(condition):
                if leaf.indicator_id in SKILL_DERIVED_INDICATORS:
                    timelines[path] = _skill_derived_timeline(leaf, history)
                    continue
                binding = binding_for_provider_condition(leaf)
                key = repr(
                    (leaf.indicator_id, binding.provider_indicator_name, binding.value_names)
                )
                if key not in requests:

                    async def load(
                        indicator_id: str = leaf.indicator_id,
                        name: str = binding.provider_indicator_name,
                        values: tuple[str, ...] = binding.value_names,
                    ) -> ProviderIndicatorSeries:
                        async with gate:
                            if isinstance(self.indicators, FileCachedHistoricalIndicatorData):
                                return await self.indicators.query_indicator_history(
                                    instrument_id=strategy.instrument.symbol,
                                    indicator_id=indicator_id,
                                    provider_indicator_name=name,
                                    value_names=values,
                                    start=history.start,
                                    end=history.end,
                                    force_refresh=force_refresh,
                                )
                            return await self.indicators.query_indicator_history(
                                instrument_id=strategy.instrument.symbol,
                                indicator_id=indicator_id,
                                provider_indicator_name=name,
                                value_names=values,
                                start=history.start,
                                end=history.end,
                            )

                    requests[key] = asyncio.create_task(load())
                series = await requests[key]
                if (
                    series.provider != "eastmoney_mx_finance_data"
                    or series.instrument_id != strategy.instrument.symbol
                ):
                    raise ValueError("unexpected indicator provider or instrument")
                timelines[path] = evaluate_provider_indicator_aligned(leaf, series, session_dates)
            return combine_condition_timelines(condition, timelines, session_dates)

        entry, exit_signals = await asyncio.gather(
            fetch(strategy.entry), fetch(_exit_condition(strategy))
        )
        return entry, exit_signals, tuple(task.result() for task in requests.values())

    def _get(self, run_id: RunId) -> BacktestRunRecord:
        record = self.store.get(run_id)
        if record is None:
            raise LookupError("unknown backtest run")
        return record

    def _stage(
        self,
        run_id: RunId,
        target: BacktestJobState,
        progress: int,
        label: str,
        *,
        result_json: str | None = None,
    ) -> BacktestRunRecord:
        current = self._get(run_id)
        if current.state is BacktestJobState.CANCEL_REQUESTED:
            raise _Cancelled
        return self.store.transition(
            run_id,
            expected=(current.state,),
            target=target,
            progress_percent=progress,
            progress_label=label,
            result_json=result_json,
        )


def _skill_derived_timeline(
    condition: IndicatorCondition,
    history: MxDailyHistory,
) -> tuple[SignalFact | None, ...]:
    """Reuse the engine's causal formulas on real Skill bars, with explicit provenance."""

    if condition.indicator_id not in SKILL_DERIVED_INDICATORS:
        raise ValueError("indicator is not enabled for Skill OHLCV calculation")
    bars = tuple(
        DailyBar(
            instrument_id=InstrumentId(history.instrument_id),
            session_date=row.session_date,
            open=Price(row.adjusted_open),
            high=Price(row.adjusted_high),
            low=Price(row.adjusted_low),
            close=Price(row.adjusted_close),
            volume=Quantity(row.volume),
            turnover=row.amount,
            available_at=datetime.combine(row.session_date, time(15), ZoneInfo("Asia/Shanghai")),
            price_basis=PriceBasis.BACK_ADJUSTED,
        )
        for row in history.rows
    )
    # available_at is the daily-close simulation clock, not a claim about the
    # provider's historical publication time. Actual retrieval is recorded separately.
    return tuple(
        None
        if fact is None
        else replace(
            fact,
            reason=f"local_formula_on_eastmoney_skill_ohlcv:{fact.reason}",
            evidence=(
                SignalEvidence(
                    evidence_type="skill_ohlcv_derived_indicator",
                    evidence_id=f"{history.instrument_id}:{fact.condition_ref}:{fact.session_date}",
                    available_at=fact.available_at,
                    provider=history.provider,
                    time_quality="daily_close_simulation",
                    timestamp_precision="date",
                    validation_status="local_formula_on_provider_ohlcv",
                ),
            ),
        )
        for fact in SignalRuntime().evaluate_aligned(condition, bars)
    )


def _exit_condition(strategy: StrategySpec) -> Condition | None:
    from ashare_lab.domain.strategy import AllCondition

    children = tuple(
        child
        for child in strategy.exit.children
        if not isinstance(
            child,
            (HoldingPeriodExit, PositionReturnExit, TrailingDrawdownExit),
        )
    )
    return (
        (
            children[0]
            if len(children) == 1
            else (
                AllCondition(children=children)
                if strategy.exit.op == "all"
                else AnyCondition(children=children)
            )
        )
        if children
        else None
    )


def _config_from_json(payload: str) -> BacktestRunConfig:
    values = json.loads(payload)
    for name in (
        "participation_rate",
        "slippage_bps",
        "allocation_ratio",
        "commission_rate",
        "minimum_commission_cny",
    ):
        values[name] = Decimal(values[name])
    values["capacity_mode"] = CapacityMode(values["capacity_mode"])
    values["limit_handling"] = LimitHandling(values["limit_handling"])
    return BacktestRunConfig(**values)


def _result_bundle(
    run_id: RunId,
    strategy: StrategySpec,
    config: BacktestRunConfig,
    history: MxDailyHistory,
    indicators: tuple[ProviderIndicatorSeries, ...],
    result: SkillBacktestResult,
    stressed: SkillBacktestResult | None,
) -> BacktestResultBundle:
    metrics = result.metrics
    initial = Decimal(str(strategy.backtest.initial_cash_cny))
    peak = initial
    curve: list[dict[str, object]] = []
    for point in result.equity_curve:
        peak = max(peak, point.equity)
        curve.append(
            {
                "date": point.session_date,
                "equity": float(point.equity / initial * 100),
                "benchmark": None
                if point.benchmark is None
                else float(point.benchmark / initial * 100),
                "drawdown": float(point.equity / peak - 1),
            }
        )
    warnings = list(dict.fromkeys((*result.assumptions, *result.limitations)))
    derived = [
        {
            "indicatorId": leaf.indicator_id,
            "definitionVersion": leaf.definition_version,
            "source": "local_formula_on_eastmoney_skill_ohlcv",
            "formula": SKILL_DERIVED_INDICATORS[leaf.indicator_id],
            "parameters": json.dumps(leaf.params, ensure_ascii=False, sort_keys=True),
            "priceBasis": "provider_back_adjusted",
            "volumeBasis": "provider_unadjusted_shares",
            "sessionPolicy": (
                "positive_volume_sessions_including_current"
                if leaf.indicator_id == "technical.ma"
                else "positive_volume_sessions_excluding_current"
            ),
        }
        for condition in (strategy.entry, _exit_condition(strategy))
        if condition is not None
        for _, leaf in provider_condition_leaves(condition)
        if leaf.indicator_id in SKILL_DERIVED_INDICATORS
    ]
    if derived:
        warnings.append(
            "新高、相对成交量及单均线由现有引擎按策略周期计算，原始日线来自东方财富查数 Skill；"
            "新高与量能基准不含当日，均线含当日，均跳过零成交量日；不是 Skill 直接提供的成品指标。"
        )
    if metrics.trade_count < 30:
        warnings.append("完整交易不足30次，不能据此断言策略有效；优化后仍需样本外检验。")
    filled = any(
        item.kind in {"fill", "partial_fill"} and item.side == "buy" for item in result.activities
    )
    comparison = (
        "benchmark_entry_not_filled"
        if not result.benchmark_entry_filled
        else "comparable"
        if filled
        else "strategy_entry_not_filled"
    )
    activities = [
        {
            "id": item.id,
            "chainId": item.chain_id,
            "decisionId": item.decision_id,
            "orderId": item.order_id,
            "fillId": item.fill_id,
            "parentId": item.parent_id,
            "kind": item.kind,
            "occurredAt": item.occurred_at,
            "side": item.side,
            "title": "卖出尝试已结束"
            if item.reason
            in {
                "exit_retry_budget_exhausted",
                "exit_retry_disabled",
            }
            else {
                "signal": f"{'买入' if item.side == 'buy' else '卖出'}信号确认",
                "order": f"提交{'买入' if item.side == 'buy' else '卖出'}委托",
                "fill": f"{'买入' if item.side == 'buy' else '卖出'}成交",
                "partial_fill": (f"{'买入' if item.side == 'buy' else '卖出'}部分成交"),
                "unfilled": f"{'买入' if item.side == 'buy' else '卖出'}委托未成交",
            }[item.kind],
            "price": None if item.raw_reference_price is None else float(item.raw_reference_price),
            "quantity": None,
            "notionalCny": None if item.notional_cny is None else float(item.notional_cny),
            "status": item.status,
            "reason": item.reason,
            "timeQuality": (
                "daily_bar_open_proxy" if item.raw_reference_price is not None else None
            ),
            "timeSemantics": (
                "日线开盘价仅为模拟成交参考，不代表已观测到 09:30 的真实逐笔成交"
                if item.raw_reference_price is not None
                else None
            ),
            "attemptNo": item.attempt_no,
            "originSignalId": item.origin_signal_id,
            "outcomeReason": (
                item.reason if item.kind in {"fill", "partial_fill", "unfilled"} else None
            ),
            "evidence": []
            if item.kind != "signal" or item.signal is None
            else [
                {
                    "type": source.evidence_type,
                    "id": source.evidence_id,
                    "availableAt": source.available_at,
                    "sourceEventId": source.source_event_id,
                    "provider": source.provider,
                    "sourceUrl": source.source_url,
                    "timeQuality": source.time_quality,
                    "timestampPrecision": source.timestamp_precision,
                    "validationStatus": source.validation_status,
                    "rawResponseSha256": source.raw_response_sha256,
                }
                for source in item.signal.evidence
            ],
        }
        for item in result.activities
    ]
    robustness = None
    if stressed is not None:
        stress_slippage = min(
            Decimal("1000"), max(config.slippage_bps * 2, config.slippage_bps + 5)
        )
        robustness = {
            "profile": "execution.v1",
            "selectionPolicy": "predeclared_scenarios_no_optimization",
            "totalReturnRange": {
                "min": min(metrics.total_return, stressed.metrics.total_return),
                "max": max(metrics.total_return, stressed.metrics.total_return),
            },
            "scenarios": [
                {
                    "id": "higher_slippage",
                    "participationRate": float(config.participation_rate),
                    "slippageBps": float(stress_slippage),
                    "totalReturn": stressed.metrics.total_return,
                    "maxDrawdown": stressed.metrics.maximum_drawdown,
                    "tradeCount": stressed.metrics.trade_count,
                    "finalEquityCny": float(stressed.equity_curve[-1].equity),
                }
            ],
        }
    bundle = BacktestResultBundle.model_validate(
        {
            "summary": {
                "runId": str(run_id),
                "totalReturn": metrics.total_return,
                "benchmarkReturn": metrics.benchmark_return,
                "benchmarkComparisonStatus": comparison,
                "annualizedReturn": metrics.annualized_return,
                "maxDrawdown": metrics.maximum_drawdown,
                "sharpeRatio": metrics.sharpe_ratio,
                "winRate": metrics.win_rate,
                "tradeCount": metrics.trade_count,
                "initialCashCny": float(initial),
                "finalEquityCny": float(result.equity_curve[-1].equity),
                "interpretation": (
                    "本结果按东方财富提供的复权序列模拟策略收益，并计入交易成本；"
                    "不是逐笔分红到账的实盘账户。策略结论与优化方向由真实模型另行分析。"
                ),
                "dataRange": {
                    "start": strategy.backtest.start,
                    "end": strategy.backtest.end,
                    "sessions": len(curve) - 1,
                },
                "warnings": warnings,
                "dataProvenance": {
                    "provider": history.provider,
                    "instrumentId": strategy.instrument.symbol,
                    "priceBasis": "provider_back_adjusted",
                    "retrievedAt": history.retrieved_at,
                    "historyStart": history.rows[0].session_date,
                    "historyEnd": history.rows[-1].session_date,
                    "historyRows": len(history.rows),
                    "indicatorSeries": len(indicators),
                    "indicatorPoints": sum(len(s.points) for s in indicators),
                    "refreshRequested": config.refresh_data,
                    "historyCacheStatus": history.cache_status or "unknown",
                    "indicatorCacheStatuses": [s.cache_status or "unknown" for s in indicators],
                    "indicatorFieldEvidence": [
                        {
                            "indicatorId": series.indicator_id,
                            "fieldName": value.field_name,
                            "sourceFieldName": value.source_field_name,
                            "sourceFieldCode": value.field_code,
                            "sourceParameters": value.source_parameters,
                            "unit": value.unit,
                            "sourceUnit": value.source_unit,
                        }
                        for series in indicators
                        for value in series.points[0].values
                    ],
                    "derivedIndicatorEvidence": derived,
                    "queries": (
                        *(e.query for e in history.query_evidence),
                        *(s.query for s in indicators),
                    ),
                },
            },
            "series": curve,
            "activities": activities,
            "robustness": robustness,
            "audit": {
                "hashSchemaVersion": RESULT_HASH_SCHEMA_VERSION,
                "openPositionShares": 0,
                "openPositionNotionalCny": float(result.open_position_notional_cny),
            },
        }
    )
    payload = cast(dict[str, object], bundle.model_dump(mode="json", by_alias=True))
    bundle.audit.result_hash = calculate_result_bundle_hash(payload)
    return bundle
