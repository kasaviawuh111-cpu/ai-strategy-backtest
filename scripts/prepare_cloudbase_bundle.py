#!/usr/bin/env python3
"""Build an allowlisted CloudBase source bundle around trusted immutable snapshots."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from ashare_lab.adapters.market_data import (
    LocalParquetMarketDataRepository,
    MarketDataAdapterError,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_BUNDLE_SCHEMA_VERSION = "ashare-lab.cloudbase-source-bundle.v3"
_RELEASE_SCHEMA_VERSION = "ashare-lab.atomic-release.v1"
_FORBIDDEN_WEB_MARKERS = (
    b"draft_mock_demo_001",
    b"mock_demo",
    b"https://ashare-backtest-api-305722-11-1330091763.sh.run.tcloudbase.com",
    "演示".encode(),
    "真实数据".encode(),
    "正式数据".encode(),
    "真实接口".encode(),
    "真实回测".encode(),
)
_ROOT_FILES = ("pyproject.toml", "uv.lock", "README.md", "LICENSE", "alembic.ini")
_SOURCE_DIRECTORIES = ("ashare_lab", "astock_backtest", "catalogs", "contracts", "alembic")
_RUNTIME_SCRIPTS = (
    "container_healthcheck.py",
    "prepare_baostock_reference.py",
    "prepare_baostock_snapshot.py",
    "prepare_choice_snapshot.py",
    "prepare_eastmoney_corporate_actions.py",
    "prepare_eastmoney_snapshot.py",
    "prepare_event_snapshot.py",
)
_CLOUDBASE_FILES = (
    "Dockerfile",
    "deployment_entrypoint.py",
    "runtime_entrypoint.py",
    "production_db.py",
    "tc3_database_release.py",
    "README.md",
    "env.example",
    "ephemeral.env.example",
)


class BundleError(RuntimeError):
    """Raised before any incomplete deployment bundle is published."""


@dataclass(frozen=True, slots=True)
class _SnapshotEvidence:
    digest: str
    producer_snapshot_id: str
    manifest_sha256: str
    instrument_id: str


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--snapshot-digest",
        action="append",
        required=True,
        help=(
            "validated Composite v2 digest; repeat for every server-trusted snapshot "
            "and put the legacy/default DATA_ROOT snapshot first"
        ),
    )
    parser.add_argument("--code-revision", required=True)
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    result = prepare_bundle(
        repository=repository,
        output=args.output,
        snapshot_digests=args.snapshot_digest,
        code_revision=args.code_revision,
    )
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


def prepare_bundle(
    *,
    repository: Path,
    output: Path,
    code_revision: str,
    snapshot_digest: str | None = None,
    snapshot_digests: Sequence[str] = (),
    verify_git: bool = True,
) -> dict[str, object]:
    repository = repository.expanduser().resolve()
    output = output.expanduser().resolve()
    selected_digests = _normalize_snapshot_digests(
        snapshot_digest=snapshot_digest,
        snapshot_digests=snapshot_digests,
    )
    if _GIT_SHA.fullmatch(code_revision) is None:
        raise BundleError("code revision must be a 40-character lowercase Git SHA")
    if output.exists() and any(output.iterdir()):
        raise BundleError("output directory must not exist or must be empty")
    if output == repository or repository in output.parents:
        raise BundleError("output directory must be outside the repository")
    if verify_git:
        _verify_clean_revision(repository, code_revision)

    snapshots: list[tuple[Path, _SnapshotEvidence]] = []
    for digest in selected_digests:
        snapshot = repository / "var" / "snapshots" / "composite" / digest
        evidence = _validate_snapshot(snapshot, digest)
        try:
            strict_snapshot = LocalParquetMarketDataRepository(
                snapshot,
                profile="composite_snapshot",
            ).pin_strict_composite_snapshot()
        except MarketDataAdapterError as exc:
            raise BundleError(
                f"selected composite snapshot failed strict runtime validation: {digest}"
            ) from exc
        if strict_snapshot.producer_snapshot_id != evidence.producer_snapshot_id:
            raise BundleError("strict runtime pin returned a different producer snapshot")
        snapshots.append((snapshot, evidence))
    _validate_allowlisted_sources(repository)
    release = _build_release_evidence(
        web_dist=repository / "web" / "dist",
        code_revision=code_revision,
    )

    output.mkdir(parents=True, exist_ok=True)
    for name in _ROOT_FILES:
        shutil.copy2(repository / name, output / name)
    for name in _SOURCE_DIRECTORIES:
        shutil.copytree(
            repository / name,
            output / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
        )
    (output / "scripts").mkdir()
    for name in _RUNTIME_SCRIPTS:
        shutil.copy2(repository / "scripts" / name, output / "scripts" / name)
    shutil.copytree(repository / "web" / "dist", output / "web" / "dist")
    copied_release = _build_release_evidence(
        web_dist=output / "web" / "dist",
        code_revision=code_revision,
    )
    if copied_release != release:
        raise BundleError("copied web/dist does not match its source release evidence")
    _copy_dockerfile_with_identity(
        repository / "deploy" / "cloudbase" / "Dockerfile",
        output / "Dockerfile",
        code_revision,
        selected_digests[0],
    )
    (output / "deploy" / "cloudbase").mkdir(parents=True)
    for name in _CLOUDBASE_FILES:
        if name == "Dockerfile":
            continue
        shutil.copy2(
            repository / "deploy" / "cloudbase" / name,
            output / "deploy" / "cloudbase" / name,
        )
    for snapshot, evidence in snapshots:
        shutil.copytree(snapshot, output / "deploy-snapshot" / evidence.digest)

    primary = snapshots[0][1]
    metadata: dict[str, object] = {
        "bundleSchemaVersion": _BUNDLE_SCHEMA_VERSION,
        "codeRevision": code_revision,
        "deploymentProfiles": {
            "ephemeral_candidate": {
                "persistence": "ephemeral",
                "restartRecoveryVerified": False,
            },
            "strict_production": {
                "persistence": "postgresql_and_durable_mount_required",
                "restartRecoveryVerified": "required_before_traffic",
            },
        },
        # Retain the singular fields for old release tooling.  They identify
        # the first/legacy DATA_ROOT snapshot, never an implicit replacement.
        "producerSnapshotId": primary.producer_snapshot_id,
        "snapshotManifestSha256": primary.manifest_sha256,
        "webDistRoot": release["webDistRoot"],
        "webBundleHash": release["webBundleHash"],
        "webFiles": release["webFiles"],
        "producerSnapshotIds": [item.producer_snapshot_id for _, item in snapshots],
        "snapshotRegistry": {
            "contractVersion": "ashare-lab.durable-snapshot-store.v1",
            "mode": "external_durable_mount",
            "requiredMountPath": "/mnt/ashare-snapshots",
            "writePolicy": "content_addressed_append_only",
        },
        "snapshots": [
            {
                "instrumentId": item.instrument_id,
                "manifestSha256": item.manifest_sha256,
                "producerSnapshotId": item.producer_snapshot_id,
            }
            for _, item in snapshots
        ],
    }
    (output / "release.json").write_text(
        json.dumps(release, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output / "bundle-metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return metadata


def _build_release_evidence(*, web_dist: Path, code_revision: str) -> dict[str, object]:
    files = _collect_web_dist_files(web_dist)
    web_bundle_hash = hashlib.sha256(
        json.dumps(
            files,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schemaVersion": _RELEASE_SCHEMA_VERSION,
        "codeRevision": code_revision,
        "webDistRoot": "web/dist",
        "webBundleHash": web_bundle_hash,
        "webFiles": files,
    }


def _collect_web_dist_files(web_dist: Path) -> dict[str, dict[str, object]]:
    if not web_dist.is_dir() or web_dist.is_symlink():
        raise BundleError("web/dist is missing or unsafe")
    paths = sorted(web_dist.rglob("*"), key=lambda item: item.relative_to(web_dist).as_posix())
    if any(path.is_symlink() for path in paths):
        raise BundleError("web/dist cannot contain symlinks")
    if not (web_dist / "index.html").is_file():
        raise BundleError("web/dist must contain index.html")

    files: dict[str, dict[str, object]] = {}
    javascript_payloads: list[bytes] = []
    for path in paths:
        if path.is_dir():
            continue
        if not path.is_file():
            raise BundleError("web/dist can contain only regular files and directories")
        payload = path.read_bytes()
        relative = path.relative_to(web_dist).as_posix()
        if path.suffix == ".js":
            javascript_payloads.append(payload)
        files[relative] = {
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    if not files:
        raise BundleError("web/dist cannot be empty")
    if not javascript_payloads:
        raise BundleError("web/dist must contain a JavaScript application asset")
    javascript = b"\n".join(javascript_payloads)
    for marker in _FORBIDDEN_WEB_MARKERS:
        if marker in javascript:
            raise BundleError("web/dist is not a verified same-origin Live build")
    return files


def _normalize_snapshot_digests(
    *,
    snapshot_digest: str | None,
    snapshot_digests: Sequence[str],
) -> tuple[str, ...]:
    values = tuple(snapshot_digests)
    if snapshot_digest is not None:
        if values:
            raise BundleError("use snapshot_digest or snapshot_digests, not both")
        values = (snapshot_digest,)
    if not values:
        raise BundleError("at least one server-trusted snapshot digest is required")
    if any(_SHA256.fullmatch(item) is None for item in values):
        raise BundleError("snapshot digest must be 64 lowercase hexadecimal characters")
    if len(set(values)) != len(values):
        raise BundleError("snapshot digests must be unique")
    return values


def _copy_dockerfile_with_identity(
    source: Path,
    destination: Path,
    code_revision: str,
    snapshot_digest: str,
) -> None:
    """Pin both validated identities when the platform cannot pass build args."""

    try:
        template = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise BundleError("CloudBase Dockerfile template is unreadable") from exc
    replacements = {
        "ARG CODE_REVISION": f"ARG CODE_REVISION={code_revision}",
        "ARG SNAPSHOT_DIGEST": f"ARG SNAPSHOT_DIGEST={snapshot_digest}",
    }
    rendered = template
    for placeholder, pinned in replacements.items():
        if template.splitlines().count(placeholder) != 1:
            argument = placeholder.removeprefix("ARG ")
            raise BundleError(f"CloudBase Dockerfile must contain one unpinned {argument} argument")
        rendered = rendered.replace(placeholder, pinned, 1)
    destination.write_text(rendered, encoding="utf-8")


def _verify_clean_revision(repository: Path, expected: str) -> None:
    try:
        actual = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ("git", "status", "--porcelain", "--untracked-files=normal"),
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise BundleError("repository does not have a readable Git revision") from exc
    if actual != expected:
        raise BundleError("code revision does not match repository HEAD")
    if dirty:
        raise BundleError("repository must be clean before building a deployment bundle")


def _validate_allowlisted_sources(repository: Path) -> None:
    required = (
        *(repository / name for name in _ROOT_FILES),
        *(repository / name for name in _SOURCE_DIRECTORIES),
        *(repository / "scripts" / name for name in _RUNTIME_SCRIPTS),
        *(repository / "deploy" / "cloudbase" / name for name in _CLOUDBASE_FILES),
    )
    missing = [str(path.relative_to(repository)) for path in required if not path.exists()]
    if missing:
        raise BundleError("deployment sources are missing: " + ", ".join(missing))
    if any(path.is_symlink() for path in required):
        raise BundleError("deployment source allowlist cannot contain symlinks")


def _validate_snapshot(snapshot: Path, expected_digest: str) -> _SnapshotEvidence:
    if not snapshot.is_dir() or snapshot.is_symlink():
        raise BundleError("selected composite snapshot directory is missing or unsafe")
    manifest_path = snapshot / "snapshot_manifest.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
        decoded = json.loads(manifest_bytes)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BundleError("selected composite snapshot manifest is unreadable") from exc
    if not isinstance(decoded, dict):
        raise BundleError("selected composite snapshot manifest must be an object")
    manifest = cast(dict[str, object], decoded)
    if manifest.get("schemaVersion") != "ashare-lab.composite-research-snapshot.v2":
        raise BundleError("selected snapshot is not Composite v2")
    if manifest.get("snapshotId") != f"composite:{expected_digest}":
        raise BundleError("selected snapshot identity does not match its directory")
    body = dict(manifest)
    body.pop("snapshotId", None)
    canonical_digest = hashlib.sha256(
        json.dumps(
            body,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    if canonical_digest != expected_digest:
        raise BundleError("selected snapshot manifest body does not match its content ID")

    files = manifest.get("files")
    if not isinstance(files, dict):
        raise BundleError("selected snapshot manifest files registry is invalid")
    for raw_name, raw_evidence in cast(dict[object, object], files).items():
        if not isinstance(raw_name, str) or not isinstance(raw_evidence, dict):
            raise BundleError("selected snapshot file evidence is invalid")
        relative = Path(raw_name)
        if relative.is_absolute() or ".." in relative.parts:
            raise BundleError("selected snapshot file path escapes its directory")
        path = snapshot / relative
        if path.is_symlink() or not path.is_file():
            raise BundleError(f"selected snapshot file is missing or unsafe: {raw_name}")
        evidence = cast(dict[object, object], raw_evidence)
        expected_bytes = evidence.get("bytes")
        expected_sha256 = evidence.get("sha256")
        payload = path.read_bytes()
        if expected_bytes != len(payload) or expected_sha256 != hashlib.sha256(payload).hexdigest():
            raise BundleError(f"selected snapshot file does not match manifest: {raw_name}")
    coverage = manifest.get("eventAcquisitionCoverage")
    coverage_map = cast(dict[object, object], coverage) if isinstance(coverage, dict) else {}
    instrument_id = coverage_map.get("instrumentId")
    if not isinstance(instrument_id, str) or not instrument_id:
        raise BundleError("selected snapshot does not declare one covered instrument")
    return _SnapshotEvidence(
        digest=expected_digest,
        producer_snapshot_id=f"composite:{expected_digest}",
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        instrument_id=instrument_id,
    )


if __name__ == "__main__":
    raise SystemExit(main())
