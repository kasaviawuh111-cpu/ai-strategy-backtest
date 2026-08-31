"""Reproducibility records for one backtest execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from ashare_lab.domain.market_data import DataSnapshotRef
from ashare_lab.domain.shared import DomainValidationError, RunId, require_aware
from ashare_lab.domain.strategy import canonical_hash


def _require_hash(value: str, field_name: str) -> None:
    if len(value) != 71 or not value.startswith("sha256:"):
        raise DomainValidationError(f"{field_name} must be a sha256 content hash")
    if any(character not in "0123456789abcdef" for character in value[7:]):
        raise DomainValidationError(f"{field_name} must contain lowercase hexadecimal digits")


@dataclass(frozen=True, slots=True)
class ExecutionAssumptions:
    resolution: str
    price_limit_mode: str
    participation_rate: str
    slippage_bps: str
    commission_rate: str
    minimum_commission_cny: str
    fee_schedule_version: str
    market_rule_version: str
    opening_auction_policy: str = (
        "cn.a_share.daily.published_open_proxy.latency_1s.cutoff_0915."
        "recorded_0930.not_exact.day_order.v2"
    )
    entry_signal_validity_policy: str = (
        "cn.a_share.daily.entry_signal_validity.edge_event_state.composite_fail_closed."
        "event_revision_unavailable_one_attempt.retryable_day_orders.v3"
    )
    edge_entry_validity_sessions: str = "3"
    event_entry_validity_sessions: str = "1"
    state_entry_validity_sessions: str = "1"
    capacity_mode: str = "point_in_time_volume"
    benchmark_policy: str = "funded_buy_and_hold.same_execution_ledger.v1"
    corporate_action_policy: str = (
        "cn.a_share.timeline_entitlement_receivable_settlement."
        "date_only_cash_after_close_conservative.v2"
    )
    dividend_tax_policy: str = "gross_research_no_withholding.v1"
    rights_issue_policy: str = "decline_no_external_cash.v1"
    allocation_ratio: str = "1"
    retry_unfilled_exits: str = "true"
    max_exit_attempts: str = "20"
    robustness_profile: str = "execution.v1"

    def __post_init__(self) -> None:
        for field_name, value in self.as_dict().items():
            if not value:
                raise DomainValidationError(f"execution assumption {field_name} cannot be empty")

    def as_dict(self) -> dict[str, str]:
        return {
            "allocation_ratio": self.allocation_ratio,
            "benchmark_policy": self.benchmark_policy,
            "capacity_mode": self.capacity_mode,
            "commission_rate": self.commission_rate,
            "corporate_action_policy": self.corporate_action_policy,
            "dividend_tax_policy": self.dividend_tax_policy,
            "edge_entry_validity_sessions": self.edge_entry_validity_sessions,
            "entry_signal_validity_policy": self.entry_signal_validity_policy,
            "event_entry_validity_sessions": self.event_entry_validity_sessions,
            "fee_schedule_version": self.fee_schedule_version,
            "market_rule_version": self.market_rule_version,
            "max_exit_attempts": self.max_exit_attempts,
            "minimum_commission_cny": self.minimum_commission_cny,
            "opening_auction_policy": self.opening_auction_policy,
            "participation_rate": self.participation_rate,
            "price_limit_mode": self.price_limit_mode,
            "retry_unfilled_exits": self.retry_unfilled_exits,
            "robustness_profile": self.robustness_profile,
            "resolution": self.resolution,
            "rights_issue_policy": self.rights_issue_policy,
            "slippage_bps": self.slippage_bps,
            "state_entry_validity_sessions": self.state_entry_validity_sessions,
        }


@dataclass(frozen=True, slots=True)
class RunManifest:
    """All inputs required to replay a run, fixed before it is queued."""

    run_id: RunId
    strategy_hash: str
    catalog_hash: str
    config_hash: str
    data_snapshot: DataSnapshotRef
    strategy_schema_version: str
    engine_version: str
    code_revision: str
    period_start: date
    period_end: date
    initial_cash_cny: str
    assumptions: ExecutionAssumptions
    created_at: datetime
    random_seed: int = 0

    def __post_init__(self) -> None:
        for field_name in ("strategy_hash", "catalog_hash", "config_hash"):
            _require_hash(getattr(self, field_name), field_name)
        require_aware(self.created_at, "created_at")
        if self.period_start > self.period_end:
            raise DomainValidationError("run period start must not exceed end")
        if not self.strategy_schema_version or not self.engine_version or not self.code_revision:
            raise DomainValidationError("schema, engine, and code versions are required")
        if not self.initial_cash_cny or self.initial_cash_cny.startswith("-"):
            raise DomainValidationError("initial_cash_cny must be a non-negative decimal string")
        if type(self.random_seed) is not int:
            raise DomainValidationError("random_seed must be an integer")

    @property
    def fingerprint(self) -> str:
        """Idempotency fingerprint; intentionally excludes identity and wall-clock time."""

        snapshot_identity: dict[str, str | None] = {
            "checksum": self.data_snapshot.checksum,
            "producer_schema_version": self.data_snapshot.producer_schema_version,
            "schema_version": self.data_snapshot.schema_version,
            "snapshot_id": str(self.data_snapshot.snapshot_id),
        }
        if self.data_snapshot.producer_snapshot_id is not None:
            snapshot_identity["producer_snapshot_id"] = self.data_snapshot.producer_snapshot_id
        return canonical_hash(
            {
                "assumptions": self.assumptions.as_dict(),
                "catalog_hash": self.catalog_hash,
                "code_revision": self.code_revision,
                "config_hash": self.config_hash,
                "data_snapshot": snapshot_identity,
                "engine_version": self.engine_version,
                "initial_cash_cny": self.initial_cash_cny,
                "period": [self.period_start.isoformat(), self.period_end.isoformat()],
                "random_seed": self.random_seed,
                "strategy_hash": self.strategy_hash,
                "strategy_schema_version": self.strategy_schema_version,
            }
        )


def result_hash(payload: Any) -> str:
    """Canonical hash for JSON-shaped result data."""

    return canonical_hash(payload)
