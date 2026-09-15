"""Validated HTTP projection of the stable completed-run result bundle."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field, model_validator

from .backtest_schemas import CamelApiModel


class BacktestDataRange(CamelApiModel):
    start: date
    end: date
    sessions: int = Field(ge=0)

    @model_validator(mode="after")
    def dates_are_ordered(self) -> BacktestDataRange:
        if self.start > self.end:
            raise ValueError("result data range start cannot follow end")
        return self


class BacktestRunEvidence(CamelApiModel):
    """Immutable inputs and implementation identity saved with one run."""

    strategy_hash: str = Field(alias="strategyHash", pattern=r"^sha256:[0-9a-f]{64}$")
    catalog_hash: str = Field(alias="catalogHash", pattern=r"^sha256:[0-9a-f]{64}$")
    data_snapshot_id: str = Field(alias="dataSnapshotId", min_length=1, max_length=256)
    data_snapshot_checksum: str = Field(
        alias="dataSnapshotChecksum", pattern=r"^sha256:[0-9a-f]{64}$"
    )
    data_schema_version: str = Field(alias="dataSchemaVersion", min_length=1, max_length=128)
    producer_snapshot_schema_version: str | None = Field(
        default=None,
        alias="producerSnapshotSchemaVersion",
        min_length=1,
        max_length=128,
    )
    producer_snapshot_id: str | None = Field(
        default=None,
        alias="producerSnapshotId",
        pattern=r"^[a-z][a-z0-9_-]*:[0-9a-f]{64}$",
    )
    code_revision: str = Field(alias="codeRevision", min_length=1, max_length=128)
    engine_version: str = Field(alias="engineVersion", min_length=1, max_length=128)
    execution_assumptions: dict[str, str] = Field(alias="executionAssumptions")


class SkillDataProvenance(CamelApiModel):
    """Provider facts for the Skill-only path, without a second snapshot ledger."""

    provider: Literal["eastmoney_mx_finance_data"]
    instrument_id: str = Field(alias="instrumentId")
    price_basis: Literal["provider_back_adjusted", "unadjusted"] = Field(alias="priceBasis")
    retrieved_at: datetime = Field(alias="retrievedAt")
    history_start: date = Field(alias="historyStart")
    history_end: date = Field(alias="historyEnd")
    history_rows: int = Field(alias="historyRows", gt=0)
    indicator_series: int = Field(alias="indicatorSeries", ge=0)
    indicator_points: int = Field(alias="indicatorPoints", ge=0)
    refresh_requested: bool = Field(
        default=False, alias="refreshRequested", exclude_if=lambda value: not value
    )
    history_cache_status: Literal["unknown", "memory", "disk", "live", "forced"] = Field(
        default="unknown", alias="historyCacheStatus", exclude_if=lambda value: value == "unknown"
    )
    indicator_cache_statuses: tuple[
        Literal["unknown", "memory", "disk", "live", "forced"], ...
    ] = Field(
        default=(), alias="indicatorCacheStatuses", exclude_if=lambda value: not value
    )
    indicator_field_evidence: tuple[dict[str, str | None], ...] = Field(
        default=(), alias="indicatorFieldEvidence"
    )
    derived_indicator_evidence: tuple[dict[str, str], ...] = Field(
        default=(), alias="derivedIndicatorEvidence"
    )
    queries: tuple[str, ...]
    holding_calendar: dict[str, str | int | None] | None = Field(
        default=None, alias="holdingCalendar", exclude_if=lambda value: value is None,
    )


class BacktestSummaryView(CamelApiModel):
    run_id: str = Field(alias="runId", min_length=1, max_length=128)
    total_return: float | None = Field(alias="totalReturn")
    benchmark_return: float | None = Field(alias="benchmarkReturn")
    benchmark_comparison_status: Literal[
        "comparable",
        "strategy_entry_not_filled",
        "benchmark_entry_not_filled",
        "benchmark_unavailable",
    ] = Field(default="benchmark_unavailable", alias="benchmarkComparisonStatus")
    annualized_return: float | None = Field(alias="annualizedReturn")
    max_drawdown: float | None = Field(alias="maxDrawdown", ge=-1, le=0)
    sharpe_ratio: float | None = Field(alias="sharpeRatio")
    win_rate: float | None = Field(alias="winRate", ge=0, le=1)
    trade_count: int = Field(alias="tradeCount", ge=0)
    trade_count_semantics: Literal["closed_position_cycles"] | None = Field(
        default=None, alias="tradeCountSemantics", exclude_if=lambda value: value is None,
    )
    initial_cash_cny: float | None = Field(default=None, alias="initialCashCny", ge=0)
    initial_equity_cny: float | None = Field(
        default=None, alias="initialEquityCny", gt=0, exclude_if=lambda value: value is None,
    )
    final_equity_cny: float = Field(alias="finalEquityCny", gt=0)
    interpretation: str = Field(min_length=1, max_length=2_000)
    execution_note: str | None = Field(
        default=None, alias="executionNote", max_length=1000, exclude_if=lambda value: value is None,
    )
    data_range: BacktestDataRange = Field(alias="dataRange")
    warnings: tuple[str, ...]
    # Results created before run evidence was introduced remain readable, but
    # the client must label them as legacy rather than inventing provenance.
    run_evidence: BacktestRunEvidence | None = Field(default=None, alias="runEvidence")
    data_provenance: SkillDataProvenance | None = Field(default=None, alias="dataProvenance")


class BacktestSeriesPoint(CamelApiModel):
    date: date | AwareDatetime
    equity: float = Field(gt=0)
    benchmark: float | None = Field(default=None, gt=0)
    drawdown: float = Field(ge=-1, le=0)


class BacktestSignalEvidence(CamelApiModel):
    type: str = Field(min_length=1, max_length=128)
    id: str = Field(min_length=1, max_length=512)
    available_at: datetime = Field(alias="availableAt")
    source_event_id: str | None = Field(default=None, alias="sourceEventId")
    provider: str | None = None
    source_url: str | None = Field(default=None, alias="sourceUrl")
    time_quality: str | None = Field(default=None, alias="timeQuality")
    timestamp_precision: Literal["second", "minute", "hour", "date"] | None = Field(
        default=None,
        alias="timestampPrecision",
    )
    validation_status: str | None = Field(default=None, alias="validationStatus")
    raw_response_sha256: str | None = Field(
        default=None,
        alias="rawResponseSha256",
        pattern=r"^(?:sha256:)?[0-9a-f]{64}$",
    )


class BacktestActivity(CamelApiModel):
    execution_details: dict[str, str | int | float | None] = Field(
        default_factory=dict, alias="executionDetails", exclude_if=lambda value: not value,
    )
    id: str = Field(min_length=1, max_length=256)
    kind: Literal["signal", "order", "fill", "partial_fill", "unfilled", "expired"]
    occurred_at: datetime = Field(alias="occurredAt")
    side: Literal["buy", "sell"]
    title: str = Field(min_length=1, max_length=256)
    price: float | None = Field(default=None, gt=0)
    quantity: int | None = Field(default=None, ge=1)
    notional_cny: float | None = Field(default=None, alias="notionalCny", gt=0)
    status: Literal["confirmed", "submitted", "filled", "partially_filled", "cancelled", "expired", "rejected"]
    reason: str = Field(min_length=1, max_length=2_000)
    chain_id: str | None = Field(default=None, alias="chainId", max_length=256)
    decision_id: str | None = Field(default=None, alias="decisionId", max_length=256)
    order_id: str | None = Field(default=None, alias="orderId", max_length=256)
    fill_id: str | None = Field(default=None, alias="fillId", max_length=256)
    parent_id: str | None = Field(default=None, alias="parentId", max_length=256)
    capacity_reason_code: str | None = Field(
        default=None, alias="capacityReasonCode", max_length=128
    )
    time_quality: str | None = Field(default=None, alias="timeQuality", max_length=128)
    time_semantics: str | None = Field(default=None, alias="timeSemantics", max_length=512)
    signal_semantics: Literal["edge", "event", "state", "composite"] | None = Field(
        default=None,
        alias="signalSemantics",
    )
    signal_validity_sessions: int | None = Field(
        default=None,
        alias="signalValiditySessions",
        ge=1,
        le=20,
    )
    attempt_no: int | None = Field(default=None, alias="attemptNo", ge=1)
    origin_signal_id: str | None = Field(
        default=None,
        alias="originSignalId",
        max_length=256,
    )
    retry_reason: str | None = Field(default=None, alias="retryReason", max_length=256)
    outcome_reason: str | None = Field(default=None, alias="outcomeReason", max_length=256)
    evidence: tuple[BacktestSignalEvidence, ...] = ()


class BacktestAudit(CamelApiModel):
    signal_adjustment_source: str | None = Field(
        default=None, alias="signalAdjustmentSource", exclude_if=lambda value: value is None,
    )
    price_plan_ledger: str | None = Field(
        default=None, alias="pricePlanLedger", exclude_if=lambda value: value is None,
    )
    # Internal reproducibility inputs, not a report warning or model analysis input.
    # Empty legacy bundles must serialize identically for existing integrity hashes.
    skill_numeric_sources: tuple[str, ...] = Field(
        default=(), alias="skillNumericSources", exclude_if=lambda value: not value,
    )
    # Legacy bundles may have no bundle-integrity evidence. A stored hash is
    # trusted only when a supported schema is present and the read path has
    # recomputed it over the complete persisted bundle.
    result_hash: str | None = Field(
        default=None,
        alias="resultHash",
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    engine_result_hash: str | None = Field(
        default=None,
        alias="engineResultHash",
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    hash_schema_version: str | None = Field(
        default=None,
        alias="hashSchemaVersion",
        min_length=1,
        max_length=128,
    )
    open_position_shares: int = Field(alias="openPositionShares", ge=0)
    open_position_notional_cny: float | None = Field(
        default=None, alias="openPositionNotionalCny", ge=0
    )
    open_dividend_receivable_cny: float | None = Field(
        default=None, alias="openDividendReceivableCny", ge=0,
        exclude_if=lambda value: value is None,
    )


class BacktestStressScenario(CamelApiModel):
    slippage_cny: float = Field(
        default=0, alias="slippageCny", ge=0, exclude_if=lambda value: not value,
    )
    id: str
    config_hash: str | None = Field(
        default=None,
        alias="configHash",
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    participation_rate: float = Field(alias="participationRate", gt=0, le=1)
    slippage_bps: float = Field(alias="slippageBps", ge=0)
    total_return: float = Field(alias="totalReturn")
    max_drawdown: float = Field(alias="maxDrawdown", ge=-1, le=0)
    trade_count: int = Field(alias="tradeCount", ge=0)
    final_equity_cny: float = Field(alias="finalEquityCny", gt=0)


class BacktestReturnRange(CamelApiModel):
    min: float
    max: float


class BacktestRobustness(CamelApiModel):
    profile: Literal["execution.v1"]
    selection_policy: Literal["predeclared_scenarios_no_optimization"] = Field(
        alias="selectionPolicy"
    )
    total_return_range: BacktestReturnRange = Field(alias="totalReturnRange")
    scenarios: tuple[BacktestStressScenario, ...] = Field(min_length=1, max_length=6)


class BacktestResultBundle(CamelApiModel):
    summary: BacktestSummaryView
    series: tuple[BacktestSeriesPoint, ...] = Field(min_length=1)
    activities: tuple[BacktestActivity, ...]
    audit: BacktestAudit
    robustness: BacktestRobustness | None = None

    @model_validator(mode="after")
    def collections_are_ordered_and_unique(self) -> BacktestResultBundle:
        series_dates = tuple(item.date for item in self.series)
        if len({isinstance(value, datetime) for value in series_dates}) > 1:
            raise ValueError("result series must use one time granularity")
        if series_dates != tuple(sorted(series_dates)) or len(series_dates) != len(
            set(series_dates)
        ):
            raise ValueError("result series dates must be strictly increasing")
        activity_ids = tuple(item.id for item in self.activities)
        if len(activity_ids) != len(set(activity_ids)):
            raise ValueError("result activity ids must be unique")
        activity_times = tuple(item.occurred_at for item in self.activities)
        if activity_times != tuple(sorted(activity_times)):
            raise ValueError("result activities must be time ordered")
        sessions = (len({item.date.astimezone(ZoneInfo("Asia/Shanghai")).date() for item in self.series[1:]})
                    if isinstance(series_dates[0], datetime) else len(self.series) - 1)
        if self.summary.data_range.sessions != sessions:
            raise ValueError("result session count does not match series")
        return self
