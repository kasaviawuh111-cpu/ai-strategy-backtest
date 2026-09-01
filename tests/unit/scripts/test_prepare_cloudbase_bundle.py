from __future__ import annotations

import hashlib
import json
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest

from ashare_lab.adapters.event_sources import (
    EventCollectionRequest,
    EventCollectionResult,
    EventSourceCollection,
    build_event_acquisition_coverage,
)
from ashare_lab.adapters.market_data import compose_choice_event_snapshot
from ashare_lab.adapters.market_data.event_snapshot import build_event_snapshot
from ashare_lab.domain.shared import InstrumentId
from scripts.prepare_cloudbase_bundle import BundleError, prepare_bundle
from tests.unit.adapters.event_sources.query_evidence import eastmoney_query_evidence
from tests.unit.adapters.session_reference_fixture import choice_snapshot_fixture

REPOSITORY = Path(__file__).resolve().parents[3]
SHANGHAI = ZoneInfo("Asia/Shanghai")
EVENT_CODE = "event.financial_results.annual_report"


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    for name in ("pyproject.toml", "uv.lock", "README.md", "LICENSE", "alembic.ini"):
        (repository / name).write_text(name, encoding="utf-8")
    for name in ("ashare_lab", "astock_backtest", "catalogs", "contracts", "alembic"):
        directory = repository / name
        directory.mkdir()
        (directory / "kept.txt").write_text(name, encoding="utf-8")
        (directory / "__pycache__").mkdir()
        (directory / "__pycache__" / "ignored.pyc").write_bytes(b"cache")
    (repository / "scripts").mkdir()
    for name in (
        "container_healthcheck.py",
        "prepare_baostock_reference.py",
        "prepare_baostock_snapshot.py",
        "prepare_choice_snapshot.py",
        "prepare_eastmoney_corporate_actions.py",
        "prepare_eastmoney_snapshot.py",
        "prepare_event_snapshot.py",
    ):
        (repository / "scripts" / name).write_text("print('ok')\n", encoding="utf-8")
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
    for name in (
        "deployment_entrypoint.py",
        "runtime_entrypoint.py",
        "production_db.py",
        "tc3_database_release.py",
        "README.md",
        "env.example",
        "ephemeral.env.example",
    ):
        (repository / "deploy" / "cloudbase" / name).write_text(name, encoding="utf-8")
    web_dist = repository / "web" / "dist"
    (web_dist / "assets").mkdir(parents=True)
    (web_dist / "index.html").write_text(
        '<!doctype html><script type="module" src="/assets/index-test.js"></script>\n',
        encoding="utf-8",
    )
    (web_dist / "assets" / "index-test.js").write_text(
        "fetch('/api/v1/ready')\n",
        encoding="utf-8",
    )
    (web_dist / "assets" / "index-test.css").write_text(
        ":root{color:#111}\n",
        encoding="utf-8",
    )
    (repository / "web" / "src").mkdir()
    (repository / "web" / "src" / "do-not-copy.ts").write_text(
        "throw new Error('source must not ship')\n",
        encoding="utf-8",
    )
    (repository / "web" / "node_modules").mkdir()
    (repository / "web" / "node_modules" / "do-not-copy").write_text(
        "dependency cache",
        encoding="utf-8",
    )

    instrument = InstrumentId("300059.SZ")
    start = date(2025, 1, 2)
    end = date(2025, 1, 3)
    choice = choice_snapshot_fixture(repository / "source-fixtures" / "choice")
    source = EventSourceCollection(
        provider="eastmoney",
        status="empty",
        observations=(),
        acquisition_evidence=eastmoney_query_evidence(
            instrument_id=instrument,
            start=start,
            end=end,
            event_codes=(EVENT_CODE,),
            total_hits=0,
        ),
    )
    request = EventCollectionRequest(
        instrument_id=instrument,
        start=start,
        end=end,
        retrieved_at=datetime(2025, 1, 4, 8, 0, tzinfo=SHANGHAI),
    )
    coverage = build_event_acquisition_coverage(
        request,
        EventCollectionResult(observations=(), sources=(source,)),
        requested_event_codes=(EVENT_CODE,),
    )
    events = build_event_snapshot(
        observations=(),
        acquisition_coverage=coverage,
        output_root=repository / "source-fixtures" / "events",
        captured_at=datetime(2025, 1, 4, 9, 0, tzinfo=SHANGHAI),
    )
    composite = compose_choice_event_snapshot(
        choice_snapshot_path=choice.path,
        event_snapshot_path=events.path,
        output_root=repository / "var" / "snapshots" / "composite",
        composed_at=datetime(2025, 1, 4, 10, 0, tzinfo=SHANGHAI),
    )
    return repository, composite.path.name


def _republish_with_semantically_invalid_source_evidence(
    repository: Path,
    digest: str,
) -> str:
    source = repository / "var" / "snapshots" / "composite" / digest
    manifest = cast(
        dict[str, Any],
        json.loads((source / "snapshot_manifest.json").read_text(encoding="utf-8")),
    )
    coverage = cast(dict[str, Any], manifest["eventAcquisitionCoverage"])
    providers = cast(dict[str, Any], coverage["providerQueryEvidence"])
    eastmoney = cast(dict[str, Any], providers["eastmoney"])
    lanes = cast(dict[str, Any], eastmoney["eventCodeCoverage"])
    annual = cast(dict[str, Any], lanes[EVENT_CODE])
    annual.update(
        {
            "querySucceeded": False,
            "coverageBasis": "deterministic_title_rule_no_semantic_recall_proof",
            "providerColumnCodes": [],
        }
    )
    eastmoney.pop("evidenceSha256", None)
    eastmoney["evidenceSha256"] = _canonical_sha256(eastmoney)
    manifest.pop("snapshotId", None)
    invalid_digest = _canonical_sha256(manifest)
    manifest["snapshotId"] = f"composite:{invalid_digest}"
    destination = source.parent / invalid_digest
    shutil.copytree(source, destination)
    (destination / "snapshot_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return invalid_digest


def _republish_with_new_composed_at(repository: Path, digest: str) -> str:
    source = repository / "var" / "snapshots" / "composite" / digest
    manifest = cast(
        dict[str, Any],
        json.loads((source / "snapshot_manifest.json").read_text(encoding="utf-8")),
    )
    manifest["composedAt"] = "2025-01-04T10:00:01+08:00"
    manifest.pop("snapshotId", None)
    new_digest = _canonical_sha256(manifest)
    manifest["snapshotId"] = f"composite:{new_digest}"
    destination = source.parent / new_digest
    shutil.copytree(source, destination)
    (destination / "snapshot_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return new_digest


def test_bundle_contains_only_allowlisted_sources_and_selected_snapshot(tmp_path: Path) -> None:
    repository, digest = _repository(tmp_path)
    (repository / ".env").write_text("SECRET=do-not-copy", encoding="utf-8")
    (repository / "EmQuantAPI.py").write_text("SECRET = 'do-not-copy'", encoding="utf-8")
    output = tmp_path / "bundle"

    metadata = prepare_bundle(
        repository=repository,
        output=output,
        snapshot_digest=digest,
        code_revision="a" * 40,
        verify_git=False,
    )

    assert metadata["producerSnapshotId"] == f"composite:{digest}"
    assert metadata["bundleSchemaVersion"] == "ashare-lab.cloudbase-source-bundle.v3"
    assert metadata["deploymentProfiles"] == {
        "ephemeral_candidate": {
            "persistence": "ephemeral",
            "restartRecoveryVerified": False,
        },
        "strict_production": {
            "persistence": "postgresql_and_durable_mount_required",
            "restartRecoveryVerified": "required_before_traffic",
        },
    }
    assert metadata["snapshotRegistry"] == {
        "contractVersion": "ashare-lab.durable-snapshot-store.v1",
        "mode": "external_durable_mount",
        "requiredMountPath": "/mnt/ashare-snapshots",
        "writePolicy": "content_addressed_append_only",
    }
    dockerfile = (output / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG CODE_REVISION=" + "a" * 40 in dockerfile
    assert "ARG CODE_REVISION\n" not in dockerfile
    assert f"ARG SNAPSHOT_DIGEST={digest}" in dockerfile
    assert "ARG SNAPSHOT_DIGEST\n" not in dockerfile
    assert (output / "deploy-snapshot" / digest / "daily_ohlcv.parquet").read_bytes() == (
        repository / "var" / "snapshots" / "composite" / digest / "daily_ohlcv.parquet"
    ).read_bytes()
    assert not (output / ".env").exists()
    assert not (output / "EmQuantAPI.py").exists()
    assert not (output / "ashare_lab" / "__pycache__").exists()
    assert (output / "alembic.ini").is_file()
    assert (output / "alembic" / "kept.txt").is_file()
    assert (output / "scripts" / "prepare_event_snapshot.py").is_file()
    assert (output / "deploy" / "cloudbase" / "production_db.py").is_file()
    assert (output / "web" / "dist" / "index.html").is_file()
    assert (output / "web" / "dist" / "assets" / "index-test.js").is_file()
    assert not (output / "web" / "src").exists()
    assert not (output / "web" / "node_modules").exists()

    release = json.loads((output / "release.json").read_text(encoding="utf-8"))
    assert release["schemaVersion"] == "ashare-lab.atomic-release.v1"
    assert release["codeRevision"] == "a" * 40
    assert release["webDistRoot"] == "web/dist"
    assert release["webBundleHash"] == metadata["webBundleHash"]
    assert release["webFiles"] == metadata["webFiles"]
    assert set(release["webFiles"]) == {
        "assets/index-test.css",
        "assets/index-test.js",
        "index.html",
    }
    for relative, evidence in release["webFiles"].items():
        payload = (output / "web" / "dist" / relative).read_bytes()
        assert evidence == {
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    assert len(release["webBundleHash"]) == 64
    assert (
        release["webBundleHash"]
        == hashlib.sha256(
            json.dumps(
                release["webFiles"],
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
    )


def test_bundle_keeps_multiple_content_addressed_seed_snapshots(tmp_path: Path) -> None:
    repository, first = _repository(tmp_path)
    second = _republish_with_new_composed_at(repository, first)

    metadata = prepare_bundle(
        repository=repository,
        output=tmp_path / "bundle",
        snapshot_digests=(first, second),
        code_revision="a" * 40,
        verify_git=False,
    )

    assert metadata["producerSnapshotIds"] == [f"composite:{first}", f"composite:{second}"]
    assert (tmp_path / "bundle" / "deploy-snapshot" / first).is_dir()
    assert (tmp_path / "bundle" / "deploy-snapshot" / second).is_dir()


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
    assert dockerfile.count("uv sync --locked --extra demo") == 2
    assert "import baostock, pypdfium2" in dockerfile
    assert "DEPLOYMENT_PROFILE=ephemeral_candidate" in dockerfile
    assert "APP_ENV=staging" in dockerfile
    assert "DATABASE_URL=sqlite+pysqlite:////app/var/ephemeral/ashare.db" in dockerfile
    assert "PERSISTENCE_MODE=ephemeral" in dockerfile
    assert "RESTART_RECOVERY_VERIFIED=false" in dockerfile
    assert "/app/var/ephemeral/snapshots/composite" in dockerfile
    assert "MARKET_DATA_PROFILE=on_demand_snapshot" in dockerfile
    assert "ON_DEMAND_REFRESH_EACH_SUBMISSION=false" in dockerfile
    assert "SNAPSHOT_STORAGE_MODE=ephemeral_local" in dockerfile
    assert "SNAPSHOT_STORAGE_MARKER=" not in dockerfile
    assert "DATA_ROOT=/app/var/snapshots/composite/${SNAPSHOT_DIGEST}" in dockerfile
    assert "COMPOSITE_SNAPSHOT_ROOT=/app/var/snapshots/composite" in dockerfile
    assert "deploy-snapshot/ /app/var/snapshots/composite/" in dockerfile
    assert "COPY --chown=app:app scripts ./scripts" in dockerfile
    assert "COPY --chown=app:app web/dist ./web/dist" in dockerfile
    assert "COPY --chown=app:app release.json ./release.json" in dockerfile
    assert "COPY --chown=app:app bundle-metadata.json ./bundle-metadata.json" in dockerfile
    assert "WEB_DIST_ROOT=/app/web/dist" in dockerfile
    assert 'CMD ["python", "deploy/cloudbase/deployment_entrypoint.py"]' in dockerfile
    assert 'CORS_ALLOWED_ORIGINS=""' in dockerfile
    assert "app.workbuddy.link" not in dockerfile
    assert "tcloudbaseapp.com" not in dockerfile


def test_cloudbase_environment_templates_default_to_same_origin_cors() -> None:
    for name in ("env.example", "ephemeral.env.example"):
        template = (REPOSITORY / "deploy" / "cloudbase" / name).read_text(encoding="utf-8")
        assert "CORS_ALLOWED_ORIGINS=" in template
        assert "app.workbuddy.link" not in template
        assert "tcloudbaseapp.com" not in template


def test_bundle_rejects_missing_or_unsafe_web_dist(tmp_path: Path) -> None:
    repository, digest = _repository(tmp_path)
    shutil.rmtree(repository / "web" / "dist")

    with pytest.raises(BundleError, match="web/dist"):
        prepare_bundle(
            repository=repository,
            output=tmp_path / "missing-bundle",
            snapshot_digest=digest,
            code_revision="a" * 40,
            verify_git=False,
        )

    unsafe_root = tmp_path / "unsafe"
    unsafe_root.mkdir()
    repository, digest = _repository(unsafe_root)
    asset = repository / "web" / "dist" / "assets" / "index-test.js"
    asset.unlink()
    asset.symlink_to(repository / "web" / "src" / "do-not-copy.ts")

    with pytest.raises(BundleError, match=r"web/dist.*symlink"):
        prepare_bundle(
            repository=repository,
            output=tmp_path / "unsafe-bundle",
            snapshot_digest=digest,
            code_revision="a" * 40,
            verify_git=False,
        )


@pytest.mark.parametrize(
    "marker",
    [
        "mock_demo",
        "https://ashare-backtest-api-305722-11-1330091763.sh.run.tcloudbase.com",
        "演示",
    ],
)
def test_bundle_rejects_non_same_origin_web_artifacts(tmp_path: Path, marker: str) -> None:
    repository, digest = _repository(tmp_path)
    (repository / "web" / "dist" / "assets" / "index-test.js").write_text(
        f"const forbidden={marker!r}\n",
        encoding="utf-8",
    )

    with pytest.raises(BundleError, match="same-origin Live build"):
        prepare_bundle(
            repository=repository,
            output=tmp_path / "bundle",
            snapshot_digest=digest,
            code_revision="a" * 40,
            verify_git=False,
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


def test_bundle_rejects_resealed_semantically_invalid_external_source_evidence(
    tmp_path: Path,
) -> None:
    repository, digest = _repository(tmp_path)
    invalid_digest = _republish_with_semantically_invalid_source_evidence(
        repository,
        digest,
    )

    with pytest.raises(
        BundleError,
        match="selected composite snapshot failed strict runtime validation",
    ):
        prepare_bundle(
            repository=repository,
            output=tmp_path / "bundle",
            snapshot_digest=invalid_digest,
            code_revision="a" * 40,
            verify_git=False,
        )
