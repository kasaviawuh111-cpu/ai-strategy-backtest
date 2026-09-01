#!/usr/bin/env python3
"""Fail-closed production entrypoint for the CloudBase API container."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_FALSE_VALUES = {"0", "false", "no", "off"}
_STORAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{2,255}$")
_FORBIDDEN_DATABASE_USERS = {
    "cloudbase_admin",
    "cloudbase_postgres",
    "postgres",
    "service_role",
}
_SNAPSHOT_ROOT_VARIABLES = (
    "CHOICE_SNAPSHOT_ROOT",
    "TECHNICAL_SNAPSHOT_ROOT",
    "EVENT_SNAPSHOT_ROOT",
    "COMPOSITE_SNAPSHOT_ROOT",
    "SNAPSHOT_PREPARATION_ROOT",
)


class ProductionConfigurationError(RuntimeError):
    """Raised before application code can open an unsafe production store."""


def validate_runtime_environment(environment: Mapping[str, str]) -> None:
    """Require a durable PostgreSQL store and externally applied migrations."""

    if environment.get("APP_ENV", "").strip().lower() != "production":
        raise ProductionConfigurationError("CloudBase runtime requires APP_ENV=production")
    database_url = environment.get("DATABASE_URL", "").strip()
    if not database_url:
        raise ProductionConfigurationError("DATABASE_URL must be supplied as a platform secret")
    try:
        parsed_url = make_url(database_url)
        backend = parsed_url.get_backend_name()
    except ArgumentError as exc:
        raise ProductionConfigurationError("DATABASE_URL is not a valid SQLAlchemy URL") from exc
    if backend != "postgresql":
        raise ProductionConfigurationError("production DATABASE_URL must use durable PostgreSQL")
    if not parsed_url.username or parsed_url.username.lower() in _FORBIDDEN_DATABASE_USERS:
        raise ProductionConfigurationError(
            "production DATABASE_URL must authenticate a dedicated non-admin runtime role"
        )

    initialize_schema = environment.get("INITIALIZE_SCHEMA", "").strip().lower()
    if initialize_schema not in _FALSE_VALUES:
        raise ProductionConfigurationError(
            "production requires INITIALIZE_SCHEMA=false and an Alembic migration gate"
        )
    revision = environment.get("CODE_REVISION", "").strip()
    if _GIT_SHA.fullmatch(revision) is None:
        raise ProductionConfigurationError("CODE_REVISION must be the exact clean 40-character SHA")
    _validate_snapshot_storage(environment)


def _validate_snapshot_storage(environment: Mapping[str, str]) -> None:
    if environment.get("MARKET_DATA_PROFILE", "").strip() != "on_demand_snapshot":
        raise ProductionConfigurationError(
            "CloudBase production requires the on-demand trusted snapshot registry"
        )
    if environment.get("SNAPSHOT_STORAGE_MODE", "").strip() != "durable_mount":
        raise ProductionConfigurationError(
            "SNAPSHOT_STORAGE_MODE must identify a durable external mount"
        )
    storage_id = environment.get("SNAPSHOT_STORAGE_ID", "").strip()
    if _STORAGE_ID.fullmatch(storage_id) is None:
        raise ProductionConfigurationError("SNAPSHOT_STORAGE_ID is missing or invalid")
    restart_probe_id = environment.get("SNAPSHOT_STORAGE_RESTART_PROBE_ID", "").strip()
    if _STORAGE_ID.fullmatch(restart_probe_id) is None:
        raise ProductionConfigurationError(
            "SNAPSHOT_STORAGE_RESTART_PROBE_ID must come from a cross-restart mount probe"
        )

    marker = _absolute_safe_path(environment, "SNAPSHOT_STORAGE_MARKER")
    if marker.is_symlink() or not marker.is_file():
        raise ProductionConfigurationError("durable snapshot storage marker is missing or unsafe")
    mount_root = marker.parent.resolve()
    if mount_root == Path("/") or _under_ephemeral_runtime_root(mount_root):
        raise ProductionConfigurationError(
            "snapshot storage marker must be on an external mount, not the image or temporary disk"
        )
    try:
        decoded = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProductionConfigurationError("durable snapshot storage marker is unreadable") from exc
    if not isinstance(decoded, dict):
        raise ProductionConfigurationError("durable snapshot storage marker is invalid")
    marker_payload = cast(dict[str, object], decoded)
    if (
        marker_payload.get("schemaVersion") != "ashare-lab.durable-snapshot-store.v1"
        or marker_payload.get("storageId") != storage_id
        or marker_payload.get("restartProbeId") != restart_probe_id
        or marker_payload.get("durable") is not True
    ):
        raise ProductionConfigurationError(
            "durable snapshot storage marker does not match the candidate configuration"
        )

    roots: list[Path] = []
    for variable in _SNAPSHOT_ROOT_VARIABLES:
        root = _absolute_safe_path(environment, variable)
        if root.is_symlink() or not root.is_dir():
            raise ProductionConfigurationError(f"{variable} is missing or unsafe")
        resolved = root.resolve()
        if not resolved.is_relative_to(mount_root) or resolved == mount_root:
            raise ProductionConfigurationError(
                f"{variable} must be a distinct child of the durable snapshot mount"
            )
        if not os.access(resolved, os.R_OK | os.W_OK | os.X_OK):
            raise ProductionConfigurationError(
                f"{variable} is not readable and writable by the runtime role"
            )
        roots.append(resolved)
    if len(set(roots)) != len(roots):
        raise ProductionConfigurationError("snapshot storage roots must be distinct")


def _absolute_safe_path(environment: Mapping[str, str], variable: str) -> Path:
    raw = environment.get(variable, "").strip()
    path = Path(raw).expanduser()
    if not raw or not path.is_absolute() or ".." in path.parts:
        raise ProductionConfigurationError(f"{variable} must be an absolute normalized path")
    return path


def _under_ephemeral_runtime_root(path: Path) -> bool:
    return any(path == root or path.is_relative_to(root) for root in (Path("/app"), Path("/tmp")))


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    validate_runtime_environment(os.environ)
    port = os.environ.get("PORT") or os.environ.get("APP_PORT") or "8000"
    command = (
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
    os.execvp(command[0], command)
    return 1  # pragma: no cover - os.execvp replaces the process


if __name__ == "__main__":
    raise SystemExit(main())
