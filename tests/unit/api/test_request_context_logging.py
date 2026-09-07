from __future__ import annotations

import asyncio
import logging
from datetime import date

import httpx
import pytest
from pydantic import SecretStr
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from ashare_lab.adapters.language.openai_compatible import (
    CandidateProviderTransportError,
    OpenAICompatibleCandidateTransport,
)
from ashare_lab.adapters.language.vibe_candidates import CandidateTransportRequest
from ashare_lab.api.middleware import RequestContextMiddleware
from ashare_lab.ports.request_context import (
    candidate_attempt,
    current_candidate_attempt,
    current_request_id,
)
from deploy.private_preview.dialogue_requests import PreviewDialogueRequests


@pytest.mark.asyncio
async def test_async_polling_preserves_isolated_model_request_and_attempt_ids(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Use the deployed bridge order; polling must retain the POST's model ID."""
    private = "private-prompt-and-api-key-must-not-be-logged"
    entered: list[tuple[str, int]] = []
    release = asyncio.Event()
    both_started = asyncio.Event()

    async def upstream(request: httpx.Request) -> httpx.Response:
        correlation = current_request_id()
        entered.append((correlation, current_candidate_attempt()))
        if len(entered) == 2:
            both_started.set()
        await release.wait()
        assert correlation == current_request_id()
        if correlation == "request-b":
            return httpx.Response(503, text=private, request=request)
        if correlation == "request-c":
            return httpx.Response(200, text=private, request=request)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "{}"}}]}, request=request,
        )

    transport = OpenAICompatibleCandidateTransport(
        endpoint="https://provider.example.test/chat/completions", provider="fixture",
        model="fixture", prompt_version="v1", schema_version="v1",
        api_key=SecretStr(private), transport=httpx.MockTransport(upstream),
    )
    request = CandidateTransportRequest(
        utterance=private, instrument_context="000333.SZ", as_of_date=date(2026, 9, 6),
        max_candidates=1, response_schema={"type": "object"}, system_contract="Fixture only",
        capability_matrix={}, capability_projection_version="fixture",
        capability_projection_hash=f"sha256:{'a' * 64}",
    )

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        attempt = 2 if current_request_id() == "request-b" else 1
        token = candidate_attempt.set(attempt)
        status = 201
        try:
            await transport.generate_json(request)
        except CandidateProviderTransportError:
            status = 502
        finally:
            candidate_attempt.reset(token)
        assert current_candidate_attempt() == 0
        await JSONResponse({"id": current_request_id()}, status_code=status)(scope, receive, send)

    app = PreviewDialogueRequests(RequestContextMiddleware(downstream, max_body_bytes=1024))
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test",
        headers={"Prefer": "respond-async"},
    ) as client:
        responses = [
            await client.post("/api/v1/strategy-drafts", headers={"X-Request-ID": correlation})
            for correlation in ("request-a", "request-b", "request-c")
        ]
        assert all(response.status_code == 202 for response in responses)
        await asyncio.wait_for(both_started.wait(), timeout=1)
        assert set(entered) == {("request-a", 1), ("request-b", 2)}
        assert current_request_id() == "-" and current_candidate_attempt() == 0
        release.set()
        await asyncio.gather(*(record.task for record in app.records.values() if record.task))
        completed = [await client.get(response.headers["Location"]) for response in responses]
    assert [response.status_code for response in completed] == [201, 502, 502]
    assert [response.headers["X-Request-ID"] for response in completed] == [
        "request-a", "request-b", "request-c",
    ]
    assert [response.headers["X-Backend-Request-ID"] for response in completed] == [
        "request-a", "request-b", "request-c",
    ]
    assert entered == [("request-a", 1), ("request-b", 2), ("request-c", 1)]
    for correlation, attempt in entered:
        model_lines = [
            record.getMessage() for record in caplog.records
            if record.name.endswith("openai_compatible")
            and f"request_id={correlation} " in record.getMessage()
        ]
        assert len(model_lines) == 1 and f"attempt={attempt} " in model_lines[0]
        assert f"http_request_completed request_id={correlation} " in caplog.text
    assert private not in caplog.text
    assert current_request_id() == "-" and current_candidate_attempt() == 0


@pytest.mark.asyncio
async def test_request_context_resets_after_errors_and_rejects_unsafe_header() -> None:
    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        assert current_request_id() == scope["state"]["request_id"]
        if scope["path"] == "/crash":
            raise RuntimeError("fixture")
        await JSONResponse({"id": current_request_id()})(scope, receive, send)

    app = RequestContextMiddleware(downstream, max_body_bytes=10)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test",
    ) as client:
        with pytest.raises(RuntimeError, match="fixture"):
            await client.get("/crash", headers={"X-Request-ID": "request-crash"})
        assert current_request_id() == "-"
        rejected = await client.post("/test", content="too-long-body")
        assert rejected.status_code == 413 and current_request_id() == "-"
        unsafe = await client.get("/test", headers={"X-Request-ID": "unsafe injected=true"})
        generated = unsafe.headers["X-Request-ID"]
        assert len(generated) == 32 and generated.isalnum()
        assert unsafe.json()["id"] == generated
        assert unsafe.headers["X-Backend-Request-ID"] == generated
        assert current_request_id() == "-"
