from __future__ import annotations

import json
from datetime import date

import httpx
import pytest
from pydantic import SecretStr

from ashare_lab.adapters.language.openai_compatible import (
    CandidateProviderTransportError,
    DisabledCandidateJsonTransport,
    OpenAICompatibleCandidateTransport,
)
from ashare_lab.adapters.language.vibe_candidates import CandidateTransportRequest


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
            json={"choices": [{"message": {"content": "```json\n{}\n```"}}]},
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
    assert json.loads(requests[0].content)["response_format"] == {"type": "json_object"}


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
    common = {
        "provider": "fixture-gateway",
        "model": "fixture-model",
        "prompt_version": "prompt.v1",
        "schema_version": "schema.v1",
    }

    with pytest.raises(ValueError, match="credentials"):
        OpenAICompatibleCandidateTransport(
            endpoint="https://user:password@gateway.example.test/v1/chat/completions",
            **common,
        )
    with pytest.raises(ValueError, match="query or fragment"):
        OpenAICompatibleCandidateTransport(
            endpoint="https://gateway.example.test/v1/chat/completions?token=unsafe",
            **common,
        )
