"""Idempotent worker use case for one durable daily-backtest work item."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol, cast

from ashare_lab.application.corporate_action_timeline import (
    TimelineCorporateActionApplier,
)
from ashare_lab.application.daily_backtest import (
    DailyBacktestConfig,
    DailyBacktestInput,
    run_daily_backtest,
)
from ashare_lab.application.funded_benchmark import (
    FundedBenchmarkInput,
    FundedBuyAndHoldConfig,
    run_funded_buy_and_hold,
)
from ashare_lab.application.result_views import build_result_bundle
from ashare_lab.application.robustness import run_execution_robustness
from ashare_lab.domain.execution import (
    AshareExchange,
    CapacityMode,
    FeeCalculator,
    FeePolicy,
    LimitHandling,
    TradingCalendar,
)
from ashare_lab.domain.financials import FinancialFactRecord
from ashare_lab.domain.market_data import DataSnapshotRef
from ashare_lab.domain.provenance import SourceRef
from ashare_lab.domain.runs import ExecutionAssumptions, RunManifest
from ashare_lab.domain.shared import InstrumentId, Money, RunId, StrongId
from ashare_lab.domain.strategy import (
    StrategySpec,
    canonical_hash,
    canonical_json,
    iter_event_conditions,
    strategy_requires_events,
    strategy_requires_financials,
)
from ashare_lab.domain.time import PointInTimeAvailability
from ashare_lab.ports.backtest_runs import (
    BacktestJobState,
    BacktestRunRecord,
    BacktestRunStore,
)
from ashare_lab.ports.market_data import DataRequirements, DateRange, MarketDataRepository
from ashare_lab.ports.session_reference import SessionReferenceProvider


class BacktestWorkItemError(RuntimeError):
    """A durable work item is missing, mutated or internally inconsistent."""


class RuntimeVersionSource(Protocol):
    """Submission/runtime versions required to construct worker identity."""

    @property
    def catalog_hash(self) -> str: ...

    @property
    def engine_version(self) -> str: ...

    @property
    def code_revision(self) -> str: ...

    @property
    def fee_schedule_version(self) -> str: ...

    @property
    def market_rule_version(self) -> str: ...

    @property
    def opening_auction_policy(self) -> str: ...

    @property
    def entry_signal_validity_policy(self) -> str: ...

    @property
    def benchmark_policy(self) -> str: ...

    @property
    def corporate_action_policy(self) -> str: ...

    @property
    def dividend_tax_policy(self) -> str: ...

    @property
    def rights_issue_policy(self) -> str: ...


@dataclass(frozen=True, slots=True)
class WorkerRuntimeIdentity:
    """Current worker implementation identity that queued work must match."""

    catalog_hash: str
    engine_version: str
    code_revision: str
    fee_schedule_version: str
    market_rule_version: str
    opening_auction_policy: str
    entry_signal_validity_policy: str
    benchmark_policy: str
    corporate_action_policy: str
    dividend_tax_policy: str
    rights_issue_policy: str
    strict_replay: bool

    @classmethod
    def from_submission_versions(
        cls,
        versions: RuntimeVersionSource,
        *,
        strict_replay: bool,
    ) -> WorkerRuntimeIdentity:
        return cls(
            catalog_hash=versions.catalog_hash,
            engine_version=versions.engine_version,
            code_revision=versions.code_revision,
            fee_schedule_version=versions.fee_schedule_version,
            market_rule_version=versions.market_rule_version,
            opening_auction_policy=versions.opening_auction_policy,
            entry_signal_validity_policy=versions.entry_signal_validity_policy,
            benchmark_policy=versions.benchmark_policy,
            corporate_action_policy=versions.corporate_action_policy,
            dividend_tax_policy=versions.dividend_tax_policy,
            rights_issue_policy=versions.rights_issue_policy,
            strict_replay=strict_replay,
        )


@dataclass(frozen=True, slots=True)
class BacktestExecutionService:
    market_data: MarketDataRepository
    session_reference: SessionReferenceProvider
    run_store: BacktestRunStore
    runtime_identity: WorkerRuntimeIdentity
    sessions_from_snapshot: bool = False

    def execute(self, run_id: RunId) -> BacktestRunRecord:
        """Replay a pinned work item; terminal duplicate deliveries are no-ops."""

        record = self._require_record(run_id)
        if record.state.is_terminal:
            return record
        if record.state is BacktestJobState.CANCEL_REQUESTED:
            return self._cancel(record)
        if record.state is not BacktestJobState.QUEUED:
            # A delivery that did not atomically claim QUEUED is a duplicate.
            # The owner is the only worker allowed to advance the run.
            return record

        try:
            record = self.run_store.transition(
                run_id,
                expected=(BacktestJobState.QUEUED,),
                target=BacktestJobState.RUNNING_DATA,
                progress_percent=10,
                progress_label="固定并读取数据快照",
                expected_version=record.version,
            )
        except Exception:
            current = self._require_record(run_id)
            if current.state is not BacktestJobState.QUEUED:
                return current
            raise

        try:
            strategy = StrategySpec.model_validate_json(record.strategy_json)
            manifest = _object_json(record.manifest_json, "manifest_json")
            config = _object_json(record.config_json, "config_json")
            _validate_identity(
                record,
                strategy,
                manifest,
                config,
                runtime_identity=self.runtime_identity,
            )
            period = DateRange(
                start=date.fromisoformat(_text(config, "snapshot_start")),
                end=date.fromisoformat(_text(config, "snapshot_end")),
            )
            instrument_id = InstrumentId(strategy.instrument.symbol)
            requires_events = strategy_requires_events(strategy)
            requires_financials = strategy_requires_financials(strategy)
            requires_event_document_text = any(
                condition.document_text is not None for condition in iter_event_conditions(strategy)
            )
            expected_snapshot = _mapping(manifest, "data_snapshot")
            snapshot = self.market_data.pin_snapshot(
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
                                {
                                    condition.event_code
                                    for condition in iter_event_conditions(strategy)
                                }
                            )
                        )
                        if requires_events
                        else ()
                    ),
                    needs_event_document_text=requires_event_document_text,
                    expected_snapshot_id=StrongId(_text(expected_snapshot, "snapshot_id")),
                    expected_snapshot_checksum=_text(expected_snapshot, "checksum"),
                    expected_snapshot_schema_version=_text(
                        expected_snapshot,
                        "schema_version",
                    ),
                    expected_producer_snapshot_schema_version=_optional_text(
                        expected_snapshot,
                        "producer_schema_version",
                    ),
                    expected_producer_snapshot_id=_optional_text(
                        expected_snapshot,
                        "producer_snapshot_id",
                    ),
                ),
                period,
            )
            if str(snapshot.snapshot_id) != _text(expected_snapshot, "snapshot_id"):
                raise BacktestWorkItemError("data_snapshot_id_mismatch")
            if snapshot.checksum != _text(expected_snapshot, "checksum"):
                raise BacktestWorkItemError("data_snapshot_checksum_mismatch")
            if snapshot.schema_version != _text(expected_snapshot, "schema_version"):
                raise BacktestWorkItemError("data_snapshot_schema_version_mismatch")
            if snapshot.producer_schema_version != _optional_text(
                expected_snapshot,
                "producer_schema_version",
            ):
                raise BacktestWorkItemError("producer_snapshot_schema_version_mismatch")
            if snapshot.producer_snapshot_id != _optional_text(
                expected_snapshot,
                "producer_snapshot_id",
            ):
                raise BacktestWorkItemError("producer_snapshot_id_mismatch")
            bars = tuple(self.market_data.load_daily_bars(snapshot, instrument_id, period))
            if not bars:
                raise BacktestWorkItemError("data_snapshot_contains_no_daily_bars")
            signal_bars = tuple(self.market_data.load_signal_bars(snapshot, instrument_id, period))
            events = (
                tuple(self.market_data.load_events(snapshot, instrument_id, period))
                if requires_events
                else ()
            )
            financial_facts = _financial_facts_from_work_item(
                strategy,
                manifest,
                config,
            )
            corporate_actions = tuple(
                self.market_data.load_corporate_actions(snapshot, instrument_id, period)
            )

            record = self._advance(
                record,
                BacktestJobState.RUNNING_SIGNAL,
                progress=35,
                label=(
                    "计算技术、财务与事件信号"
                    if requires_events and requires_financials
                    else "计算技术与事件信号"
                    if requires_events
                    else "计算技术与财务信号"
                    if requires_financials
                    else "计算技术信号"
                ),
            )
            if record.state.is_terminal:
                return record
            sessions = (
                tuple(self.market_data.load_sessions(snapshot, instrument_id, period))
                if self.sessions_from_snapshot
                else tuple(self.session_reference.sessions_for(bars))
            )
            _validate_session_axis(bars, sessions)
            assumptions = _mapping(manifest, "assumptions")
            if self.session_reference.version != _text(assumptions, "market_rule_version"):
                raise BacktestWorkItemError("market_rule_version_mismatch")

            record = self._advance(
                record,
                BacktestJobState.RUNNING_EXECUTION,
                progress=55,
                label="执行 A 股撮合与账本",
            )
            if record.state.is_terminal:
                return record
            calendar = TradingCalendar(
                version=f"{snapshot.schema_version}:{snapshot.checksum}",
                sessions=tuple(bar.session_date for bar in bars),
            )
            engine_config = _engine_config(config)
            fees = _fee_calculator(strategy, config)
            active_actions = tuple(
                action
                for action in corporate_actions
                if strategy.backtest.start <= action.record_date <= strategy.backtest.end
            )
            action_applier = TimelineCorporateActionApplier(active_actions)
            benchmark = run_funded_buy_and_hold(
                FundedBenchmarkInput(
                    run_key=f"{run_id.value}:funded-benchmark",
                    period_start=strategy.backtest.start,
                    period_end=strategy.backtest.end,
                    bars=bars,
                    sessions=sessions,
                    calendar=calendar,
                    fee_calculator=fees,
                    corporate_actions=action_applier,
                    config=FundedBuyAndHoldConfig(
                        initial_cash=Money(
                            Decimal(strategy.backtest.initial_cash_cny),
                            "CNY",
                        ),
                        participation_rate=engine_config.participation_rate,
                        slippage_bps=engine_config.slippage_bps,
                        limit_handling=engine_config.limit_handling,
                        allocation_ratio=engine_config.allocation_ratio,
                        capacity_mode=engine_config.capacity_mode,
                    ),
                )
            )
            engine_input = DailyBacktestInput(
                run_key=run_id.value,
                strategy=strategy,
                bars=bars,
                signal_bars=(signal_bars if signal_bars != bars else None),
                sessions=sessions,
                calendar=calendar,
                fee_calculator=fees,
                events=events,
                financial_facts=financial_facts,
                corporate_actions=active_actions,
                config=engine_config,
                benchmark_equity=benchmark.funded_equity_path,
                benchmark_initial_equity=benchmark.initial_cash.amount,
                benchmark_entry_filled=benchmark.entry_fill is not None,
            )
            result = run_daily_backtest(engine_input)

            record = self._advance(
                record,
                BacktestJobState.RUNNING_REPORT,
                progress=90,
                label="生成回测报告与审计结果",
            )
            if record.state.is_terminal:
                return record
            robustness = None
            if _boolean(config, "run_robustness"):
                robustness = run_execution_robustness(
                    engine_input,
                    base_result=result,
                ).as_dict()
            bundle = build_result_bundle(
                result,
                run_id=run_id.value,
                period_start=strategy.backtest.start,
                period_end=strategy.backtest.end,
                manifest=manifest,
                robustness=robustness,
            )
            result_json = canonical_json(bundle)
            current = self._require_record(run_id)
            if current.state is BacktestJobState.CANCEL_REQUESTED:
                return self._cancel(current)
            return self.run_store.transition(
                run_id,
                expected=(BacktestJobState.RUNNING_REPORT,),
                target=BacktestJobState.SUCCEEDED,
                progress_percent=100,
                progress_label="回测完成",
                result_json=result_json,
            )
        except Exception as exc:
            current = self._require_record(run_id)
            if current.state is BacktestJobState.CANCEL_REQUESTED:
                return self._cancel(current)
            if current.state.is_terminal:
                return current
            if current.version != record.version or current.state is not record.state:
                return current
            try:
                return self.run_store.transition(
                    run_id,
                    expected=(record.state,),
                    target=BacktestJobState.FAILED,
                    progress_percent=record.progress_percent,
                    progress_label="回测失败",
                    error_code=_error_code(exc),
                    expected_version=record.version,
                )
            except Exception:
                latest = self._require_record(run_id)
                if latest.version != record.version:
                    return latest
                raise

    def _advance(
        self,
        record: BacktestRunRecord,
        target: BacktestJobState,
        *,
        progress: int,
        label: str,
    ) -> BacktestRunRecord:
        current = self._require_record(record.run_id)
        if current.state is BacktestJobState.CANCEL_REQUESTED:
            return self._cancel(current)
        current_rank = _RUNNING_RANK.get(current.state, -1)
        target_rank = _RUNNING_RANK[target]
        if current_rank >= target_rank:
            return current
        return self.run_store.transition(
            current.run_id,
            expected=(current.state,),
            target=target,
            progress_percent=max(progress, current.progress_percent),
            progress_label=label,
        )

    def _cancel(self, record: BacktestRunRecord) -> BacktestRunRecord:
        return self.run_store.transition(
            record.run_id,
            expected=(BacktestJobState.CANCEL_REQUESTED,),
            target=BacktestJobState.CANCELLED,
            progress_percent=record.progress_percent,
            progress_label="已取消",
        )

    def _require_record(self, run_id: RunId) -> BacktestRunRecord:
        record = self.run_store.get(run_id)
        if record is None:
            raise BacktestWorkItemError(f"run_not_found:{run_id.value}")
        return record


_RUNNING_RANK = {
    BacktestJobState.QUEUED: 0,
    BacktestJobState.RUNNING_DATA: 1,
    BacktestJobState.RUNNING_SIGNAL: 2,
    BacktestJobState.RUNNING_EXECUTION: 3,
    BacktestJobState.RUNNING_REPORT: 4,
}


def _financial_facts_from_work_item(
    strategy: StrategySpec,
    manifest: dict[str, Any],
    config: dict[str, Any],
) -> tuple[FinancialFactRecord, ...]:
    requires_financials = strategy_requires_financials(strategy)
    raw_facts = config.get("financial_facts")
    raw_snapshot = config.get("financial_snapshot")
    manifest_snapshot = manifest.get("financial_snapshot")
    if not requires_financials:
        if raw_facts is not None or raw_snapshot is not None or manifest_snapshot is not None:
            raise BacktestWorkItemError("unexpected_financial_payload")
        return ()
    if not isinstance(raw_facts, list) or not raw_facts:
        raise BacktestWorkItemError("financial_facts_missing")
    if not isinstance(raw_snapshot, dict) or not isinstance(manifest_snapshot, dict):
        raise BacktestWorkItemError("financial_snapshot_missing")
    snapshot = cast(dict[str, Any], raw_snapshot)
    persisted_snapshot = cast(dict[str, Any], manifest_snapshot)
    if persisted_snapshot != snapshot:
        raise BacktestWorkItemError("financial_snapshot_manifest_mismatch")
    identity_basis = snapshot.get("identity_basis")
    if not isinstance(identity_basis, dict):
        raise BacktestWorkItemError("financial_snapshot_identity_basis_invalid")
    checksum = _text(snapshot, "checksum")
    if canonical_hash(cast(dict[str, object], identity_basis)) != checksum:
        raise BacktestWorkItemError("financial_snapshot_checksum_mismatch")
    snapshot_id = _text(snapshot, "snapshot_id")
    if snapshot_id != "financial:" + checksum.removeprefix("sha256:"):
        raise BacktestWorkItemError("financial_snapshot_id_mismatch")

    facts: list[FinancialFactRecord] = []
    for raw_fact in cast(list[object], raw_facts):
        if not isinstance(raw_fact, dict):
            raise BacktestWorkItemError("financial_fact_must_be_an_object")
        item = cast(dict[str, Any], raw_fact)
        try:
            availability = PointInTimeAvailability.from_dict(item.get("availability"))
            raw_sources = item.get("source_refs")
            if not isinstance(raw_sources, list) or not raw_sources:
                raise ValueError("source_refs_missing")
            source_refs = tuple(
                SourceRef.from_dict(value) for value in cast(list[object], raw_sources)
            )
            raw_value = item.get("value")
            value = None if raw_value is None else Decimal(str(raw_value))
            fact = FinancialFactRecord.model_validate(
                {
                    **item,
                    "availability": availability,
                    "source_refs": source_refs,
                    "value": value,
                }
            )
        except Exception as exc:
            raise BacktestWorkItemError("financial_fact_invalid") from exc
        if fact.snapshot_id != snapshot_id:
            raise BacktestWorkItemError("financial_fact_snapshot_mismatch")
        if fact.instrument_id != strategy.instrument.symbol:
            raise BacktestWorkItemError("financial_fact_instrument_mismatch")
        facts.append(fact)
    return tuple(facts)


def _engine_config(config: dict[str, Any]) -> DailyBacktestConfig:
    return DailyBacktestConfig(
        participation_rate=Decimal(_text(config, "participation_rate")),
        slippage_bps=Decimal(_text(config, "slippage_bps")),
        limit_handling=LimitHandling(_text(config, "limit_handling")),
        allocation_ratio=Decimal(_text(config, "allocation_ratio")),
        retry_unfilled_exits=_boolean(config, "retry_unfilled_exits"),
        max_exit_attempts=_integer(config, "max_exit_attempts"),
        capacity_mode=CapacityMode(_text(config, "capacity_mode")),
        edge_entry_validity_sessions=_integer(config, "edge_entry_validity_sessions"),
        event_entry_validity_sessions=_integer(config, "event_entry_validity_sessions"),
        state_entry_validity_sessions=_integer(config, "state_entry_validity_sessions"),
    )


def _validate_session_axis(
    bars: tuple[Any, ...],
    sessions: tuple[Any, ...],
) -> None:
    """Reject a market-rule series that does not exactly match pinned bars."""

    if not sessions:
        raise BacktestWorkItemError("data_snapshot_contains_no_instrument_sessions")
    bar_axis = tuple((bar.instrument_id, bar.session_date) for bar in bars)
    session_axis = tuple((item.instrument_id, item.session_date) for item in sessions)
    if session_axis != bar_axis:
        raise BacktestWorkItemError("instrument_session_axis_mismatch")


def _fee_calculator(strategy: StrategySpec, config: dict[str, Any]) -> FeeCalculator:
    suffix = strategy.instrument.symbol.rsplit(".", 1)[1]
    return FeeCalculator(
        FeePolicy(
            exchange=AshareExchange(suffix),
            commission_rate=Decimal(_text(config, "commission_rate")),
            minimum_commission=Money(
                Decimal(_text(config, "minimum_commission_cny")),
                "CNY",
            ),
        )
    )


def _validate_identity(
    record: BacktestRunRecord,
    strategy: StrategySpec,
    manifest: dict[str, Any],
    config: dict[str, Any],
    *,
    runtime_identity: WorkerRuntimeIdentity,
) -> None:
    parsed = _parse_manifest(manifest)
    if parsed.run_id != record.run_id:
        raise BacktestWorkItemError("manifest_run_id_mismatch")
    if _text(manifest, "fingerprint") != parsed.fingerprint:
        raise BacktestWorkItemError("manifest_fingerprint_not_reproducible")
    if parsed.fingerprint != record.fingerprint:
        raise BacktestWorkItemError("manifest_fingerprint_mismatch")
    if parsed.strategy_hash != canonical_hash(strategy):
        raise BacktestWorkItemError("strategy_hash_mismatch")
    if parsed.config_hash != canonical_hash(config):
        raise BacktestWorkItemError("config_hash_mismatch")
    _validate_runtime_identity(parsed, runtime_identity)
    _validate_strategy_manifest_identity(parsed, strategy)
    _validate_config_assumptions(parsed.assumptions, config)


def _validate_runtime_identity(
    manifest: RunManifest,
    runtime: WorkerRuntimeIdentity,
) -> None:
    if runtime.strict_replay:
        if re.fullmatch(r"[0-9a-f]{40}", runtime.code_revision) is None:
            raise BacktestWorkItemError("runtime_code_revision_not_clean")
        if re.fullmatch(r"[0-9a-f]{40}", manifest.code_revision) is None:
            raise BacktestWorkItemError("manifest_code_revision_not_clean")
    scalar_identity = {
        "catalog_hash": (manifest.catalog_hash, runtime.catalog_hash),
        "engine_version": (manifest.engine_version, runtime.engine_version),
        "code_revision": (manifest.code_revision, runtime.code_revision),
    }
    for field_name, (persisted, current) in scalar_identity.items():
        if persisted != current:
            raise BacktestWorkItemError(f"{field_name}_mismatch")

    policy_identity = {
        "fee_schedule_version": runtime.fee_schedule_version,
        "market_rule_version": runtime.market_rule_version,
        "opening_auction_policy": runtime.opening_auction_policy,
        "entry_signal_validity_policy": runtime.entry_signal_validity_policy,
        "benchmark_policy": runtime.benchmark_policy,
        "corporate_action_policy": runtime.corporate_action_policy,
        "dividend_tax_policy": runtime.dividend_tax_policy,
        "rights_issue_policy": runtime.rights_issue_policy,
    }
    for field_name, current in policy_identity.items():
        if getattr(manifest.assumptions, field_name) != current:
            raise BacktestWorkItemError(f"{field_name}_mismatch")


def _validate_strategy_manifest_identity(
    manifest: RunManifest,
    strategy: StrategySpec,
) -> None:
    if manifest.strategy_schema_version != strategy.schema_version:
        raise BacktestWorkItemError("strategy_schema_version_mismatch")
    if manifest.period_start != strategy.backtest.start:
        raise BacktestWorkItemError("period_start_mismatch")
    if manifest.period_end != strategy.backtest.end:
        raise BacktestWorkItemError("period_end_mismatch")
    if manifest.initial_cash_cny != str(strategy.backtest.initial_cash_cny):
        raise BacktestWorkItemError("initial_cash_cny_mismatch")


def _validate_config_assumptions(
    assumptions: ExecutionAssumptions,
    config: dict[str, Any],
) -> None:
    expected = {
        "allocation_ratio": _text(config, "allocation_ratio"),
        "capacity_mode": _text(config, "capacity_mode"),
        "commission_rate": _text(config, "commission_rate"),
        "edge_entry_validity_sessions": str(_integer(config, "edge_entry_validity_sessions")),
        "event_entry_validity_sessions": str(_integer(config, "event_entry_validity_sessions")),
        "max_exit_attempts": str(_integer(config, "max_exit_attempts")),
        "minimum_commission_cny": _text(config, "minimum_commission_cny"),
        "participation_rate": _text(config, "participation_rate"),
        "price_limit_mode": _text(config, "limit_handling"),
        "retry_unfilled_exits": str(_boolean(config, "retry_unfilled_exits")).lower(),
        "robustness_profile": (
            "execution.v1" if _boolean(config, "run_robustness") else "disabled"
        ),
        "slippage_bps": _text(config, "slippage_bps"),
        "state_entry_validity_sessions": str(_integer(config, "state_entry_validity_sessions")),
    }
    if assumptions.resolution != "1d":
        raise BacktestWorkItemError("resolution_mismatch")
    for field_name, value in expected.items():
        if getattr(assumptions, field_name) != value:
            raise BacktestWorkItemError(f"{field_name}_mismatch")


def _parse_manifest(value: dict[str, Any]) -> RunManifest:
    snapshot = _mapping(value, "data_snapshot")
    assumptions = _mapping(value, "assumptions")
    return RunManifest(
        run_id=RunId(_text(value, "run_id")),
        strategy_hash=_text(value, "strategy_hash"),
        catalog_hash=_text(value, "catalog_hash"),
        config_hash=_text(value, "config_hash"),
        data_snapshot=DataSnapshotRef(
            snapshot_id=StrongId(_text(snapshot, "snapshot_id")),
            checksum=_text(snapshot, "checksum"),
            schema_version=_text(snapshot, "schema_version"),
            created_at=datetime.fromisoformat(_text(snapshot, "created_at")),
            producer_schema_version=_optional_text(
                snapshot,
                "producer_schema_version",
            ),
            producer_snapshot_id=_optional_text(
                snapshot,
                "producer_snapshot_id",
            ),
        ),
        strategy_schema_version=_text(value, "strategy_schema_version"),
        engine_version=_text(value, "engine_version"),
        code_revision=_text(value, "code_revision"),
        period_start=date.fromisoformat(_text(value, "period_start")),
        period_end=date.fromisoformat(_text(value, "period_end")),
        initial_cash_cny=_text(value, "initial_cash_cny"),
        assumptions=ExecutionAssumptions(
            resolution=_text(assumptions, "resolution"),
            price_limit_mode=_text(assumptions, "price_limit_mode"),
            participation_rate=_text(assumptions, "participation_rate"),
            slippage_bps=_text(assumptions, "slippage_bps"),
            commission_rate=_text(assumptions, "commission_rate"),
            minimum_commission_cny=_text(assumptions, "minimum_commission_cny"),
            fee_schedule_version=_text(assumptions, "fee_schedule_version"),
            market_rule_version=_text(assumptions, "market_rule_version"),
            opening_auction_policy=_text(assumptions, "opening_auction_policy"),
            entry_signal_validity_policy=_text(
                assumptions,
                "entry_signal_validity_policy",
            ),
            edge_entry_validity_sessions=_text(
                assumptions,
                "edge_entry_validity_sessions",
            ),
            event_entry_validity_sessions=_text(
                assumptions,
                "event_entry_validity_sessions",
            ),
            state_entry_validity_sessions=_text(
                assumptions,
                "state_entry_validity_sessions",
            ),
            capacity_mode=_text(assumptions, "capacity_mode"),
            benchmark_policy=_text(assumptions, "benchmark_policy"),
            corporate_action_policy=_text(assumptions, "corporate_action_policy"),
            dividend_tax_policy=_text(assumptions, "dividend_tax_policy"),
            rights_issue_policy=_text(assumptions, "rights_issue_policy"),
            allocation_ratio=_text(assumptions, "allocation_ratio"),
            retry_unfilled_exits=_text(assumptions, "retry_unfilled_exits"),
            max_exit_attempts=_text(assumptions, "max_exit_attempts"),
            robustness_profile=_text(assumptions, "robustness_profile"),
        ),
        created_at=datetime.fromisoformat(_text(value, "created_at")),
        random_seed=_integer(value, "random_seed"),
    )


def _object_json(payload: str, field_name: str) -> dict[str, Any]:
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise BacktestWorkItemError(f"{field_name}_must_be_an_object")
    return cast(dict[str, Any], value)


def _mapping(value: dict[str, Any], field_name: str) -> dict[str, Any]:
    item = value.get(field_name)
    if not isinstance(item, dict):
        raise BacktestWorkItemError(f"{field_name}_must_be_an_object")
    return cast(dict[str, Any], item)


def _text(value: dict[str, Any], field_name: str) -> str:
    item = value.get(field_name)
    if not isinstance(item, str) or not item:
        raise BacktestWorkItemError(f"{field_name}_must_be_text")
    return item


def _optional_text(value: dict[str, Any], field_name: str) -> str | None:
    item = value.get(field_name)
    if item is None:
        return None
    if not isinstance(item, str) or not item:
        raise BacktestWorkItemError(f"{field_name}_must_be_null_or_text")
    return item


def _integer(value: dict[str, Any], field_name: str) -> int:
    item = value.get(field_name)
    if isinstance(item, bool) or not isinstance(item, int):
        raise BacktestWorkItemError(f"{field_name}_must_be_integer")
    return item


def _boolean(value: dict[str, Any], field_name: str) -> bool:
    item = value.get(field_name)
    if not isinstance(item, bool):
        raise BacktestWorkItemError(f"{field_name}_must_be_boolean")
    return item


def _error_code(exc: Exception) -> str:
    if isinstance(exc, BacktestWorkItemError):
        return str(exc).split(":", 1)[0][:96]
    return f"execution_{type(exc).__name__}"[:96]
