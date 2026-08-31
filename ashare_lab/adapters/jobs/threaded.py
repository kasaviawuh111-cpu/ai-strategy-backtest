"""Process-local asynchronous queue for the zero-infrastructure demo profile."""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from uuid import uuid4

from ashare_lab.domain.shared import RunId

logger = logging.getLogger(__name__)


class ThreadBacktestJobQueue:
    """Run jobs outside the HTTP request thread without claiming durability.

    This adapter makes the sample app runnable with only SQLite.  Production
    uses the RQ adapter so queued identities survive API process restarts.
    """

    def __init__(
        self,
        handler: Callable[[RunId], object],
        *,
        max_workers: int = 1,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        self._handler = handler
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="ashare-backtest",
        )
        self._futures: dict[str, Future[object]] = {}

    def enqueue(self, run_id: RunId) -> str:
        job_id = f"thread:{uuid4().hex}"
        future = self._executor.submit(self._handler, run_id)
        self._futures[job_id] = future
        future.add_done_callback(lambda completed: self._report(job_id, completed))
        return job_id

    def shutdown(self, *, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=False)

    def _report(self, job_id: str, future: Future[object]) -> None:
        self._futures.pop(job_id, None)
        error = future.exception()
        if error is not None:
            logger.exception(
                "local backtest job failed",
                exc_info=(type(error), error, error.__traceback__),
                extra={"job_id": job_id},
            )
