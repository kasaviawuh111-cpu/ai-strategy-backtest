from __future__ import annotations

from dataclasses import dataclass

import pytest

from ashare_lab.adapters.jobs import (
    BACKTEST_TASK_PATH,
    BacktestJobEnqueueError,
    RQBacktestJobQueue,
)
from ashare_lab.domain.shared import RunId


@dataclass
class StubJob:
    id: str | None


class StubRQQueue:
    def __init__(self, job_id: str | None = "rq-job-1") -> None:
        self.job_id = job_id
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def enqueue(self, function: str, *args: object, **kwargs: object) -> StubJob:
        self.calls.append((function, args, kwargs))
        return StubJob(self.job_id)


@pytest.mark.integration
def test_enqueue_passes_only_the_run_id_string_to_reserved_worker_path() -> None:
    stub = StubRQQueue()
    queue = RQBacktestJobQueue(queue_name="ashare-backtests", queue=stub)

    job_id = queue.enqueue(RunId("run:abc123"))

    assert job_id == "rq-job-1"
    assert queue.queue_name == "ashare-backtests"
    assert stub.calls == [(BACKTEST_TASK_PATH, ("run:abc123",), {})]


@pytest.mark.integration
def test_missing_rq_job_identity_is_rejected() -> None:
    queue = RQBacktestJobQueue(queue=StubRQQueue(job_id=None))

    with pytest.raises(BacktestJobEnqueueError, match="job id"):
        queue.enqueue(RunId("run:abc123"))


@pytest.mark.integration
def test_queue_configuration_rejects_ambiguous_or_empty_inputs() -> None:
    with pytest.raises(ValueError, match="queue_name"):
        RQBacktestJobQueue(queue_name=" ", queue=StubRQQueue())
    with pytest.raises(ValueError, match="required"):
        RQBacktestJobQueue()
    with pytest.raises(ValueError, match="not both"):
        RQBacktestJobQueue(queue=StubRQQueue(), redis_url="redis://localhost:6379/0")
