from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import httpx

from ashare_lab.adapters import host_http
from ashare_lab.adapters.host_http import HostThrottle, HostThrottledHttpClient


class _AdvancingClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _response_transport(status_code: int = 200, content: bytes = b"ok") -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda request: httpx.Response(status_code, content=content, request=request)
    )


def test_same_host_reservations_preserve_interval_and_injected_jitter() -> None:
    clock = _AdvancingClock()
    throttle = HostThrottle(
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
        jitter=lambda: 0.25,
    )

    throttle.wait("PUSH2HIS.EASTMONEY.COM.", 1.0)
    throttle.wait("push2his.eastmoney.com", 1.0)
    throttle.wait("push2his.eastmoney.com", 1.0)

    assert clock.sleeps == [1.25, 1.25]
    assert clock.now == 2.5


def test_different_hosts_have_independent_reservation_windows() -> None:
    sleeps: list[float] = []
    throttle = HostThrottle(
        monotonic=lambda: 0.0,
        sleeper=sleeps.append,
        jitter=lambda: 0.0,
    )

    throttle.wait("push2his.eastmoney.com", 1.0)
    throttle.wait("push2his.eastmoney.com", 1.0)
    throttle.wait("np-anotice-stock.eastmoney.com", 1.0)

    assert sleeps == [1.0]


def test_concurrent_same_host_callers_reserve_distinct_future_slots() -> None:
    sleeps: list[float] = []
    sleeps_lock = threading.Lock()

    def record_sleep(seconds: float) -> None:
        with sleeps_lock:
            sleeps.append(seconds)

    throttle = HostThrottle(
        monotonic=lambda: 0.0,
        sleeper=record_sleep,
        jitter=lambda: 0.0,
    )
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(throttle.wait, "push2his.eastmoney.com", 1.0) for _index in range(4)
        ]
        for future in futures:
            future.result()

    assert sorted(sleeps) == [1.0, 2.0, 3.0]


def test_default_wrappers_share_one_process_client_and_do_not_close_it() -> None:
    first = HostThrottledHttpClient(timeout=1.0)
    second = HostThrottledHttpClient(timeout=1.0)

    assert first.client is second.client
    first.close()
    second.close()
    assert not first.client.is_closed


def test_injected_client_is_borrowed_and_does_not_use_process_sleep(
    monkeypatch: object,
) -> None:
    def fail_sleep(_seconds: float) -> None:
        raise AssertionError("injected clients must not use the process throttle")

    monkeypatch.setattr(  # type: ignore[attr-defined]
        host_http,
        "_PROCESS_HOST_THROTTLE",
        HostThrottle(monotonic=lambda: 0.0, sleeper=fail_sleep, jitter=lambda: 0.0),
    )
    client = httpx.Client(transport=_response_transport())
    wrapper = HostThrottledHttpClient(client=client, timeout=1.0)
    try:
        wrapper.get("https://example.test/one", min_interval=10.0, timeout=1.0)
        wrapper.get("https://example.test/two", min_interval=10.0, timeout=1.0)
        wrapper.close()
        assert not client.is_closed
    finally:
        client.close()


def test_injected_transport_client_is_owned_and_http_semantics_are_untouched() -> None:
    wrapper = HostThrottledHttpClient(
        transport=_response_transport(status_code=503, content=b"raw-response"),
        timeout=1.0,
    )
    client = wrapper.client

    response = wrapper.get(
        "https://example.test/data",
        min_interval=10.0,
        timeout=1.0,
    )

    assert response.status_code == 503
    assert response.content == b"raw-response"
    wrapper.close()
    assert client.is_closed
