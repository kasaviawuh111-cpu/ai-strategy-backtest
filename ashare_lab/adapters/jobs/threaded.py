"""Process-local asynchronous queue for the zero-infrastructure demo profile."""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from threading import BoundedSemaphore
from uuid import uuid4

from ashare_lab.domain.shared import RunId
from ashare_lab.ports.backtest_runs import BacktestQueueFullError

logger = logging.getLogger(__name__)


class ThreadBacktestJobQueue:
    """Run jobs outside the HTTP request thread without claiming durability.

    This adapter makes the sample app runnable with only SQLite. Durable
    deployments use RQ so queued identities survive API process restarts.
    Optional max_pending limits waiting capacity, not persistence.
    """

    def __init__(
        self,
        handler: Callable[[RunId], object],
        *,
        max_workers: int = 1,
        max_pending: int | None = None,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        if max_pending is not None and (type(max_pending) is not int or max_pending < 0):
            raise ValueError("max_pending must be a non-negative integer or None")
        self._handler = handler
        # Bound unfinished jobs: running workers plus waiting slots. None keeps
        # the existing unlimited waiting behavior; this is not a durable queue.
        self._capacity = (
            None if max_pending is None else BoundedSemaphore(max_workers + max_pending)
        )
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="ashare-backtest",
        )
        self._futures: dict[str, Future[object]] = {}

    def enqueue(self, run_id: RunId) -> str:
        if self._capacity is not None and not self._capacity.acquire(blocking=False):
            raise BacktestQueueFullError("local backtest queue is full")
        job_id = f"thread:{uuid4().hex}"
        try:
            future = self._executor.submit(self._handler, run_id)
        except BaseException:
            if self._capacity is not None:
                self._capacity.release()
            raise
        self._futures[job_id] = future
        future.add_done_callback(lambda completed: self._report(job_id, completed))
        return job_id

    def shutdown(self, *, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=False)

    def _report(self, job_id: str, future: Future[object]) -> None:
        self._futures.pop(job_id, None)
        if self._capacity is not None:
            self._capacity.release()
        if future.cancelled():
            return
        error = future.exception()
        if error is not None:
            logger.exception(
                "local backtest job failed",
                exc_info=(type(error), error, error.__traceback__),
                extra={"job_id": job_id},
            )
