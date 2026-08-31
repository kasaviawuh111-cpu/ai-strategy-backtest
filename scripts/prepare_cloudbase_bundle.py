#!/usr/bin/env python3
"""Build an allowlisted CloudBase source bundle around one immutable snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import cast

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_ROOT_FILES = ("pyproject.toml", "uv.lock", "README.md", "LICENSE")
_SOURCE_DIRECTORIES = ("ashare_lab", "astock_backtest", "catalogs", "contracts")


class BundleError(RuntimeError):
    """Raised before any incomplete deployment bundle is published."""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--snapshot-digest", required=True)
    parser.add_argument("--code-revision", required=True)
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    result = prepare_bundle(
        repository=repository,
        output=args.output,
        snapshot_digest=args.snapshot_digest,
        code_revision=args.code_revision,
    )
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


def prepare_bundle(
    *,
    repository: Path,
    output: Path,
    snapshot_digest: str,
    code_revision: str,
    verify_git: bool = True,
) -> dict[str, str]:
    repository = repository.expanduser().resolve()
    output = output.expanduser().resolve()
    if _SHA256.fullmatch(snapshot_digest) is None:
        raise BundleError("snapshot digest must be 64 lowercase hexadecimal characters")
    if _GIT_SHA.fullmatch(code_revision) is None:
        raise BundleError("code revision must be a 40-character lowercase Git SHA")
    if output.exists() and any(output.iterdir()):
        raise BundleError("output directory must not exist or must be empty")
    if output == repository or repository in output.parents:
        raise BundleError("output directory must be outside the repository")
    if verify_git:
        _verify_clean_revision(repository, code_revision)

    snapshot = repository / "var" / "snapshots" / "composite" / snapshot_digest
    manifest_sha256 = _validate_snapshot(snapshot, snapshot_digest)
    _validate_allowlisted_sources(repository)

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
    shutil.copy2(
        repository / "scripts" / "container_healthcheck.py",
        output / "scripts" / "container_healthcheck.py",
    )
    _copy_dockerfile_with_identity(
        repository / "deploy" / "cloudbase" / "Dockerfile",
        output / "Dockerfile",
        code_revision,
        snapshot_digest,
    )
    shutil.copytree(snapshot, output / "deploy-snapshot" / snapshot_digest)

    metadata = {
        "bundleSchemaVersion": "ashare-lab.cloudbase-source-bundle.v1",
        "codeRevision": code_revision,
        "producerSnapshotId": f"composite:{snapshot_digest}",
        "snapshotManifestSha256": manifest_sha256,
    }
    (output / "bundle-metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return metadata


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
        repository / "scripts" / "container_healthcheck.py",
        repository / "deploy" / "cloudbase" / "Dockerfile",
    )
    missing = [str(path.relative_to(repository)) for path in required if not path.exists()]
    if missing:
        raise BundleError("deployment sources are missing: " + ", ".join(missing))
    if any(path.is_symlink() for path in required):
        raise BundleError("deployment source allowlist cannot contain symlinks")


def _validate_snapshot(snapshot: Path, expected_digest: str) -> str:
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
    return hashlib.sha256(manifest_bytes).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
