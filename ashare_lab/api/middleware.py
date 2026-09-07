"""Small ASGI middleware for request correlation and bounded request bodies."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from time import perf_counter
from typing import cast
from uuid import uuid4

from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ashare_lab.ports.request_context import request_id

from .schemas import ErrorBody, ErrorEnvelope

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})
type HeaderList = list[tuple[bytes, bytes]]

# Uvicorn configures this logger for container stdout.  Do not use
# ``uvicorn.access`` here: its formatter interprets positional arguments as a
# socket access tuple and would discard our field labels.
_ACCESS_LOG = logging.getLogger("uvicorn.error")


class RequestContextMiddleware:
    """Attach a request ID and reject bodies above the configured byte limit."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int,
        path_max_body_bytes: Mapping[str, int] | None = None,
    ) -> None:
        if max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be positive")
        selected_path_limits = dict(path_max_body_bytes or {})
        for path, maximum in selected_path_limits.items():
            if not path.startswith("/"):
                raise ValueError("path-specific body limits require absolute paths")
            if maximum <= 0:
                raise ValueError("path-specific max body bytes must be positive")
        self._app = app
        self._max_body_bytes = max_body_bytes
        self._path_max_body_bytes = selected_path_limits

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        correlation_id = _read_request_id(scope) or uuid4().hex
        maximum = self._path_max_body_bytes.get(
            str(scope.get("path", "")),
            self._max_body_bytes,
        )
        state = scope.setdefault("state", {})
        state["request_id"] = correlation_id
        request_token = request_id.set(correlation_id)
        started_at = perf_counter()
        status_code = 500

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = correlation_id
                # Gateways may replace their standard X-Request-ID. Keep the
                # application log key independently, including async readback.
                headers["X-Backend-Request-ID"] = correlation_id
            await send(message)

        try:
            declared_length = _content_length(scope)
            if declared_length is None and _has_invalid_content_length(scope):
                await _send_error(
                    scope,
                    receive,
                    send_with_request_id,
                    status_code=400,
                    code="invalid_content_length",
                    message="Content-Length must be a non-negative integer",
                    request_id_value=correlation_id,
                )
                return
            if declared_length is not None and declared_length > maximum:
                await self._send_too_large(
                    scope,
                    receive,
                    send_with_request_id,
                    correlation_id,
                    maximum=maximum,
                )
                return

            bounded_receive = receive
            if scope.get("method") in _BODY_METHODS:
                messages, too_large = await _buffer_messages(receive, maximum)
                if too_large:
                    await self._send_too_large(
                        scope,
                        receive,
                        send_with_request_id,
                        correlation_id,
                        maximum=maximum,
                    )
                    return
                bounded_receive = _replay(messages)

            await self._app(scope, bounded_receive, send_with_request_id)
        finally:
            request_id.reset(request_token)
            duration_ms = (perf_counter() - started_at) * 1000
            _ACCESS_LOG.info(
                "http_request_completed request_id=%s method=%s path=%s status=%d duration_ms=%.3f",
                correlation_id,
                scope.get("method", "UNKNOWN"),
                scope.get("path", ""),
                status_code,
                duration_ms,
            )

    async def _send_too_large(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        correlation_id: str,
        *,
        maximum: int,
    ) -> None:
        await _send_error(
            scope,
            receive,
            send,
            status_code=413,
            code="request_body_too_large",
            message=f"Request body exceeds {maximum} bytes",
            request_id_value=correlation_id,
        )


def _read_request_id(scope: Scope) -> str | None:
    value = _header_value(scope, b"x-request-id")
    if value is None:
        return None
    try:
        decoded = value.decode("ascii")
    except UnicodeDecodeError:
        return None
    return decoded if _REQUEST_ID_RE.fullmatch(decoded) else None


def _content_length(scope: Scope) -> int | None:
    raw = _header_value(scope, b"content-length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _has_invalid_content_length(scope: Scope) -> bool:
    raw = _header_value(scope, b"content-length")
    return raw is not None and _content_length(scope) is None


def _header_value(scope: Scope, name: bytes) -> bytes | None:
    headers = cast(HeaderList, scope.get("headers", []))
    return next((value for key, value in headers if key.lower() == name), None)


async def _buffer_messages(receive: Receive, maximum: int) -> tuple[list[Message], bool]:
    messages: list[Message] = []
    total = 0
    while True:
        message = await receive()
        messages.append(message)
        if message["type"] != "http.request":
            return messages, False
        total += len(message.get("body", b""))
        if total > maximum:
            return messages, True
        if not message.get("more_body", False):
            return messages, False


def _replay(messages: list[Message]) -> Receive:
    remaining = iter(messages)

    async def receive() -> Message:
        return next(
            remaining,
            {"type": "http.request", "body": b"", "more_body": False},
        )

    return receive


async def _send_error(
    scope: Scope,
    receive: Receive,
    send: Send,
    *,
    status_code: int,
    code: str,
    message: str,
    request_id_value: str,
) -> None:
    payload = ErrorEnvelope(
        error=ErrorBody(code=code, message=message),
        request_id=request_id_value,
    )
    response = JSONResponse(status_code=status_code, content=payload.model_dump(mode="json"))
    await response(scope, receive, send)
