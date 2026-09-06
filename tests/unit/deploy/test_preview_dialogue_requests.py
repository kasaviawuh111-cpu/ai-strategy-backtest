from __future__ import annotations

import asyncio

import httpx
import pytest
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from deploy.private_preview.dialogue_requests import PreviewDialogueRequests


@pytest.mark.asyncio
async def test_pending_returns_original_status_and_body_without_reexecuting() -> None:
    release = asyncio.Event()
    calls = 0

    async def slow(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal calls
        calls += 1
        request = await receive()
        await release.wait()
        await JSONResponse({"body": request["body"].decode()}, status_code=201)(
            scope, receive, send
        )

    app = PreviewDialogueRequests(slow)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/api/v1/strategy-drafts", content="example", headers={"Prefer": "respond-async"}
        )
        assert created.status_code == 202
        location = created.headers["Location"]
        assert (await client.get(location)).headers["X-Preview-Pending"] == "1"
        release.set()
        tasks = [r.task for r in app.records.values() if r.task is not None]
        await asyncio.gather(*tasks)
        for _ in range(2):
            result = await client.get(location)
            assert result.status_code == 201
            assert result.json() == {"body": "example"}
            assert "X-Preview-Pending" not in result.headers
        assert calls == 1


@pytest.mark.asyncio
async def test_capacity_errors_and_real_backtest_202_passthrough() -> None:
    release = asyncio.Event()

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["path"] == "/api/v1/backtest-runs":
            await JSONResponse({"id": "real-run"}, status_code=202)(scope, receive, send)
        else:
            await release.wait()
            await JSONResponse({"detail": "original validation"}, status_code=422)(
                scope, receive, send
            )

    app = PreviewDialogueRequests(downstream)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Prefer": "respond-async"},
    ) as client:
        one = await client.post("/api/v1/strategy-drafts", json={})
        await client.post("/api/v1/strategy-drafts", json={})
        full = await client.post("/api/v1/strategy-drafts", json={})
        assert full.status_code == 429
        run = await client.post("/api/v1/backtest-runs", json={})
        assert run.json() == {"id": "real-run"}
        assert "X-Preview-Pending" not in run.headers
        release.set()
        await asyncio.gather(*(r.task for r in app.records.values() if r.task is not None))
        result = await client.get(one.headers["Location"])
        assert result.status_code == 422
        assert result.json()["detail"] == "original validation"
        assert (await client.get("/api/v1/preview-requests/missing")).status_code == 410
