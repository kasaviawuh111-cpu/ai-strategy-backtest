"""Filesystem loader for immutable Catalog release manifests."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from ashare_lab.domain.strategy.canonical import canonical_hash

from .models import CatalogManifest, CatalogSnapshot, IndicatorDefinition


class CatalogLoadError(ValueError):
    """Raised when Catalog files cannot form one unambiguous active snapshot."""


def load_catalog_manifest(path: str | Path) -> CatalogManifest:
    manifest_path = Path(path)
    try:
        return CatalogManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as exc:
        raise CatalogLoadError(f"invalid catalog manifest {manifest_path}: {exc}") from exc


def load_catalog_directory(root: str | Path) -> CatalogSnapshot:
    root_path = Path(root)
    paths = sorted(root_path.rglob("*.manifest.json"))
    if not paths:
        raise CatalogLoadError(f"no *.manifest.json files found under {root_path}")

    manifests = tuple(load_catalog_manifest(path) for path in paths)
    release_keys: set[tuple[str, str]] = set()
    active_by_catalog: dict[str, CatalogManifest] = {}

    for manifest in manifests:
        release_key = (manifest.catalog_id, manifest.release_version)
        if release_key in release_keys:
            raise CatalogLoadError(f"duplicate catalog release: {release_key[0]}@{release_key[1]}")
        release_keys.add(release_key)
        if manifest.state == "active":
            previous = active_by_catalog.get(manifest.catalog_id)
            if previous is not None:
                raise CatalogLoadError(
                    "multiple active releases for "
                    f"{manifest.catalog_id}: {previous.release_version}, {manifest.release_version}"
                )
            active_by_catalog[manifest.catalog_id] = manifest

    if not active_by_catalog:
        raise CatalogLoadError("catalog directory has no active release")

    active_manifests = tuple(active_by_catalog[key] for key in sorted(active_by_catalog))
    indicators: list[IndicatorDefinition] = []
    owner_by_indicator: dict[str, str] = {}
    for manifest in active_manifests:
        for indicator in manifest.indicators:
            previous_owner = owner_by_indicator.get(indicator.id)
            if previous_owner is not None:
                raise CatalogLoadError(
                    f"indicator {indicator.id!r} is active in both {previous_owner!r} "
                    f"and {manifest.catalog_id!r}"
                )
            owner_by_indicator[indicator.id] = manifest.catalog_id
            indicators.append(indicator)

    hash_input = {"manifests": [manifest.model_dump(mode="json") for manifest in active_manifests]}
    return CatalogSnapshot(
        manifests=active_manifests,
        indicators=tuple(indicators),
        content_hash=canonical_hash(hash_input),
    )
