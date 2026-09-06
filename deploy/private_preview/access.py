"""Single-process admission boundary for a public or invited HTTPS MVP.

Wrap the complete ASGI application, including static files and progress routes.
Public mode has no accounts. Optional Basic Auth is not multi-user tenancy.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .dialogue_requests import preview_client

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class PreviewAccessConfig:
    username: str = field(repr=False)
    password: str = field(repr=False)
    trusted_origin: str
    max_concurrent_writes: int = 4
    writes_per_window: int = 12
    window_seconds: int = 60
    access_mode: str = "private"

    def __post_init__(self) -> None:
        if self.access_mode not in {"private", "public"}:
            raise ValueError("Preview access mode must be private or public")
        if self.access_mode == "private" and (
            not self.username.strip()
            or len(self.username) > 128
            or ":" in self.username
            or any(ord(char) < 32 for char in self.username)
        ):
            raise ValueError("Preview username is required and must be a valid Basic Auth username")
        if self.access_mode == "private" and (
            not 24 <= len(self.password) <= 1024 or not self.password.strip()
        ):
            raise ValueError("Preview password must contain between 24 and 1024 characters")
        try:
            origin = urlsplit(self.trusted_origin)
            valid_origin = (
                origin.scheme == "https"
                and bool(origin.hostname)
                and origin.username is None
                and origin.password is None
                and origin.path in {"", "/"}
                and not origin.query
                and not origin.fragment
                and origin.port != 0
                and self.trusted_origin.isascii()
                and not any(char.isspace() for char in self.trusted_origin)
            )
        except ValueError:
            raise ValueError("Preview requires one explicit trusted HTTPS origin") from None
        if not valid_origin:
            raise ValueError("Preview requires one explicit trusted HTTPS origin")
        object.__setattr__(self, "trusted_origin", f"https://{origin.netloc.lower()}")
        if not 1 <= self.max_concurrent_writes <= 4:
            raise ValueError("Preview write concurrency must be between 1 and 4")
        if not 1 <= self.writes_per_window <= 30 or not 10 <= self.window_seconds <= 300:
            raise ValueError("Preview requires a bounded short-window write limit")

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> PreviewAccessConfig:
        return cls(
            username=environment.get("PREVIEW_USERNAME", ""),
            password=environment.get("PREVIEW_PASSWORD", ""),
            trusted_origin=environment.get("PREVIEW_ORIGIN", ""),
            access_mode=environment.get("PREVIEW_ACCESS_MODE", "private"),
        )


class PrivatePreviewAccess:
    """Optional Basic Auth, exact-origin writes, and immediate admission limits."""

    def __init__(
        self,
        app: ASGIApp,
        config: PreviewAccessConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.app = app
        self.config = config
        self._clock = clock
        self._lock = threading.Lock()
        self._inflight = 0
        self._starts: deque[tuple[float, str]] = deque(maxlen=config.writes_per_window * 5)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers: dict[bytes, list[bytes]] = {}
        for key, value in scope.get("headers", []):
            headers.setdefault(key.lower(), []).append(value)
        if self.config.access_mode == "private" and not self._authenticated(
            headers.get(b"authorization", [])
        ):
            await self._reject(scope, receive, send, 401, "Authentication required")
            return

        is_write = scope.get("method", "GET").upper() not in _SAFE_METHODS
        if is_write:
            if headers.get(b"origin") != [
                self.config.trusted_origin.encode("ascii")
            ] or headers.get(b"sec-fetch-site", [b"same-origin"]) not in (
                [b"same-origin"],
                [b"none"],
            ):
                await self._reject(scope, receive, send, 403, "Same-origin request required")
                return
            if not self._admit_write(preview_client(scope)):
                await self._reject(scope, receive, send, 429, "Preview request limit reached")
                return

        # Do not allow application logging or downstream error handlers to see credentials.
        child_scope = dict(scope)
        child_scope["headers"] = [
            (key, value)
            for key, value in scope.get("headers", [])
            if key.lower() not in {b"authorization", b"proxy-authorization"}
        ]

        async def private_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != b"cache-control"
                ] + [(b"cache-control", b"no-store")]
            await send(message)

        try:
            await self.app(child_scope, receive, private_send)
        finally:
            if is_write:
                with self._lock:
                    self._inflight -= 1

    def _authenticated(self, values: list[bytes]) -> bool:
        if len(values) != 1 or len(values[0]) > 4096:
            return False
        try:
            scheme, encoded = values[0].split(b" ", 1)
            if scheme.lower() != b"basic":
                return False
            username, password = base64.b64decode(encoded, validate=True).split(b":", 1)
        except (ValueError, binascii.Error):
            return False
        user_matches = hmac.compare_digest(username, self.config.username.encode("utf-8"))
        password_matches = hmac.compare_digest(password, self.config.password.encode("utf-8"))
        return user_matches & password_matches

    def _admit_write(self, client: str = "legacy") -> bool:
        with self._lock:
            now = self._clock()
            while self._starts and self._starts[0][0] <= now - self.config.window_seconds:
                self._starts.popleft()
            if (
                self._inflight >= self.config.max_concurrent_writes
                or len(self._starts) >= self.config.writes_per_window * 5
                or sum(key == client for _, key in self._starts) >= self.config.writes_per_window
            ):
                return False
            self._starts.append((now, client))
            self._inflight += 1
            return True

    async def _reject(
        self, scope: Scope, receive: Receive, send: Send, status: int, detail: str
    ) -> None:
        headers = {"Cache-Control": "no-store"}
        if status == 401:
            headers["WWW-Authenticate"] = 'Basic realm="Private preview", charset="UTF-8"'
        if status == 429:
            headers["Retry-After"] = str(self.config.window_seconds)
        await JSONResponse({"detail": detail}, status_code=status, headers=headers)(
            scope, receive, send
        )
