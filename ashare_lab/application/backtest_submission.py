"""Create a fully pinned work item and enqueue only its durable run identity."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from math import ceil
from uuid import uuid4
from zoneinfo import ZoneInfo

from ashare_lab.domain.execution import CapacityMode, LimitHandling
from ashare_lab.domain.runs import ExecutionAssumptions, RunManifest
from ashare_lab.domain.shared import DomainValidationError, InstrumentId, RunId
from ashare_lab.domain.signals import (
    binding_for_provider_condition,
    provider_condition_leaves,
)
from ashare_lab.domain.strategy import (
    HoldingPeriodExit,
    IndicatorCondition,
    PositionReturnExit,
    StrategySpec,
    TrailingDrawdownExit,
    canonical_hash,
    canonical_json,
    iter_event_conditions,
    iter_indicator_conditions,
    strategy_requires_events,
    strategy_requires_financials,
)
from ashare_lab.domain.strategy.models import Condition
from ashare_lab.ports.backtest_runs import (
    BacktestJobQueue,
    BacktestJobState,
    BacktestRunRecord,
    BacktestRunStore,
    CreateRunResult,
)
from ashare_lab.ports.financial_data import FinancialFactLoader, PinnedFinancialFacts
from ashare_lab.ports.market_data import DataRequirements, DateRange, MarketDataRepository
from ashare_lab.ports.provider_indicator_data import (
    HistoricalIndicatorData,
    ProviderIndicatorSeries,
)

from .corporate_action_timeline import (
    CORPORATE_ACTION_POLICY,
    DIVIDEND_TAX_POLICY,
    RIGHTS_ISSUE_POLICY,
)
from .daily_backtest import (
    ENTRY_SIGNAL_VALIDITY_POLICY,
    MAX_ENTRY_VALIDITY_SESSIONS,
    OPENING_AUCTION_POLICY,
)
from .funded_benchmark import FUNDED_BENCHMARK_POLICY

MAX_WARMUP_CALENDAR_DAYS = 3_650
# Product input boundary, not a claim that every security has data since this date.
EARLIEST_A_SHARE_BACKTEST_DATE = date(1990, 1, 1)
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_DAILY_DATA_STABLE_AT = time(16, 5)


class EventDataUnavailableError(RuntimeError):
    """Raised before pinning when an event strategy has no executable dataset."""


class FinancialDataUnavailableError(RuntimeError):
    """Raised before queueing when financial facts cannot be pinned safely."""


class BacktestDataNotYetAvailableError(RuntimeError):
    """Raised when a requested backtest end is newer than stable daily data."""


class BacktestDateRangeError(ValueError):
    """Reject malformed historical ranges before queueing any provider work."""


def validate_a_share_backtest_range(start: date, end: date) -> None:
    if start < EARLIEST_A_SHARE_BACKTEST_DATE or start > end:
        raise BacktestDateRangeError("backtest start must be on or after 1990-01-01 and before end")


def latest_stable_a_share_data_date(observed_at: datetime) -> date:
    """Return the latest date eligible for a completed A-share daily snapshot."""

    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    shanghai = observed_at.astimezone(_SHANGHAI)
    if shanghai.timetz().replace(tzinfo=None) < _DAILY_DATA_STABLE_AT:
        return shanghai.date() - timedelta(days=1)
    return shanghai.date()


def _event_data_available_by_default() -> bool:
    return True


@dataclass(frozen=True, slots=True)
class BacktestRunConfig:
    participation_rate: Decimal = Decimal("0.05")
    slippage_bps: Decimal = Decimal("5")
    allocation_ratio: Decimal = Decimal("1")
    limit_handling: LimitHandling = LimitHandling.WAIT_FOR_UNLOCK
    commission_rate: Decimal = Decimal("0.0003")
    minimum_commission_cny: Decimal = Decimal("5")
    retry_unfilled_exits: bool = True
    max_exit_attempts: int = 20
    edge_entry_validity_sessions: int = 3
    event_entry_validity_sessions: int = 1
    state_entry_validity_sessions: int = 1
    warmup_calendar_days: int = 180
    settlement_extension_days: int = 14
    run_robustness: bool = True
    capacity_mode: CapacityMode = CapacityMode.POINT_IN_TIME_VOLUME
    refresh_data: bool = False

    def __post_init__(self) -> None:
        if type(self.refresh_data) is not bool:
            raise DomainValidationError("refresh_data must be a boolean")
        unit_interval = (self.participation_rate, self.allocation_ratio)
        if any(not Decimal("0") < item <= Decimal("1") for item in unit_interval):
            raise DomainValidationError("participation and allocation must be in (0, 1]")
        if not Decimal("0") <= self.slippage_bps <= Decimal("1000"):
            raise DomainValidationError("slippage_bps must be in [0, 1000]")
        if self.commission_rate < 0 or self.minimum_commission_cny < 0:
            raise DomainValidationError("commission terms cannot be negative")
        if self.max_exit_attempts < 1:
            raise DomainValidationError("max_exit_attempts must be positive")
        entry_validities = (
            self.edge_entry_validity_sessions,
            self.event_entry_validity_sessions,
            self.state_entry_validity_sessions,
        )
        if any(
            type(value) is not int or not 1 <= value <= MAX_ENTRY_VALIDITY_SESSIONS
            for value in entry_validities
        ):
            raise DomainValidationError(
                f"entry signal validity sessions must be integers in [1, "
                f"{MAX_ENTRY_VALIDITY_SESSIONS}]"
            )
        if self.event_entry_validity_sessions != 1:
            raise DomainValidationError(
                "event entry validity sessions must be 1 until point-in-time "
                "revision invalidation is supported"
            )
        if self.state_entry_validity_sessions != 1:
            raise DomainValidationError(
                "state entry validity sessions must be 1 until point-in-time "
                "state revalidation is supported"
            )
        if type(self.capacity_mode) is not CapacityMode:
            raise DomainValidationError("capacity_mode must be a CapacityMode")
        if (
            not 0 <= self.warmup_calendar_days <= MAX_WARMUP_CALENDAR_DAYS
            or self.settlement_extension_days < 1
        ):
            raise DomainValidationError("snapshot extensions are invalid")

    def as_dict(
        self,
        *,
        snapshot_period: DateRange,
        effective_warmup_calendar_days: int,
    ) -> dict[str, object]:
        return {
            "allocation_ratio": str(self.allocation_ratio),
            "capacity_mode": self.capacity_mode.value,
            "commission_rate": str(self.commission_rate),
            "edge_entry_validity_sessions": self.edge_entry_validity_sessions,
            "event_entry_validity_sessions": self.event_entry_validity_sessions,
            "limit_handling": self.limit_handling.value,
            "max_exit_attempts": self.max_exit_attempts,
            "minimum_commission_cny": str(self.minimum_commission_cny),
            "participation_rate": str(self.participation_rate),
            "retry_unfilled_exits": self.retry_unfilled_exits,
            "run_robustness": self.run_robustness,
            "settlement_extension_days": self.settlement_extension_days,
            "slippage_bps": str(self.slippage_bps),
            "state_entry_validity_sessions": self.state_entry_validity_sessions,
            "snapshot_end": snapshot_period.end.isoformat(),
            "snapshot_start": snapshot_period.start.isoformat(),
            "effective_warmup_calendar_days": effective_warmup_calendar_days,
            "warmup_calendar_days": self.warmup_calendar_days,
        }


@dataclass(frozen=True, slots=True)
class SubmissionVersions:
    catalog_hash: str
    engine_version: str
    code_revision: str
    fee_schedule_version: str = "cn.a_share.cash_equity_fees.2015_present.v1"
    market_rule_version: str = "cn_a.daily_market_rules.fallback.v2"
    opening_auction_policy: str = OPENING_AUCTION_POLICY
    entry_signal_validity_policy: str = ENTRY_SIGNAL_VALIDITY_POLICY
    benchmark_policy: str = FUNDED_BENCHMARK_POLICY
    corporate_action_policy: str = CORPORATE_ACTION_POLICY
    dividend_tax_policy: str = DIVIDEND_TAX_POLICY
    rights_issue_policy: str = RIGHTS_ISSUE_POLICY


class BacktestSubmissionService:
    def __init__(
        self,
        *,
        market_data: MarketDataRepository,
        run_store: BacktestRunStore,
        job_queue: BacktestJobQueue,
        versions: SubmissionVersions,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        run_id_factory: Callable[[], RunId] = lambda: RunId(f"run:{uuid4().hex}"),
        event_data_available: Callable[[], bool] = _event_data_available_by_default,
        financial_fact_loader: FinancialFactLoader | None = None,
        provider_indicator_data: HistoricalIndicatorData | None = None,
        latest_stable_data_date: Callable[[datetime], date] | None = None,
    ) -> None:
        self._market_data = market_data
        self._run_store = run_store
        self._job_queue = job_queue
        self._versions = versions
        self._clock = clock
        self._run_id_factory = run_id_factory
        self._event_data_available = event_data_available
        self._financial_fact_loader = financial_fact_loader
        self._provider_indicator_data = provider_indicator_data
        self._latest_stable_data_date = latest_stable_data_date

    def submit(
        self,
        strategy: StrategySpec,
        config: BacktestRunConfig,
        *,
        run_id: RunId | None = None,
    ) -> CreateRunResult:
        requires_events = strategy_requires_events(strategy)
        requires_financials = strategy_requires_financials(strategy)
        requires_event_document_text = any(
            condition.document_text is not None for condition in iter_event_conditions(strategy)
        )
        if requires_events and not self._event_data_available():
            raise EventDataUnavailableError(
                "event strategy requires an available events.parquet dataset"
            )
        instrument_id = InstrumentId(strategy.instrument.symbol)
        effective_warmup = _effective_warmup_calendar_days(strategy, config)
        created_at = self._clock()
        requested_snapshot_end = strategy.backtest.end + timedelta(
            days=config.settlement_extension_days
        )
        snapshot_end = requested_snapshot_end
        available_data_end: date | None = None
        if self._latest_stable_data_date is not None:
            available_data_end = self._latest_stable_data_date(created_at)
            if strategy.backtest.end > available_data_end:
                raise BacktestDataNotYetAvailableError(
                    "backtest end exceeds the latest stable A-share daily data date"
                )
            snapshot_end = min(requested_snapshot_end, available_data_end)
        period = DateRange(
            start=strategy.backtest.start - timedelta(days=effective_warmup),
            end=snapshot_end,
        )
        financial_bundle: PinnedFinancialFacts | None = None
        if requires_financials:
            if self._financial_fact_loader is None:
                raise FinancialDataUnavailableError("financial_data_loader_unavailable")
            try:
                financial_bundle = self._financial_fact_loader.load(
                    strategy,
                    period,
                    retrieved_at=created_at,
                )
            except Exception as exc:
                raise FinancialDataUnavailableError(str(exc)) from exc
        snapshot = self._market_data.pin_snapshot(
            DataRequirements(
                instruments=(instrument_id,),
                datasets=(
                    ("daily_ohlcv", "corporate_actions", "events")
                    if requires_events
                    else ("daily_ohlcv", "corporate_actions")
                ),
                event_codes=(
                    tuple(
                        sorted(
                            {condition.event_code for condition in iter_event_conditions(strategy)}
                        )
                    )
                    if requires_events
                    else ()
                ),
                needs_event_document_text=requires_event_document_text,
            ),
            period,
        )
        config_payload = config.as_dict(
            snapshot_period=period,
            effective_warmup_calendar_days=effective_warmup,
        )
        if available_data_end is not None:
            config_payload["requested_snapshot_end"] = requested_snapshot_end.isoformat()
            config_payload["available_data_end"] = available_data_end.isoformat()
        provider_indicator_payload: dict[str, object] | None = None
        if (
            self._provider_indicator_data is not None
            and not requires_events
            and not requires_financials
        ):
            provider_indicator_payload = _load_provider_indicator_payload(
                strategy=strategy,
                period=period,
                provider=self._provider_indicator_data,
            )
            if provider_indicator_payload is not None:
                config_payload["provider_indicator_series"] = provider_indicator_payload
        financial_snapshot_payload: dict[str, object] | None = None
        if financial_bundle is not None:
            financial_snapshot_payload = _financial_snapshot_payload(financial_bundle)
            config_payload["financial_snapshot"] = financial_snapshot_payload
            config_payload["financial_facts"] = [
                item.model_dump(mode="json") for item in financial_bundle.facts
            ]
        manifest = RunManifest(
            run_id=run_id or self._run_id_factory(),
            strategy_hash=canonical_hash(strategy),
            catalog_hash=self._versions.catalog_hash,
            config_hash=canonical_hash(config_payload),
            data_snapshot=snapshot,
            strategy_schema_version=strategy.schema_version,
            engine_version=self._versions.engine_version,
            code_revision=self._versions.code_revision,
            period_start=strategy.backtest.start,
            period_end=strategy.backtest.end,
            initial_cash_cny=str(strategy.backtest.initial_cash_cny),
            assumptions=ExecutionAssumptions(
                resolution="1d",
                price_limit_mode=config.limit_handling.value,
                participation_rate=str(config.participation_rate),
                slippage_bps=str(config.slippage_bps),
                commission_rate=str(config.commission_rate),
                minimum_commission_cny=str(config.minimum_commission_cny),
                fee_schedule_version=self._versions.fee_schedule_version,
                market_rule_version=self._versions.market_rule_version,
                opening_auction_policy=self._versions.opening_auction_policy,
                entry_signal_validity_policy=self._versions.entry_signal_validity_policy,
                edge_entry_validity_sessions=str(config.edge_entry_validity_sessions),
                event_entry_validity_sessions=str(config.event_entry_validity_sessions),
                state_entry_validity_sessions=str(config.state_entry_validity_sessions),
                allocation_ratio=str(config.allocation_ratio),
                retry_unfilled_exits=str(config.retry_unfilled_exits).lower(),
                max_exit_attempts=str(config.max_exit_attempts),
                capacity_mode=config.capacity_mode.value,
                benchmark_policy=self._versions.benchmark_policy,
                corporate_action_policy=self._versions.corporate_action_policy,
                dividend_tax_policy=self._versions.dividend_tax_policy,
                rights_issue_policy=self._versions.rights_issue_policy,
                robustness_profile=("execution.v1" if config.run_robustness else "disabled"),
            ),
            created_at=created_at,
        )
        record = BacktestRunRecord(
            run_id=manifest.run_id,
            fingerprint=manifest.fingerprint,
            strategy_json=canonical_json(strategy),
            manifest_json=canonical_json(
                _manifest_payload(
                    manifest,
                    financial_snapshot=financial_snapshot_payload,
                    provider_indicator_snapshot=provider_indicator_payload,
                )
            ),
            config_json=canonical_json(config_payload),
            state=BacktestJobState.QUEUED,
            progress_percent=0,
            progress_label="等待计算资源",
            created_at=created_at,
            updated_at=created_at,
        )
        result = self._run_store.create_or_get(record)
        if result.record.state is BacktestJobState.QUEUED:
            self._job_queue.enqueue(result.record.run_id)
        return result


def _manifest_payload(
    manifest: RunManifest,
    *,
    financial_snapshot: Mapping[str, object] | None = None,
    provider_indicator_snapshot: Mapping[str, object] | None = None,
) -> dict[str, object]:
    snapshot_payload: dict[str, object] = {
        "checksum": manifest.data_snapshot.checksum,
        "created_at": manifest.data_snapshot.created_at.isoformat(),
        "producer_schema_version": manifest.data_snapshot.producer_schema_version,
        "schema_version": manifest.data_snapshot.schema_version,
        "snapshot_id": str(manifest.data_snapshot.snapshot_id),
    }
    if manifest.data_snapshot.producer_snapshot_id is not None:
        snapshot_payload["producer_snapshot_id"] = manifest.data_snapshot.producer_snapshot_id
    payload: dict[str, object] = {
        "assumptions": manifest.assumptions.as_dict(),
        "catalog_hash": manifest.catalog_hash,
        "code_revision": manifest.code_revision,
        "config_hash": manifest.config_hash,
        "created_at": manifest.created_at.isoformat(),
        "data_snapshot": snapshot_payload,
        "engine_version": manifest.engine_version,
        "fingerprint": manifest.fingerprint,
        "initial_cash_cny": manifest.initial_cash_cny,
        "period_end": manifest.period_end.isoformat(),
        "period_start": manifest.period_start.isoformat(),
        "random_seed": manifest.random_seed,
        "run_id": str(manifest.run_id),
        "strategy_hash": manifest.strategy_hash,
        "strategy_schema_version": manifest.strategy_schema_version,
    }
    if financial_snapshot is not None:
        payload["financial_snapshot"] = dict(financial_snapshot)
    if provider_indicator_snapshot is not None:
        payload["provider_indicator_series"] = dict(provider_indicator_snapshot)
    return payload


def _load_provider_indicator_payload(
    *,
    strategy: StrategySpec,
    period: DateRange,
    provider: HistoricalIndicatorData,
) -> dict[str, object] | None:
    exit_condition = _exit_condition(strategy)
    entry, exit_series = asyncio.run(
        _load_provider_indicator_sides(
            entry_condition=strategy.entry,
            exit_condition=exit_condition,
            strategy=strategy,
            period=period,
            provider=provider,
        )
    )
    if not entry and not exit_series:
        return None
    identity_basis = {
        "entry": entry,
        "exit": exit_series,
    }
    checksum = canonical_hash(identity_basis)
    return {
        "checksum": checksum,
        "identity_basis": identity_basis,
        "schema_version": "ashare-lab.provider-indicator-series.v1",
        "snapshot_id": "provider-indicators:" + checksum.removeprefix("sha256:"),
    }


async def _load_provider_indicator_sides(
    *,
    entry_condition: Condition,
    exit_condition: Condition | None,
    strategy: StrategySpec,
    period: DateRange,
    provider: HistoricalIndicatorData,
) -> tuple[tuple[dict[str, object], ...], tuple[dict[str, object], ...]]:
    """Fetch independent provider series concurrently, preserving tree order."""

    entry_task = _provider_series_for_condition(
        entry_condition,
        strategy=strategy,
        period=period,
        provider=provider,
    )
    exit_task = (
        _provider_series_for_condition(
            exit_condition,
            strategy=strategy,
            period=period,
            provider=provider,
        )
        if exit_condition is not None
        else _empty_provider_series()
    )
    entry, exit_series = await asyncio.gather(entry_task, exit_task)
    return entry, exit_series


async def _empty_provider_series() -> tuple[dict[str, object], ...]:
    return ()


async def _provider_series_for_condition(
    condition: Condition,
    *,
    strategy: StrategySpec,
    period: DateRange,
    provider: HistoricalIndicatorData,
) -> tuple[dict[str, object], ...]:
    leaves = provider_condition_leaves(condition)

    async def fetch(path: str, leaf: object) -> dict[str, object]:
        if not isinstance(leaf, IndicatorCondition):
            raise TypeError("provider condition leaf must be an IndicatorCondition")
        binding = binding_for_provider_condition(leaf)
        series = await provider.query_indicator_history(
            instrument_id=strategy.instrument.symbol,
            indicator_id=leaf.indicator_id,
            provider_indicator_name=binding.provider_indicator_name,
            value_names=binding.value_names,
            start=period.start,
            end=period.end,
        )
        return {
            "path": path,
            "series": _provider_indicator_series_payload(series),
        }

    return tuple(
        await asyncio.gather(*(fetch(path, leaf) for path, leaf in leaves))
    )


def _provider_indicator_series_payload(series: ProviderIndicatorSeries) -> dict[str, object]:
    return {
        "provider": series.provider,
        "instrument_id": series.instrument_id,
        "indicator_id": series.indicator_id,
        "requested_start": series.requested_start.isoformat(),
        "requested_end": series.requested_end.isoformat(),
        "response_sha256": series.response_sha256,
        "retrieved_at": series.retrieved_at.isoformat(),
        "schema_version": series.schema_version,
        "query": series.query,
        "points": [
            {
                "session_date": point.session_date.isoformat(),
                "observed_at": point.observed_at.isoformat(),
                "first_available_at": point.first_available_at.isoformat(),
                "values": [
                    {
                        "field_code": value.field_code,
                        "field_name": value.field_name,
                        "value": str(value.value),
                        "unit": value.unit,
                    }
                    for value in point.values
                ],
            }
            for point in series.points
        ],
    }


def _exit_condition(strategy: StrategySpec) -> Condition | None:
    children = tuple(
        child
        for child in strategy.exit.children
        if not isinstance(
            child,
            (HoldingPeriodExit, PositionReturnExit, TrailingDrawdownExit),
        )
    )
    if not children:
        return None
    from ashare_lab.domain.strategy import AnyCondition

    return children[0] if len(children) == 1 else AnyCondition(children=children)


def _financial_snapshot_payload(bundle: PinnedFinancialFacts) -> dict[str, object]:
    return {
        "checksum": bundle.checksum,
        "coverage_end": bundle.coverage_end.isoformat(),
        "coverage_start": bundle.coverage_start.isoformat(),
        "identity_basis": bundle.identity_basis,
        "provider": bundle.provider,
        "schema_version": bundle.schema_version,
        "snapshot_id": bundle.snapshot_id,
    }


def _effective_warmup_calendar_days(
    strategy: StrategySpec,
    config: BacktestRunConfig,
) -> int:
    """Translate the largest declared indicator dependency into calendar history."""

    required_trading_bars = 1
    for condition in iter_indicator_conditions(strategy):
        params = condition.params
        if condition.indicator_id == "technical.macd":
            slow = params.get("slow")
            signal = params.get("signal")
            if isinstance(slow, int) and not isinstance(slow, bool):
                required_trading_bars = max(required_trading_bars, slow)
            if (
                isinstance(slow, int)
                and not isinstance(slow, bool)
                and isinstance(signal, int)
                and not isinstance(signal, bool)
            ):
                required_trading_bars = max(required_trading_bars, slow + signal)
            continue

        if condition.indicator_id == "technical.ma_cross":
            required_trading_bars = max(
                required_trading_bars,
                _positive_int_param(params, "slow_period")
                + _cross_trigger_extra(condition.trigger),
            )
            continue

        if condition.indicator_id == "technical.rsi":
            # Wilder RSI(period) first exists after ``period + 1`` closes;
            # a threshold crossing additionally needs the previous RSI point.
            required_trading_bars = max(
                required_trading_bars,
                _positive_int_param(params, "period") + 1 + _cross_trigger_extra(condition.trigger),
            )
            continue

        if condition.indicator_id == "technical.bbi":
            longest = _positive_int_param(params, "period_4")
            required_trading_bars = max(
                required_trading_bars,
                longest + _cross_trigger_extra(condition.trigger),
            )
            continue

        if condition.indicator_id in {"price.return_pct", "price.rolling_high"}:
            period = _positive_int_param(params, "period")
            required_trading_bars = max(
                required_trading_bars,
                period
                + 1
                + (
                    _cross_trigger_extra(condition.trigger)
                    if condition.indicator_id == "price.return_pct"
                    else 0
                ),
            )
            continue

        if condition.indicator_id == "price.consecutive_up":
            required_trading_bars = max(
                required_trading_bars,
                _positive_int_param(params, "days") + 1,
            )
            continue

        if condition.indicator_id in {"price.close", "price.amplitude", "market.amount"}:
            base_bars = 2 if condition.indicator_id == "price.amplitude" else 1
            required_trading_bars = max(
                required_trading_bars,
                base_bars + _cross_trigger_extra(condition.trigger),
            )
            continue

        if condition.indicator_id == "amount.average":
            required_trading_bars = max(
                required_trading_bars,
                _positive_int_param(params, "period") + _cross_trigger_extra(condition.trigger),
            )
            continue

        if condition.indicator_id == "volume.relative":
            baseline = _positive_int_param(params, "baseline_period")
            consecutive = (
                _positive_int_param(params, "consecutive_days")
                if condition.trigger == "consecutive_gte_multiple"
                else 1
            )
            required_trading_bars = max(
                required_trading_bars,
                baseline + consecutive,
            )
            continue

        if condition.indicator_id == "volume.price_confirmation":
            required_trading_bars = max(
                required_trading_bars,
                _positive_int_param(params, "baseline_period") + 1,
            )
            continue

        if condition.indicator_id == "technical.obv":
            required_trading_bars = max(required_trading_bars, 2)
            continue

        if condition.indicator_id == "volume.price_divergence":
            pivot_history = max(
                _positive_int_param(params, "average_volume_period"),
                _positive_int_param(params, "left_bars")
                + _positive_int_param(params, "min_separation"),
            )
            required_trading_bars = max(
                required_trading_bars,
                pivot_history
                + _positive_int_param(params, "max_separation")
                + _positive_int_param(params, "right_bars")
                + 1,
            )
            continue

        if condition.indicator_id == "technical.trend_regime":
            confirmation = _positive_int_param(params, "confirmation_days")
            base = max(
                _positive_int_param(params, "stability_bars"),
                _positive_int_param(params, "long_period"),
                _positive_int_param(params, "short_period")
                + _positive_int_param(params, "slope_lookback"),
                2 * _positive_int_param(params, "adx_period"),
            )
            required_trading_bars = max(
                required_trading_bars,
                base + confirmation - 1,
            )
            continue

        if condition.indicator_id == "price.true_range":
            required_trading_bars = max(
                required_trading_bars,
                2 + _cross_trigger_extra(condition.trigger),
            )
            continue

        if condition.indicator_id in {"technical.atr", "technical.natr"}:
            required_trading_bars = max(
                required_trading_bars,
                _positive_int_param(params, "period") + 1 + _cross_trigger_extra(condition.trigger),
            )
            continue

        if condition.indicator_id in {"technical.adx", "technical.dmi"}:
            required_trading_bars = max(
                required_trading_bars,
                2 * _positive_int_param(params, "period") + _cross_trigger_extra(condition.trigger),
            )
            continue

        if condition.indicator_id == "technical.bias":
            required_trading_bars = max(
                required_trading_bars,
                _positive_int_param(params, "period") + _cross_trigger_extra(condition.trigger),
            )
            continue

        if condition.indicator_id in {
            "technical.roc",
            "technical.momentum",
            "technical.return_stddev",
            "technical.historical_volatility",
        }:
            required_trading_bars = max(
                required_trading_bars,
                _positive_int_param(params, "period") + 1 + _cross_trigger_extra(condition.trigger),
            )
            continue

        if condition.indicator_id == "technical.stochastic":
            required_trading_bars = max(
                required_trading_bars,
                _positive_int_param(params, "k_period")
                + _positive_int_param(params, "d_period")
                - 1
                + _cross_trigger_extra(condition.trigger),
            )
            continue

        if condition.indicator_id == "technical.williams_r":
            required_trading_bars = max(
                required_trading_bars,
                _positive_int_param(params, "period") + _cross_trigger_extra(condition.trigger),
            )
            continue

        if condition.indicator_id == "technical.donchian":
            required_trading_bars = max(
                required_trading_bars,
                _positive_int_param(params, "period") + 1 + _cross_trigger_extra(condition.trigger),
            )
            continue

        parameter_name = {
            "technical.ma": "period",
            "technical.ema": "period",
            "technical.bollinger": "period",
            "technical.kdj": "period",
            "technical.cci": "period",
            "technical.ema_bias": "period",
            "market.volume": "baseline_period",
        }.get(condition.indicator_id)
        value = params.get(parameter_name) if parameter_name is not None else None
        if isinstance(value, int) and not isinstance(value, bool):
            crossover_extra = _cross_trigger_extra(condition.trigger)
            required_trading_bars = max(required_trading_bars, value + crossover_extra)

    # A plain 7/5 weekday conversion is not enough for the mainland calendar:
    # long Spring Festival/National Day closures accumulate over multi-year
    # lookbacks.  Eight calendar days per five required bars plus a two-week
    # guard is deliberately conservative until submission can invert the pinned
    # exchange-session calendar directly.
    market_calendar_estimate = ceil(required_trading_bars * 8 / 5) + 14
    effective_warmup = max(config.warmup_calendar_days, market_calendar_estimate)
    if effective_warmup > MAX_WARMUP_CALENDAR_DAYS:
        raise DomainValidationError(
            "indicator parameters require "
            f"{effective_warmup} calendar days of warmup, exceeding the "
            f"{MAX_WARMUP_CALENDAR_DAYS}-day snapshot limit; reduce the lookback parameters"
        )
    return effective_warmup


def _positive_int_param(params: Mapping[str, object], name: str) -> int:
    value = params.get(name)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return 1


def _cross_trigger_extra(trigger: str) -> int:
    return 1 if "cross" in trigger else 0
