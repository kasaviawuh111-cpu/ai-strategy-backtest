"""Explicitly ephemeral Skill-backed MVP with bounded public or invited access."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

import uvicorn
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from starlette.types import ASGIApp

from ashare_lab.api.skill_app import _compose_skill_app  # pyright: ignore[reportPrivateUsage]
from ashare_lab.api.web_hosting import validate_web_dist_root
from ashare_lab.settings import AppSettings

from .access import PreviewAccessConfig, PrivatePreviewAccess
from .dialogue_requests import PreviewDialogueRequests

_ROOT = Path(__file__).resolve().parents[2]


class PreviewDeploymentError(RuntimeError):
    """Startup rejected when the explicit MVP deployment contract is incomplete."""


def create_app() -> ASGIApp:
    """Wrap the whole site in admission controls, with no local environment fallback."""
    environment = os.environ
    access = PreviewAccessConfig.from_environment(environment)
    settings = AppSettings(_env_file=None)  # pyright: ignore[reportCallIssue]
    _validate_preview_settings(settings, environment, access)
    # Match the local Skill entrypoint: expose only provider-emitted reasoning
    # through the existing request-scoped progress stream, without another call.
    app = _compose_skill_app(settings, include_model_reasoning=True, max_pending=2)

    async def preview_meta() -> dict[str, object]:
        return {"persistence": "ephemeral", "revision": settings.code_revision}

    app.add_api_route("/api/v1/preview-meta", preview_meta, methods=["GET"])
    return PrivatePreviewAccess(PreviewDialogueRequests(app), access)


def _validate_preview_settings(
    settings: AppSettings,
    environment: Mapping[str, str],
    access: PreviewAccessConfig,
) -> None:
    if environment.get("DEPLOYMENT_PROFILE") != "private_skill_ephemeral":
        raise PreviewDeploymentError("DEPLOYMENT_PROFILE must select private_skill_ephemeral")
    if environment.get("APP_ENV") != "production" or not settings.is_production:
        raise PreviewDeploymentError("Private preview requires APP_ENV=production")
    if (
        environment.get("PERSISTENCE_MODE") != "ephemeral"
        or environment.get("RESTART_RECOVERY_VERIFIED") != "false"
        or environment.get("INITIALIZE_SCHEMA") != "true"
        or not settings.initialize_schema
    ):
        raise PreviewDeploymentError(
            "Explicit ephemeral persistence and initialization are required"
        )
    if settings.market_data_profile != "eastmoney_skill" or settings.strategy_v2_enabled:
        raise PreviewDeploymentError("Private preview requires only the eastmoney_skill runtime")
    if settings.queue_backend != "thread" or settings.local_worker_threads != 1:
        raise PreviewDeploymentError("Private preview requires one bounded in-process worker")
    if any(environment.get(key, "1") != "1" for key in ("WEB_CONCURRENCY", "UVICORN_WORKERS")):
        raise PreviewDeploymentError("Private preview requires one ASGI worker")
    _validate_ephemeral_database(settings.database_url, environment.get("PREVIEW_STATE_ROOT", ""))
    if environment.get("DATABASE_ADMIN_URL"):
        raise PreviewDeploymentError("Admin database credentials do not belong in this preview")
    if settings.mx_saas_api_key is None or not settings.mx_saas_api_key.get_secret_value().strip():
        raise PreviewDeploymentError(
            "Private preview requires an explicitly injected MX credential"
        )
    if (
        settings.candidate_provider_mode != "openai_compatible"
        or settings.candidate_provider_name != "deepseek"
        or settings.plan_deep_provider_mode == "disabled"
        or (
            settings.plan_deep_provider_mode == "openai_compatible"
            and settings.plan_deep_provider_name != "deepseek"
        )
    ):
        raise PreviewDeploymentError("Private preview requires real DeepSeek model profiles")
    endpoints = [settings.candidate_provider_endpoint]
    if settings.plan_deep_provider_mode == "openai_compatible":
        endpoints.append(settings.plan_deep_provider_endpoint)
    if any(endpoint is None or endpoint.scheme != "https" for endpoint in endpoints):
        raise PreviewDeploymentError("Private preview model endpoints require HTTPS")
    if settings.cors_origins not in {(), (access.trusted_origin,)}:
        raise PreviewDeploymentError("Private preview permits only its own browser origin")
    if re.fullmatch(r"(?:[0-9a-f]{40}|sha256:[0-9a-f]{64})", settings.code_revision) is None:
        raise PreviewDeploymentError(
            "Release requires a Git SHA or labeled workspace manifest digest"
        )
    if settings.web_dist_root is None:
        raise PreviewDeploymentError("Private preview requires the built same-origin frontend")
    validate_web_dist_root(_ROOT / settings.web_dist_root)


def _validate_ephemeral_database(database_url: str, configured_root: str) -> None:
    """No in-memory DB, local project DB, URI tricks, symlinks, or arbitrary database file."""
    state_root = Path(configured_root)
    if not configured_root or not state_root.is_absolute():
        raise PreviewDeploymentError("PREVIEW_STATE_ROOT must be a dedicated absolute directory")
    if any(part.is_symlink() for part in (state_root, *state_root.parents)):
        raise PreviewDeploymentError("PREVIEW_STATE_ROOT cannot contain symbolic links")
    root = state_root.resolve()
    if root in {Path("/"), Path("/app"), Path("/tmp"), Path("/private/tmp"), Path.home(), _ROOT}:
        raise PreviewDeploymentError("PREVIEW_STATE_ROOT must not be a shared root directory")
    expected = root / "ashare.db"
    if expected == (_ROOT / "var/ashare.db").resolve() or expected.is_symlink():
        raise PreviewDeploymentError("Private preview must not use the existing project database")
    try:
        database = make_url(database_url)
    except ArgumentError:
        raise PreviewDeploymentError("Private preview requires its dedicated SQLite URL") from None
    if (
        database.drivername not in {"sqlite", "sqlite+pysqlite"}
        or database.database != str(expected)
        or database.query
        or database.host
        or database.username
        or database.password
    ):
        raise PreviewDeploymentError("SQLite must use PREVIEW_STATE_ROOT/ashare.db exactly")
    if root.exists():
        permitted = {"ashare.db", "ashare.db-wal", "ashare.db-shm", "ashare.db-journal"}
        if not root.is_dir() or any(item.name not in permitted for item in root.iterdir()):
            raise PreviewDeploymentError("PREVIEW_STATE_ROOT contains unrelated files")


def main() -> None:
    """Use this fixed launcher, not a second bare API or multi-process runner."""
    port = int(os.environ.get("PORT", os.environ.get("APP_PORT", "8000")))
    if not 1 <= port <= 65535:
        raise PreviewDeploymentError("Private preview port is invalid")
    uvicorn.run(
        "deploy.private_preview.entrypoint:create_app",
        factory=True,
        host="0.0.0.0",
        port=port,
        workers=1,
        reload=False,
        access_log=False,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
