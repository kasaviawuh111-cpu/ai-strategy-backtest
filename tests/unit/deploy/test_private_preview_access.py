from __future__ import annotations

import asyncio
import base64

import httpx
import pytest
from starlette.responses import JSONResponse
from starlette.types import Message, Receive, Scope, Send

from deploy.private_preview.access import PreviewAccessConfig, PrivatePreviewAccess

_USERNAME = "preview-guest"
_PASSWORD = "test-only-password-not-a-real-secret"
_ORIGIN = "https://preview.example.test"


def _config(**overrides: object) -> PreviewAccessConfig:
    return PreviewAccessConfig(  # type: ignore[arg-type]
        **{"username": _USERNAME, "password": _PASSWORD, "trusted_origin": _ORIGIN, **overrides}
    )


def _auth(username: str = _USERNAME, password: str = _PASSWORD) -> str:
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {encoded}"


class Recorder:
    def __init__(self) -> None:
        self.calls: list[Scope] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.calls.append(scope)
        if scope["type"] == "http":
            await JSONResponse({"ok": True}, headers={"Cache-Control": "public, max-age=3600"})(
                scope, receive, send
            )


@pytest.mark.parametrize("path", ["/", "/assets/app.js", "/docs", "/api/v1/health", "/progress"])
@pytest.mark.parametrize("authorization", [None, "Basic invalid", _auth(password="wrong")])
@pytest.mark.asyncio
async def test_authentication_protects_every_path_without_entering_application(
    path: str, authorization: str | None
) -> None:
    downstream = Recorder()
    app = PrivatePreviewAccess(downstream, _config())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=_ORIGIN) as client:
        headers = {} if authorization is None else {"Authorization": authorization}
        response = await client.get(path, headers=headers)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Basic ")
    assert response.headers["Cache-Control"] == "no-store"
    assert _USERNAME not in response.text and _PASSWORD not in response.text
    assert downstream.calls == []


@pytest.mark.asyncio
async def test_authenticated_content_is_private_and_credentials_do_not_reach_application() -> None:
    downstream = Recorder()
    app = PrivatePreviewAccess(downstream, _config())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=_ORIGIN) as client:
        response = await client.get("/docs", headers={"Authorization": _auth()})
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert b"authorization" not in dict(downstream.calls[0]["headers"])


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "null"},
        {"Origin": "https://attacker.example", "Host": "attacker.example"},
        {"Origin": _ORIGIN + "/"},
        {"Origin": _ORIGIN, "Sec-Fetch-Site": "cross-site"},
        {"Origin": _ORIGIN, "Sec-Fetch-Site": "same-site"},
    ],
)
@pytest.mark.asyncio
async def test_authenticated_cross_site_or_missing_origin_write_is_rejected(
    headers: dict[str, str],
) -> None:
    downstream = Recorder()
    app = PrivatePreviewAccess(downstream, _config())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=_ORIGIN) as client:
        response = await client.post("/api/v1/strategy-drafts", headers={
            "Authorization": _auth(), **headers,
        })
    assert response.status_code == 403
    assert downstream.calls == []


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.asyncio
async def test_same_origin_write_is_allowed(method: str) -> None:
    downstream = Recorder()
    app = PrivatePreviewAccess(downstream, _config())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=_ORIGIN) as client:
        response = await client.request(method, "/api/action", headers={
            "Authorization": _auth(), "Origin": _ORIGIN, "Sec-Fetch-Site": "same-origin",
        })
    assert response.status_code == 200
    assert len(downstream.calls) == 1


@pytest.mark.asyncio
async def test_duplicate_auth_or_origin_headers_are_rejected() -> None:
    downstream = Recorder()
    app = PrivatePreviewAccess(downstream, _config())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=_ORIGIN) as client:
        duplicate_auth = await client.get("/", headers=[
            ("Authorization", _auth()), ("Authorization", _auth()),
        ])
        duplicate_origin = await client.post("/api/action", headers=[
            ("Authorization", _auth()), ("Origin", _ORIGIN), ("Origin", _ORIGIN),
        ])
    assert duplicate_auth.status_code == 401
    assert duplicate_origin.status_code == 403
    assert downstream.calls == []


@pytest.mark.asyncio
async def test_rate_limit_expires_and_does_not_block_reading_progress() -> None:
    now = [100.0]
    downstream = Recorder()
    app = PrivatePreviewAccess(downstream, _config(writes_per_window=2), clock=lambda: now[0])
    headers = {"Authorization": _auth(), "Origin": _ORIGIN}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=_ORIGIN) as client:
        assert (await client.post("/api/action", headers=headers)).status_code == 200
        assert (await client.post("/api/action", headers=headers)).status_code == 200
        limited = await client.post("/api/action", headers=headers)
        assert limited.status_code == 429 and limited.headers["Retry-After"] == "60"
        assert (await client.get("/progress", headers=headers)).status_code == 200
        now[0] += 60
        assert (await client.post("/api/action", headers=headers)).status_code == 200
    assert len(downstream.calls) == 4


@pytest.mark.asyncio
async def test_inflight_limit_rejects_immediately_then_releases_after_completion() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def delayed(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        await JSONResponse({"ok": True})(scope, receive, send)

    app = PrivatePreviewAccess(delayed, _config(max_concurrent_writes=1))
    headers = {"Authorization": _auth(), "Origin": _ORIGIN}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=_ORIGIN) as client:
        running = asyncio.create_task(client.post("/api/action", headers=headers))
        await asyncio.wait_for(entered.wait(), timeout=1)
        try:
            limited = await asyncio.wait_for(client.post("/api/action", headers=headers), timeout=1)
            assert limited.status_code == 429
            assert calls == 1
        finally:
            release.set()
            assert (await running).status_code == 200
        assert (await client.post("/api/action", headers=headers)).status_code == 200
        assert calls == 2


@pytest.mark.asyncio
async def test_failed_request_releases_concurrency_slot() -> None:
    calls = 0

    async def fail_once(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("test downstream failure")
        await JSONResponse({"ok": True})(scope, receive, send)

    app = PrivatePreviewAccess(fail_once, _config(max_concurrent_writes=1))
    headers = {"Authorization": _auth(), "Origin": _ORIGIN}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=_ORIGIN) as client:
        with pytest.raises(RuntimeError, match="test downstream"):
            await client.post("/api/action", headers=headers)
        assert (await client.post("/api/action", headers=headers)).status_code == 200


@pytest.mark.asyncio
async def test_websocket_is_not_supported_and_lifespan_passes_through() -> None:
    downstream = Recorder()
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def send(message: Message) -> None:
        sent.append(message)

    app = PrivatePreviewAccess(downstream, _config())
    await app({"type": "websocket"}, receive, send)
    assert sent == [{"type": "websocket.close", "code": 1008}]
    assert downstream.calls == []
    await app({"type": "lifespan"}, receive, send)
    assert downstream.calls == [{"type": "lifespan"}]


@pytest.mark.parametrize(
    "overrides",
    [
        {"username": ""}, {"username": "guest:other"}, {"password": "short"},
        {"trusted_origin": ""}, {"trusted_origin": "http://preview.example.test"},
        {"trusted_origin": "https://preview.example.test/path"},
        {"trusted_origin": "https://guest:secret@preview.example.test"},
        {"trusted_origin": "https://preview.example.test:bad"},
        {"max_concurrent_writes": 5}, {"writes_per_window": 1000},
    ],
)
def test_invalid_configuration_fails_before_serving(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _config(**overrides)


def test_environment_requires_all_access_settings_without_exposing_secret() -> None:
    with pytest.raises(ValueError):
        PreviewAccessConfig.from_environment({})
    config = PreviewAccessConfig.from_environment({
        "PREVIEW_USERNAME": _USERNAME, "PREVIEW_PASSWORD": _PASSWORD, "PREVIEW_ORIGIN": _ORIGIN,
    })
    assert config.trusted_origin == _ORIGIN
    assert _PASSWORD not in repr(config) and _USERNAME not in repr(config)


@pytest.mark.asyncio
async def test_explicit_public_mode_works_without_accounts_and_keeps_admission_guards() -> None:
    config = PreviewAccessConfig.from_environment({
        "PREVIEW_ACCESS_MODE": "public", "PREVIEW_ORIGIN": _ORIGIN,
    })
    assert config.username == config.password == ""
    downstream = Recorder()
    app = PrivatePreviewAccess(downstream, config)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=_ORIGIN) as client:
        page = await client.get("/")
        assert page.status_code == 200
        assert "WWW-Authenticate" not in page.headers
        assert page.headers["Cache-Control"] == "no-store"
        result = await client.post("/api/action", headers={"Origin": _ORIGIN})
        assert result.status_code == 200
        assert (await client.post("/api/action")).status_code == 403
        assert (await client.post("/api/action", headers={
            "Origin": "https://another.example",
        })).status_code == 403
        for _ in range(config.writes_per_window - 1):
            allowed = await client.post("/api/action", headers={"Origin": _ORIGIN})
            assert allowed.status_code == 200
        assert (await client.post("/api/action", headers={"Origin": _ORIGIN})).status_code == 429
        assert (await client.get("/progress")).status_code == 200


def test_public_mode_requires_explicit_valid_mode_and_https_origin() -> None:
    for environment in (
        {"PREVIEW_ACCESS_MODE": "pubic", "PREVIEW_ORIGIN": _ORIGIN},
        {"PREVIEW_ACCESS_MODE": "public"},
        {"PREVIEW_ACCESS_MODE": "public", "PREVIEW_ORIGIN": "http://localhost"},
    ):
        with pytest.raises(ValueError):
            PreviewAccessConfig.from_environment(environment)
