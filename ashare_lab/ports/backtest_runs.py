"""Durable backtest work-item and queue boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from ashare_lab.domain.shared import DomainValidationError, RunId, require_aware


class BacktestJobState(StrEnum):
    QUEUED = "queued"
    RUNNING_DATA = "running:data"
    RUNNING_SIGNAL = "running:signal"
    RUNNING_EXECUTION = "running:execution"
    RUNNING_REPORT = "running:report"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {
            BacktestJobState.SUCCEEDED,
            BacktestJobState.FAILED,
            BacktestJobState.CANCELLED,
        }


class BacktestResultIntegrityPolicy(StrEnum):
    """Persisted run-generation identity for completed-result verification."""

    BUNDLE_HASH_V1 = "bundle_hash_v1"
    LEGACY_UNVERIFIED = "legacy_unverified"


@dataclass(frozen=True, slots=True)
class BacktestRunRecord:
    run_id: RunId
    fingerprint: str
    strategy_json: str
    manifest_json: str
    config_json: str
    state: BacktestJobState
    progress_percent: int
    progress_label: str
    created_at: datetime
    updated_at: datetime
    result_integrity_policy: BacktestResultIntegrityPolicy = (
        BacktestResultIntegrityPolicy.BUNDLE_HASH_V1
    )
    result_json: str | None = None
    error_code: str | None = None
    version: int = 1

    def __post_init__(self) -> None:
        require_aware(self.created_at, "created_at")
        require_aware(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise DomainValidationError("run update cannot predate creation")
        if not self.fingerprint.startswith("sha256:") or len(self.fingerprint) != 71:
            raise DomainValidationError("run fingerprint must be a sha256 hash")
        if not self.strategy_json or not self.manifest_json or not self.config_json:
            raise DomainValidationError("run work item JSON payloads cannot be empty")
        if not 0 <= self.progress_percent <= 100:
            raise DomainValidationError("run progress must be in [0, 100]")
        if not self.progress_label:
            raise DomainValidationError("run progress label cannot be empty")
        if self.version < 1:
            raise DomainValidationError("run version must be positive")
        if self.state is BacktestJobState.SUCCEEDED:
            if self.result_json is None or self.progress_percent != 100:
                raise DomainValidationError("succeeded run requires result JSON and 100% progress")
        elif self.result_json is not None:
            raise DomainValidationError("only succeeded runs can contain result JSON")
        if self.state is BacktestJobState.FAILED:
            if not self.error_code:
                raise DomainValidationError("failed run requires an error code")
        elif self.error_code is not None:
            raise DomainValidationError("only failed runs can contain an error code")


@dataclass(frozen=True, slots=True)
class CreateRunResult:
    record: BacktestRunRecord
    replayed: bool


class BacktestRunStore(Protocol):
    def create_or_get(self, record: BacktestRunRecord) -> CreateRunResult: ...

    def get(self, run_id: RunId) -> BacktestRunRecord | None: ...

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
    ) -> BacktestRunRecord: ...

    def request_cancel(self, run_id: RunId) -> BacktestRunRecord: ...


class BacktestJobQueue(Protocol):
    def enqueue(self, run_id: RunId) -> str: ...


class BacktestQueueFullError(RuntimeError):
    """No job was accepted because the optional local capacity is exhausted."""
