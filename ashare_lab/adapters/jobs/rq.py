"""Redis Queue adapter for durable backtest work-item identities."""

from __future__ import annotations

from typing import Protocol, cast

from redis import Redis
from rq import Queue

from ashare_lab.domain.shared import RunId

BACKTEST_TASK_PATH = "ashare_lab.worker.tasks.execute_backtest_job"


class EnqueuedJob(Protocol):
    @property
    def id(self) -> str | None: ...


class RQQueueClient(Protocol):
    def enqueue(self, function: str, *args: object, **kwargs: object) -> EnqueuedJob: ...


class BacktestJobEnqueueError(RuntimeError):
    """Raised when RQ accepts a call without returning a durable job identity."""


class RQBacktestJobQueue:
    """Enqueue one run identity; workers reload all immutable inputs from storage."""

    def __init__(
        self,
        *,
        queue_name: str = "backtests",
        redis_url: str | None = None,
        connection: Redis | None = None,
        queue: RQQueueClient | None = None,
        default_timeout: int = 3600,
    ) -> None:
        if not queue_name.strip():
            raise ValueError("queue_name cannot be empty")
        if default_timeout < 1:
            raise ValueError("default_timeout must be positive")
        if queue is not None and (redis_url is not None or connection is not None):
            raise ValueError("inject either a queue or a Redis connection, not both")
        if redis_url is not None and connection is not None:
            raise ValueError("inject either redis_url or connection, not both")

        if queue is None:
            redis_connection = connection
            if redis_connection is None:
                if redis_url is None:
                    raise ValueError("redis_url or connection is required")
                # redis-py's published signature leaves ``**kwargs`` untyped.
                redis_connection = Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
                    redis_url
                )
            queue = cast(
                RQQueueClient,
                Queue(
                    name=queue_name,
                    connection=redis_connection,
                    default_timeout=default_timeout,
                ),
            )

        self._queue = queue
        self._queue_name = queue_name

    @property
    def queue_name(self) -> str:
        return self._queue_name

    def enqueue(self, run_id: RunId) -> str:
        job = self._queue.enqueue(BACKTEST_TASK_PATH, str(run_id))
        if not job.id:
            raise BacktestJobEnqueueError("RQ did not return a job id")
        return job.id


__all__ = [
    "BACKTEST_TASK_PATH",
    "BacktestJobEnqueueError",
    "EnqueuedJob",
    "RQBacktestJobQueue",
    "RQQueueClient",
]
