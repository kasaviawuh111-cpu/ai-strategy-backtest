"""Bounded polling bridge for model calls and real-data preparation."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from time import monotonic
from uuid import UUID, uuid4

from starlette.datastructures import Headers
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_PREFIX = "/api/v1/preview-requests/"
_MAX_BYTES = 8 * 1024 * 1024


def preview_client(scope: Scope) -> str:
    """Anonymous scheduling key, not an account or an authorization boundary."""
    try:
        return str(UUID(Headers(scope=scope).get("x-preview-client-id", "")))
    except ValueError:
        return "legacy"


@dataclass
class _Pending:
    lane: str = "strategy"
    client: str = "legacy"
    progress_id: str | None = None
    request_key: str | None = None
    request_hash: str | None = None
    preparation_only: bool = False
    running: bool = False
    task: asyncio.Task[None] | None = None
    response: Response | None = None
    updated: float = field(default_factory=monotonic)


class PreviewDialogueRequests:
    """Wrap inside whole-site auth; never retry a paid POST on a lost connection."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.records: dict[str, _Pending] = {}
        # Report interpretation must never consume foreground strategy slots.
        self.slots = {"strategy": asyncio.Semaphore(2), "review": asyncio.Semaphore(1)}
        self.capacity = {"strategy": 10, "review": 9}  # running + eight waiting

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":

            async def lifecycle() -> Message:
                event = await receive()
                if event["type"] == "lifespan.shutdown":
                    tasks = [
                        r.task
                        for r in self.records.values()
                        if r.task is not None and not r.task.done()
                    ]
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                return event

            await self.app(scope, lifecycle, send)
            return
        path = str(scope.get("path", ""))
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        self._prune()
        if path.startswith("/api/v1/dialogue-progress/") and scope.get("method") == "GET":
            progress_id = path.rsplit("/", 1)[-1]
            queued = next(
                (
                    r
                    for r in self.records.values()
                    if r.progress_id == progress_id and not r.running and r.response is None
                ),
                None,
            )
            if queued:
                await JSONResponse(
                    {
                        "events": [
                            {
                                "stage": "queued",
                                "message": "请求已受理，正在排队，轮到后会自动继续，无需重复提交。",
                                "elapsed_ms": round((monotonic() - queued.updated) * 1000),
                            }
                        ],
                        "finished": False,
                    }
                )(scope, receive, send)
                return
        if path.startswith(_PREFIX) and scope.get("method") == "GET":
            key = path.removeprefix(_PREFIX)
            record = self.records.get(key)
            response = (
                (record.response or self._pending(key, record))
                if record
                else JSONResponse(
                    {"detail": "本次临时请求已过期或服务已重建，请重新提交。"},
                    status_code=410,
                )
            )
            await response(scope, receive, send)
            return
        slow_post = (
            path == "/api/v1/strategy-drafts"
            or path == "/api/v1/backtest-runs"
            or path == "/api/v1/backtest-runs/prepare"
            or (
                path.startswith("/api/v1/strategy-drafts/")
                and path.endswith(("/clarification-answers", "/revisions"))
            )
            or (path.startswith("/api/v1/backtest-runs/") and path.endswith("/review"))
        )
        if (
            scope.get("method") != "POST"
            or not slow_post
            or Headers(scope=scope).get("prefer") != "respond-async"
        ):
            await self.app(scope, receive, send)
            return
        lane = "review" if path.endswith("/review") else "strategy"
        client = preview_client(scope)
        body = bytearray()
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            body.extend(event.get("body", b""))
            if len(body) > 1024 * 1024:
                response = JSONResponse({"detail": "请求内容过长。"}, status_code=413)
                await response(scope, receive, send)
                return
            if not event.get("more_body", False):
                break
        headers = Headers(scope=scope)
        request_key = headers.get("idempotency-key") or headers.get("x-dialogue-progress-id")
        fingerprint = hashlib.sha256(json.dumps({
            "path": path,
            "query": bytes(scope.get("query_string", b"")).hex(),
            "parent": headers.get("x-conversation-parent-draft-id"),
            "body": bytes(body).hex(),
        }, sort_keys=True).encode()).hexdigest()
        if request_key:
            existing = next(((key, r) for key, r in self.records.items()
                             if r.client == client and r.request_key == request_key), None)
            if existing is not None:
                key, record = existing
                if record.request_hash != fingerprint:
                    await JSONResponse(
                        {"code": "idempotency_key_conflict",
                         "detail": "该请求编号已用于另一条内容，请使用新的请求编号。"},
                        status_code=409,
                    )(scope, receive, send)
                    return
                # Lookup precedes admission: a retry of accepted work does not
                # consume another queue slot, even when the queue is now full.
                await (record.response or self._pending(key, record))(scope, receive, send)
                return
        preparation_only = path == "/api/v1/backtest-runs/prepare"
        if preparation_only and client != "legacy":
            # A card edit replaces its older read-only check. Browser aborts
            # cannot cancel an accepted polling task by themselves. Never
            # cancel actual submissions, saved revisions, or model turns here.
            for old in self.records.values():
                if old.client == client and old.preparation_only and old.response is None:
                    old.response = JSONResponse(
                        {"code": "backtest_preparation_superseded",
                         "detail": "参数已更新，已停止旧参数检查，正在检查最新参数。"},
                        status_code=409,
                    )
                    old.updated = monotonic()
                    if old.task is not None:
                        old.task.cancel()
        # Reserve before yielding so two arrivals cannot exceed the capacity.
        active = [r for r in self.records.values() if r.response is None and r.lane == lane]
        if len(active) >= self.capacity[lane] or sum(r.client == client for r in active) >= 3:
            await JSONResponse(
                {"detail": "当前等待队列已满，本次请求尚未受理。请等待已有请求完成后再试。"},
                status_code=429,
                headers={"Retry-After": "5"},
            )(scope, receive, send)
            return
        key = str(uuid4())
        record = self.records[key] = _Pending(
            lane=lane, client=client, progress_id=headers.get("x-dialogue-progress-id"),
            request_key=request_key, request_hash=fingerprint,
            preparation_only=preparation_only,
        )
        record.task = asyncio.create_task(self._execute(dict(scope), bytes(body), record))
        await self._pending(key, record)(scope, receive, send)

    @staticmethod
    def _pending(key: str, record: _Pending) -> JSONResponse:
        location = _PREFIX + key
        return JSONResponse(
            {"status": "running" if record.running else "queued"},
            status_code=202,
            headers={
                "Location": location,
                "X-Preview-Pending": "1",
                "Retry-After": "1",
            },
        )

    def _prune(self) -> None:
        for key, record in tuple(self.records.items()):
            if record.response is not None and monotonic() - record.updated > 600:
                del self.records[key]
        finished = [key for key, record in self.records.items() if record.response is not None]
        for key in finished[:-30]:
            del self.records[key]

    async def _execute(self, scope: Scope, body: bytes, record: _Pending) -> None:
        first = True
        status = 500
        headers: list[tuple[bytes, bytes]] = []
        result = bytearray()

        async def receive() -> Message:
            nonlocal first
            if first:
                first = False
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.Event().wait()
            return {"type": "http.disconnect"}

        async def send(message: Message) -> None:
            nonlocal status, headers
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = message.get("headers", [])
            elif message["type"] == "http.response.body":
                result.extend(message.get("body", b""))
                if len(result) > _MAX_BYTES:
                    raise ValueError("preview response exceeds bounded storage")

        try:
            async with asyncio.timeout(600):
                async with self.slots[record.lane]:
                    record.running = True
                    await self.app(scope, receive, send)
            response = Response(bytes(result), status_code=status)
            response.raw_headers = [
                (key, value)
                for key, value in headers
                if key.lower() not in {b"transfer-encoding", b"connection"}
            ]
            record.response = response
        except TimeoutError:
            record.response = JSONResponse(
                {"detail": "本次分析等待时间过长，尚未完成，请稍后重试。"},
                status_code=504,
            )
        except Exception:
            record.response = JSONResponse(
                {"detail": "本次分析未完成，原有策略和报告已保留。"},
                status_code=500,
            )
        finally:
            record.updated = monotonic()
