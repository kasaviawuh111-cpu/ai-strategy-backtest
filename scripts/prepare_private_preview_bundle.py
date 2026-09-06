#!/usr/bin/env python3
"""Copy an explicit, credential-free private-preview Docker context (no deployment)."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import cast

_ROOT_FILES = ("pyproject.toml", "uv.lock", "README.md", "LICENSE", "alembic.ini")
_TREES = {
    "ashare_lab": {".py"},
    "astock_backtest": {".py"},
    "catalogs": {".py", ".json"},
    "contracts": {".py", ".json"},
    "alembic": {".py", ".mako"},
    "deploy/private_preview": {".py"},
    "web/dist": {".html", ".js", ".css", ".svg", ".png", ".ico", ".woff", ".woff2"},
}
_SKIP_DIRS = {"__pycache__", "tests", "test-results", "cache", "logs", "node_modules"}
_DIRECTORY = "ashare_lab/resources/a_share_directory.json"
_SCRIPT = "scripts/prepare_private_preview_bundle.py"
_DOCKERFILE = "deploy/private_preview/Dockerfile"


class BundleError(RuntimeError):
    """The source tree cannot produce a complete safe bundle."""


def _regular_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise BundleError(f"Required regular file is missing or unsafe: {path.name}")


def _tree_files(root: Path, suffixes: set[str]) -> list[Path]:
    if root.is_symlink() or not root.is_dir():
        raise BundleError(f"Required source directory is missing or unsafe: {root.name}")
    selected: list[Path] = []
    for path in sorted(root.iterdir()):
        if path.name.startswith(".") or path.name in _SKIP_DIRS:
            continue
        if path.is_symlink():
            raise BundleError(f"Symlinks are forbidden in bundle sources: {path.name}")
        if path.is_dir():
            selected.extend(_tree_files(path, suffixes))
        elif path.suffix in suffixes and not path.name.startswith("test_"):
            _regular_file(path)
            selected.append(path)
    return selected


def _source_files(repository: Path) -> dict[str, Path]:
    selected = {name: repository / name for name in (*_ROOT_FILES, _DIRECTORY, _SCRIPT)}
    selected["Dockerfile"] = repository / _DOCKERFILE
    for name, suffixes in _TREES.items():
        root = repository / name
        # Check every parent too: a nested allowlisted root must not escape via a symlink.
        if any(parent.is_symlink() for parent in (root, *root.parents) if parent != repository):
            raise BundleError(f"Symlinked source root is forbidden: {name}")
        for path in _tree_files(root, suffixes):
            selected[path.relative_to(repository).as_posix()] = path
    for path in selected.values():
        _regular_file(path)
        if not path.resolve().is_relative_to(repository):
            raise BundleError("Bundle source escapes repository")
    for required in ("deploy/private_preview/entrypoint.py", "web/dist/index.html"):
        if required not in selected:
            raise BundleError(f"Missing required runtime file: {required}")
    return dict(sorted(selected.items()))


def _validate_web(files: dict[str, Path]) -> None:
    javascript = b"\n".join(
        path.read_bytes() for name, path in files.items()
        if name.startswith("web/dist/") and path.suffix == ".js"
    )
    if not javascript or b"/api/v1/strategy-drafts" not in javascript:
        raise BundleError("web/dist must contain the built live API client")
    for marker in (b"mock_demo", b"draft_mock_demo_001", b"localhost:", b"127.0.0.1:"):
        if marker in javascript:
            raise BundleError("web/dist contains mock or local-service markers")
    # Vite's production tree-shaking fixes apiMode to live. This is static build
    # evidence only, not a replacement for the authenticated live acceptance journey.
    modes = re.findall(rb'["\']data-api-mode["\']\s*:\s*([A-Za-z_$][\w$]*)', javascript)
    if not modes or not all(
        re.search(rb'(?<![\w$])' + re.escape(mode) + rb'\s*=\s*["\']live["\']', javascript)
        for mode in modes
    ):
        raise BundleError("web/dist does not expose a statically live application mode")
    for marker in (b"respond-async", b"backtest-preview-client"):
        if marker not in javascript:
            raise BundleError(
                f"web/dist is missing required preview marker {marker.decode('ascii')}; "
                "rebuild with VITE_PRIVATE_PREVIEW=true before preparing the bundle"
            )


def _validate_directory(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    raw_items: object = data.get("items")
    if not isinstance(raw_items, list) or data.get("reported_total") != 5567:
        raise BundleError("Expected the reviewed 5,567-stock identity resource")
    items = cast(list[dict[str, str]], raw_items)
    if len(items) != 5567:
        raise BundleError("Expected the reviewed 5,567-stock identity resource")
    symbols = {item.get("symbol") for item in items}
    exchanges = {item.get("exchange", "") for item in items}
    if len(symbols) != len(items) or None in symbols or exchanges != {"SH", "SZ", "BJ"}:
        raise BundleError("Stock identity resource is duplicated or missing a reviewed market")
    return {"path": _DIRECTORY, "rowCount": len(items), "markets": sorted(exchanges)}


def _manifest_files(files: dict[str, Path]) -> list[dict[str, str]]:
    return [
        {"path": name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for name, path in sorted(files.items())
    ]


def prepare_bundle(*, repository: Path, output: Path) -> dict[str, object]:
    repository = repository.expanduser().resolve()
    output = output.expanduser()
    if output.is_symlink():
        raise BundleError("Output cannot be a symlink")
    output = output.resolve()
    if output == repository or output.is_relative_to(repository):
        raise BundleError("Output must be outside the source repository")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise BundleError("Output must be a new or empty directory; existing data is never removed")
    sources = _source_files(repository)
    _validate_web(sources)
    directory = _validate_directory(sources[_DIRECTORY])
    files = _manifest_files(sources)
    encoded = json.dumps(files, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    revision = "sha256:" + hashlib.sha256(encoded).hexdigest()
    output.mkdir(parents=True, exist_ok=True)
    for name, source in sources.items():
        destination = output / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    copied = {name: output / name for name in sources}
    if _manifest_files(copied) != files or _manifest_files(sources) != files:
        raise BundleError("Sources changed while copying; incomplete output must not be deployed")
    metadata: dict[str, object] = {
        "schemaVersion": "ashare-lab.private-preview-source-bundle.v1",
        "codeRevision": revision,
        "files": files,
        "digestDefinition": 'sha256(UTF-8 compact JSON of files, keys ordered path then sha256)',
        "sourcePathMappings": {"Dockerfile": _DOCKERFILE},
        "manifestExcludedFromDigest": True,
        "deploymentProfile": "private_skill_ephemeral",
        "persistence": "ephemeral",
        "restartRecoveryVerified": False,
        "instrumentDirectory": directory,
        "webValidation": "static live-mode and no mock/local-service markers; not live acceptance",
    }
    # Written last: its presence indicates the complete copy passed source/copy checks.
    (output / "source-manifest.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        metadata = prepare_bundle(repository=args.source_root, output=args.output)
    except (BundleError, OSError, ValueError) as exc:
        parser.exit(1, f"Private-preview bundle rejected: {exc}\n")
    print(f"CODE_REVISION={metadata['codeRevision']}")
    print(f"SOURCE_MANIFEST={args.output.expanduser().resolve() / 'source-manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
