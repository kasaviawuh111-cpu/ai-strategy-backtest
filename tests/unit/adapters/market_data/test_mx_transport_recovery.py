"""Offline response-body interruption regressions for the original MX read request."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest

from ashare_lab.adapters.market_data.mx_saas import (
    MxRetryProgress,
    MxSaasMarketDataClient,
    MxSaasProviderAuthError,
    MxSaasProviderUnavailableError,
    observe_mx_retries,
)

_QUERY = "查询东方财富300059.SZ 2024-09-01至2026-09-04每个交易日数据"
_INDICATORS = "不复权开盘价、不复权收盘价、成交量、成交额"
_PRIVATE_KEY = "test-private-mx-recovery-key"
_PRIVATE_ERROR = "private upstream body with em_api_key=" + _PRIVATE_KEY
_PRIVATE_ERROR_TYPE = type("PrivateTransportWithSensitiveSuffix", (httpx.ReadError,), {})
_TABLE = {
    "title": "东方财富历史行情",
    "rawTable": {"headers": ["交易日期", "收盘价"], "data": [["2026-09-04", "20.01"]]},
}
_SUCCESS = {"code": 200, "data": {"searchDataResultDTO": {"dataTableDTOList": [_TABLE]}}}


class _InterruptedBody(httpx.AsyncByteStream):
    def __init__(self, error: httpx.TransportError) -> None:
        self.error = error
        self.read_started = False
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.read_started = True
        yield b'{"code":200,"data":'
        raise self.error

    async def aclose(self) -> None:
        self.closed = True


def _assert_original_request(requests: list[httpx.Request]) -> None:
    assert requests
    original = requests[0].content
    assert all(request.content == original for request in requests)
    assert all(request.method == "POST" for request in requests)
    assert all(request.url.path == "/proxy/b/mcp/tool/searchData" for request in requests)
    assert all(request.headers["em_api_key"] == _PRIVATE_KEY for request in requests)
    payload = json.loads(original)
    assert payload["query"] == f"{_QUERY}；获取{_INDICATORS}"
    assert payload["toolContext"]["callId"].startswith("finance_")
    assert payload["toolContext"]["userInfo"] == {"userId": "ashare-backtest-service"}


def _assert_no_private_logging(caplog: pytest.LogCaptureFixture) -> None:
    assert _PRIVATE_ERROR not in caplog.text
    assert _PRIVATE_KEY not in caplog.text
    assert _QUERY not in caplog.text
    assert _INDICATORS not in caplog.text
    assert "toolContext" not in caplog.text
    assert "Traceback" not in caplog.text
    assert _PRIVATE_ERROR_TYPE.__name__ not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [httpx.ReadError, httpx.RemoteProtocolError])
@pytest.mark.parametrize("failures_before_success", [1, 2])
async def test_finance_retries_interrupted_response_body_without_rewriting_request(
    error_type: type[httpx.TransportError],
    failures_before_success: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    requests: list[httpx.Request] = []
    interrupted: list[_InterruptedBody] = []
    delays: list[float] = []

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) <= failures_before_success:
            body = _InterruptedBody(error_type(_PRIVATE_ERROR, request=request))
            interrupted.append(body)
            return httpx.Response(200, stream=body)
        return httpx.Response(200, json=_SUCCESS)

    client = MxSaasMarketDataClient(
        api_key=_PRIVATE_KEY,
        transport=httpx.MockTransport(handler),
        sleeper=sleeper,
        clock=lambda: datetime(2026, 9, 6, 9, 43, tzinfo=UTC),
    )
    with caplog.at_level(logging.INFO):
        result = await client.query_finance(query=_QUERY, indicators=_INDICATORS)

    assert len(requests) == failures_before_success + 1
    assert delays == [1.0, 2.0][:failures_before_success]
    assert all(body.read_started and body.closed for body in interrupted)
    _assert_original_request(requests)
    assert result.query == f"{_QUERY}；获取{_INDICATORS}"
    assert result.indicators == _INDICATORS
    assert result.tables == (_TABLE,)
    assert result.provenance.response_sha256.startswith("sha256:")
    _assert_no_private_logging(caplog)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_type", "expected_kind"),
    [
        (httpx.ReadError, "ReadError"),
        (httpx.RemoteProtocolError, "RemoteProtocolError"),
        pytest.param(_PRIVATE_ERROR_TYPE, "TransportError", id="unknown-subclass"),
    ],
)
async def test_finance_stops_after_three_interrupted_bodies_with_safe_metadata(
    error_type: type[httpx.TransportError], expected_kind: str, caplog: pytest.LogCaptureFixture,
) -> None:
    requests: list[httpx.Request] = []
    interrupted: list[_InterruptedBody] = []
    delays: list[float] = []

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = _InterruptedBody(error_type(_PRIVATE_ERROR, request=request))
        interrupted.append(body)
        return httpx.Response(200, stream=body)

    client = MxSaasMarketDataClient(
        api_key=_PRIVATE_KEY, transport=httpx.MockTransport(handler), sleeper=sleeper,
    )
    with caplog.at_level(logging.INFO), pytest.raises(MxSaasProviderUnavailableError) as caught:
        await client.query_finance(query=_QUERY, indicators=_INDICATORS)

    error = caught.value
    assert len(requests) == 3
    assert delays == [1.0, 2.0]
    assert all(body.read_started and body.closed for body in interrupted)
    _assert_original_request(requests)
    assert error.tool == "searchData"
    assert error.reason == "transport_error"
    assert error.transport_kind == expected_kind
    assert error.attempts == 3
    assert error.call_id == json.loads(requests[0].content)["toolContext"]["callId"]
    assert error.http_status is None
    public_error = f"{error!s} {error!r} {vars(error)!r}"
    assert _PRIVATE_ERROR not in public_error
    assert _PRIVATE_KEY not in public_error
    assert _PRIVATE_ERROR_TYPE.__name__ not in public_error
    assert "attempt=3/3" in caplog.text
    assert f"kind={expected_kind}" in caplog.text
    _assert_no_private_logging(caplog)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_finance_auth_failure_is_not_retried(
    status: int, caplog: pytest.LogCaptureFixture,
) -> None:
    requests: list[httpx.Request] = []
    delays: list[float] = []

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, text=_PRIVATE_ERROR)

    client = MxSaasMarketDataClient(
        api_key=_PRIVATE_KEY, transport=httpx.MockTransport(handler), sleeper=sleeper,
    )
    with caplog.at_level(logging.INFO), pytest.raises(MxSaasProviderAuthError) as caught:
        await client.query_finance(query=_QUERY, indicators=_INDICATORS)

    assert len(requests) == 1
    assert delays == []
    assert caught.value.http_status == status
    _assert_original_request(requests)
    _assert_no_private_logging(caplog)


@pytest.mark.asyncio
async def test_finance_task_cancellation_while_reading_body_is_not_retried(
    caplog: pytest.LogCaptureFixture,
) -> None:
    requests: list[httpx.Request] = []
    delays: list[float] = []
    body_started = asyncio.Event()
    body_closed = asyncio.Event()

    class WaitingBody(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b'{"code":200,"data":'
            body_started.set()
            await asyncio.Future()

        async def aclose(self) -> None:
            body_closed.set()

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, stream=WaitingBody())

    client = MxSaasMarketDataClient(
        api_key=_PRIVATE_KEY, transport=httpx.MockTransport(handler), sleeper=sleeper,
    )
    with caplog.at_level(logging.INFO):
        task = asyncio.create_task(client.query_finance(query=_QUERY, indicators=_INDICATORS))
        try:
            await asyncio.wait_for(body_started.wait(), timeout=1)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert len(requests) == 1
    assert delays == []
    assert body_closed.is_set()
    _assert_original_request(requests)
    _assert_no_private_logging(caplog)


@pytest.mark.asyncio
async def test_finance_cancellation_during_retry_backoff_stops_next_attempt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    requests: list[httpx.Request] = []
    delays: list[float] = []

    async def sleeper(delay: float) -> None:
        delays.append(delay)
        raise asyncio.CancelledError

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, stream=_InterruptedBody(httpx.ReadError(_PRIVATE_ERROR, request=request)),
        )

    client = MxSaasMarketDataClient(
        api_key=_PRIVATE_KEY, transport=httpx.MockTransport(handler), sleeper=sleeper,
    )
    with caplog.at_level(logging.INFO), pytest.raises(asyncio.CancelledError):
        await client.query_finance(query=_QUERY, indicators=_INDICATORS)

    assert len(requests) == 1
    assert delays == [1.0]
    _assert_original_request(requests)
    _assert_no_private_logging(caplog)


@pytest.mark.asyncio
async def test_retry_observer_isolated_between_concurrent_contexts_and_reset_after_exit() -> None:
    attempts: dict[str, int] = {}
    call_ids: dict[str, str] = {}
    observed: dict[str, list[MxRetryProgress]] = {"run-a": [], "run-b": []}

    async def sleeper(_delay: float) -> None:
        await asyncio.sleep(0)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        query = payload["query"]
        call_ids[query] = payload["toolContext"]["callId"]
        attempts[query] = attempts.get(query, 0) + 1
        if attempts[query] == 1:
            return httpx.Response(
                200, stream=_InterruptedBody(httpx.ReadError(_PRIVATE_ERROR, request=request)),
            )
        return httpx.Response(200, json=_SUCCESS)

    mx = MxSaasMarketDataClient(
        api_key=_PRIVATE_KEY, transport=httpx.MockTransport(handler), sleeper=sleeper,
    )

    async def run_queries(owner: str) -> None:
        with observe_mx_retries(observed[owner].append):
            # gather children must inherit this task's observer, not the other run's.
            await asyncio.gather(*(
                mx.query_finance(query=f"{owner}-query-{number}", indicators=None)
                for number in range(2)
            ))

    await asyncio.gather(run_queries("run-a"), run_queries("run-b"))
    for owner, events in observed.items():
        expected_ids = {call_ids[f"{owner}-query-{number}"] for number in range(2)}
        assert len(events) == 4
        assert {event.call_id for event in events} == expected_ids
        for call_id in expected_ids:
            request_events = [event for event in events if event.call_id == call_id]
            assert [event.recovered for event in request_events] == [False, True]
            assert all(
                event.retry_number == 1 and event.max_retries == 2 for event in request_events
            )
        assert all(event.tool == "searchData" for event in events)

    before_outside_query = {owner: tuple(events) for owner, events in observed.items()}
    await mx.query_finance(query="outside-context", indicators=None)
    assert attempts["outside-context"] == 2
    assert {owner: tuple(events) for owner, events in observed.items()} == before_outside_query
