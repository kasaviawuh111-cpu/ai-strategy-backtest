from __future__ import annotations

import asyncio
from uuid import uuid4

import httpx
import pytest
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from deploy.private_preview.dialogue_requests import PreviewDialogueRequests


@pytest.mark.asyncio
async def test_global_queue_is_bounded_and_errors_release_slots() -> None:
    release = asyncio.Event()
    calls = 0

    async def failing(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal calls
        calls += 1
        await release.wait()
        raise RuntimeError("provider failure")

    app = PreviewDialogueRequests(failing)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Prefer": "respond-async"},
    ) as client:
        responses = []
        for _ in range(11):
            responses.append(
                await client.post(
                    "/api/v1/strategy-drafts", headers={"X-Preview-Client-ID": str(uuid4())}
                )
            )
        assert [r.status_code for r in responses] == [202] * 10 + [429]
        await asyncio.sleep(0)
        assert calls == 2
        release.set()
        await asyncio.gather(*(r.task for r in app.records.values() if r.task))
        assert calls == 10
        assert all(
            r.response is not None and r.response.status_code == 500 for r in app.records.values()
        )
        accepted = await client.post("/api/v1/strategy-drafts")
        assert accepted.status_code == 202
        await asyncio.gather(*(r.task for r in app.records.values() if r.task))


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


@pytest.mark.asyncio
async def test_reports_do_not_block_two_customers_and_queue_executes_once() -> None:
    release = asyncio.Event()
    entered: list[str] = []

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        body = (await receive())["body"].decode()
        entered.append(body)
        await release.wait()
        await JSONResponse({"source": body}, status_code=201)(scope, receive, send)

    app = PreviewDialogueRequests(downstream)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Prefer": "respond-async"},
    ) as client:
        responses = []
        for label, path in [
            ("old-report-1", "/api/v1/backtest-runs/one/review"),
            ("old-report-2", "/api/v1/backtest-runs/two/review"),
            ("customer-a", "/api/v1/strategy-drafts"),
            ("customer-b", "/api/v1/strategy-drafts"),
            ("customer-c", "/api/v1/strategy-drafts"),
        ]:
            responses.append(await client.post(path, content=label))
        assert all(r.status_code == 202 for r in responses)
        await asyncio.sleep(0)
        assert set(entered) == {"old-report-1", "customer-a", "customer-b"}
        assert (await client.get(responses[1].headers["Location"])).json()["status"] == "queued"
        release.set()
        await asyncio.gather(*(r.task for r in app.records.values() if r.task))
        assert len(entered) == len(set(entered)) == 5
        assert [(await client.get(r.headers["Location"])).json()["source"] for r in responses] == [
            "old-report-1",
            "old-report-2",
            "customer-a",
            "customer-b",
            "customer-c",
        ]


@pytest.mark.asyncio
async def test_client_limit_isolated_and_waiting_progress_visible() -> None:
    release = asyncio.Event()

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        await release.wait()
        await JSONResponse({"ok": True})(scope, receive, send)

    app = PreviewDialogueRequests(downstream)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Prefer": "respond-async"},
    ) as client:
        headers = {
            "X-Preview-Client-ID": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "X-Dialogue-Progress-ID": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        }
        for _ in range(3):
            assert (
                await client.post("/api/v1/strategy-drafts", headers=headers)
            ).status_code == 202
        assert (await client.post("/api/v1/strategy-drafts", headers=headers)).status_code == 429
        other = await client.post(
            "/api/v1/strategy-drafts",
            headers={"X-Preview-Client-ID": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"},
        )
        assert other.status_code == 202
        progress = await client.get(
            "/api/v1/dialogue-progress/cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        )
        assert progress.json()["events"][0]["stage"] == "queued"
        release.set()
        await asyncio.gather(*(r.task for r in app.records.values() if r.task))
