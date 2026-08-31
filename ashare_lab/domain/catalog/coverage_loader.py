"""Filesystem loader for immutable coverage catalog releases."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from ashare_lab.domain.strategy.canonical import canonical_hash

from .coverage_models import (
    CoverageCatalogRelease,
    CoverageCatalogSnapshot,
    EventDefinition,
    MetricDefinition,
)


class CoverageCatalogLoadError(ValueError):
    """Raised when coverage files do not form one unambiguous active snapshot."""


def load_coverage_catalog_release(path: str | Path) -> CoverageCatalogRelease:
    release_path = Path(path)
    try:
        return CoverageCatalogRelease.model_validate_json(release_path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as exc:
        raise CoverageCatalogLoadError(f"invalid coverage catalog {release_path}: {exc}") from exc


def load_coverage_catalog_directory(root: str | Path) -> CoverageCatalogSnapshot:
    root_path = Path(root)
    paths = sorted(root_path.rglob("*.catalog.json"))
    if not paths:
        raise CoverageCatalogLoadError(f"no *.catalog.json files found under {root_path}")

    releases = tuple(load_coverage_catalog_release(path) for path in paths)
    release_keys: set[tuple[str, str]] = set()
    active_by_catalog: dict[str, CoverageCatalogRelease] = {}
    for release in releases:
        release_key = (release.catalog_id, release.release_version)
        if release_key in release_keys:
            raise CoverageCatalogLoadError(
                f"duplicate coverage catalog release: {release_key[0]}@{release_key[1]}"
            )
        release_keys.add(release_key)
        if release.state == "active":
            previous = active_by_catalog.get(release.catalog_id)
            if previous is not None:
                raise CoverageCatalogLoadError(
                    "multiple active coverage releases for "
                    f"{release.catalog_id}: {previous.release_version}, {release.release_version}"
                )
            active_by_catalog[release.catalog_id] = release

    if not active_by_catalog:
        raise CoverageCatalogLoadError("coverage catalog directory has no active release")

    active = tuple(active_by_catalog[key] for key in sorted(active_by_catalog))
    metrics: list[MetricDefinition] = []
    events: list[EventDefinition] = []
    metric_owner: dict[str, str] = {}
    event_owner: dict[str, str] = {}
    for release in active:
        for metric in release.metrics:
            previous = metric_owner.get(metric.id)
            if previous is not None:
                raise CoverageCatalogLoadError(
                    f"metric {metric.id!r} is active in both {previous!r} "
                    f"and {release.catalog_id!r}"
                )
            metric_owner[metric.id] = release.catalog_id
            metrics.append(metric)
        for event in release.events:
            previous = event_owner.get(event.id)
            if previous is not None:
                raise CoverageCatalogLoadError(
                    f"event {event.id!r} is active in both {previous!r} and {release.catalog_id!r}"
                )
            event_owner[event.id] = release.catalog_id
            events.append(event)

    hash_input = {"releases": [item.model_dump(mode="json") for item in active]}
    return CoverageCatalogSnapshot(
        releases=active,
        metrics=tuple(metrics),
        events=tuple(events),
        content_hash=canonical_hash(hash_input),
    )
