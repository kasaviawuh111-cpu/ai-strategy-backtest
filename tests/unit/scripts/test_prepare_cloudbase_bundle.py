from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.prepare_cloudbase_bundle import BundleError, prepare_bundle


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
        "FROM scratch\n", encoding="utf-8"
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
    assert (output / "Dockerfile").is_file()
    assert (output / "deploy-snapshot" / "daily_ohlcv.parquet").read_bytes() == (
        b"real parquet bytes"
    )
    assert not (output / ".env").exists()
    assert not (output / "ashare_lab" / "__pycache__").exists()


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
