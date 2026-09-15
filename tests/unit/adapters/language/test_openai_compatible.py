from __future__ import annotations

import asyncio
import json
import ssl
from dataclasses import replace
from datetime import date

import httpcore
import httpx
import pytest
from pydantic import SecretStr

from ashare_lab.adapters.language.openai_compatible import (
    CandidateProviderTransportError,
    DisabledCandidateJsonTransport,
    OpenAICompatibleCandidateTransport,
    _parse_candidate_json,
)
from ashare_lab.adapters.language.vibe_candidates import (
    CandidateFailureKind,
    CandidateTransportRequest,
)
from ashare_lab.ports.dialogue_progress import model_reasoning_sink, progress_sink


class _CountingStream(httpx.AsyncByteStream):
    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self._chunks = chunks
        self.yielded = 0
        self.closed = False

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        for chunk in self._chunks:
            self.yielded += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class _DelayedStream(_CountingStream):
    def __init__(self, chunks: tuple[bytes, ...], *, delay_seconds: float) -> None:
        super().__init__(chunks)
        self._delay_seconds = delay_seconds

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        for chunk in self._chunks:
            await asyncio.sleep(self._delay_seconds)
            self.yielded += 1
            yield chunk


def _request() -> CandidateTransportRequest:
    return CandidateTransportRequest(
        utterance="快线向上穿越慢线时买入，反向时卖出",
        instrument_context="300059.SZ",
        as_of_date=date(2026, 8, 30),
        max_candidates=3,
        response_schema={"type": "object", "properties": {"candidates": {"type": "array"}}},
        capability_matrix={
            "schema_version": "candidate-capabilities.v1",
            "indicators": [],
            "events": [],
        },
        capability_projection_version="candidate-capabilities.v1",
        capability_projection_hash=f"sha256:{'a' * 64}",
        system_contract="只返回受限 JSON，不得生成代码。",
    )


@pytest.mark.parametrize("content", [
    '{"candidates":[]}',
    '  {"candidates":[]}\n',
    '```json\n{"candidates":[]}\n```',
    '```\n{"candidates":[]}\n```',
])
def test_candidate_json_accepts_only_plain_or_single_fenced_object(content: str) -> None:
    assert _parse_candidate_json(content) == {"candidates": []}


@pytest.mark.parametrize("content", [
    '结果如下：{"candidates":[]}',
    '```json\n{"candidates":[]}\n``` trailing',
])
def test_candidate_json_rejects_prose_and_trailing_material(content: str) -> None:
    with pytest.raises(json.JSONDecodeError):
        _parse_candidate_json(content)


@pytest.mark.asyncio
@pytest.mark.parametrize(("schema", "response_only", "connect"), [
    ("ashare_clarification_dialogue", True, 20.0),
    ("ashare_clarification_dialogue", False, 10.0),
    ("ashare_bounded_strategy_candidates", True, 10.0),
])
async def test_response_only_connect_budget_preserves_active_read_timeout_and_no_deadline(
    schema: str, response_only: bool, connect: float, monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    deadlines: list[float | None] = []
    original_timeout = asyncio.timeout

    def record_timeout(delay: float | None) -> asyncio.Timeout:
        deadlines.append(delay)
        return original_timeout(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _sse_response(request, {"candidates": []})

    provider = OpenAICompatibleCandidateTransport(
        endpoint="https://gateway.example.test/v1/chat/completions",
        provider="deepseek", model="fixture", prompt_version="v1", schema_version="v1",
        timeout_seconds=300.0, transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr(asyncio, "timeout", record_timeout)
    await provider.startup()
    try:
        await provider.generate_json(replace(
            _request(), response_schema_name=schema, user_payload={"responseOnly": response_only},
        ))
        await provider.generate_json(_request())
    finally:
        await provider.aclose()
    ordinary = requests[1].extensions["timeout"]
    assert ordinary == {"connect": 10.0, "read": 300.0, "write": 30.0, "pool": 10.0}
    assert requests[0].extensions["timeout"] == {**ordinary, "connect": connect}
    assert deadlines.count(None) == 2  # no whole-generation deadline
    assert all(delay is None or 0 < delay <= 300 for delay in deadlines)
    assert any(delay is not None for delay in deadlines)  # per-delta inactivity budget


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize(("status", "kind", "api_status"), [
    (401, "authentication_failed", 503), (403, "permission_denied", 503),
    (402, "insufficient_balance", 503), (429, "rate_limited", 429),
    (500, "service_unavailable", 503), (503, "service_unavailable", 503),
])
async def test_known_http_failure_is_classified_once_without_body_or_secret(
    status: int, kind: CandidateFailureKind, api_status: int,
    caplog: pytest.LogCaptureFixture, managed: bool,
) -> None:
    calls = 0
    private = "private-body-key-reasoning-must-not-leak"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, text=private, request=request)

    provider = OpenAICompatibleCandidateTransport(
        endpoint="https://api.deepseek.com/chat/completions", provider="deepseek",
        model="fixture", prompt_version="v1", schema_version="v1",
        api_key=SecretStr(private), transport=httpx.MockTransport(handler),
    )
    if managed:
        await provider.startup()
    try:
        with pytest.raises(CandidateProviderTransportError) as caught:
            await provider.generate_json(_request())
    finally:
        await provider.aclose()
    error = caught.value
    assert calls == 1
    assert error.is_classified and error.failure_kind == kind
    assert error.http_status == status and error.api_status_code == api_status
    assert error.public_code == f"candidate_provider_{kind}"
    assert f"status={status}" in caplog.text and f"reason={kind}" in caplog.text
    assert private not in str(error) + error.public_message + caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(("endpoint", "provider_name"), [
    ("https://gateway.example.test/chat/completions", "deepseek"),
    ("https://api.deepseek.com/chat/completions", "gateway"),
    ("https://api.deepseek.com.example.test/chat/completions", "deepseek"),
])
async def test_gateway_402_does_not_claim_deepseek_balance(
    endpoint: str, provider_name: str,
) -> None:
    provider = OpenAICompatibleCandidateTransport(
        endpoint=endpoint, provider=provider_name, model="fixture", prompt_version="v1",
        schema_version="v1", transport=httpx.MockTransport(
            lambda request: httpx.Response(402, request=request),
        ),
    )
    with pytest.raises(CandidateProviderTransportError) as caught:
        await provider.generate_json(_request())
    assert caught.value.failure_kind == "billing_restricted"
    assert "余额" not in caught.value.public_message


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize(("fault", "kind"), [
    ("timeout", "timeout"), ("connection", "connection_failed"),
    ("invalid_json", "invalid_response"), ("missing_choices", "invalid_response"),
    ("truncated_stream", "incomplete_response"),
    ("unfinished_stream", "incomplete_response"),
])
async def test_transport_fault_keeps_safe_classification(
    fault: str, kind: CandidateFailureKind, caplog: pytest.LogCaptureFixture, managed: bool,
) -> None:
    calls = 0
    private = "private-upstream-error-must-not-leak"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if fault == "timeout":
            raise httpx.ReadTimeout(private, request=request)
        if fault == "connection":
            raise httpx.ConnectError(private, request=request)
        if fault == "invalid_json":
            return httpx.Response(200, content=private.encode(), request=request)
        if fault == "missing_choices":
            return httpx.Response(200, json={"private": private}, request=request)
        chunks = (_sse_delta(content='{"candidates":[]}'),)
        if fault == "unfinished_stream":
            chunks += (b"data: [DONE]\n\n",)
        return _sse_response(request, {}, stream=_CountingStream(chunks))

    provider = OpenAICompatibleCandidateTransport(
        endpoint="https://api.deepseek.com/chat/completions", provider="deepseek",
        model="fixture", prompt_version="v1", schema_version="v1",
        transport=httpx.MockTransport(handler),
    )
    if managed:
        await provider.startup()
    try:
        with pytest.raises(CandidateProviderTransportError) as caught:
            await provider.generate_json(_request())
    finally:
        await provider.aclose()
    assert calls == 1 and caught.value.failure_kind == kind
    assert caught.value.http_status == (None if fault in {"timeout", "connection"} else 200)
    assert caught.value.timed_out is (fault == "timeout")
    assert f"reason={kind}" in caplog.text
    assert private not in str(caught.value) + caught.value.public_message + caplog.text


def _completion(content: object) -> dict[str, object]:
    return {
        "id": "completion-fixture",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": json.dumps(content, ensure_ascii=False),
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"total_tokens": 100},
    }


def _sse_event(payload: object) -> bytes:
    return (
        b"data: "
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        + b"\n\n"
    )


def _sse_delta(
    *,
    content: str | None = None,
    reasoning_content: str | None = None,
    finish_reason: str | None = None,
) -> bytes:
    delta: dict[str, str] = {}
    if content is not None:
        delta["content"] = content
    if reasoning_content is not None:
        delta["reasoning_content"] = reasoning_content
    return _sse_event(
        {
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": finish_reason,
                }
            ]
        }
    )


def _sse_completion(
    content: object,
    *,
    reasoning_content: str = "private model reasoning",
) -> tuple[bytes, ...]:
    serialized = json.dumps(content, ensure_ascii=False)
    midpoint = max(1, len(serialized) // 2)
    return (
        b": keepalive\n\n",
        _sse_delta(reasoning_content=reasoning_content),
        _sse_delta(content=serialized[:midpoint]),
        _sse_delta(content=serialized[midpoint:]),
        _sse_delta(finish_reason="stop"),
        b"data: [DONE]\n\n",
    )


def _sse_response(
    request: httpx.Request,
    content: object,
    *,
    stream: httpx.AsyncByteStream | None = None,
) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"Content-Type": "text/event-stream"},
        stream=stream or _CountingStream(_sse_completion(content)),
        request=request,
    )


@pytest.mark.asyncio
async def test_default_client_enables_connection_only_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options: list[dict[str, object]] = []

    def transport_factory(**kwargs: object) -> httpx.MockTransport:
        options.append(kwargs)
        return httpx.MockTransport(lambda request: httpx.Response(
            200, json=_completion({"candidates": []}), request=request,
        ))

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", transport_factory)
    provider = OpenAICompatibleCandidateTransport(
        endpoint="https://gateway.example.test/v1/chat/completions", provider="fixture-gateway",
        model="fixture-model", prompt_version="prompt.v1", schema_version="schema.v1",
    )
    await provider.generate_json(_request())
    assert len(options) == 1
    assert options[0]["retries"] == 2 and options[0]["trust_env"] is False
    assert options[0]["limits"] == httpx.Limits(keepalive_expiry=30.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["tcp", "tls"])
@pytest.mark.parametrize("failures", [2, 3])
@pytest.mark.parametrize("broken_progress", [False, True])
async def test_real_connect_retries_three_attempts_before_sending_once(
    phase: str, failures: int, broken_progress: bool,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    # Exercise the installed httpx/httpcore retry and trace implementation,
    # replacing only the socket backend. No network or model call is made.
    body = json.dumps(_completion({"candidates": []})).encode()
    wire = b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
    attempts = 0
    writes: list[bytes] = []
    progress: list[tuple[str, str]] = []

    class Stream(httpcore.AsyncMockStream):
        async def start_tls(
            self, ssl_context: ssl.SSLContext, server_hostname: str | None = None,
            timeout: float | None = None,
        ) -> httpcore.AsyncMockStream:
            if phase == "tls" and attempts <= failures:
                raise httpcore.ConnectError("private-provider-error")
            return self

        async def write(self, buffer: bytes, timeout: float | None = None) -> None:
            writes.append(buffer)

    class Backend(httpcore.AsyncMockBackend):
        async def connect_tcp(self, **kwargs: object) -> Stream:
            nonlocal attempts
            attempts += 1
            if phase == "tcp" and attempts <= failures:
                raise httpcore.ConnectTimeout("private-provider-error")
            return Stream([wire])

    original_transport = httpx.AsyncHTTPTransport

    def transport_factory(**kwargs: object) -> httpx.AsyncHTTPTransport:
        transport = original_transport(**kwargs)  # type: ignore[arg-type]
        transport._pool._network_backend = Backend([])
        return transport

    def sink(stage: str, message: str) -> None:
        progress.append((stage, message))
        if broken_progress and stage == "model_retry":
            raise RuntimeError("private-progress-error")

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", transport_factory)
    provider = OpenAICompatibleCandidateTransport(
        endpoint="https://private.example.test/private-route", provider="fixture-gateway",
        model="fixture", prompt_version="v1", schema_version="v1",
        api_key=SecretStr("private-key"), timeout_seconds=300.0,
    )
    token = progress_sink.set(sink)
    try:
        if failures == 2:
            assert await provider.generate_json(_request()) == {"candidates": []}
        else:
            with pytest.raises(CandidateProviderTransportError) as caught:
                await provider.generate_json(_request())
            assert caught.value.failure_kind == (
                "timeout" if phase == "tcp" else "connection_failed"
            )
    finally:
        progress_sink.reset(token)
    assert attempts == 3
    assert sum(part.startswith(b"POST ") for part in writes) == (1 if failures == 2 else 0)
    assert [message for stage, message in progress if stage == "model_retry"] == [
        "模型连接暂时未建立，正在自动重连（第 2/3 次）。",
        "模型连接暂时未建立，正在自动重连（第 3/3 次）。",
    ]
    assert "private-" not in repr(progress) + caplog.text
    assert "private.example.test" not in repr(progress) + caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [
    "http401", "http503", "invalid_json", "partial_read", "partial_write",
])
async def test_real_connect_retry_never_replays_http_or_partly_sent_or_received_request(
    fault: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = (b"bad-json" if fault == "invalid_json" else
            json.dumps(_completion({"candidates": []})).encode())
    status = fault[4:] if fault.startswith("http") else "200"
    head = (f"HTTP/1.1 {status} Response\r\nContent-Length: {len(body)}\r\n\r\n").encode()
    attempts = 0
    writes: list[bytes] = []
    progress: list[tuple[str, str]] = []

    class Stream(httpcore.AsyncMockStream):
        async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
            if fault == "partial_write":
                raise httpcore.ReadError("connection lost after partial send")
            if fault == "partial_read" and not self._buffer:
                raise httpcore.ReadError("stream interrupted after partial body")
            return await super().read(max_bytes, timeout)

        async def write(self, buffer: bytes, timeout: float | None = None) -> None:
            writes.append(buffer)
            if fault == "partial_write":
                raise httpcore.WriteError("write interrupted after sending headers")

    class Backend(httpcore.AsyncMockBackend):
        async def connect_tcp(self, **kwargs: object) -> Stream:
            nonlocal attempts
            attempts += 1
            return Stream([head + (body[:5] if fault == "partial_read" else body)])

    original_transport = httpx.AsyncHTTPTransport

    def transport_factory(**kwargs: object) -> httpx.AsyncHTTPTransport:
        transport = original_transport(**kwargs)  # type: ignore[arg-type]
        transport._pool._network_backend = Backend([])
        return transport

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", transport_factory)
    provider = OpenAICompatibleCandidateTransport(
        endpoint="https://gateway.example.test/model", provider="fixture-gateway",
        model="fixture", prompt_version="v1", schema_version="v1",
    )
    token = progress_sink.set(lambda stage, message: progress.append((stage, message)))
    try:
        with pytest.raises(CandidateProviderTransportError):
            await provider.generate_json(_request())
    finally:
        progress_sink.reset(token)
    assert attempts == 1
    assert sum(part.startswith(b"POST ") for part in writes) == 1
    assert not any(stage == "model_retry" for stage, _ in progress)


@pytest.mark.asyncio
async def test_managed_client_reuses_main_loop_pool_and_isolates_worker_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pools = []

    class Pool(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.loop = asyncio.get_running_loop()
            self.requests = 0
            self.closes = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            assert asyncio.get_running_loop() is self.loop
            assert self.closes == 0
            self.requests += 1
            return _sse_response(request, {"candidates": []})

        async def aclose(self) -> None:
            assert asyncio.get_running_loop() is self.loop
            self.closes += 1

    def transport_factory(**kwargs: object) -> Pool:
        pool = Pool()
        pools.append(pool)
        return pool

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", transport_factory)
    provider = OpenAICompatibleCandidateTransport(
        endpoint="https://api.deepseek.com/chat/completions", provider="deepseek",
        model="fixture", prompt_version="v1", schema_version="v1",
    )
    await provider.startup()
    await provider.startup()
    assert len(pools) == 1 and pools[0].requests == 0  # Startup is not a model call.
    try:
        await provider.generate_json(_request())
        await provider.generate_json(_request())
        assert len(pools) == 1 and pools[0].requests == 2 and pools[0].closes == 0
        await asyncio.to_thread(lambda: asyncio.run(provider.generate_json(_request())))
        assert len(pools) == 2 and pools[1].requests == 1 and pools[1].closes == 1
        assert pools[1].loop is not pools[0].loop
        await provider.generate_json(_request())
        assert pools[0].requests == 3 and pools[0].closes == 0
    finally:
        await provider.aclose()
    assert pools[0].closes == 1
    await provider.aclose()
    assert pools[0].closes == 1


@pytest.mark.asyncio
async def test_transport_posts_strict_json_schema_without_leaking_secret() -> None:
    requests: list[httpx.Request] = []
    secret = "provider-secret-must-not-leak"
    candidate = {"candidates": [{"confidence": 0.91}]}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_completion(candidate), request=request)

    provider = OpenAICompatibleCandidateTransport(
        endpoint="https://gateway.example.test/v1/chat/completions",
        provider="fixture-gateway",
        model="fixture-model",
        prompt_version="prompt.v1",
        schema_version="schema.v1",
        api_key=SecretStr(secret),
        timeout_seconds=1.0,
        transport=httpx.MockTransport(handler),
    )

    result = await provider.generate_json(_request())

    assert result == candidate
    assert len(requests) == 1
    outbound = requests[0]
    body = json.loads(outbound.content)
    assert outbound.headers["Authorization"] == f"Bearer {secret}"
    assert body["model"] == "fixture-model"
    assert body["temperature"] == 0
    assert "stream" not in body
    assert body["n"] == 1
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "ashare_bounded_strategy_candidates",
            "strict": True,
            "schema": _request().response_schema,
        },
    }
    user_payload = json.loads(body["messages"][1]["content"])
    assert user_payload["instrumentContext"] == "300059.SZ"
    assert user_payload["capabilityProjectionVersion"] == "candidate-capabilities.v1"
    assert user_payload["capabilityProjectionHash"] == f"sha256:{'a' * 64}"
    assert user_payload["capabilityMatrix"] == _request().capability_matrix
    assert secret not in outbound.content.decode()
    assert secret not in repr(provider)
    assert provider.identity.provider == "fixture-gateway"
    assert provider.identity.model == "fixture-model"
    assert not hasattr(provider.identity, "api_key")
    assert not hasattr(provider.identity, "endpoint")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, text="provider-secret-must-not-leak"),
        httpx.Response(200, content=b"not-json"),
        httpx.Response(
            200,
            json={"choices": [{"message": {"content": "结果如下：{}"}}]},
        ),
    ],
)
async def test_provider_failures_are_sanitized(
    response: httpx.Response,
) -> None:
    secret = "provider-secret-must-not-leak"

    def handler(request: httpx.Request) -> httpx.Response:
        response.request = request
        return response

    provider = OpenAICompatibleCandidateTransport(
        endpoint="https://gateway.example.test/v1/chat/completions",
        provider="fixture-gateway",
        model="fixture-model",
        prompt_version="prompt.v1",
        schema_version="schema.v1",
        api_key=SecretStr(secret),
        timeout_seconds=1.0,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(CandidateProviderTransportError) as caught:
        await provider.generate_json(_request())

    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)


@pytest.mark.asyncio
async def test_timeout_is_bounded_and_sanitized() -> None:
    secret = "provider-secret-must-not-leak"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(
            "upstream timeout with provider-secret-must-not-leak",
            request=request,
        )

    provider = OpenAICompatibleCandidateTransport(
        endpoint="https://gateway.example.test/v1/chat/completions",
        provider="fixture-gateway",
        model="fixture-model",
        prompt_version="prompt.v1",
        schema_version="schema.v1",
        api_key=SecretStr(secret),
        timeout_seconds=0.25,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(CandidateProviderTransportError) as caught:
        await provider.generate_json(_request())

    assert str(caught.value) == "candidate provider response unavailable"
    assert secret not in repr(caught.value)


@pytest.mark.asyncio
async def test_transport_rejects_non_object_and_oversized_candidate_payloads() -> None:
    responses = iter(
        (
            httpx.Response(200, json=_completion(["not", "an", "object"])),
            httpx.Response(200, json=_completion({"large": "x" * 2_000})),
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        response = next(responses)
        response.request = request
        return response

    provider = OpenAICompatibleCandidateTransport(
        endpoint="https://gateway.example.test/v1/chat/completions",
        provider="fixture-gateway",
        model="fixture-model",
        prompt_version="prompt.v1",
        schema_version="schema.v1",
        max_response_bytes=1_024,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(CandidateProviderTransportError, match="not a JSON object"):
        await provider.generate_json(_request())
    with pytest.raises(CandidateProviderTransportError, match="too large"):
        await provider.generate_json(_request())


@pytest.mark.asyncio
async def test_streaming_response_aborts_immediately_after_limit() -> None:
    stream = _CountingStream((b"x" * 700, b"y" * 700, b"must-not-be-read"))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, request=request)

    provider = OpenAICompatibleCandidateTransport(
        endpoint="https://gateway.example.test/v1/chat/completions",
        provider="fixture-gateway",
        model="fixture-model",
        prompt_version="prompt.v1",
        schema_version="schema.v1",
        max_response_bytes=1_024,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(CandidateProviderTransportError, match="too large"):
        await provider.generate_json(_request())

    assert stream.yielded == 2
    assert stream.closed is True


@pytest.mark.asyncio
async def test_request_projection_is_bounded_before_network() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_completion({"candidates": []}), request=request)

    provider = OpenAICompatibleCandidateTransport(
        endpoint="https://gateway.example.test/v1/chat/completions",
        provider="fixture-gateway",
        model="fixture-model",
        prompt_version="prompt.v1",
        schema_version="schema.v1",
        max_request_bytes=1_024,
        transport=httpx.MockTransport(handler),
    )
    request = _request()
    oversized = CandidateTransportRequest(
        utterance=request.utterance,
        instrument_context=request.instrument_context,
        as_of_date=request.as_of_date,
        max_candidates=request.max_candidates,
        response_schema=request.response_schema,
        capability_matrix={"blob": "x" * 5_000},
        capability_projection_version=request.capability_projection_version,
        capability_projection_hash=request.capability_projection_hash,
        system_contract=request.system_contract,
    )

    with pytest.raises(CandidateProviderTransportError, match="request is too large"):
        await provider.generate_json(oversized)

    assert calls == 0


@pytest.mark.asyncio
async def test_json_object_mode_is_explicit_and_still_returns_only_local_json() -> None:
    requests: list[httpx.Request] = []
    candidate = {"candidates": [{"confidence": 0.91}]}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_completion(candidate), request=request)

    provider = OpenAICompatibleCandidateTransport(
        endpoint="https://gateway.example.test/v1/chat/completions",
        provider="fixture-gateway",
        model="fixture-model",
        prompt_version="prompt.v1",
        schema_version="schema.v1",
        response_mode="json_object",
        transport=httpx.MockTransport(handler),
    )

    result = await provider.generate_json(_request())

    assert result == candidate
    assert provider.response_mode == "json_object"
    body = json.loads(requests[0].content)
    assert body["response_format"] == {"type": "json_object"}
    user_payload = json.loads(body["messages"][1]["content"])
    assert user_payload["responseSchema"] == _request().response_schema
    assert (
        "responseSchema field in the user JSON is authoritative" in (body["messages"][0]["content"])
    )
    assert "volume.relative" in body["messages"][0]["content"]
    assert "instead of splitting the clause into fragments" in body["messages"][0]["content"]


@pytest.mark.asyncio
async def test_deepseek_structured_requests_disable_default_thinking() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _sse_response(
            request,
            {"candidates": [{"confidence": 0.91}]},
        )

    provider = OpenAICompatibleCandidateTransport(
        endpoint="https://api.deepseek.com/chat/completions",
        provider="deepseek-official",
        model="deepseek-v4-pro",
        prompt_version="prompt.v1",
        schema_version="schema.v1",
        response_mode="json_object",
        transport=httpx.MockTransport(handler),
    )

    await provider.generate_json(_request())

    body = json.loads(requests[0].content)
    assert body["thinking"] == {"type": "disabled"}
    assert body["stream"] is True
    assert body["temperature"] == 0
    assert "reasoning_effort" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("reasoning_effort", ["low", "high", "max"])
async def test_deepseek_enabled_thinking_uses_supported_effort_without_sampling_controls(
    reasoning_effort: str,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _sse_response(
            request,
            {"candidates": [{"confidence": 0.91}]},
        )

    provider = OpenAICompatibleCandidateTransport(
        endpoint="https://api.deepseek.com/chat/completions",
        provider="deepseek-official",
        model="deepseek-v4-pro",
        prompt_version="prompt.v1",
        schema_version="schema.v1",
        response_mode="json_object",
        thinking="enabled",
        reasoning_effort=reasoning_effort,  # type: ignore[arg-type]
        transport=httpx.MockTransport(handler),
    )

    await provider.generate_json(_request())

    body = json.loads(requests[0].content)
    assert body["thinking"] == {"type": "enabled"}
    assert body["stream"] is True
    assert body["reasoning_effort"] == reasoning_effort
    assert "temperature" not in body
    assert "top_p" not in body


@pytest.mark.asyncio
async def test_deepseek_heartbeats_do_not_keep_request_alive_forever() -> None:
    from ashare_lab.adapters.language.openai_compatible import _read_deepseek_stream
    response = httpx.Response(200, headers={"content-type": "text/event-stream"},
        stream=_DelayedStream((b": keepalive\n\n",) * 10, delay_seconds=0.02))
    with pytest.raises(TimeoutError, match="no semantic progress"):
        await _read_deepseek_stream(response, max_bytes=4096, progress_timeout=0.05)


@pytest.mark.asyncio
async def test_deepseek_progress_deadline_does_not_wait_for_next_wire_chunk() -> None:
    from ashare_lab.adapters.language.openai_compatible import _read_deepseek_stream
    stream = _DelayedStream((b": keepalive\n\n",), delay_seconds=0.5)
    response = httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)
    with pytest.raises(TimeoutError, match="no semantic progress"):
        await _read_deepseek_stream(response, max_bytes=4096, progress_timeout=0.02)
    assert stream.yielded == 0  # timer cancels waiting before any chunk arrives


@pytest.mark.asyncio
async def test_deepseek_stream_uses_real_delta_progress_and_can_outlive_total_timeout() -> None:
    candidate = {"candidates": [{"confidence": 0.91}]}
    private_reasoning = "reasoning-must-never-be-exposed"
    requests: list[httpx.Request] = []
    progress: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        chunks = _sse_completion(candidate, reasoning_content=private_reasoning)
        return _sse_response(
            request,
            candidate,
            stream=_DelayedStream(chunks, delay_seconds=0.06),
        )

    provider = OpenAICompatibleCandidateTransport(
        endpoint="https://api.deepseek.com/chat/completions",
        provider="deepseek",
        model="deepseek-v4-pro",
        prompt_version="prompt.v1",
        schema_version="schema.v1",
        timeout_seconds=0.25,
        response_mode="json_object",
        thinking="enabled",
        reasoning_effort="high",
        transport=httpx.MockTransport(handler),
    )
    token = progress_sink.set(lambda stage, message: progress.append((stage, message)))
    try:
        result = await provider.generate_json(_request())
    finally:
        progress_sink.reset(token)

    assert result == candidate
    assert requests[0].headers["Accept"] == "text/event-stream"
    assert ("model_reasoning", "已收到模型推理流，仍在生成。") in progress
    assert ("model_output", "模型开始返回结果。") in progress
    assert private_reasoning not in repr(progress)


@pytest.mark.asyncio
async def test_deepseek_reasoning_uses_only_opt_in_sink_not_json_or_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    candidate = {"candidates": [{"confidence": 0.91}]}
    deltas = ["unit-only-delta-A\n", "unit-only-delta-B：结束。"]
    received: list[str] = []
    statuses: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response(
            request,
            candidate,
            stream=_CountingStream(
                (
                    _sse_delta(reasoning_content=deltas[0]),
                    _sse_delta(reasoning_content=deltas[1]),
                    _sse_delta(content=json.dumps(candidate)),
                    _sse_delta(finish_reason="stop"),
                    b"data: [DONE]\n\n",
                )
            ),
        )

    provider = OpenAICompatibleCandidateTransport(
        endpoint="https://api.deepseek.com/chat/completions",
        provider="deepseek",
        model="deepseek-v4-pro",
        prompt_version="prompt.v1",
        schema_version="schema.v1",
        response_mode="json_object",
        thinking="enabled",
        reasoning_effort="high",
        transport=httpx.MockTransport(handler),
    )
    reasoning_token = model_reasoning_sink.set(received.append)
    progress_token = progress_sink.set(lambda stage, message: statuses.append((stage, message)))
    try:
        with caplog.at_level("DEBUG"):
            result = await provider.generate_json(_request())
    finally:
        model_reasoning_sink.reset(reasoning_token)
        progress_sink.reset(progress_token)

    assert received == deltas
    assert result == candidate
    assert all(delta.rstrip("\n") not in caplog.text for delta in deltas)
    assert all(delta.rstrip("\n") not in repr(statuses) for delta in deltas)
    assert all(delta.rstrip("\n") not in repr(result) for delta in deltas)
    assert model_reasoning_sink.get() is None
    assert progress_sink.get() is None


@pytest.mark.asyncio
async def test_deepseek_stream_fails_closed_on_truncation_or_semantic_overflow(
    caplog: pytest.LogCaptureFixture,
) -> None:
    candidate = {"candidates": []}
    oversized_reasoning = "private-reasoning-" + "x" * 1_100
    streams = iter(
        (
            _CountingStream(_sse_completion(candidate)[:-1]),
            _CountingStream(_sse_completion(candidate, reasoning_content=oversized_reasoning)),
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response(request, candidate, stream=next(streams))

    provider = OpenAICompatibleCandidateTransport(
        endpoint="https://api.deepseek.com/chat/completions",
        provider="deepseek",
        model="deepseek-v4-pro",
        prompt_version="prompt.v1",
        schema_version="schema.v1",
        timeout_seconds=1,
        max_response_bytes=1_024,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(CandidateProviderTransportError, match="ended before DONE"):
        await provider.generate_json(_request())
    with pytest.raises(CandidateProviderTransportError, match="too large"):
        await provider.generate_json(_request())

    assert oversized_reasoning not in caplog.text


@pytest.mark.asyncio
async def test_non_deepseek_preserves_total_wall_timeout() -> None:
    candidate = {"candidates": []}
    raw = json.dumps(_completion(candidate)).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=_DelayedStream((raw[:20], raw[20:]), delay_seconds=0.15),
            request=request,
        )

    provider = OpenAICompatibleCandidateTransport(
        endpoint="https://gateway.example.test/v1/chat/completions",
        provider="fixture-gateway",
        model="fixture-model",
        prompt_version="prompt.v1",
        schema_version="schema.v1",
        timeout_seconds=0.25,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(CandidateProviderTransportError) as caught:
        await provider.generate_json(_request())

    assert caught.value.timed_out is True


@pytest.mark.parametrize(
    ("provider_name", "thinking", "reasoning_effort", "error"),
    [
        ("deepseek-official", "automatic", None, "thinking mode"),
        ("deepseek-official", "enabled", "medium", "reasoning effort"),
        ("deepseek-official", "enabled", None, "requires a reasoning effort"),
        ("deepseek-official", "disabled", "low", "requires enabled thinking"),
        ("fixture-gateway", "enabled", "high", "only supported for DeepSeek"),
    ],
)
def test_transport_rejects_invalid_thinking_configuration(
    provider_name: str,
    thinking: str,
    reasoning_effort: str | None,
    error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        OpenAICompatibleCandidateTransport(
            endpoint="https://gateway.example.test/v1/chat/completions",
            provider=provider_name,
            model="fixture-model",
            prompt_version="prompt.v1",
            schema_version="schema.v1",
            thinking=thinking,  # type: ignore[arg-type]
            reasoning_effort=reasoning_effort,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_disabled_transport_is_explicit_and_never_attempts_network() -> None:
    provider = DisabledCandidateJsonTransport(
        prompt_version="prompt.v1",
        schema_version="schema.v1",
    )

    with pytest.raises(CandidateProviderTransportError, match="not configured"):
        await provider.generate_json(_request())

    assert provider.identity.provider == "disabled"
    assert provider.identity.model == "unconfigured"


def test_transport_rejects_credentials_or_query_in_endpoint() -> None:
    with pytest.raises(ValueError, match="credentials"):
        OpenAICompatibleCandidateTransport(
            endpoint="https://user:password@gateway.example.test/v1/chat/completions",
            provider="fixture-gateway",
            model="fixture-model",
            prompt_version="prompt.v1",
            schema_version="schema.v1",
        )
    with pytest.raises(ValueError, match="query or fragment"):
        OpenAICompatibleCandidateTransport(
            endpoint="https://gateway.example.test/v1/chat/completions?token=unsafe",
            provider="fixture-gateway",
            model="fixture-model",
            prompt_version="prompt.v1",
            schema_version="schema.v1",
        )


def test_transport_accepts_bounded_long_thinking_timeout() -> None:
    OpenAICompatibleCandidateTransport(
        endpoint="https://api.deepseek.com/chat/completions",
        provider="deepseek",
        model="deepseek-v4-pro",
        prompt_version="prompt.v1",
        schema_version="schema.v1",
        thinking="enabled",
        reasoning_effort="high",
        timeout_seconds=300.0,
    )

    with pytest.raises(ValueError, match=r"between 0\.25 and 300 seconds"):
        OpenAICompatibleCandidateTransport(
            endpoint="https://api.deepseek.com/chat/completions",
            provider="deepseek",
            model="deepseek-v4-pro",
            prompt_version="prompt.v1",
            schema_version="schema.v1",
            thinking="enabled",
            reasoning_effort="high",
            timeout_seconds=300.01,
        )
