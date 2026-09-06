import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from ashare_lab.adapters.language.openai_compatible import _read_deepseek_stream
from ashare_lab.api import skill_app
from ashare_lab.api.dialogue_progress import install_dialogue_progress
from ashare_lab.ports.dialogue_progress import model_reasoning_sink, progress_sink
from ashare_lab.settings import AppSettings
from deploy.private_preview import entrypoint
from deploy.private_preview.access import PreviewAccessConfig, PrivatePreviewAccess
from deploy.private_preview.dialogue_requests import PreviewDialogueRequests


@pytest.fixture
def preview(tmp_path: Path) -> tuple[AppSettings, dict[str, str]]:
    tmp_path = tmp_path.resolve()
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>Fixture</title>")
    (dist / "assets").mkdir()
    settings = AppSettings(
        _env_file=None,
        app_env="production",
        market_data_profile="eastmoney_skill",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'ephemeral/ashare.db'}",
        initialize_schema=True,
        queue_backend="thread",
        local_worker_threads=1,
        candidate_provider_mode="openai_compatible",
        candidate_provider_name="deepseek",
        candidate_provider_endpoint="https://api.deepseek.com/v1",
        candidate_provider_model="test-model",
        candidate_provider_api_key="test-not-real",
        plan_deep_provider_mode="inherit_extract_fast",
        mx_saas_api_key="test-not-real",
        web_dist_root=dist,
        code_revision="sha256:" + "a" * 64,
    )
    environment = {
        "APP_ENV": "production",
        "DEPLOYMENT_PROFILE": "private_skill_ephemeral",
        "PERSISTENCE_MODE": "ephemeral",
        "RESTART_RECOVERY_VERIFIED": "false",
        "INITIALIZE_SCHEMA": "true",
        "PREVIEW_STATE_ROOT": str(tmp_path / "ephemeral"),
        "PREVIEW_USERNAME": "guest",
        "PREVIEW_PASSWORD": "test-only-private-preview-password",
        "PREVIEW_ORIGIN": "https://preview.example.test",
    }
    return settings, environment


def test_normal_skill_production_remains_forbidden(
    preview: tuple[AppSettings, dict[str, str]],
) -> None:
    with pytest.raises(ValueError, match="not a public deployment"):
        skill_app.create_skill_app(preview[0])


@pytest.mark.parametrize(
    "update",
    [
        {"APP_ENV": "local"},
        {"APP_ENV": "staging"},
        {"DEPLOYMENT_PROFILE": "private_skill_preview"},
        {"PERSISTENCE_MODE": "persistent"},
        {"RESTART_RECOVERY_VERIFIED": "true"},
        {"INITIALIZE_SCHEMA": "false"},
        {"WEB_CONCURRENCY": "2"},
        {"DATABASE_ADMIN_URL": "not-for-preview"},
    ],
)
def test_explicit_ephemeral_declaration_required(
    preview: tuple[AppSettings, dict[str, str]],
    update: dict[str, str],
) -> None:
    settings, original = preview
    environment = {**original, **update}
    with pytest.raises(entrypoint.PreviewDeploymentError):
        entrypoint._validate_preview_settings(
            settings, environment, PreviewAccessConfig.from_environment(environment)
        )


@pytest.mark.parametrize(
    "update",
    [
        {"initialize_schema": False},
        {"local_worker_threads": 2},
        {"queue_backend": "rq"},
        {"candidate_provider_mode": "disabled"},
        {"mx_saas_api_key": None},
        {"web_dist_root": None},
        {"code_revision": "old-commit+dirty"},
        {"cors_allowed_origins": "https://other.example.test"},
    ],
)
def test_unsafe_settings_rejected(
    preview: tuple[AppSettings, dict[str, str]],
    update: dict[str, object],
) -> None:
    settings, environment = preview
    with pytest.raises(entrypoint.PreviewDeploymentError):
        entrypoint._validate_preview_settings(
            settings.model_copy(update=update),
            environment,
            PreviewAccessConfig.from_environment(environment),
        )


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///:memory:",
        "sqlite:///var/ashare.db",
        "postgresql://db.test/preview",
        "sqlite:///file:preview?mode=memory&uri=true",
    ],
)
def test_only_dedicated_absolute_sqlite_accepted(tmp_path: Path, url: str) -> None:
    with pytest.raises(entrypoint.PreviewDeploymentError):
        entrypoint._validate_ephemeral_database(url, str(tmp_path.resolve() / "ephemeral"))


def test_existing_project_database_and_unrelated_files_rejected(tmp_path: Path) -> None:
    project_db = entrypoint._ROOT / "var/ashare.db"
    with pytest.raises(entrypoint.PreviewDeploymentError):
        entrypoint._validate_ephemeral_database(f"sqlite:///{project_db}", str(project_db.parent))
    tmp_path = tmp_path.resolve()
    (tmp_path / "user.txt").write_text("must not be touched")
    with pytest.raises(entrypoint.PreviewDeploymentError):
        entrypoint._validate_ephemeral_database(
            f"sqlite:///{tmp_path / 'ashare.db'}", str(tmp_path)
        )
    assert (tmp_path / "user.txt").read_text() == "must not be touched"


def test_factory_protects_meta_and_matches_local_reasoning_without_settings_overrides(
    monkeypatch: pytest.MonkeyPatch,
    preview: tuple[AppSettings, dict[str, str]],
) -> None:
    settings, environment = preview
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    calls: list[str] = []

    def load_settings(**kwargs: object) -> AppSettings:
        assert kwargs == {"_env_file": None}
        return settings

    def compose(selected: AppSettings, **kwargs: object) -> FastAPI:
        assert selected is settings and selected.app_env == "production"
        assert kwargs == {"include_model_reasoning": True, "max_pending": 2}
        calls.append("compose")
        return FastAPI()

    monkeypatch.setattr(entrypoint, "AppSettings", load_settings)
    monkeypatch.setattr(entrypoint, "_compose_skill_app", compose)
    protected = entrypoint.create_app()
    assert isinstance(protected, PrivatePreviewAccess)
    with TestClient(protected, base_url=environment["PREVIEW_ORIGIN"]) as client:
        assert client.get("/api/v1/preview-meta").status_code == 401
        assert client.get("/docs").status_code == 401
        response = client.get(
            "/api/v1/preview-meta", auth=("guest", environment["PREVIEW_PASSWORD"])
        )
        assert response.status_code == 200
        assert response.json() == {"persistence": "ephemeral", "revision": settings.code_revision}
    assert calls == ["compose"]


@pytest.mark.asyncio
@pytest.mark.parametrize("has_reasoning", [False, True])
async def test_public_factory_preserves_provider_reasoning_per_async_request(
    monkeypatch: pytest.MonkeyPatch,
    preview: tuple[AppSettings, dict[str, str]],
    caplog: pytest.LogCaptureFixture,
    has_reasoning: bool,
) -> None:
    """Offline SSE fixture through the real public admission and async wrappers."""
    settings, environment = preview
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PREVIEW_ACCESS_MODE", "public")
    monkeypatch.delenv("PREVIEW_USERNAME")
    monkeypatch.delenv("PREVIEW_PASSWORD")
    monkeypatch.setattr(entrypoint, "AppSettings", lambda **kwargs: settings)
    ids = [str(uuid4()), str(uuid4())]
    entered = {key: asyncio.Event() for key in ids}
    release = asyncio.Event()
    fixture_reasoning = {key: f"offline-provider-delta-{index}" for index, key in enumerate(ids)}
    calls: list[str] = []

    def delta(field: str, text: str, finish: str | None = None) -> bytes:
        payload = {"choices": [{"delta": {field: text}, "finish_reason": finish}]}
        return b"data: " + json.dumps(payload).encode() + b"\n\n"

    class ProviderStream(httpx.AsyncByteStream):
        def __init__(self, progress_id: str) -> None:
            self.progress_id = progress_id

        async def __aiter__(self) -> AsyncIterator[bytes]:
            if has_reasoning:
                yield delta("reasoning_content", fixture_reasoning[self.progress_id])
            entered[self.progress_id].set()
            await release.wait()
            yield delta("content", '{"status":"ready"}', "stop")
            yield b"data: [DONE]\n\n"

    def compose(selected: AppSettings, **kwargs: object) -> FastAPI:
        assert selected is settings
        app = FastAPI()

        @app.post("/api/v1/strategy-drafts", status_code=201)
        async def draft(request: Request) -> dict[str, str]:
            progress_id = request.headers["X-Dialogue-Progress-ID"]
            calls.append(progress_id)
            response = httpx.Response(
                200, headers={"Content-Type": "text/event-stream"},
                stream=ProviderStream(progress_id),
            )
            try:
                content, _ = await _read_deepseek_stream(response, max_bytes=4096)
            finally:
                await response.aclose()
            assert content == '{"status":"ready"}'
            return {"status": "ready"}

        install_dialogue_progress(
            app, include_model_reasoning=kwargs["include_model_reasoning"] is True,
        )
        return app

    monkeypatch.setattr(entrypoint, "_compose_skill_app", compose)
    protected = entrypoint.create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(protected), base_url=environment["PREVIEW_ORIGIN"],
    ) as client:
        locations: list[str] = []
        try:
            for key in ids:
                created = await client.post("/api/v1/strategy-drafts", json={}, headers={
                    "Origin": environment["PREVIEW_ORIGIN"], "Prefer": "respond-async",
                    "X-Dialogue-Progress-ID": key,
                })
                assert created.status_code == 202
                locations.append(created.headers["Location"])
            for key in ids:
                await asyncio.wait_for(entered[key].wait(), timeout=2)
                partial = await client.get(f"/api/v1/dialogue-progress/{key}")
                assert partial.headers["Cache-Control"] == "no-store"
                assert partial.json()["finished"] is False
                streams = [
                    event["reasoning"] for event in partial.json()["events"]
                    if "reasoning" in event
                ]
                assert streams == ([fixture_reasoning[key]] if has_reasoning else [])
            assert (await client.get(f"/api/v1/dialogue-progress/{uuid4()}")).status_code == 404
        finally:
            release.set()
            bridge = protected.app
            assert isinstance(bridge, PreviewDialogueRequests)
            await asyncio.gather(*(r.task for r in bridge.records.values() if r.task is not None))
        for key, location in zip(ids, locations, strict=True):
            result = await client.get(location)
            assert result.status_code == 201
            assert result.json() == {"status": "ready"}
            assert (await client.get(f"/api/v1/dialogue-progress/{key}")).json()["finished"] is True
        assert calls == ids
    assert all(text not in caplog.text for text in fixture_reasoning.values())
    assert model_reasoning_sink.get() is None
    assert progress_sink.get() is None


def test_invalid_declaration_prevents_runtime_construction(
    monkeypatch: pytest.MonkeyPatch,
    preview: tuple[AppSettings, dict[str, str]],
) -> None:
    settings, environment = preview
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("RESTART_RECOVERY_VERIFIED", "true")
    monkeypatch.setattr(entrypoint, "AppSettings", lambda **kwargs: settings)
    monkeypatch.setattr(
        entrypoint, "_compose_skill_app", lambda *args, **kwargs: pytest.fail("called")
    )
    with pytest.raises(entrypoint.PreviewDeploymentError):
        entrypoint.create_app()
