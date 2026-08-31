from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, cast

import pytest

from scripts.prepare_cloudbase_bundle import prepare_bundle

REPOSITORY = Path(__file__).resolve().parents[2]


@pytest.mark.integration
def test_generated_cloudbase_bundle_reaches_strict_ready(tmp_path: Path) -> None:
    digest = os.environ.get("ASHARE_CLOUDBASE_TEST_SNAPSHOT_DIGEST")
    if digest is None:
        pytest.skip("set ASHARE_CLOUDBASE_TEST_SNAPSHOT_DIGEST for the release bundle gate")
    source_snapshot = REPOSITORY / "var" / "snapshots" / "composite" / digest
    if not source_snapshot.is_dir():
        pytest.fail(f"release snapshot is missing: {source_snapshot}")

    revision = "a" * 40
    bundle = tmp_path / "bundle"
    prepare_bundle(
        repository=REPOSITORY,
        output=bundle,
        snapshot_digest=digest,
        code_revision=revision,
        verify_git=False,
    )
    deployed_snapshot = bundle / "deploy-snapshot" / digest
    assert deployed_snapshot.name == digest
    assert (deployed_snapshot / "snapshot_manifest.json").is_file()

    port = _free_port()
    environment = os.environ.copy()
    environment.update(
        {
            "APP_ENV": "production",
            "APP_HOST": "127.0.0.1",
            "APP_PORT": str(port),
            "PORT": str(port),
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'runs.db'}",
            "INITIALIZE_SCHEMA": "true",
            "QUEUE_BACKEND": "thread",
            "LOCAL_WORKER_THREADS": "1",
            "DATA_ROOT": str(deployed_snapshot),
            "MARKET_DATA_PROFILE": "composite_snapshot",
            "SESSION_REFERENCE_MODE": "parquet",
            "SESSION_REFERENCE_PATH": str(deployed_snapshot / "instrument_sessions.parquet"),
            "EVENT_DATA_REQUIRED": "true",
            "ARTIFACT_ROOT": str(tmp_path / "artifacts"),
            "CATALOG_ROOT": str(bundle / "catalogs"),
            "CODE_REVISION": revision,
            "CORS_ALLOWED_ORIGINS": ("https://59ac3319a9594be59fa3034fcae82a8f.app.workbuddy.link"),
            "PYTHONPATH": str(bundle),
        }
    )
    process = subprocess.Popen(
        (
            sys.executable,
            "-m",
            "uvicorn",
            "ashare_lab.main:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workers",
            "1",
        ),
        cwd=bundle,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        ready = _wait_for_ready(process, port)
        assert ready["status"] == "ready"
        checks = cast(dict[str, str], ready["checks"])
        assert checks
        assert set(checks.values()) == {"ok"}
        health = subprocess.run(
            (sys.executable, "scripts/container_healthcheck.py", "api"),
            cwd=bundle,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert health.returncode == 0, health.stderr
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return cast(int, listener.getsockname()[1])


def _wait_for_ready(process: subprocess.Popen[str], port: int) -> dict[str, Any]:
    url = f"http://127.0.0.1:{port}/api/v1/ready"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = "" if process.stdout is None else process.stdout.read()
            pytest.fail(f"bundle API exited before readiness:\n{output}")
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    decoded = json.loads(response.read())
                    if isinstance(decoded, dict):
                        return cast(dict[str, Any], decoded)
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.1)
    output = "" if process.stdout is None else process.stdout.read()
    pytest.fail(f"bundle API did not become ready:\n{output}")
