from threading import Event

from ashare_lab.adapters.jobs import ThreadBacktestJobQueue
from ashare_lab.domain.shared import RunId


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
