"""Process-local orchestration for slow historical snapshot preparation.

The immutable backtest run row can only be created after its real snapshot is
known.  This coordinator therefore keeps a short-lived preparation status
outside the run store, returns its reserved ``run_id`` immediately, and lets
the existing submission service materialise the real run in the background.
No current-value response is ever promoted to historical evidence here.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import fields, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from threading import RLock
from uuid import uuid4

from ashare_lab.domain.shared import RunId
from ashare_lab.domain.strategy import StrategySpec, canonical_hash, canonical_json
from ashare_lab.ports.backtest_runs import (
    BacktestJobState,
    BacktestRunRecord,
    BacktestRunStore,
    CreateRunResult,
)

from .backtest_submission import (
    BacktestDataNotYetAvailableError,
    BacktestRunConfig,
    BacktestSubmissionService,
    EventDataUnavailableError,
    FinancialDataUnavailableError,
)

_LOGGER = logging.getLogger(__name__)


class AsyncBacktestSubmissionCoordinator:
    """Return a run identity before slow provider acquisition has completed.

    This adapter deliberately makes only an in-process durability claim.  The
    final run remains stored by ``BacktestRunStore`` exactly as before.  A
    production/RQ deployment still needs a durable preparation queue before it
    can promise recovery across API-process restarts.
    """

    def __init__(
        self,
        *,
        submission: BacktestSubmissionService,
        run_store: BacktestRunStore,
        max_workers: int = 1,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        self._submission = submission
        self._run_store = run_store
        self._clock = clock
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="ashare-snapshot-preparation",
        )
        self._lock = RLock()
        self._preparations: dict[RunId, BacktestRunRecord] = {}
        self._run_by_request_fingerprint: dict[str, RunId] = {}
        self._futures: dict[RunId, Future[None]] = {}

    def submit(
        self,
        strategy: StrategySpec,
        config: BacktestRunConfig,
    ) -> CreateRunResult:
        request_fingerprint = canonical_hash(
            {
                "strategy": strategy.model_dump(mode="json"),
                "config": _request_config_payload(config),
            }
        )
        with self._lock:
            existing_id = self._run_by_request_fingerprint.get(request_fingerprint)
            if existing_id is not None:
                existing = self._lookup_locked(existing_id)
                if existing is not None:
                    return CreateRunResult(existing, replayed=True)

            now = self._clock()
            run_id = RunId(f"run:{uuid4().hex}")
            record = BacktestRunRecord(
                run_id=run_id,
                fingerprint=request_fingerprint,
                strategy_json=canonical_json(strategy),
                manifest_json=canonical_json(
                    {
                        "run_id": str(run_id),
                        "stage": "historical_snapshot_preparation",
                    }
                ),
                config_json=canonical_json(_request_config_payload(config)),
                state=BacktestJobState.RUNNING_DATA,
                progress_percent=5,
                progress_label="准备历史数据",
                created_at=now,
                updated_at=now,
            )
            self._preparations[run_id] = record
            self._run_by_request_fingerprint[request_fingerprint] = run_id
            future = self._executor.submit(self._prepare, run_id, strategy, config)
            self._futures[run_id] = future
            future.add_done_callback(lambda completed: self._report(run_id, completed))
            return CreateRunResult(record, replayed=False)

    def get_preparation(self, run_id: RunId) -> BacktestRunRecord | None:
        """Return pending/failure state, or an aliased completed run."""

        with self._lock:
            return self._lookup_locked(run_id)

    def request_cancel_preparation(self, run_id: RunId) -> BacktestRunRecord | None:
        with self._lock:
            current = self._preparations.get(run_id)
            if current is None:
                return None
            if current.state.is_terminal or current.state is BacktestJobState.CANCEL_REQUESTED:
                return current
            future = self._futures.get(run_id)
            cancelled_before_start = future is not None and future.cancel()
            updated = replace(
                current,
                state=(BacktestJobState.CANCELLED if cancelled_before_start
                       else BacktestJobState.CANCEL_REQUESTED),
                progress_label="已取消" if cancelled_before_start else "正在取消",
                updated_at=self._clock(),
                version=current.version + 1,
            )
            self._preparations[run_id] = updated
            if (cancelled_before_start
                    and self._run_by_request_fingerprint.get(current.fingerprint) == run_id):
                # No immutable work item was created. An explicit new request
                # may retry; the cancelled ID itself remains pollable.
                self._run_by_request_fingerprint.pop(current.fingerprint)
            return updated

    def shutdown(self, *, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=False)

    def _prepare(
        self,
        reserved_run_id: RunId,
        strategy: StrategySpec,
        config: BacktestRunConfig,
    ) -> None:
        try:
            result = self._submission.submit(
                strategy,
                config,
                run_id=reserved_run_id,
            )
        except Exception as exc:
            self._mark_failed(reserved_run_id, exc)
            return

        with self._lock:
            pending = self._preparations.get(reserved_run_id)
            if pending is not None and pending.state is BacktestJobState.CANCEL_REQUESTED:
                try:
                    result = CreateRunResult(
                        self._run_store.request_cancel(result.record.run_id),
                        replayed=result.replayed,
                    )
                except Exception:
                    _LOGGER.exception(
                        "prepared backtest could not be cancelled",
                        extra={"run_id": str(result.record.run_id)},
                    )
            # A completed-fingerprint replay can have an older run id.  Keep
            # the reserved path as an alias so polling the id returned by POST
            # still reaches the canonical immutable record.
            self._preparations[reserved_run_id] = result.record

    def _mark_failed(self, run_id: RunId, error: Exception) -> None:
        with self._lock:
            current = self._preparations.get(run_id)
            if current is None:
                return
            self._preparations[run_id] = replace(
                current,
                state=BacktestJobState.FAILED,
                progress_label="历史数据准备失败",
                error_code=_preparation_error_code(error),
                updated_at=self._clock(),
                version=current.version + 1,
            )
            # A failed acquisition has no immutable result to replay. Keep its
            # status pollable, but let the next explicit submission retry the
            # provider instead of permanently replaying this preparation error.
            if (self._run_store.get(run_id) is None
                    and self._run_by_request_fingerprint.get(current.fingerprint) == run_id):
                self._run_by_request_fingerprint.pop(current.fingerprint)

    def _lookup_locked(self, run_id: RunId) -> BacktestRunRecord | None:
        persisted = self._run_store.get(run_id)
        if persisted is not None:
            return persisted
        preparation = self._preparations.get(run_id)
        if preparation is not None and preparation.run_id != run_id:
            # Deduplication can resolve a temporary preparation ID to an
            # existing run. Poll that run, not its stale preparation snapshot.
            return self._run_store.get(preparation.run_id) or preparation
        return preparation

    def _report(self, run_id: RunId, future: Future[None]) -> None:
        with self._lock:
            self._futures.pop(run_id, None)
        if future.cancelled():
            return
        error = future.exception()
        if error is not None:
            _LOGGER.exception(
                "snapshot preparation worker crashed",
                exc_info=(type(error), error, error.__traceback__),
                extra={"run_id": str(run_id)},
            )


def _request_config_payload(config: BacktestRunConfig) -> dict[str, object]:
    payload: dict[str, object] = {}
    for field in fields(config):
        name = field.name
        value = getattr(config, name)
        if isinstance(value, Decimal):
            payload[name] = str(value)
        elif isinstance(value, Enum):
            payload[name] = value.value
        else:
            payload[name] = value
    return payload


def _preparation_error_code(error: Exception) -> str:
    if isinstance(error, BacktestDataNotYetAvailableError):
        return "backtest_data_not_yet_available"
    if isinstance(error, EventDataUnavailableError):
        return "event_data_unavailable"
    if isinstance(error, FinancialDataUnavailableError):
        return "financial_data_unavailable"
    return "historical_data_unavailable"
