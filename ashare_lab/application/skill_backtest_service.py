"""Prepare Eastmoney Skill inputs and execute the queued research simulator.

Daily price history remains sourced through the Skill. Optional source-backed
corporate actions feed signal adjustment and the share ledger. The existing run
store and HTTP result contract identify the report presented to the review model.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, fields, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from hashlib import sha256
from threading import RLock
from time import monotonic
from types import MappingProxyType
from typing import TypeVar, cast
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
    UnsupportedSkillIndicatorError,
    build_indicator_contract,
)
from ashare_lab.adapters.market_data.mx_saas import (
    MxRetryProgress,
    mx_retry_message,
    MxSaasProviderAuthError,
    MxSaasProviderDataError,
    MxSaasProviderError,
    MxSaasProviderNoDataError,
    MxSaasProviderUnavailableError,
    observe_mx_retries,
)
from ashare_lab.adapters.market_data.provider_indicator_cache import (
    FileCachedHistoricalIndicatorData,
    ProviderIndicatorCacheMissError,
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
from ashare_lab.application.daily_backtest import DailyBacktestInputError
from ashare_lab.application.execution_feedback import execution_capability_feedback
from ashare_lab.application.local_minute_grid import LocalMinuteGrid
from ashare_lab.application.minute_grid_plan import MinuteGridCapabilityError
from ashare_lab.application.minute_replay_input import MinuteReplayDataError
from ashare_lab.application.result_views import (
    RESULT_HASH_SCHEMA_VERSION,
    calculate_result_bundle_hash,
)
from ashare_lab.application.skill_backtest import (
    SkillBacktestInputError,
    SkillBacktestResult,
    _validate_input,  # pyright: ignore[reportPrivateUsage]
    run_skill_backtest,
)
from ashare_lab.application.skill_indicator_routes import (
    SkillIndicatorRoute,
    default_skill_indicator_routes,
)
from ashare_lab.application.skill_numeric_history import (
    SkillNumericHistoryError,
    prepare_skill_numeric_series,
)
from ashare_lab.application.skill_series_discovery import SkillSeriesDiscovery
from ashare_lab.domain.execution import CapacityMode, LimitHandling
from ashare_lab.domain.market_data import DailyBar, PriceBasis, TradingStatus
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
from ashare_lab.domain.signals.skill_numeric import (
    SKILL_COMPARISON_IDS,
    skill_comparison_binding,
    validate_skill_comparison_condition,
)
from ashare_lab.domain.strategy import (
    AnyCondition,
    HoldingPeriodExit,
    HybridExecutionPolicy,
    ComposedExecutionPolicy,
    MinuteProtectionExit,
    PositionReturnExit,
    StrategySpec,
    TrailingDrawdownExit,
    canonical_hash,
    canonical_json,
    strategy_requires_events,
    strategy_requires_financials,
)
from ashare_lab.domain.strategy.models import Condition, IndicatorCondition
from ashare_lab.domain.strategy.price_plans import ConditionalPlan, GridPlan, ScheduledPlan, GridSpecificationError
from ashare_lab.ports.backtest_runs import (
    BacktestJobState,
    BacktestQueueFullError,
    BacktestRunRecord,
    BacktestRunStore,
    CreateRunResult,
)
from ashare_lab.ports.finance_history_format import FinanceHistoryDecoder
from ashare_lab.ports.live_market_data import LiveFinanceData
from ashare_lab.ports.provider_indicator_data import (
    HistoricalIndicatorData,
    ProviderConditionParameters,
    ProviderIndicatorSeries,
)
from ashare_lab.ports.skill_metric_binding import MetricBindingReviewer

logger = logging.getLogger(__name__)
_T = TypeVar("_T")
_PROFILE = "eastmoney_skill_research.v1"
_PREPARATION_VERSION = "skill_candidate_preparation.v2_geometry"
_PREPARATION_CACHE_TTL_SECONDS = 300.0
_PREPARATION_CACHE_MAX_ENTRIES = 16


def _strategy_price_plans(strategy: StrategySpec):
    if strategy.independent_plans is not None:
        return (strategy.independent_plans.entry_plan, strategy.independent_plans.exit_plan)
    return (strategy.trading_plan,) if strategy.trading_plan is not None else ()


def _price_plan_config(strategy: StrategySpec, config: BacktestRunConfig) -> BacktestRunConfig:
    """Persist the settings actually used; plan fees own the single source of truth."""
    plans = _strategy_price_plans(strategy)
    if not plans:
        return config
    params = plans[0].parameters
    return replace(config, commission_rate=params.commission_rate,
                   minimum_commission_cny=params.minimum_commission_cny,
                   slippage_bps=params.slippage_bps,
                   slippage_cny=params.slippage_cny,
                   limit_handling=LimitHandling.STRICT_NO_FILL_AT_LIMIT,
                   allocation_ratio=Decimal(1), run_robustness=False)


@dataclass(frozen=True, slots=True)
class SkillCandidatePreparation:
    history: MxDailyHistory
    entry_timeline: tuple[SignalFact | None, ...]
    exit_timeline: tuple[SignalFact | None, ...]
    indicator_series: tuple[ProviderIndicatorSeries, ...]
    data_version: str
    derived_condition_hashes: frozenset[str] = frozenset()
    signal_adjustment_source: str | None = None


class SkillCandidatePreparationError(ValueError):
    """Request-scoped historical readiness, not permanent indicator availability."""

    def __init__(
        self, code: str, message: str, *, indicator_id: str | None = None,
        condition_path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.indicator_id = indicator_id
        self.condition_path = condition_path


class _Cancelled(Exception):
    pass


class SkillBacktestService:
    indicator_routes: Mapping[str, SkillIndicatorRoute] = default_skill_indicator_routes()
    available_indicator_ids = frozenset(
        key for key, route in indicator_routes.items() if route.source != "unavailable"
    )
    indicator_unavailable_reasons: Mapping[str, str] = MappingProxyType({
        key: route.source_note
        for key, route in indicator_routes.items()
        if route.source == "unavailable"
    })

    def __init__(
        self,
        *,
        history: MxDailyHistoryClient,
        indicators: HistoricalIndicatorData,
        store: BacktestRunStore,
        max_workers: int = 1,
        max_pending: int | None = None,
        indicator_routes: Mapping[str, SkillIndicatorRoute] | None = None,
        finance: LiveFinanceData | None = None,
        finance_decoder: FinanceHistoryDecoder | None = None,
        numeric_binding_reviewer: MetricBindingReviewer | None = None,
        minute_grid: LocalMinuteGrid | None = None,
        market_calendar_loader: Callable | None = None,
        signal_corporate_loader: Callable | None = None,
        runtime_evidence: Mapping[str, str] | None = None,
        available_data_end: Callable[[], date] | None = None,
    ) -> None:
        self.available_data_end = available_data_end
        if indicator_routes is not None:
            self.indicator_routes = dict(indicator_routes)
            self.available_indicator_ids = frozenset(
                key for key, route in indicator_routes.items() if route.source != "unavailable"
            )
            self.indicator_unavailable_reasons = {
                key: route.source_note for key, route in indicator_routes.items()
                if route.source == "unavailable"
            }
        self.history = history
        self.minute_grid = minute_grid
        self.market_calendar_loader = market_calendar_loader
        self.signal_corporate_loader = signal_corporate_loader
        self.runtime_evidence = dict(runtime_evidence) if runtime_evidence is not None else None
        self.indicators = indicators
        if finance is not None and finance_decoder is None:
            raise ValueError("financial history discovery requires an injected decoder")
        self.numeric_discovery = (
            SkillSeriesDiscovery(finance, decoder=finance_decoder)
            if finance is not None and finance_decoder is not None else None
        )
        self.numeric_binding_reviewer = numeric_binding_reviewer
        self.numeric_series_enabled = (
            self.numeric_discovery is not None
            and bool(SKILL_COMPARISON_IDS.intersection(self.indicator_routes))
        )
        self.store = store
        self._preparation_lock = RLock()
        self._preparation_epoch = 0
        self._preparation_cache: OrderedDict[
            tuple[str, str], tuple[float, SkillCandidatePreparation]
        ] = OrderedDict()
        self.queue = ThreadBacktestJobQueue(
            self.execute,
            max_workers=max_workers,
            max_pending=max_pending,
        )

    def shutdown(self) -> None:
        self.queue.shutdown()

    def submit(
        self, strategy: StrategySpec, config: BacktestRunConfig, *, prepared_inputs: bool = False,
    ) -> CreateRunResult:
        self._validate_request(strategy)
        self.validate_execution_capability(strategy)
        config = _price_plan_config(strategy, config)
        config_payload = {field.name: getattr(config, field.name) for field in fields(config)}
        now = datetime.now(UTC)
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
                {
                    "profile": _PROFILE, "provider": "eastmoney_mx_finance_data",
                    "preparedInputs": prepared_inputs,
                    "refreshRequested": config.refresh_data,
                    "refreshConsumed": prepared_inputs and config.refresh_data,
                }
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

    def _validate_request(self, strategy: StrategySpec) -> None:
        validate_a_share_backtest_range(strategy.backtest.start, strategy.backtest.end)
        # Do not silently reinterpret an unsupported fundamental/event condition
        # as a technical strategy. Such histories still need their Skill adapter.
        if strategy_requires_events(strategy):
            raise EventDataUnavailableError("eastmoney_skill_event_history_not_connected")
        if strategy_requires_financials(strategy):
            raise FinancialDataUnavailableError("eastmoney_skill_financial_history_not_connected")
        end_limit = (self.available_data_end() if self.available_data_end
                     else latest_stable_a_share_data_date(datetime.now(UTC)))
        if strategy.backtest.end > end_limit:
            raise BacktestDataNotYetAvailableError("backtest end exceeds completed daily data")
        for condition in (strategy.entry, _exit_condition(strategy)):
            if condition is not None:
                for _, leaf in provider_condition_leaves(condition):
                    route = self.indicator_routes.get(leaf.indicator_id)
                    if route is None or route.source == "unavailable":
                        raise UnsupportedSkillIndicatorError(
                            leaf.indicator_id,
                            route.source_note if route else "尚无该指标的数据接入与计算定义。",
                        )
                    if route.source == "skill_ohlcv_python":
                        continue
                    if route.source == "skill_numeric_history":
                        validate_skill_comparison_condition(leaf)
                        if not self.numeric_series_enabled:
                            raise UnsupportedSkillIndicatorError(
                                leaf.indicator_id, "通用历史指标的 Skill 查询尚未配置。",
                            )
                        continue
                    binding = binding_for_provider_condition(leaf)
                    try:
                        build_indicator_contract(
                            leaf.indicator_id, binding.provider_indicator_name, binding.value_names,
                            condition_params=leaf.params,
                        )
                    except UnsupportedSkillIndicatorError:
                        raise
                    except ValueError as exc:
                        raise UnsupportedSkillIndicatorError(
                            leaf.indicator_id, "该指标的请求参数尚未对应到已验证的 Skill 字段。"
                        ) from exc

    def validate_execution_capability(self, strategy: StrategySpec) -> None:
        """Check execution wiring without downgrading understood strategy drafts."""
        if isinstance(strategy.execution, ComposedExecutionPolicy):
            from ashare_lab.application.composed_execution import validate_composed_route
            validate_composed_route(strategy)
            if self.minute_grid is None or self.signal_corporate_loader is None:
                raise MinuteGridCapabilityError("composed_execution_inputs_not_connected")
        if isinstance(strategy.execution, HybridExecutionPolicy) and self.minute_grid is None:
            raise MinuteGridCapabilityError("hybrid_execution_disabled")
        plan = strategy.trading_plan
        if isinstance(plan, ScheduledPlan) and plan.parameters.exit_rules and self.minute_grid is None:
            raise MinuteGridCapabilityError("minute_scheduled_exit_execution_disabled")
        if (isinstance(plan, (GridPlan, ConditionalPlan))
                and plan.parameters.observation == "minute_bar" and self.minute_grid is None):
            raise MinuteGridCapabilityError(
                "minute_grid_execution_disabled" if isinstance(plan, GridPlan)
                else "minute_conditional_execution_disabled")
        # Match the runtime's route selection, including persisted server-selected grids.
        # Explicit daily plans retain their existing execution semantics.
        if (isinstance(plan, GridPlan) and self.minute_grid is not None
                and plan.parameters.observation != "daily_close"
                and plan.parameters.anchor_update == "last_fill"):
            raise MinuteGridCapabilityError("moving_anchor_execution_not_connected")
        if (isinstance(plan, GridPlan) and plan.parameters.anchor_update == "last_trigger"
                and (self.minute_grid is None or plan.parameters.observation == "daily_close")):
            raise MinuteGridCapabilityError("trigger_grid_requires_minute_execution")

    def _bind_grid_history_anchor(self, strategy: StrategySpec, history: MxDailyHistory) -> StrategySpec:
        from ashare_lab.application.grid_previous_close import bind_previous_close
        plans = _strategy_price_plans(strategy)
        if not any(isinstance(plan, GridPlan) and plan.parameters.anchor_mode == 'previous_close'
                   for plan in plans):
            return strategy
        loader = self.market_calendar_loader or (self.minute_grid.load_calendar if self.minute_grid else None)
        if loader is None:
            raise SkillCandidatePreparationError("grid_previous_close_unavailable", "缺少交易日历，不能确定起始日昨收价。")
        sessions, _, _ = loader()
        try:
            bound = tuple(plan.model_copy(update={'parameters': bind_previous_close(
                plan.parameters, start=strategy.backtest.start, end=strategy.backtest.end,
                symbol=strategy.instrument.symbol, history=history, sessions=sessions)})
                if isinstance(plan, GridPlan) and plan.parameters.anchor_mode == 'previous_close'
                else plan for plan in plans)
        except ValueError as exc:
            raise SkillCandidatePreparationError("grid_previous_close_unavailable", str(exc)) from exc
        if strategy.independent_plans is not None:
            return strategy.model_copy(update={'independent_plans': strategy.independent_plans.model_copy(
                update={'entry_plan': bound[0], 'exit_plan': bound[1]})})
        return strategy.model_copy(update={'trading_plan': bound[0]})

    async def resolve_grid_anchor(self, strategy: StrategySpec) -> StrategySpec:
        if not any(isinstance(plan, GridPlan) and plan.parameters.anchor_mode == 'previous_close'
                   for plan in _strategy_price_plans(strategy)):
            return strategy
        history = await self._load_history(None, strategy, BacktestRunConfig(), 40)
        return self._bind_grid_history_anchor(strategy, history)

    async def prepare_candidate(
        self, strategy: StrategySpec, config: BacktestRunConfig,
    ) -> SkillCandidatePreparation:
        """Prepare real input data without creating a run or simulating returns."""
        return await self._prepare_candidate(strategy, config, run_id=None)

    async def _prepare_candidate(
        self, strategy: StrategySpec, config: BacktestRunConfig, *, run_id: RunId | None,
    ) -> SkillCandidatePreparation:
        self._validate_request(strategy)
        self.validate_execution_capability(strategy)
        config = _price_plan_config(strategy, config)
        request_key = canonical_hash({
            "version": _PREPARATION_VERSION, "profile": _PROFILE,
            "strategy": strategy.model_dump(mode="json"),
            "config": json.loads(json.dumps(asdict(replace(config, refresh_data=False)),
                                             default=str)),
            "routes": {key: asdict(value) for key, value in self.indicator_routes.items()},
        })
        cached: SkillCandidatePreparation | None = None
        with self._preparation_lock:
            if config.refresh_data:
                # A forced read invalidates old results and prevents an older
                # concurrent preparation from restoring stale successful data.
                self._preparation_cache.clear()
                self._preparation_epoch += 1
            epoch = self._preparation_epoch
            for key, (expires_at, prepared) in tuple(self._preparation_cache.items()):
                if expires_at <= monotonic():
                    del self._preparation_cache[key]
                elif key[0] == request_key:
                    cached = prepared
                    self._preparation_cache.move_to_end(key)
        if cached is not None and not (self.signal_corporate_loader is not None and (
            strategy.trading_plan is None or isinstance(strategy.execution, ComposedExecutionPolicy)
        )):
            if run_id is not None:
                self._stage(run_id, BacktestJobState.RUNNING_SIGNAL, 45,
                            "复用已核验历史数据和指标，正在继续回测")
            return cached
        warmup = _effective_warmup_calendar_days(strategy, config)
        if any(isinstance(plan, GridPlan) and plan.parameters.anchor_mode == 'previous_close'
               for plan in _strategy_price_plans(strategy)):
            warmup = max(warmup, 40)
        history = await self._load_history(run_id, strategy, config, warmup)
        strategy = self._bind_grid_history_anchor(strategy, history)
        if _strategy_price_plans(strategy):
            if history.instrument_id != strategy.instrument.symbol:
                history = await self._load_history(
                    run_id, strategy, replace(config, refresh_data=True), warmup,
                )
            if history.instrument_id != strategy.instrument.symbol:
                raise MxDailyHistoryError("grid_history_instrument_mismatch_after_refresh")
            eligible = [row for row in history.rows
                        if strategy.backtest.start <= row.session_date <= strategy.backtest.end]
            minute_grid = (isinstance(strategy.trading_plan, GridPlan)
                           and (strategy.trading_plan.parameters.observation == "minute_bar"
                                or self.minute_grid is not None
                                and strategy.trading_plan.parameters.observation != "daily_close")
                           or isinstance(strategy.trading_plan, ScheduledPlan)
                           or isinstance(strategy.trading_plan, ConditionalPlan)
                           and strategy.trading_plan.parameters.observation == "minute_bar")
            # A minute plan can observe and execute within one session. Actual
            # complete bars and settlement calendar are checked by its adapter.
            minimum_sessions = 1 if minute_grid or strategy.independent_plans is not None else 2
            if len(eligible) < minimum_sessions:
                raise SkillCandidatePreparationError("skill_history_no_execution_session",
                                                     "当前区间缺少执行所需的交易日数据，原交易计划已保留。")
            for grid_plan in _strategy_price_plans(strategy):
                if not isinstance(grid_plan, GridPlan):
                    continue
                params = grid_plan.parameters
                from ashare_lab.domain.market_data.models import standard_buy_quantity_rule
                from ashare_lab.domain.strategy.price_plans import validate_grid_buy_quantities
                validate_grid_buy_quantities(params, *standard_buy_quantity_rule(history.board))
                if params.anchor_mode == "first_open":
                    params = params.model_copy(update={"anchor_price": eligible[0].raw_open})
                # Same geometry as execution, after the REAL historical anchor
                # is bound. Never erase requested levels to make bounds fit.
                resolved = params.resolve_geometry()
                if not resolved.lower_price <= resolved.resolved_anchor <= resolved.upper_price:
                    raise MinuteGridCapabilityError("historical_anchor_outside_grid_bounds")
        else:
            _candidate_signal_indices(strategy, history)
        if run_id is not None:
            self._stage(run_id, BacktestJobState.RUNNING_SIGNAL, 45,
                        "读取必要数据并计算策略指标")
        derived_condition_hashes: set[str] = set()
        signal_source = None
        price_rebases = None
        if strategy.independent_plans is not None:
            # These legs observe prices/calendars themselves. Do not ask the
            # signal compiler to invent missing top-level entry/exit trees.
            corporate = await self._prepare_signal_corporate(run_id, strategy, history, config)
            signal_source = json.dumps(corporate.evidence, default=str, sort_keys=True)
            entry, exits, series = (None,) * len(history.rows), (None,) * len(history.rows), ()
        elif strategy.trading_plan is not None and not isinstance(strategy.execution, ComposedExecutionPolicy):
            entry, exits, series = (None,) * len(history.rows), (None,) * len(history.rows), ()
        else:
            if self.signal_corporate_loader is not None:
                corporate = await self._prepare_signal_corporate(run_id, strategy, history, config)
                price_rebases = corporate.price_rebases
                signal_source = json.dumps(corporate.evidence, default=str, sort_keys=True)
            entry, exits, series = await self._with_retry_progress(
                run_id, self._signals(strategy, history, force_refresh=config.refresh_data,
                                      require_ready=True,
                                      price_rebases=price_rebases,
                                      derived_condition_hashes=derived_condition_hashes),
            )
        data_version = canonical_hash(json.loads(json.dumps({
            "history": asdict(history), "indicators": [asdict(item) for item in series],
            "derivedConditions": sorted(derived_condition_hashes),
            "signalAdjustmentSource": signal_source,
        }, default=str)))
        prepared = SkillCandidatePreparation(
            history, entry, exits, series, data_version, frozenset(derived_condition_hashes),
            signal_source,
        )
        if run_id is not None and self._get(run_id).state is BacktestJobState.CANCEL_REQUESTED:
            raise _Cancelled
        with self._preparation_lock:
            if epoch == self._preparation_epoch:
                for key in tuple(self._preparation_cache):
                    if key[0] == request_key:
                        del self._preparation_cache[key]
                self._preparation_cache[(request_key, data_version)] = (
                    monotonic() + _PREPARATION_CACHE_TTL_SECONDS, prepared,
                )
                while len(self._preparation_cache) > _PREPARATION_CACHE_MAX_ENTRIES:
                    self._preparation_cache.popitem(last=False)
        return prepared

    def execute(self, run_id: RunId) -> BacktestRunRecord:
        record = self._get(run_id)
        if record.state.is_terminal:
            return record
        strategy: StrategySpec | None = None
        try:
            strategy = StrategySpec.model_validate_json(record.strategy_json)
            # Also protect queued/older records if execution wiring changed since submission.
            self.validate_execution_capability(strategy)
            self._stage(run_id, BacktestJobState.RUNNING_DATA, 10, "东方财富 Skill 获取历史数据")
            config = _config_from_json(record.config_json)
            # The HTTP preflight already performed an explicit refresh. Keep
            # the user's original config for audit/reporting, but do not evict
            # those prepared inputs when the worker starts. Older/direct runs
            # without this internal marker retain their original refresh path.
            manifest = json.loads(record.manifest_json)
            preparation_config = replace(config, refresh_data=False) if (
                isinstance(manifest, dict)
                and cast(dict[str, object], manifest).get("refreshConsumed") is True
            ) else config
            prepared = asyncio.run(self._prepare_candidate(
                strategy, preparation_config, run_id=run_id,
            ))
            history = prepared.history
            strategy = self._bind_grid_history_anchor(strategy, history)
            entry, exit_signals = prepared.entry_timeline, prepared.exit_timeline
            indicator_series = prepared.indicator_series
            self._stage(run_id, BacktestJobState.RUNNING_EXECUTION, 70, "模拟交易并计算收益")
            if _strategy_price_plans(strategy):
                from ashare_lab.application.price_plan_result import execute_price_plan
                minute_args = {}
                if (isinstance(strategy.trading_plan, ConditionalPlan)
                        and strategy.trading_plan.parameters.observation == "daily_close"
                        and any(rule.kind == "holding_period" for rule in strategy.trading_plan.parameters.rules)):
                    loader = self.market_calendar_loader or (self.minute_grid.load_calendar if self.minute_grid is not None else None)
                    if loader is None:
                        raise MinuteReplayDataError("market_calendar_source_missing")
                    sessions, calendar, raw_calendar = loader()
                    minute_args.update(market_sessions=sessions, calendar_evidence=dict(
                        provider=calendar["provider"], sourceSha256=calendar["sourceSha256"],
                        fileSha256=sha256(raw_calendar).hexdigest(), sessions=calendar["sessions"]))
                wants_schedule = isinstance(strategy.trading_plan, ScheduledPlan)
                wants_minute_conditions = (isinstance(strategy.trading_plan, ConditionalPlan)
                                           and strategy.trading_plan.parameters.observation == "minute_bar")
                self.validate_execution_capability(strategy)
                composed = isinstance(strategy.execution, ComposedExecutionPolicy)
                if wants_schedule and not strategy.trading_plan.parameters.exit_rules and not composed:
                    from ashare_lab.application.daily_scheduled_plan import (
                        execute_sourced_daily_schedule,
                    )
                    calendar_loader = self.market_calendar_loader or (
                        self.minute_grid.load_calendar if self.minute_grid is not None else None)
                    if calendar_loader is None:
                        raise MinuteReplayDataError("market_calendar_source_missing")
                    corporate_loader = self.signal_corporate_loader or (
                        self.minute_grid.corporate_loader if self.minute_grid is not None else None)
                    corporate = corporate_loader(strategy, history) if corporate_loader is not None else None
                    result, snapshot_id, reconciliation, source_evidence = execute_sourced_daily_schedule(
                        strategy, history, calendar_input=calendar_loader(), corporate=corporate, config=config)
                    minute_args = dict(minute_result=result, minute_snapshot_id=snapshot_id,
                                       minute_reconciliation=reconciliation, minute_source_evidence=source_evidence)
                elif self.minute_grid is not None and ((isinstance(strategy.trading_plan, GridPlan)
                        and strategy.trading_plan.parameters.observation != "daily_close") or wants_minute_conditions
                        or wants_schedule or composed):
                    signal_args = {}
                    if composed:
                        corporate = asyncio.run(self._prepare_signal_corporate(
                            run_id, strategy, history, preparation_config))
                        if json.dumps(corporate.evidence, default=str, sort_keys=True) != prepared.signal_adjustment_source:
                            raise MinuteReplayDataError("corporate_action_source_changed_after_signal_preparation")
                        signal_args = dict(signal_input=prepared, corporate_inputs=corporate)
                    result, snapshot_id, reconciliation, source_evidence = self.minute_grid.execute(
                        strategy, history, config=config, **signal_args)
                    minute_args = dict(minute_result=result, minute_snapshot_id=snapshot_id,
                                       minute_reconciliation=reconciliation, minute_source_evidence=source_evidence)
                else:
                    # Explicit daily price plans share the same pinned action
                    # loader and calendar as daily indicators and schedules.
                    corporate_loader = self.signal_corporate_loader or (
                        self.minute_grid.corporate_loader if self.minute_grid is not None else None)
                    if corporate_loader is not None:
                        calendar_loader = self.market_calendar_loader or (
                            self.minute_grid.load_calendar if self.minute_grid is not None else None)
                        if calendar_loader is None:
                            raise MinuteReplayDataError("market_calendar_source_missing")
                        sessions, calendar, raw_calendar = calendar_loader()
                        minute_args.update(corporate=corporate_loader(strategy, history),
                            market_sessions=sessions, calendar_evidence=dict(
                                provider=calendar["provider"], sourceSha256=calendar["sourceSha256"],
                                fileSha256=sha256(raw_calendar).hexdigest(), sessions=calendar["sessions"]))
                bundle = execute_price_plan(run_id, strategy, history, config,
                                            runtime_evidence=self.runtime_evidence, **minute_args)
                self._stage(run_id, BacktestJobState.RUNNING_REPORT, 95, "汇总交易计划与逐笔委托")
                return self._stage(run_id, BacktestJobState.SUCCEEDED, 100, "回测完成",
                                   result_json=bundle.model_dump_json(by_alias=True))
            if isinstance(strategy.execution, HybridExecutionPolicy):
                if self.minute_grid is None:
                    raise MinuteGridCapabilityError("hybrid_execution_disabled")
                if self.signal_corporate_loader is None:
                    raise MinuteReplayDataError("hybrid_corporate_inputs_missing")
                corporate = asyncio.run(self._prepare_signal_corporate(
                    run_id, strategy, history, preparation_config))
                if json.dumps(corporate.evidence, default=str, sort_keys=True) != prepared.signal_adjustment_source:
                    raise MinuteReplayDataError("corporate_action_source_changed_after_signal_preparation")
                result, snapshot_id, reconciliation, evidence = self.minute_grid.execute(
                    strategy, history, signal_input=prepared, config=config, corporate_inputs=corporate)
                from ashare_lab.application.minute_result import minute_result_bundle
                bundle = minute_result_bundle(run_id=str(run_id), result=result,
                    initial_cash=Decimal(strategy.backtest.initial_cash_cny), snapshot_id=snapshot_id,
                    reconciliation=reconciliation, source_evidence=evidence,
                    strategy=strategy, runtime_evidence=self.runtime_evidence)
                if config.run_robustness:
                    stress_slippage = min(Decimal("1000"), max(
                        config.slippage_bps * 2, config.slippage_bps + 5,
                    ))
                    stress_config = replace(config, slippage_bps=stress_slippage,
                                            slippage_cny=config.slippage_cny * 2,
                                            run_robustness=False)
                    stressed, stressed_snapshot, stressed_reconciliation, stressed_evidence = self.minute_grid.execute(
                        strategy, history, signal_input=prepared, config=stress_config,
                        corporate_inputs=corporate)
                    if (stressed_snapshot != snapshot_id
                            or json.dumps(stressed_evidence, default=str, sort_keys=True)
                            != json.dumps({**evidence, "dailySignals": {
                                **evidence["dailySignals"],
                                "executionConfig": asdict(stress_config),
                            }}, default=str, sort_keys=True)
                            or stressed_reconciliation != reconciliation):
                        raise MinuteReplayDataError("hybrid_robustness_inputs_changed")
                    stressed_bundle = minute_result_bundle(
                        run_id=f"{run_id}:higher_slippage", result=stressed,
                        initial_cash=Decimal(strategy.backtest.initial_cash_cny),
                        snapshot_id=stressed_snapshot, reconciliation=stressed_reconciliation,
                        source_evidence=stressed_evidence, strategy=strategy,
                        runtime_evidence=self.runtime_evidence)
                    from ashare_lab.application.minute_result import attach_minute_robustness
                    bundle = attach_minute_robustness(base=bundle, stressed=stressed_bundle,
                                                      config=config,
                                                      stress_slippage_bps=stress_slippage)
                self._stage(run_id, BacktestJobState.RUNNING_REPORT, 95, "汇总日线信号与分钟保护账本")
                return self._stage(run_id, BacktestJobState.SUCCEEDED, 100, "日线信号与分钟保护回测完成",
                                   result_json=bundle.model_dump_json(by_alias=True))
            if self.signal_corporate_loader is not None and self.market_calendar_loader is not None:
                from ashare_lab.application.mx_daily_share_replay import replay_mx_daily_shares
                corporate = asyncio.run(self._prepare_signal_corporate(
                    run_id, strategy, history, preparation_config))
                if json.dumps(corporate.evidence, default=str, sort_keys=True) != prepared.signal_adjustment_source:
                    raise MinuteReplayDataError("corporate_action_source_changed_after_signal_preparation")
                bundle = replay_mx_daily_shares(run_id=run_id, strategy=strategy, prepared=prepared,
                    config=config, calendar_input=self.market_calendar_loader(), corporate=corporate,
                    runtime_evidence=self.runtime_evidence)
                self._stage(run_id, BacktestJobState.RUNNING_REPORT, 95, "汇总股份、费用与公司行动账本")
                return self._stage(run_id, BacktestJobState.SUCCEEDED, 100, "股份账本回测完成",
                                   result_json=bundle.model_dump_json(by_alias=True))
            holding_calendar = None
            holding_calendar_evidence = None
            if any(isinstance(rule, HoldingPeriodExit) for rule in strategy.exit.children):
                loader = self.market_calendar_loader or (self.minute_grid.load_calendar if self.minute_grid is not None else None)
                if loader is None:
                    raise MinuteReplayDataError("market_calendar_source_missing")
                holding_calendar, calendar_manifest, calendar_bytes = loader()
                holding_calendar = tuple(holding_calendar)
                holding_calendar_evidence = {
                    "provider": calendar_manifest.get("provider"),
                    "sourceSha256": calendar_manifest.get("sourceSha256"),
                    "fileSha256": sha256(calendar_bytes).hexdigest(),
                    "sessionsSha256": sha256("\n".join(
                        day.isoformat() for day in holding_calendar
                    ).encode()).hexdigest(),
                    "sessionCount": len(holding_calendar),
                    "holdingPeriodConvention": "buy_session_D0_target_D_plus_N_open",
                }
            result = run_skill_backtest(
                strategy=strategy,
                history=history,
                entry_timeline=entry,
                exit_timeline=exit_signals,
                config=config,
                market_sessions=holding_calendar,
            )
            stressed = None
            if config.run_robustness:
                stressed = run_skill_backtest(
                    strategy=strategy,
                    history=history,
                    entry_timeline=entry,
                    exit_timeline=exit_signals,
                    market_sessions=holding_calendar,
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
                        slippage_cny=config.slippage_cny * 2,
                    ),
                )
            self._stage(
                run_id, BacktestJobState.RUNNING_REPORT, 95, "生成可供真实模型分析的回测结果"
            )
            bundle = _result_bundle(
                run_id, strategy, config, history, indicator_series, result, stressed,
                indicator_routes=self.indicator_routes,
                derived_condition_hashes=prepared.derived_condition_hashes,
                signal_adjustment_source=prepared.signal_adjustment_source,
                holding_calendar_evidence=holding_calendar_evidence,
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
                elif isinstance(exc, MxSaasProviderDataError):
                    reason = exc.data_reason
                    failure_labels = {
                        "provider_sql_error": "服务暂时不稳定，自动重试后仍未成功，请稍后再试",
                        "protocol_invalid_json": "返回的响应不是有效 JSON",
                        "protocol_invalid_payload": "返回的响应结构不完整",
                        "protocol_tables_missing": "返回的响应缺少数据表",
                        "protocol_table_invalid": "返回的数据表结构无法读取",
                        "protocol_raw_table_missing": "返回的响应缺少逐日原始表 rawTable",
                        "protocol_dates_missing": "返回的响应缺少逐日日期表头",
                        "provider_query_rejected": "未接受本次数据查询",
                        "provider_partial_result": "只返回了部分查询结果",
                        "data_security_mismatch": "返回的数据未唯一对应本次股票",
                        "data_dates_mismatch": "返回的数据日期未与本次查询对齐",
                        "data_field_binding_mismatch": "返回的指标字段或计算参数未与请求匹配",
                        "data_values_invalid": "返回的指标数值或数量未通过检查",
                    }
                    if reason not in failure_labels:
                        reason = "data_validation_failed"
                    failure = failure_labels.get(reason, "返回的数据未通过完整性校验")
                    # Preserve the existing error-code contract while exposing
                    # safe, actionable diagnostics in the status and server log.
                    progress_label = f"{source}{failure}，本次回测已停止；原规则和设置已保留。"
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
            elif isinstance(exc, (DailyBacktestInputError, MinuteReplayDataError)):
                # These messages are fixed engine invariants, not provider
                # payloads or user text.  Retain the specific invariant in the
                # local server log so an execution wiring bug can be diagnosed
                # without exposing it as a raw class name in the UI.
                logger.warning(
                    "Skill backtest failed: run=%s exception=%s invariant=%s",
                    run_id,
                    type(exc).__name__,
                    str(exc),
                )
            else:
                logger.warning(
                    "Skill backtest failed: run=%s exception=%s",
                    run_id,
                    type(exc).__name__,
                )
            if isinstance(exc, MinuteReplayDataError):
                detail = str(exc).split(":", 1)[0]
                if detail in {"minute_source_network_error", "minute_source_rate_limited", "minute_source_access_denied"}:
                    error_code = "minute_data_unavailable"
                    progress_label = "策略已理解，但分钟行情取数链路暂不可用；原规则与设置已保留。本次没有用缺失数据生成回测结果。"
                elif detail in {"minute_snapshot_range_unavailable", "incomplete_minute_session",
                                "minute_execution_data_missing"}:
                    error_code = "minute_data_unavailable"
                    progress_label = "策略已理解，但所选区间尚缺完整分钟行情；原规则、股票与回测区间已保留，未自动缩短区间，也未改用日线回测。"
                elif detail == 'external_minute_file_invalid':
                    error_code = 'minute_data_unavailable'
                    progress_label = '策略已理解，但外接盘分钟文件不完整、正在变化或字段校验未通过；请待文件下载完成后重试，原规则和区间已保留。'
                elif detail in {"daily_minute_ohlc_mismatch", "daily_minute_volume_mismatch",
                                "daily_minute_turnover_mismatch"}:
                    error_code = "minute_data_unavailable"
                    progress_label = "策略已理解，但分钟行情与日线的价格、成交量或成交额核对不一致；原规则与设置已保留，需核对数据后再回测。"
                elif detail.startswith("corporate_action_") or detail in {
                    "daily_corporate_action_price_rebase_missing", "invalid_daily_price_rebases",
                }:
                    error_code = "corporate_action_data_unavailable"
                    progress_label = "策略已理解，但分红送转或除权换算数据缺失、覆盖不足或未通过核对；原规则与设置已保留。本次未生成跨除权回测结果。"
                elif detail.startswith("market_calendar_") or detail in {
                    "invalid_market_calendar", "daily_schedule_next_market_session_missing",
                }:
                    error_code = "market_calendar_unavailable"
                    progress_label = "策略已理解，但所需交易日历缺失或尚未核对完整；原规则与设置已保留，未用自然日或个股有行情的日期替代。"
                elif detail in {"daily_schedule_session_data_missing", "raw_daily_control_missing", "security_session_missing", "nontrading_daily_volume_conflict"}:
                    error_code = "daily_execution_data_unavailable"
                    progress_label = "策略已理解，但所需日行情或证券交易状态不完整；原规则与设置已保留，补齐数据后可继续回测。"
                else:
                    error_code = "minute_data_unavailable"
                    progress_label = "策略已理解，但分钟行情与日线或交易日历尚未完整对齐；原规则与设置已保留，未改用日线回测。"
            elif isinstance(exc, SkillBacktestInputError) and str(exc) in {
                "holding market calendar does not cover backtest end",
                "holding target market session has no security data",
            }:
                missing_calendar = str(exc) == "holding market calendar does not cover backtest end"
                error_code = "market_calendar_unavailable" if missing_calendar else "daily_execution_data_unavailable"
                progress_label = (
                    "策略已理解，但交易日历覆盖不足或顺序异常，持有期限尚不能准确计算；原规则与设置已保留。"
                    if missing_calendar else
                    "策略已理解，但持有期到期日的行情或停牌状态缺失；原规则与设置已保留，未将缺失日期当作休市日。"
                )
            elif isinstance(exc, DailyBacktestInputError):
                error_code = "daily_execution_data_unavailable"
                progress_label = (
                    "策略已理解，但日线行情、交易状态或公司行动输入尚未满足一致性校验；"
                    "原规则与设置已保留，本次未生成不完整结果。"
                )
                if "auditable source provenance" in str(exc):
                    error_code = "signal_provenance_unavailable"
                    progress_label = (
                        "策略已理解，但指标信号的来源凭据尚未通过校验；"
                        "原规则与设置已保留，本次未生成回测结果。"
                    )
            elif isinstance(exc, GridSpecificationError):
                error_code, progress_label = exc.code, exc.safe_message
            elif isinstance(exc, MinuteGridCapabilityError):
                error_code, progress_label = execution_capability_feedback(str(exc))
            elif isinstance(exc, (SkillCandidatePreparationError, SkillNumericHistoryError)):
                error_code = exc.code
                progress_label = exc.message
            elif isinstance(exc, MxDailyHistoryFieldsMissingError):
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
                requested_start = (
                    strategy.backtest.start.isoformat() if strategy is not None else "上市前"
                )
                progress_label = (
                    f"这只股票于 {exc.listing_date.isoformat()} 上市，"
                    f"你选择的区间从 {requested_start} 开始，包含上市前日期。"
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
                logger.warning(
                    "Skill history validation: run=%s symbol=%s start=%s end=%s reason=%s",
                    run_id, strategy.instrument.symbol if strategy else "unknown",
                    strategy.backtest.start if strategy else "unknown",
                    strategy.backtest.end if strategy else "unknown",
                    str(exc) if str(exc) in date_errors else "history_validation_failed",
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

    async def _prepare_signal_corporate(self, run_id, strategy, history, config):
        from ashare_lab.adapters.market_data.local_price_plan_actions import (
            CorporateActionBoundaryPriorCloseMissing,
        )

        required_start = history.rows[0].session_date
        try:
            return await asyncio.to_thread(self.signal_corporate_loader, strategy, history,
                                           required_start=required_start)
        except CorporateActionBoundaryPriorCloseMissing as exc:
            calendar_loader = self.market_calendar_loader or (
                self.minute_grid.load_calendar if self.minute_grid is not None else None)
            if exc.ex_date != required_start or calendar_loader is None:
                raise
            dates, _, _ = calendar_loader()
            if (not dates or any(a >= b for a, b in zip(dates, dates[1:]))
                    or required_start not in dates or dates.index(required_start) == 0):
                raise
            previous_day = dates[dates.index(required_start) - 1]
            # Fetch only the source context needed for this boundary. Do not
            # prepend it to indicator warmup or change the user's backtest.
            supplement = await self._with_retry_progress(run_id, self.history.load(
                instrument_id=strategy.instrument.symbol, start=previous_day,
                end=required_start, force_refresh=config.refresh_data,
            ))
            by_day = {row.session_date: row for row in supplement.rows}
            previous = by_day.get(previous_day)
            overlap = by_day.get(required_start)
            raw_fields = ("raw_open", "raw_high", "raw_low", "raw_close", "raw_preclose")
            raw_evidence = tuple(item for item in supplement.query_evidence
                                 if item.purpose == "raw_prices" and item.provider == history.provider)
            if (supplement.instrument_id != history.instrument_id or not raw_evidence
                    or previous is None or previous.trading_status not in {
                        TradingStatus.TRADING, TradingStatus.SUSPENDED}
                    or overlap is None or any(getattr(overlap, key) != getattr(history.rows[0], key)
                                              for key in raw_fields)):
                raise exc
            # A source-marked suspension may supply a valid carried raw close;
            # do not invent a trade or substitute an unverified calendar day.
            context_history = replace(
                history, start=min(history.start, previous_day), rows=(previous, *history.rows),
                query_evidence=tuple(dict.fromkeys((*history.query_evidence, *raw_evidence))),
            )
            # The factor hash binds the preceding date, raw close, and both
            # histories' source hashes. The final signal rows stay unchanged.
            return await asyncio.to_thread(self.signal_corporate_loader, strategy, context_history,
                                           required_start=required_start)

    async def _load_history(
        self, run_id: RunId | None, strategy: StrategySpec, config: BacktestRunConfig, warmup: int,
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
            if run_id is not None:
                self._stage(run_id, BacktestJobState.RUNNING_DATA, 10,
                            "预热数据早于上市日，正在读取上市后数据；指标就绪后才判断信号，回测区间不变。")
            return await self._with_retry_progress(run_id, self.history.load(
                instrument_id=strategy.instrument.symbol,
                start=exc.listing_date,
                end=strategy.backtest.end,
                force_refresh=config.refresh_data,
            ))

    async def _with_retry_progress(self, run_id: RunId | None, request: Awaitable[_T]) -> _T:
        if run_id is None:
            # Retain any caller-installed dialogue retry observer.
            return await request
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
                if event.failure_reason:
                    label = mx_retry_message(event) + "你的策略已保留，无需重新提交。"
                elif event.data_incomplete:
                    label = "这次返回的数据还不完整，我正在补查，请稍等片刻。你的策略已保留，无需重新提交。"
                else:
                    label = (
                        "这次查询有点慢，我正在重新获取数据，请稍等片刻。"
                        "你的策略已保留，无需重新提交。"
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
        require_ready: bool = False,
        derived_condition_hashes: set[str] | None = None,
        price_rebases: tuple | None = None,
    ) -> tuple[
        tuple[SignalFact | None, ...],
        tuple[SignalFact | None, ...],
        tuple[ProviderIndicatorSeries, ...],
    ]:
        requests: dict[str, asyncio.Task[ProviderIndicatorSeries]] = {}
        gate = asyncio.Semaphore(2)
        session_dates = tuple(row.session_date for row in history.rows)
        eligible_indices = _candidate_signal_indices(strategy, history) if require_ready else ()

        async def fetch(condition: Condition | None, leg: str) -> tuple[SignalFact | None, ...]:
            if condition is None:
                return (None,) * len(history.rows)
            timelines: dict[str, tuple[SignalFact | None, ...]] = {}

            def keep(path: str, leaf: IndicatorCondition,
                     timeline: tuple[SignalFact | None, ...]) -> None:
                if require_ready and not any(timeline[index] is not None
                                             for index in eligible_indices):
                    raise SkillCandidatePreparationError(
                        "skill_indicator_history_not_ready",
                        "本次区间内，这条条件尚无可用于下一交易日执行的历史指标值。"
                        "请调整区间或指标周期；原买卖规则已保留。",
                        indicator_id=leaf.indicator_id, condition_path=f"{leg}.{path}",
                    )
                timelines[path] = timeline

            for path, leaf in provider_condition_leaves(condition):
                route = self.indicator_routes.get(leaf.indicator_id)
                if route is None or route.source == "unavailable":
                    raise UnsupportedSkillIndicatorError(
                        leaf.indicator_id, route.source_note if route else "缺少数据接入定义。",
                    )
                if (price_rebases is not None and route.source == "provider_indicator"
                        and route.fallback_source == "skill_ohlcv_python"):
                    # Explicit point-in-time factors select the already verified
                    # local formula, rather than an opaque precomputed series.
                    # Do not silently replace vendor-specific unsupported formulas.
                    route = replace(route, source="skill_ohlcv_python", fallback_source=None)
                    if derived_condition_hashes is not None:
                        derived_condition_hashes.add(canonical_hash(leaf))
                if route.source == "skill_ohlcv_python":
                    keep(path, leaf, _skill_derived_timeline(leaf, history, route=route, price_rebases=price_rebases))
                    continue
                if route.source == "skill_numeric_history":
                    key = canonical_hash({
                        "operator": leaf.indicator_id, "params": leaf.params,
                        "instrument": strategy.instrument.symbol,
                        "start": history.start.isoformat(), "end": history.end.isoformat(),
                    })
                    if key not in requests:
                        async def load_numeric(
                            numeric_condition: IndicatorCondition = leaf,
                        ) -> ProviderIndicatorSeries:
                            async with gate:
                                if self.numeric_discovery is None:
                                    raise ValueError("numeric history discovery is not configured")
                                return await prepare_skill_numeric_series(
                                    self.numeric_discovery, condition=numeric_condition,
                                    instrument_id=strategy.instrument.symbol,
                                    start=history.start, end=history.end,
                                    binding_reviewer=self.numeric_binding_reviewer,
                                    expected_session_dates=session_dates,
                                )
                        requests[key] = asyncio.create_task(load_numeric())
                    try:
                        numeric_series = await requests[key]
                    except SkillNumericHistoryError as exc:
                        exc.indicator_id = leaf.indicator_id
                        exc.condition_path = f"{leg}.{path}"
                        raise
                    keep(path, leaf, _skill_numeric_timeline(leaf, numeric_series, session_dates))
                    continue
                binding = binding_for_provider_condition(leaf)
                key = repr(
                    (leaf.indicator_id, binding.provider_indicator_name, binding.value_names,
                     sorted(leaf.params.items()))
                )
                if key not in requests:
                    request_parameters: ProviderConditionParameters = (
                        {"condition_params": leaf.params}
                        if leaf.indicator_id not in ACTIVE_SKILL_INDICATORS
                        or leaf.params.get("price_field", "close") != "close" else {}
                    )

                    async def load(
                        indicator_id: str = leaf.indicator_id,
                        name: str = binding.provider_indicator_name,
                        values: tuple[str, ...] = binding.value_names,
                        parameter_kwargs: ProviderConditionParameters = request_parameters,
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
                                    expected_session_dates=session_dates,
                                    **parameter_kwargs,
                                )
                            return await self.indicators.query_indicator_history(
                                instrument_id=strategy.instrument.symbol,
                                indicator_id=indicator_id,
                                provider_indicator_name=name,
                                value_names=values,
                                start=history.start,
                                end=history.end,
                                **parameter_kwargs,
                            )

                    requests[key] = asyncio.create_task(load())
                try:
                    series = await requests[key]
                except (MxSaasProviderDataError, MxSaasProviderUnavailableError,
                        ProviderIndicatorCacheMissError) as exc:
                    # Never use mismatched values. A formula route consumes the
                    # separately validated Skill OHLCV, with identical requested
                    # parameters and its own explicit provenance. A wrong-stock
                    # indicator response is never consumed. A bad indicator date
                    # axis cannot invalidate independently verified OHLCV. Corrupt
                    # values and authorization errors still fail closed.
                    recoverable = isinstance(exc, (
                        MxSaasProviderUnavailableError, ProviderIndicatorCacheMissError,
                    )) or (
                        exc.data_reason in {
                            "data_field_binding_mismatch", "data_fields_missing",
                            "data_history_unavailable", "data_unit_unconfirmed",
                            "data_security_mismatch",
                            "data_dates_mismatch",
                        }
                    )
                    if route.fallback_source != "skill_ohlcv_python" or not recoverable:
                        raise
                    formula_route = replace(
                        route, source="skill_ohlcv_python", fallback_source=None,
                    )
                    keep(path, leaf, _skill_derived_timeline(leaf, history, route=formula_route, price_rebases=price_rebases))
                    if derived_condition_hashes is not None:
                        derived_condition_hashes.add(canonical_hash(leaf))
                    logger.info(
                        "skill_indicator_formula_fallback indicator_id=%s reason=%s",
                        leaf.indicator_id, (
                            "indicator_cache_miss"
                            if isinstance(exc, ProviderIndicatorCacheMissError)
                            else getattr(exc, "data_reason", "transport_unavailable")
                        ),
                    )
                    continue
                if (
                    series.provider not in {"eastmoney_mx_finance_data", "eastmoney_mx_screener"}
                    or series.instrument_id != strategy.instrument.symbol
                ):
                    raise ValueError("unexpected indicator provider or instrument")
                keep(path, leaf, evaluate_provider_indicator_aligned(leaf, series, session_dates))
            return combine_condition_timelines(condition, timelines, session_dates)

        entry, exit_signals = await asyncio.gather(
            fetch(strategy.entry, "entry"), fetch(_exit_condition(strategy), "exit")
        )
        return entry, exit_signals, tuple(
            task.result() for task in requests.values()
            if not task.cancelled() and task.exception() is None
        )

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


def _candidate_signal_indices(strategy: StrategySpec, history: MxDailyHistory) -> tuple[int, ...]:
    if strategy.backtest.start < history.listing_date:
        raise MxDailyHistoryBeforeListingError(
            start=strategy.backtest.start, listing_date=history.listing_date,
        )
    _, eligible = _validate_input(
        strategy=strategy, history=history,
        entry_timeline=(None,) * len(history.rows), exit_timeline=(None,) * len(history.rows),
    )
    trading = tuple(index for index in eligible
                    if history.rows[index].trading_status is TradingStatus.TRADING
                    and history.rows[index].volume > 0)
    # This is an opportunity check, never a demand that a buy signal occurs or
    # an order fills. The simulator remains authoritative for limits and fills.
    if len(trading) < 2:
        raise SkillCandidatePreparationError(
            "skill_history_no_execution_session",
            "本次区间内没有足够交易日用于收盘判断条件并在后续交易日尝试成交。"
            "请调整回测区间；原买卖规则已保留。",
        )
    return trading[:-1]


def _skill_numeric_timeline(
    condition: IndicatorCondition,
    series: ProviderIndicatorSeries,
    session_dates: tuple[date, ...],
) -> tuple[SignalFact | None, ...]:
    """Reuse comparisons, retaining exact field identity and dependency clocks."""
    timeline = evaluate_provider_indicator_aligned(
        condition, series, session_dates, binding=skill_comparison_binding(condition),
    )
    identity = canonical_hash({
        "request": condition.params, "response": series.response_sha256,
        "fields": [
            {"code": item.field_code, "parameters": item.source_parameters, "unit": item.unit}
            for item in series.points[0].values
        ],
    })
    by_date = {point.session_date: point for point in series.points}
    needs_previous = condition.trigger in {"crosses_above", "crosses_below"}
    result: list[SignalFact | None] = []
    for index, fact in enumerate(timeline):
        if fact is None:
            result.append(None)
            continue
        available_at = fact.available_at
        if needs_previous and index > 0:
            previous = by_date.get(session_dates[index - 1])
            if previous is not None:
                available_at = max(available_at, previous.first_available_at)
        result.append(replace(
            fact, available_at=available_at,
            condition_ref=f"{fact.condition_ref}:{identity}",
            evidence=(SignalEvidence(
                evidence_type="skill_numeric_history",
                evidence_id=f"{identity}:{fact.session_date.isoformat()}",
                available_at=available_at, provider=series.provider,
                validation_status="provider_history_bound",
                raw_response_sha256=series.response_sha256,
            ),),
        ))
    return tuple(result)


def _skill_derived_timeline(
    condition: IndicatorCondition,
    history: MxDailyHistory,
    *,
    route: SkillIndicatorRoute | None = None,
    price_rebases: tuple | None = None,
) -> tuple[SignalFact | None, ...]:
    """Reuse the engine's causal formulas on real Skill bars, with explicit provenance."""

    route = (
        route if route is not None else default_skill_indicator_routes().get(condition.indicator_id)
    )
    if route is None or route.source != "skill_ohlcv_python":
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
    runtime = SignalRuntime()
    raw_bars = tuple(replace(bar, open=Price(row.raw_open), high=Price(row.raw_high),
                             low=Price(row.raw_low), close=Price(row.raw_close),
                             price_basis=PriceBasis.UNADJUSTED)
                     for bar, row in zip(bars, history.rows, strict=True))
    # A whole-query response hash is not causal: asking the provider for one
    # additional future day would change the evidence attached to every past
    # signal.  Build a content-addressed prefix chain instead.  Each day's
    # evidence commits only to provider rows and corporate-action facts that
    # were available by that decision close.
    source_prefix_hashes: list[str] = []
    row_chain = canonical_hash({
        "schema": "ashare-lab.skill-ohlcv-prefix.v1",
        "provider": history.provider,
        "instrumentId": history.instrument_id,
    })
    rebases = tuple(price_rebases or ())
    for row, bar in zip(history.rows, raw_bars, strict=True):
        row_chain = canonical_hash({
            "previous": row_chain,
            "row": {
                "sessionDate": row.session_date,
                "rawOpen": str(row.raw_open),
                "rawHigh": str(row.raw_high),
                "rawLow": str(row.raw_low),
                "rawClose": str(row.raw_close),
                "rawPreclose": str(row.raw_preclose),
                "volume": row.volume,
                "amount": str(row.amount),
                "tradingStatus": row.trading_status.value,
                "isSt": row.is_st,
                "upperLimit": None if row.upper_limit is None else str(row.upper_limit),
                "lowerLimit": None if row.lower_limit is None else str(row.lower_limit),
            },
        })
        active_rebases = [
            {
                "exDate": item.ex_date,
                "factor": str(item.factor),
                "availableAt": item.available_at,
                "sourceSha256": item.source_sha256,
            }
            for item in rebases
            if item.ex_date <= row.session_date and item.available_at <= bar.available_at
        ]
        source_prefix_hashes.append(canonical_hash({
            "ohlcvPrefix": row_chain,
            "priceRebases": active_rebases,
        }))
    if price_rebases is None:
        evaluated = runtime.evaluate_aligned(condition, bars, execution_bars=raw_bars)
    else:
        from ashare_lab.application.minute_price_rebase import dynamic_front_adjusted_bars
        evaluated = tuple(runtime.evaluate_aligned(condition,
            dynamic_front_adjusted_bars(raw_bars[:index + 1], price_rebases=price_rebases,
                                       as_of=bar.available_at),
            execution_bars=raw_bars[:index + 1])[-1] for index, bar in enumerate(raw_bars))
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
                    validation_status="local_formula_on_provider_ohlcv" if price_rebases is None else "local_formula_on_point_in_time_rebases",
                    raw_response_sha256=source_prefix_hash,
                ),
            ),
        )
        for fact, source_prefix_hash in zip(evaluated, source_prefix_hashes, strict=True)
    )


def _exit_condition(strategy: StrategySpec) -> Condition | None:
    from ashare_lab.domain.strategy import AllCondition

    if strategy.exit is None:
        return None

    children = tuple(
        child
        for child in strategy.exit.children
        if not isinstance(
            child,
            (HoldingPeriodExit, PositionReturnExit, TrailingDrawdownExit, MinuteProtectionExit),
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
    values["slippage_cny"] = Decimal(values.get("slippage_cny", "0"))
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
    indicator_routes: Mapping[str, SkillIndicatorRoute] | None = None,
    derived_condition_hashes: frozenset[str] = frozenset(),
    signal_adjustment_source: str | None = None,
    holding_calendar_evidence: Mapping[str, object] | None = None,
) -> BacktestResultBundle:
    routes = indicator_routes if indicator_routes is not None else default_skill_indicator_routes()
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
    if signal_adjustment_source is not None:
        warnings = [item for item in warnings if not item.startswith("本路径未逐日重建")]
        warnings.append("已配置的时点因子用于本地公式逐日动态前复权；供应商成品指标未因此取得同口径证明。因子来源实数验收和执行账户口径仍须单独核对。")
    derived = [
        {
            "indicatorId": leaf.indicator_id,
            "definitionVersion": leaf.definition_version,
            "source": "local_formula_on_eastmoney_skill_ohlcv",
            "formula": routes[leaf.indicator_id].formula_summary,
            "implementationRef": routes[leaf.indicator_id].implementation_ref,
            "parameters": json.dumps(leaf.params, ensure_ascii=False, sort_keys=True),
            "priceBasis": (
                "not_applicable"
                if leaf.indicator_id in {"market.volume", "amount.average", "volume.relative"}
                else "unadjusted" if leaf.indicator_id == "price.close"
                else "dynamic_front_adjusted" if signal_adjustment_source is not None
                else "provider_back_adjusted"
            ),
            "volumeBasis": "provider_unadjusted_shares",
            "sessionPolicy": (
                "positive_volume_sessions_including_current"
                if leaf.indicator_id in {"technical.ma", "technical.macd"}
                else "positive_volume_sessions_excluding_current"
                if leaf.indicator_id in {
                    "price.rolling_high", "volume.relative", "technical.donchian",
                }
                else "positive_volume_sessions; window_and_confirmation_follow_formula"
            ),
            **({"warmupPolicy": (
                "first_value_seeded_ema_over_available_positive_volume_history; "
                "values_after_slow_plus_signal_minus_one_sessions; "
                "cross_after_slow_plus_signal_sessions"
            )} if leaf.indicator_id == "technical.macd" else {}),
        }
        for condition in (strategy.entry, _exit_condition(strategy))
        if condition is not None
        for _, leaf in provider_condition_leaves(condition)
        if routes[leaf.indicator_id].source == "skill_ohlcv_python"
        or canonical_hash(leaf) in derived_condition_hashes
    ]
    if derived:
        warnings.append(
            "本次派生指标由现有引擎按策略周期计算，原始日线来自东方财富查数 Skill；"
            "按各指标定义处理当前值、历史窗口、预热与确认日，跳过零成交量日；"
            "不是 Skill 直接提供的成品指标。"
        )
        warnings.append(
            "递推指标采用目录声明的初始化及平滑方法，早期数值可能受可得预热历史影响；"
            "不保证与供应商成品指标或其他终端逐点一致。"
        )
    if any(item["indicatorId"] == "technical.macd" for item in derived):
        warnings.append(
            "MACD以本次取得历史中的首个有效值初始化EMA，逐日递推；"
            "累计slow+signal-1个有效交易日后提供数值，再多一个有效日才判断交叉。"
            "预热按策略周期向前取数，但节假日、停牌或上市时间可能缩短有效历史；"
            "预热不足时不生成信号，递推初值仍可能影响早期数值，不保证与其他终端逐点一致。"
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
                    **({"slippageCny": float(config.slippage_cny * 2)}
                       if config.slippage_cny else {}),
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
                    "属于近似研究，尚未完成逐日动态前复权核验，也不是逐笔分红到账的实盘账户。"
                    "策略结论与优化方向由真实模型另行分析。"
                ),
                "dataRange": {
                    "start": strategy.backtest.start,
                    "end": strategy.backtest.end,
                    "sessions": len(curve) - 1,
                },
                "warnings": warnings,
                "dataProvenance": {
                    **({"holdingCalendar": dict(holding_calendar_evidence)}
                       if holding_calendar_evidence is not None else {}),
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
                            "provider": series.provider,
                            "responseSha256": series.response_sha256,
                            "fieldName": value.field_name,
                            "sourceFieldName": value.source_field_name,
                            "sourceFieldCode": value.field_code,
                            "sourceParameters": (
                                json.loads(value.source_parameters or "{}").get("fixedParamValue")
                                if series.indicator_id in SKILL_COMPARISON_IDS
                                else value.source_parameters
                            ),
                            "unit": value.unit,
                            "sourceUnit": value.source_unit,
                            "unitNormalization": value.unit_normalization,
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
                "signalAdjustmentSource": signal_adjustment_source,
                "skillNumericSources": tuple(
                    json.dumps(asdict(series), ensure_ascii=False, sort_keys=True, default=str)
                    for series in indicators if series.indicator_id in SKILL_COMPARISON_IDS
                ),
                "hashSchemaVersion": RESULT_HASH_SCHEMA_VERSION,
                "openPositionShares": 0,
                "openPositionNotionalCny": float(result.open_position_notional_cny),
            },
        }
    )
    from ashare_lab.application.execution_feedback import zero_fill_execution_note
    bundle.summary.execution_note = zero_fill_execution_note(activities)
    payload = cast(dict[str, object], bundle.model_dump(mode="json", by_alias=True))
    bundle.audit.result_hash = calculate_result_bundle_hash(payload)
    return bundle
