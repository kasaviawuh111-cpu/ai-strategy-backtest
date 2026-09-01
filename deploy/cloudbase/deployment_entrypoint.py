#!/usr/bin/env python3
"""Select an explicit CloudBase deployment profile without weakening production."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_FALSE_VALUES = {"0", "false", "no", "off"}
_TRUE_VALUES = {"1", "true", "yes", "on"}


class DeploymentProfileError(RuntimeError):
    """Raised before an ambiguous or mislabeled candidate can start."""


def command_for_environment(environment: Mapping[str, str]) -> tuple[str, ...]:
    profile = environment.get("DEPLOYMENT_PROFILE", "").strip().lower()
    if profile == "strict_production":
        return ("python", "deploy/cloudbase/runtime_entrypoint.py")
    if profile != "ephemeral_candidate":
        raise DeploymentProfileError(
            "DEPLOYMENT_PROFILE must be strict_production or ephemeral_candidate"
        )
    _validate_ephemeral_candidate(environment)
    port = environment.get("PORT") or environment.get("APP_PORT") or "8000"
    return (
        "uvicorn",
        "ashare_lab.main:create_app",
        "--factory",
        "--host",
        "0.0.0.0",
        "--port",
        port,
        "--workers",
        "1",
        "--proxy-headers",
    )


def _validate_ephemeral_candidate(environment: Mapping[str, str]) -> None:
    if environment.get("APP_ENV", "").strip().lower() != "staging":
        raise DeploymentProfileError("ephemeral candidate requires APP_ENV=staging")
    database_url = environment.get("DATABASE_URL", "").strip()
    try:
        backend = make_url(database_url).get_backend_name()
    except ArgumentError as exc:
        raise DeploymentProfileError("ephemeral DATABASE_URL is invalid") from exc
    if backend != "sqlite":
        raise DeploymentProfileError("ephemeral candidate requires an explicit SQLite URL")
    if environment.get("INITIALIZE_SCHEMA", "").strip().lower() not in _TRUE_VALUES:
        raise DeploymentProfileError("ephemeral candidate requires INITIALIZE_SCHEMA=true")
    if environment.get("QUEUE_BACKEND", "").strip().lower() != "thread":
        raise DeploymentProfileError("ephemeral candidate requires QUEUE_BACKEND=thread")
    if environment.get("PERSISTENCE_MODE", "").strip().lower() != "ephemeral":
        raise DeploymentProfileError("ephemeral candidate must declare PERSISTENCE_MODE=ephemeral")
    if environment.get("RESTART_RECOVERY_VERIFIED", "").strip().lower() not in _FALSE_VALUES:
        raise DeploymentProfileError(
            "ephemeral candidate must declare RESTART_RECOVERY_VERIFIED=false"
        )
    if environment.get("SNAPSHOT_STORAGE_MODE", "").strip().lower() != "ephemeral_local":
        raise DeploymentProfileError(
            "ephemeral candidate requires SNAPSHOT_STORAGE_MODE=ephemeral_local"
        )
    if environment.get("MARKET_DATA_PROFILE", "").strip() != "on_demand_snapshot":
        raise DeploymentProfileError(
            "ephemeral candidate requires the on-demand trusted snapshot registry"
        )
    if _GIT_SHA.fullmatch(environment.get("CODE_REVISION", "").strip()) is None:
        raise DeploymentProfileError("CODE_REVISION must be the exact clean 40-character SHA")


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    command = command_for_environment(os.environ)
    os.execvp(command[0], command)
    return 1  # pragma: no cover - os.execvp replaces the process


if __name__ == "__main__":
    raise SystemExit(main())
