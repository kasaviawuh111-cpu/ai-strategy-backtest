"""Bounded ephemeral polling bridge for model calls behind short HTTP gateways."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from time import monotonic
from uuid import uuid4

from starlette.datastructures import Headers
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_PREFIX = "/api/v1/preview-requests/"
_MAX_BYTES = 8 * 1024 * 1024


@dataclass
class _Pending:
    task: asyncio.Task[None] | None = None
    response: Response | None = None
    updated: float = field(default_factory=monotonic)


class PreviewDialogueRequests:
    """Wrap inside whole-site auth; never retry a paid POST on a lost connection."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.records: dict[str, _Pending] = {}

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
        if path.startswith(_PREFIX) and scope.get("method") == "GET":
            key = path.removeprefix(_PREFIX)
            record = self.records.get(key)
            response = (
                (record.response or self._pending(key))
                if record
                else JSONResponse(
                    {"detail": "本次临时请求已过期或服务已重建，请重新提交。"},
                    status_code=410,
                )
            )
            await response(scope, receive, send)
            return
        model_post = (
            path == "/api/v1/strategy-drafts"
            or (
                path.startswith("/api/v1/strategy-drafts/")
                and path.endswith("/clarification-answers")
            )
            or (path.startswith("/api/v1/backtest-runs/") and path.endswith("/review"))
        )
        if (
            scope.get("method") != "POST"
            or not model_post
            or Headers(scope=scope).get("prefer") != "respond-async"
        ):
            await self.app(scope, receive, send)
            return
        active = sum(r.response is None for r in self.records.values())
        if active >= 2:
            await JSONResponse(
                {"detail": "当前正在处理其他分析，请稍后再试。"},
                status_code=429,
                headers={"Retry-After": "5"},
            )(scope, receive, send)
            return
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
        # Reserve before yielding so two arrivals cannot exceed the capacity.
        if sum(r.response is None for r in self.records.values()) >= 2:
            await JSONResponse(
                {"detail": "当前正在处理其他分析，请稍后再试。"}, status_code=429,
                headers={"Retry-After": "5"},
            )(scope, receive, send)
            return
        key = str(uuid4())
        record = self.records[key] = _Pending()
        record.task = asyncio.create_task(self._execute(dict(scope), bytes(body), record))
        await self._pending(key)(scope, receive, send)

    @staticmethod
    def _pending(key: str) -> JSONResponse:
        location = _PREFIX + key
        return JSONResponse(
            {"status": "pending"},
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
