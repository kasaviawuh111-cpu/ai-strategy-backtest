from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.prepare_private_preview_bundle import BundleError, prepare_bundle


def _source(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    files = {
        "pyproject.toml": "[project]\nname='preview'\n",
        "uv.lock": "version = 1\n",
        "README.md": "preview",
        "LICENSE": "license",
        "alembic.ini": "[alembic]\n",
        "ashare_lab/__init__.py": "",
        "astock_backtest/__init__.py": "",
        "catalogs/example.json": "{}",
        "contracts/example.json": "{}",
        "alembic/env.py": "",
        "deploy/private_preview/entrypoint.py": "def create_app(): pass\n",
        "deploy/private_preview/Dockerfile": "FROM python:3.12-slim\n",
        "scripts/prepare_private_preview_bundle.py": "# packager\n",
        "web/dist/index.html": '<script src="/assets/app.js"></script>',
        "web/dist/assets/app.js": (
            'const mode="live";({"data-api-mode":mode});"/api/v1/strategy-drafts";'
            '"respond-async";"backtest-preview-client";'
        ),
        "ashare_lab/resources/a_share_directory.json": json.dumps({
            "reported_total": 5567,
            "items": [
                {"symbol": f"{index:06}.{('SH', 'SZ', 'BJ')[index % 3]}",
                 "exchange": ("SH", "SZ", "BJ")[index % 3]}
                for index in range(5567)
            ],
        }),
    }
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return root


def test_bundle_is_allowlisted_and_revision_is_reproducible(tmp_path: Path) -> None:
    root = _source(tmp_path)
    forbidden = (".env", "var/user.db", ".git/config", "scripts/local_secret.py",
                 "ashare_lab/cache/user.json", "ashare_lab/.env", "ashare_lab/user.db",
                 "ashare_lab/tests/test_history.py", "web/dist/assets/app.js.map")
    for name in forbidden:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("must never ship")
    first = prepare_bundle(repository=root, output=tmp_path / "first")
    second = prepare_bundle(repository=root, output=tmp_path / "second")
    assert first == second
    files = first["files"]
    expected = "sha256:" + hashlib.sha256(
        json.dumps(files, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert first["codeRevision"] == expected
    assert first["instrumentDirectory"] == {
        "path": "ashare_lab/resources/a_share_directory.json", "rowCount": 5567,
        "markets": ["BJ", "SH", "SZ"],
    }
    assert all(not (tmp_path / "first" / name).exists() for name in forbidden)
    assert (tmp_path / "first/Dockerfile").exists()
    assert (tmp_path / "first/source-manifest.json").exists()
    (root / "ashare_lab/__init__.py").write_text("# changed\n")
    assert prepare_bundle(repository=root, output=tmp_path / "third")["codeRevision"] != expected


@pytest.mark.parametrize("marker", ["mock_demo", "localhost:8011", "mode='mock'"])
def test_mock_or_local_web_is_rejected(tmp_path: Path, marker: str) -> None:
    root = _source(tmp_path)
    (root / "web/dist/assets/app.js").write_text(
        f'const {marker}; ({{"data-api-mode":mode}}); "/api/v1/strategy-drafts";'
    )
    with pytest.raises(BundleError, match="web/dist"):
        prepare_bundle(repository=root, output=tmp_path / "bundle")
    assert not (tmp_path / "bundle").exists()


@pytest.mark.parametrize("marker", ["respond-async", "backtest-preview-client"])
def test_missing_preview_client_marker_is_rejected(tmp_path: Path, marker: str) -> None:
    root = _source(tmp_path)
    javascript = root / "web/dist/assets/app.js"
    javascript.write_text(javascript.read_text().replace(marker, ""))
    with pytest.raises(BundleError, match="VITE_PRIVATE_PREVIEW=true") as error:
        prepare_bundle(repository=root, output=tmp_path / "bundle")
    assert marker in str(error.value)
    assert not (tmp_path / "bundle").exists()


def test_symlink_source_and_missing_entrypoint_fail_closed(tmp_path: Path) -> None:
    root = _source(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("not part of source")
    link = root / "ashare_lab/outside.py"
    link.symlink_to(outside)
    with pytest.raises(BundleError, match="Symlinks"):
        prepare_bundle(repository=root, output=tmp_path / "bundle")
    link.unlink()
    (root / "deploy/private_preview/entrypoint.py").unlink()
    with pytest.raises(BundleError, match="entrypoint"):
        prepare_bundle(repository=root, output=tmp_path / "bundle")


def test_existing_output_and_incomplete_directory_are_rejected(tmp_path: Path) -> None:
    root = _source(tmp_path)
    output = tmp_path / "bundle"
    output.mkdir()
    preserved = output / "keep.txt"
    preserved.write_text("user data")
    with pytest.raises(BundleError, match="new or empty"):
        prepare_bundle(repository=root, output=output)
    assert preserved.read_text() == "user data"
    (root / "ashare_lab/resources/a_share_directory.json").write_text('{"items": []}')
    with pytest.raises(BundleError, match="5,567"):
        prepare_bundle(repository=root, output=tmp_path / "incomplete")
