from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest

from ashare_lab.adapters.jobs import ThreadBacktestJobQueue
from ashare_lab.domain.shared import RunId
from ashare_lab.ports.backtest_runs import BacktestQueueFullError


def test_thread_queue_passes_only_the_run_identity() -> None:
    called = Event()
    seen: list[RunId] = []

    def handler(run_id: RunId) -> None:
        seen.append(run_id)
        called.set()

    queue = ThreadBacktestJobQueue(handler)
    job_id = queue.enqueue(RunId("run:test"))

    assert job_id.startswith("thread:")
    assert called.wait(timeout=2)
    queue.shutdown()
    assert seen == [RunId("run:test")]


@pytest.mark.parametrize("max_pending", [None, 2])
def test_optional_capacity_limits_only_unfinished_jobs(max_pending: int | None) -> None:
    started, release = Event(), Event()
    seen: list[RunId] = []

    def handler(run_id: RunId) -> None:
        seen.append(run_id)
        started.set()
        assert release.wait(timeout=3)

    queue = ThreadBacktestJobQueue(handler, max_workers=1, max_pending=max_pending)
    try:
        queue.enqueue(RunId("run:0"))
        assert started.wait(timeout=2)
        for index in range(1, 3):
            queue.enqueue(RunId(f"run:{index}"))
        if max_pending is None:
            for index in range(3, 6):
                queue.enqueue(RunId(f"run:{index}"))
        else:
            with pytest.raises(BacktestQueueFullError):
                queue.enqueue(RunId("run:rejected"))
        assert seen == [RunId("run:0")]
    finally:
        release.set()
        queue.shutdown()
    assert len(seen) == (6 if max_pending is None else 3)


@pytest.mark.parametrize("fails", [False, True])
def test_capacity_is_released_after_success_or_failure(fails: bool) -> None:
    release, completed = Event(), Event()

    def handler(run_id: RunId) -> None:
        assert release.wait(timeout=3)
        if fails:
            raise RuntimeError("offline fixture handler failure")

    queue = ThreadBacktestJobQueue(handler, max_pending=0)
    try:
        job_id = queue.enqueue(RunId("run:0"))
        queue._futures[job_id].add_done_callback(lambda future: completed.set())
        with pytest.raises(BacktestQueueFullError):
            queue.enqueue(RunId("run:rejected"))
        release.set()
        assert completed.wait(timeout=2)
        assert queue.enqueue(RunId("run:retry")).startswith("thread:")
    finally:
        release.set()
        queue.shutdown()


def test_failed_executor_submission_does_not_leak_capacity() -> None:
    queue = ThreadBacktestJobQueue(lambda run_id: None, max_pending=0)
    queue.shutdown()
    for _ in range(2):
        with pytest.raises(RuntimeError, match="shutdown"):
            queue.enqueue(RunId("run:closed"))


def test_concurrent_producers_cannot_exceed_two_waiting_slots() -> None:
    release, started = Event(), Event()
    barrier = Barrier(8)

    def handler(run_id: RunId) -> None:
        started.set()
        assert release.wait(timeout=3)

    queue = ThreadBacktestJobQueue(handler, max_workers=1, max_pending=2)

    def submit(index: int) -> bool:
        barrier.wait(timeout=2)
        try:
            queue.enqueue(RunId(f"run:{index}"))
            return True
        except BacktestQueueFullError:
            return False

    try:
        queue.enqueue(RunId("run:first"))
        assert started.wait(timeout=2)
        with ThreadPoolExecutor(max_workers=8) as producers:
            accepted = list(producers.map(submit, range(8)))
        assert accepted.count(True) == 2 and accepted.count(False) == 6
    finally:
        release.set()
        queue.shutdown()


@pytest.mark.parametrize("max_pending", [-1, True, 1.5])
def test_pending_limit_requires_a_non_negative_integer(max_pending: object) -> None:
    with pytest.raises(ValueError, match="max_pending"):
        ThreadBacktestJobQueue(lambda run_id: None, max_pending=max_pending)  # type: ignore[arg-type]
