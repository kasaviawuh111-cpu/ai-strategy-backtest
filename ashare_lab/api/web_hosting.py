"""Optional, fail-closed hosting for a pre-built web distribution."""

from __future__ import annotations

import re
from os import PathLike, stat_result
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

_HASHED_ASSET_STEM = re.compile(r"(?:-|\.)(?:[A-Za-z0-9_-]{8}|[0-9a-f]{9,64})$")


class WebDistConfigurationError(RuntimeError):
    """Raised before startup when the configured web distribution is incomplete."""


class _CacheControlledStaticFiles(StaticFiles):
    def file_response(
        self,
        full_path: str | PathLike[str],
        stat_result: stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        stem = Path(full_path).stem
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable" if _HASHED_ASSET_STEM.search(stem) else "no-cache"
        )
        return response


def install_web_hosting(app: FastAPI, dist_root: str | Path | None) -> None:
    """Attach the built entry point, public gallery data and asset tree."""

    root = validate_web_dist_root(dist_root)
    if root is None:
        return

    index = root / "index.html"
    assets = root / "assets"

    async def serve_index() -> FileResponse:
        return FileResponse(index, headers={"Cache-Control": "no-store"})

    async def serve_gallery_samples() -> FileResponse:
        samples = root / "strategy-gallery-samples.json"
        if samples.is_symlink() or not samples.is_file():
            raise HTTPException(status_code=404, detail="Gallery samples unavailable")
        return FileResponse(
            samples, media_type="application/json", headers={"Cache-Control": "no-cache"},
        )

    # Explicit allowlist: do not expose manifests or other files in the dist root.
    app.add_api_route(
        "/strategy-gallery-samples.json", serve_gallery_samples,
        methods=["GET", "HEAD"], include_in_schema=False, name="web-gallery-samples",
    )

    route_options: dict[str, Any] = {
        "endpoint": serve_index,
        "methods": ["GET", "HEAD"],
        "include_in_schema": False,
    }
    app.add_api_route("/", name="web-index", **route_options)
    app.add_api_route("/index.html", name="web-index-html", **route_options)
    app.add_api_route(
        "/portfolio-review",
        name="web-portfolio-review",
        **route_options,
    )
    app.add_api_route(
        "/portfolio-review/",
        name="web-portfolio-review-slash",
        **route_options,
    )
    app.mount(
        "/assets",
        _CacheControlledStaticFiles(directory=assets, check_dir=True),
        name="web-assets",
    )


def validate_web_dist_root(dist_root: str | Path | None) -> Path | None:
    """Resolve and validate a complete pre-built distribution without side effects."""

    if dist_root is None:
        return None
    configured = Path(dist_root).expanduser()
    if configured.is_symlink():
        raise WebDistConfigurationError("WEB_DIST_ROOT cannot be a symbolic link")
    root = configured.resolve()
    index = root / "index.html"
    assets = root / "assets"
    if not root.is_dir():
        raise WebDistConfigurationError("WEB_DIST_ROOT must be an existing directory")
    if not index.is_file():
        raise WebDistConfigurationError("WEB_DIST_ROOT/index.html is missing")
    if index.is_symlink():
        raise WebDistConfigurationError("WEB_DIST_ROOT/index.html cannot be a symbolic link")
    if not assets.is_dir():
        raise WebDistConfigurationError("WEB_DIST_ROOT/assets is missing")
    if assets.is_symlink():
        raise WebDistConfigurationError("WEB_DIST_ROOT/assets cannot be a symbolic link")
    return root
