"""Small deterministic collaborators for API contract tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from ashare_lab.application.backtest_submission import BacktestRunConfig
from ashare_lab.application.result_views import (
    RESULT_HASH_SCHEMA_VERSION,
    calculate_result_bundle_hash,
)
from ashare_lab.domain.shared import RunId
from ashare_lab.domain.strategy import StrategySpec, canonical_hash, canonical_json
from ashare_lab.ports.backtest_runs import (
    BacktestJobState,
    BacktestResultIntegrityPolicy,
    BacktestRunRecord,
    CreateRunResult,
)

HASH = "sha256:" + "a" * 64
NOW = datetime(2026, 8, 28, 1, 2, 3, tzinfo=UTC)


class FakeRunStore:
    def __init__(self) -> None:
        self.records: dict[RunId, BacktestRunRecord] = {}
        self.run_id_by_fingerprint: dict[str, RunId] = {}

    def create_or_get(self, record: BacktestRunRecord) -> CreateRunResult:
        existing_id = self.run_id_by_fingerprint.get(record.fingerprint)
        if existing_id is not None:
            return CreateRunResult(record=self.records[existing_id], replayed=True)
        self.records[record.run_id] = record
        self.run_id_by_fingerprint[record.fingerprint] = record.run_id
        return CreateRunResult(record=record, replayed=False)

    def get(self, run_id: RunId) -> BacktestRunRecord | None:
        return self.records.get(run_id)

    def transition(
        self,
        run_id: RunId,
        *,
        expected: tuple[BacktestJobState, ...],
        target: BacktestJobState,
        progress_percent: int,
        progress_label: str,
        result_json: str | None = None,
        error_code: str | None = None,
        expected_version: int | None = None,
    ) -> BacktestRunRecord:
        current = self.records[run_id]
        if expected_version is not None and current.version != expected_version:
            raise ValueError("unexpected fake run version")
        if current.state not in expected:
            raise ValueError("unexpected fake transition")
        updated = replace(
            current,
            state=target,
            progress_percent=progress_percent,
            progress_label=progress_label,
            result_json=result_json,
            error_code=error_code,
            updated_at=NOW,
            version=current.version + 1,
        )
        self.records[run_id] = updated
        return updated

    def request_cancel(self, run_id: RunId) -> BacktestRunRecord:
        current = self.records[run_id]
        if current.state in {BacktestJobState.CANCEL_REQUESTED, BacktestJobState.CANCELLED}:
            return current
        updated = replace(
            current,
            state=BacktestJobState.CANCEL_REQUESTED,
            progress_label="cancel_requested",
            updated_at=NOW,
            version=current.version + 1,
        )
        self.records[run_id] = updated
        return updated

    def seed(self, record: BacktestRunRecord) -> None:
        self.records[record.run_id] = record
        self.run_id_by_fingerprint[record.fingerprint] = record.run_id


class FakeSubmitter:
    def __init__(self, store: FakeRunStore) -> None:
        self.store = store
        self.configs: list[BacktestRunConfig] = []

    def submit(self, strategy: StrategySpec, config: BacktestRunConfig) -> CreateRunResult:
        self.configs.append(config)
        fingerprint = canonical_hash(
            {
                "strategy": strategy.model_dump(mode="json"),
                "config": {
                    "allocationRatio": str(config.allocation_ratio),
                    "capacityMode": config.capacity_mode.value,
                    "commissionRate": str(config.commission_rate),
                    "edgeEntryValiditySessions": config.edge_entry_validity_sessions,
                    "eventEntryValiditySessions": config.event_entry_validity_sessions,
                    "limitHandling": config.limit_handling.value,
                    "maxExitAttempts": config.max_exit_attempts,
                    "minimumCommissionCny": str(config.minimum_commission_cny),
                    "participationRate": str(config.participation_rate),
                    "retryUnfilledExits": config.retry_unfilled_exits,
                    "settlementExtensionDays": config.settlement_extension_days,
                    "slippageBps": str(config.slippage_bps),
                    "stateEntryValiditySessions": config.state_entry_validity_sessions,
                    "warmupCalendarDays": config.warmup_calendar_days,
                },
            }
        )
        record = make_record(
            f"run:fake:{len(self.store.records) + 1}",
            state=BacktestJobState.QUEUED,
            fingerprint=fingerprint,
            strategy_json=canonical_json(strategy),
        )
        return self.store.create_or_get(record)


def make_record(
    run_id: str,
    *,
    state: BacktestJobState,
    fingerprint: str = HASH,
    strategy_json: str = "{}",
    result_json: str | None = None,
    result_integrity_policy: BacktestResultIntegrityPolicy = (
        BacktestResultIntegrityPolicy.BUNDLE_HASH_V1
    ),
) -> BacktestRunRecord:
    progress = {
        BacktestJobState.QUEUED: 0,
        BacktestJobState.RUNNING_DATA: 10,
        BacktestJobState.RUNNING_SIGNAL: 30,
        BacktestJobState.RUNNING_EXECUTION: 60,
        BacktestJobState.RUNNING_REPORT: 90,
        BacktestJobState.CANCEL_REQUESTED: 60,
        BacktestJobState.SUCCEEDED: 100,
        BacktestJobState.FAILED: 60,
        BacktestJobState.CANCELLED: 60,
    }[state]
    return BacktestRunRecord(
        run_id=RunId(run_id),
        fingerprint=fingerprint,
        strategy_json=strategy_json,
        manifest_json="{}",
        config_json="{}",
        state=state,
        progress_percent=progress,
        progress_label=state.value,
        created_at=NOW,
        updated_at=NOW,
        result_integrity_policy=result_integrity_policy,
        result_json=result_json,
        error_code="worker_failed" if state is BacktestJobState.FAILED else None,
    )


def result_bundle_json(run_id: str) -> str:
    bundle: dict[str, object] = {
        "summary": {
            "runId": run_id,
            "totalReturn": 0.12,
            "benchmarkReturn": None,
            "annualizedReturn": None,
            "maxDrawdown": -0.08,
            "sharpeRatio": None,
            "winRate": None,
            "tradeCount": 1,
            "finalEquityCny": 112000.0,
            "interpretation": "策略有一笔完整交易。",
            "dataRange": {
                "start": "2025-01-02",
                "end": "2025-01-03",
                "sessions": 1,
            },
            "warnings": ["样本较少。"],
        },
        "series": [
            {
                "date": "2025-01-02",
                "equity": 100.0,
                "benchmark": None,
                "drawdown": 0.0,
            },
            {
                "date": "2025-01-03",
                "equity": 112.0,
                "benchmark": None,
                "drawdown": 0.0,
            },
        ],
        "activities": [
            {
                "id": "signal:1",
                "kind": "signal",
                "occurredAt": "2025-01-02T15:00:00+08:00",
                "side": "buy",
                "title": "买入信号确认",
                "status": "confirmed",
                "reason": "MACD 金叉",
            },
            {
                "id": "fill:1",
                "kind": "fill",
                "occurredAt": "2025-01-03T09:30:00+08:00",
                "side": "buy",
                "title": "买入成交",
                "price": 10.2,
                "quantity": 100,
                "status": "filled",
                "reason": "matched_at_open",
            },
        ],
        "audit": {
            "engineResultHash": HASH,
            "hashSchemaVersion": RESULT_HASH_SCHEMA_VERSION,
            "openPositionShares": 100,
        },
    }
    audit = bundle["audit"]
    assert isinstance(audit, dict)
    audit["resultHash"] = calculate_result_bundle_hash(bundle)
    return canonical_json(bundle)
