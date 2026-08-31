"""HTTP contracts for asynchronous backtest submission and status."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Literal, cast

from pydantic import ConfigDict, Field

from ashare_lab.application.backtest_submission import BacktestRunConfig
from ashare_lab.application.result_views import RESULT_HASH_SCHEMA_VERSION
from ashare_lab.domain.execution import CapacityMode, LimitHandling
from ashare_lab.domain.strategy import StrategySpec
from ashare_lab.ports.backtest_runs import BacktestJobState, BacktestRunRecord

from .schemas import ApiModel


class CamelApiModel(ApiModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        populate_by_name=True,
        serialize_by_alias=True,
        allow_inf_nan=False,
    )


class BacktestExecutionConfig(CamelApiModel):
    capacity_mode: CapacityMode = Field(
        default=CapacityMode.POINT_IN_TIME_VOLUME,
        alias="capacityMode",
    )
    participation_rate: Decimal = Field(
        default=Decimal("0.05"),
        alias="participationRate",
        gt=0,
        le=1,
    )
    slippage_bps: Decimal = Field(default=Decimal("5"), alias="slippageBps", ge=0, le=1_000)
    allocation_ratio: Decimal = Field(
        default=Decimal("1"),
        alias="allocationRatio",
        gt=0,
        le=1,
    )
    limit_handling: LimitHandling = Field(
        default=LimitHandling.WAIT_FOR_UNLOCK,
        alias="limitHandling",
    )
    commission_rate: Decimal = Field(
        default=Decimal("0.0003"),
        alias="commissionRate",
        ge=0,
    )
    minimum_commission_cny: Decimal = Field(
        default=Decimal("5"),
        alias="minimumCommissionCny",
        ge=0,
    )
    retry_unfilled_exits: bool = Field(default=True, alias="retryUnfilledExits")
    max_exit_attempts: int = Field(default=20, alias="maxExitAttempts", ge=1, le=1_000)
    edge_entry_validity_sessions: int = Field(
        default=3,
        alias="edgeEntryValiditySessions",
        ge=1,
        le=20,
    )
    event_entry_validity_sessions: int = Field(
        default=1,
        alias="eventEntryValiditySessions",
        ge=1,
        le=1,
    )
    state_entry_validity_sessions: int = Field(
        default=1,
        alias="stateEntryValiditySessions",
        ge=1,
        le=1,
    )
    warmup_calendar_days: int = Field(default=180, alias="warmupCalendarDays", ge=0, le=3_650)
    settlement_extension_days: int = Field(
        default=14,
        alias="settlementExtensionDays",
        ge=1,
        le=365,
    )
    run_robustness: bool = Field(default=True, alias="runRobustness")

    def to_application_config(self) -> BacktestRunConfig:
        return BacktestRunConfig(
            capacity_mode=self.capacity_mode,
            participation_rate=self.participation_rate,
            slippage_bps=self.slippage_bps,
            allocation_ratio=self.allocation_ratio,
            limit_handling=self.limit_handling,
            commission_rate=self.commission_rate,
            minimum_commission_cny=self.minimum_commission_cny,
            retry_unfilled_exits=self.retry_unfilled_exits,
            max_exit_attempts=self.max_exit_attempts,
            edge_entry_validity_sessions=self.edge_entry_validity_sessions,
            event_entry_validity_sessions=self.event_entry_validity_sessions,
            state_entry_validity_sessions=self.state_entry_validity_sessions,
            warmup_calendar_days=self.warmup_calendar_days,
            settlement_extension_days=self.settlement_extension_days,
            run_robustness=self.run_robustness,
        )


class BacktestRunRequest(CamelApiModel):
    strategy: StrategySpec
    config: BacktestExecutionConfig = Field(default_factory=BacktestExecutionConfig)


class BacktestRunStatusResponse(CamelApiModel):
    id: str = Field(min_length=1, max_length=128)
    state: BacktestJobState
    progress: int = Field(ge=0, le=100)
    progress_label: str = Field(alias="progressLabel", min_length=1, max_length=255)
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")
    fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    error: str | None = Field(default=None, max_length=128)
    result_available: bool = Field(alias="resultAvailable")
    result_hash: str | None = Field(
        default=None,
        alias="resultHash",
        pattern=r"^sha256:[0-9a-f]{64}$",
    )

    @classmethod
    def from_record(cls, record: BacktestRunRecord) -> BacktestRunStatusResponse:
        return cls.model_validate(
            {
                "id": str(record.run_id),
                "state": record.state,
                "progress": record.progress_percent,
                "progressLabel": record.progress_label,
                "createdAt": record.created_at,
                "updatedAt": record.updated_at,
                "fingerprint": record.fingerprint,
                "error": record.error_code,
                "resultAvailable": record.result_json is not None,
                "resultHash": _stored_result_hash(record.result_json),
            }
        )


class BacktestRunCreatedResponse(BacktestRunStatusResponse):
    replayed: bool


class BacktestCancelResponse(BacktestRunStatusResponse):
    cancellation_requested: Literal[True] = Field(default=True, alias="cancellationRequested")


def _stored_result_hash(result_json: str | None) -> object:
    if result_json is None:
        return None
    try:
        decoded: object = json.loads(result_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, Mapping):
        return None
    payload = cast(Mapping[object, object], decoded)
    audit = payload.get("audit")
    if not isinstance(audit, Mapping):
        return None
    typed_audit = cast(Mapping[object, object], audit)
    if typed_audit.get("hashSchemaVersion") != RESULT_HASH_SCHEMA_VERSION:
        return None
    return typed_audit.get("resultHash")
