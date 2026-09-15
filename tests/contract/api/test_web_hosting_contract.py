from __future__ import annotations

from pathlib import Path
from typing import NoReturn

import pytest
from fastapi.testclient import TestClient

from ashare_lab import bootstrap
from ashare_lab.api.app import create_app
from ashare_lab.api.web_hosting import WebDistConfigurationError
from ashare_lab.settings import AppSettings


def _write_web_dist(root: Path) -> Path:
    assets = root / "assets"
    assets.mkdir(parents=True)
    (root / "index.html").write_text("<main>live web</main>", encoding="utf-8")
    (assets / "index-WWLRx0Yi.js").write_text("export const live = true;", encoding="utf-8")
    (assets / "runtime.js").write_text("export const runtime = true;", encoding="utf-8")
    return root


def test_unconfigured_app_remains_api_only() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "http_404"


def test_gallery_samples_are_explicitly_served_without_exposing_other_root_files(
    tmp_path: Path,
) -> None:
    root = _write_web_dist(tmp_path / "dist")
    payload = '{"schemaVersion":"strategy-gallery-samples.v1","entries":[]}'
    (root / "strategy-gallery-samples.json").write_text(payload)
    (root / "build-provenance.json").write_text('{"private":"not public"}')
    with TestClient(create_app(web_dist_root=root)) as client:
        response = client.get("/strategy-gallery-samples.json")
        assert response.status_code == 200
        assert response.text == payload
        assert response.headers["content-type"].startswith("application/json")
        assert response.headers["cache-control"] == "no-cache"
        assert client.head("/strategy-gallery-samples.json").status_code == 200
        assert client.get("/build-provenance.json").status_code == 404


@pytest.mark.parametrize("symlink", [False, True])
def test_missing_or_linked_gallery_samples_fail_closed(tmp_path: Path, symlink: bool) -> None:
    root = _write_web_dist(tmp_path / "dist")
    if symlink:
        outside = tmp_path / "outside.json"
        outside.write_text('{}')
        (root / "strategy-gallery-samples.json").symlink_to(outside)
    with TestClient(create_app(web_dist_root=root)) as client:
        assert client.get("/strategy-gallery-samples.json").status_code == 404


def test_configured_app_serves_only_index_and_assets(tmp_path: Path) -> None:
    app = create_app(web_dist_root=_write_web_dist(tmp_path / "dist"))

    with TestClient(app) as client:
        root = client.get("/")
        index = client.get("/index.html")
        portfolio_review = client.get("/portfolio-review")
        portfolio_review_slash = client.get("/portfolio-review/")
        asset = client.get("/assets/index-WWLRx0Yi.js")
        mutable_asset = client.get("/assets/runtime.js")
        unknown_page = client.get("/portfolio")
        unknown_api = client.get("/api/v1/not-a-route")

    assert root.status_code == 200
    assert root.text == "<main>live web</main>"
    assert root.headers["cache-control"] == "no-store"
    assert index.status_code == 200
    assert index.headers["cache-control"] == "no-store"
    assert portfolio_review.status_code == 200
    assert portfolio_review.text == "<main>live web</main>"
    assert portfolio_review.headers["cache-control"] == "no-store"
    assert portfolio_review_slash.status_code == 200
    assert portfolio_review_slash.text == "<main>live web</main>"
    assert portfolio_review_slash.headers["cache-control"] == "no-store"
    assert asset.status_code == 200
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert mutable_asset.status_code == 200
    assert mutable_asset.headers["cache-control"] == "no-cache"
    assert unknown_page.status_code == 404
    assert unknown_page.headers["content-type"].startswith("application/json")
    assert unknown_api.status_code == 404
    assert unknown_api.headers["content-type"].startswith("application/json")
    assert unknown_api.json()["error"]["code"] == "http_404"


@pytest.mark.parametrize("missing", ["root", "index", "assets"])
def test_incomplete_configured_dist_fails_before_startup(tmp_path: Path, missing: str) -> None:
    root = tmp_path / "dist"
    if missing != "root":
        root.mkdir()
    if missing != "root" and missing != "index":
        (root / "index.html").write_text("web", encoding="utf-8")
    if missing != "root" and missing != "assets":
        (root / "assets").mkdir()

    with pytest.raises(WebDistConfigurationError):
        create_app(web_dist_root=root)


def test_configured_bootstrap_checks_dist_before_building_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_runtime_build(*args: object, **kwargs: object) -> NoReturn:
        del args, kwargs
        raise AssertionError("runtime must not start for an invalid web distribution")

    monkeypatch.setattr(bootstrap, "build_api_runtime", unexpected_runtime_build)
    settings = AppSettings(web_dist_root=tmp_path / "missing")

    with pytest.raises(WebDistConfigurationError):
        bootstrap.create_configured_app(settings)


@pytest.mark.parametrize("target", ["root", "index", "assets"])
def test_configured_dist_rejects_symbolic_links(tmp_path: Path, target: str) -> None:
    real = _write_web_dist(tmp_path / "real")
    configured = real
    if target == "root":
        configured = tmp_path / "linked-dist"
        configured.symlink_to(real, target_is_directory=True)
    elif target == "index":
        index = real / "index.html"
        payload = tmp_path / "index.html"
        index.replace(payload)
        index.symlink_to(payload)
    else:
        assets = real / "assets"
        payload = tmp_path / "assets"
        assets.replace(payload)
        assets.symlink_to(payload, target_is_directory=True)

    with pytest.raises(WebDistConfigurationError, match="symbolic link"):
        create_app(web_dist_root=configured)


def test_api_routes_are_registered_before_static_hosting(tmp_path: Path) -> None:
    app = create_app(web_dist_root=_write_web_dist(tmp_path / "dist"))

    with TestClient(app) as client:
        response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
