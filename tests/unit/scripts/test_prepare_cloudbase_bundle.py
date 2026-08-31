from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.prepare_cloudbase_bundle import BundleError, prepare_bundle

REPOSITORY = Path(__file__).resolve().parents[3]


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    for name in ("pyproject.toml", "uv.lock", "README.md", "LICENSE"):
        (repository / name).write_text(name, encoding="utf-8")
    for name in ("ashare_lab", "astock_backtest", "catalogs", "contracts"):
        directory = repository / name
        directory.mkdir()
        (directory / "kept.txt").write_text(name, encoding="utf-8")
        (directory / "__pycache__").mkdir()
        (directory / "__pycache__" / "ignored.pyc").write_bytes(b"cache")
    (repository / "scripts").mkdir()
    (repository / "scripts" / "container_healthcheck.py").write_text(
        "print('ok')\n", encoding="utf-8"
    )
    (repository / "deploy" / "cloudbase").mkdir(parents=True)
    (repository / "deploy" / "cloudbase" / "Dockerfile").write_text(
        (
            "FROM scratch\n"
            "ARG CODE_REVISION\n"
            "ARG SNAPSHOT_DIGEST\n"
            "ENV CODE_REVISION=${CODE_REVISION} DATA_ROOT=/data/${SNAPSHOT_DIGEST}\n"
        ),
        encoding="utf-8",
    )

    payload = b"real parquet bytes"
    body: dict[str, object] = {
        "schemaVersion": "ashare-lab.composite-research-snapshot.v2",
        "files": {
            "daily_ohlcv.parquet": {
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        },
    }
    digest = hashlib.sha256(
        json.dumps(body, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    manifest = {**body, "snapshotId": f"composite:{digest}"}
    snapshot = repository / "var" / "snapshots" / "composite" / digest
    snapshot.mkdir(parents=True)
    (snapshot / "daily_ohlcv.parquet").write_bytes(payload)
    (snapshot / "snapshot_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return repository, digest


def test_bundle_contains_only_allowlisted_sources_and_selected_snapshot(tmp_path: Path) -> None:
    repository, digest = _repository(tmp_path)
    (repository / ".env").write_text("SECRET=do-not-copy", encoding="utf-8")
    output = tmp_path / "bundle"

    metadata = prepare_bundle(
        repository=repository,
        output=output,
        snapshot_digest=digest,
        code_revision="a" * 40,
        verify_git=False,
    )

    assert metadata["producerSnapshotId"] == f"composite:{digest}"
    dockerfile = (output / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG CODE_REVISION=" + "a" * 40 in dockerfile
    assert "ARG CODE_REVISION\n" not in dockerfile
    assert f"ARG SNAPSHOT_DIGEST={digest}" in dockerfile
    assert "ARG SNAPSHOT_DIGEST\n" not in dockerfile
    assert (output / "deploy-snapshot" / digest / "daily_ohlcv.parquet").read_bytes() == (
        b"real parquet bytes"
    )
    assert not (output / ".env").exists()
    assert not (output / "ashare_lab" / "__pycache__").exists()


@pytest.mark.parametrize("missing", ["CODE_REVISION", "SNAPSHOT_DIGEST"])
def test_bundle_rejects_dockerfile_without_an_identity_placeholder(
    tmp_path: Path,
    missing: str,
) -> None:
    repository, digest = _repository(tmp_path)
    dockerfile = repository / "deploy" / "cloudbase" / "Dockerfile"
    dockerfile.write_text(
        dockerfile.read_text(encoding="utf-8").replace(f"ARG {missing}\n", ""),
        encoding="utf-8",
    )

    with pytest.raises(BundleError, match=f"one unpinned {missing}"):
        prepare_bundle(
            repository=repository,
            output=tmp_path / "bundle",
            snapshot_digest=digest,
            code_revision="a" * 40,
            verify_git=False,
        )


def test_cloudbase_dockerfile_uses_portable_source_build_contract() -> None:
    dockerfile = (REPOSITORY / "deploy" / "cloudbase" / "Dockerfile").read_text(encoding="utf-8")

    assert "ghcr.io" not in dockerfile
    assert "RUN --mount=" not in dockerfile
    assert "python -m pip install --no-cache-dir uv==0.12.1" in dockerfile
    assert "${PORT:-${APP_PORT:-8000}}" in dockerfile
    assert "DATA_ROOT=/app/var/snapshots/composite/${SNAPSHOT_DIGEST}" in dockerfile
    assert "deploy-snapshot/ /app/var/snapshots/composite/" in dockerfile
    assert (
        "CORS_ALLOWED_ORIGINS="
        "https://59ac3319a9594be59fa3034fcae82a8f.app.workbuddy.link" in dockerfile
    )


def test_bundle_rejects_snapshot_tampering_and_nonempty_output(tmp_path: Path) -> None:
    repository, digest = _repository(tmp_path)
    snapshot = repository / "var" / "snapshots" / "composite" / digest
    (snapshot / "daily_ohlcv.parquet").write_bytes(b"tampered")

    with pytest.raises(BundleError, match="does not match manifest"):
        prepare_bundle(
            repository=repository,
            output=tmp_path / "bundle",
            snapshot_digest=digest,
            code_revision="a" * 40,
            verify_git=False,
        )

    output = tmp_path / "nonempty"
    output.mkdir()
    (output / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(BundleError, match="must not exist or must be empty"):
        prepare_bundle(
            repository=repository,
            output=output,
            snapshot_digest=digest,
            code_revision="a" * 40,
            verify_git=False,
        )
