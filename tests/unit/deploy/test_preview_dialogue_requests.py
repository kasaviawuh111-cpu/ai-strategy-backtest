from __future__ import annotations

import asyncio
from uuid import uuid4

import httpx
import pytest
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from deploy.private_preview.dialogue_requests import PreviewDialogueRequests


@pytest.mark.asyncio
async def test_same_pending_request_reuses_one_task_and_rejects_changed_content() -> None:
    release = asyncio.Event()
    calls = 0

    async def slow(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal calls
        calls += 1
        await receive()
        await release.wait()
        await JSONResponse({"original": True}, status_code=201)(scope, receive, send)

    app = PreviewDialogueRequests(slow)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test",
        headers={"Prefer": "respond-async", "Idempotency-Key": "logical-request-1",
                 "X-Preview-Client-ID": str(uuid4())},
    ) as client:
        first, repeated = await asyncio.gather(*[
            client.post("/api/v1/strategy-drafts", json={"utterance": "原策略"})
            for _ in range(2)
        ])
        assert first.status_code == repeated.status_code == 202
        assert first.headers["Location"] == repeated.headers["Location"]
        assert len(app.records) == 1
        conflict = await client.post("/api/v1/strategy-drafts", json={"utterance": "新策略"})
        assert conflict.status_code == 409
        app.capacity["strategy"] = 1
        full_queue_replay = await client.post(
            "/api/v1/strategy-drafts", json={"utterance": "原策略"},
        )
        assert full_queue_replay.status_code == 202
        assert len(app.records) == 1
        release.set()
        await asyncio.gather(*(r.task for r in app.records.values() if r.task))
        finished = await client.post("/api/v1/strategy-drafts", json={"utterance": "原策略"})
        assert finished.status_code == 201 and finished.json() == {"original": True}
        assert calls == 1


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
async def test_semantic_timeout_releases_slot_for_next_request() -> None:
    from ashare_lab.adapters.language.openai_compatible import _read_deepseek_stream

    class HeartbeatStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                yield b": keepalive\n\n"
                await asyncio.sleep(0.01)

    calls = 0

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            response = httpx.Response(200, headers={"content-type": "text/event-stream"},
                                      stream=HeartbeatStream())
            try:
                await _read_deepseek_stream(response, max_bytes=4096, progress_timeout=0.03)
            except TimeoutError:
                await JSONResponse({"error": "candidate_provider_timeout"}, status_code=504)(
                    scope, receive, send,
                )
        else:
            await JSONResponse({"next_request": "completed"}, status_code=201)(scope, receive, send)

    app = PreviewDialogueRequests(downstream)
    app.slots["strategy"] = asyncio.Semaphore(1)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test",
                                headers={"Prefer": "respond-async"}) as client:
        first = await client.post("/api/v1/strategy-drafts")
        second = await client.post("/api/v1/strategy-drafts")
        await asyncio.wait_for(asyncio.gather(*(r.task for r in app.records.values() if r.task)), 1)
        assert (await client.get(first.headers["Location"])).status_code == 504
        result = await client.get(second.headers["Location"])
        assert result.status_code == 201
        assert result.json() == {"next_request": "completed"}
        assert calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "/api/v1/strategy-drafts", "/api/v1/strategy-drafts/d/revisions",
    "/api/v1/strategy-drafts/d/revisions/1/clarification-answers",
    "/api/v1/backtest-runs", "/api/v1/backtest-runs/prepare",
    "/api/v1/backtest-runs/r/review",
])
async def test_pending_returns_original_status_and_body_without_reexecuting(path: str) -> None:
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
        # Upstream is deliberately blocked: acceptance must not await model/data work.
        created = await asyncio.wait_for(client.post(
            path, content="example", headers={"Prefer": "respond-async"}
        ), timeout=1)
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
async def test_capacity_errors_and_preflight_returns_real_backtest_202_once() -> None:
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
        # Backtest preparation also uses the existing async transport, but
        # the final 202 is a real run, not another pending preview request.
        run = await client.post("/api/v1/backtest-runs", json={}, headers={
            "X-Preview-Client-ID": str(uuid4()),
        })
        assert run.headers["X-Preview-Pending"] == "1"
        release.set()
        await asyncio.gather(*(r.task for r in app.records.values() if r.task is not None))
        completed = await client.get(run.headers["Location"])
        assert completed.status_code == 202 and completed.json() == {"id": "real-run"}
        assert "X-Preview-Pending" not in completed.headers
        result = await client.get(one.headers["Location"])
        assert result.status_code == 422
        assert result.json()["detail"] == "original validation"
        assert (await client.get("/api/v1/preview-requests/missing")).status_code == 410


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "/api/v1/strategy-drafts/example/revisions", "/api/v1/backtest-runs/prepare",
])
async def test_preflight_keeps_original_failure_without_reposting(path: str) -> None:
    calls = 0

    async def blocked(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal calls
        calls += 1
        await receive()
        await JSONResponse({"detail": "回测起点早于上市日，本次尚未开始回测。"},
                           status_code=422)(scope, receive, send)

    app = PreviewDialogueRequests(blocked)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                 base_url="http://test") as client:
        pending = await client.post(path, json={},
                                    headers={"Prefer": "respond-async"})
        assert pending.headers["X-Preview-Pending"] == "1"
        await asyncio.gather(*(r.task for r in app.records.values() if r.task is not None))
        for _ in range(2):
            result = await client.get(pending.headers["Location"])
            assert result.status_code == 422
            assert "尚未开始回测" in result.json()["detail"]
        assert calls == 1


@pytest.mark.asyncio
async def test_new_card_check_supersedes_only_same_client_read_only_preparation() -> None:
    release = asyncio.Event()

    async def slow(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        await release.wait()
        await JSONResponse({"ready": True})(scope, receive, send)

    app = PreviewDialogueRequests(slow)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test",
                                 headers={"Prefer": "respond-async",
                                          "X-Preview-Client-ID": str(uuid4())}) as client:
        other = await client.post("/api/v1/backtest-runs/prepare", json={},
                                  headers={"X-Preview-Client-ID": str(uuid4())})
        submission = await client.post("/api/v1/backtest-runs", json={})
        checks = []
        for period in range(5, 10):
            checks.append(await client.post("/api/v1/backtest-runs/prepare",
                                            json={"period": period}))
        assert all(item.status_code == 202 for item in checks)
        for replaced in checks[:-1]:
            result = await client.get(replaced.headers["Location"])
            assert result.status_code == 409
            assert result.json()["code"] == "backtest_preparation_superseded"
        assert (await client.get(other.headers["Location"])).status_code == 202
        assert (await client.get(submission.headers["Location"])).status_code == 202
        release.set()
        await asyncio.gather(*(r.task for r in app.records.values() if r.task),
                             return_exceptions=True)
        assert (await client.get(checks[-1].headers["Location"])).status_code == 200


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
        for progress_id in (str(uuid4()), str(uuid4()), headers["X-Dialogue-Progress-ID"]):
            assert (
                await client.post("/api/v1/strategy-drafts", headers={
                    **headers, "X-Dialogue-Progress-ID": progress_id,
                })
            ).status_code == 202
        rejected = await client.post("/api/v1/strategy-drafts", headers={
            **headers, "X-Dialogue-Progress-ID": str(uuid4()),
        })
        assert rejected.status_code == 429
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
