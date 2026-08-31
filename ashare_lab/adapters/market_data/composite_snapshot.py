"""Compose Choice technical data and a validated event snapshot for replay."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, cast

import pyarrow.parquet as pq

from ashare_lab.domain.events.document_metrics import (
    DocumentMetricError,
    validate_document_text_artifact,
)

from .choice_snapshot import (
    CORPORATE_ACTION_FILENAME,
    STRICT_CORPORATE_ACTION_CATEGORIES,
    TECHNICAL_SNAPSHOT_SCHEMA_VERSION,
    ChoiceSnapshotError,
    validate_corporate_action_coverage_evidence,
)
from .choice_snapshot import SNAPSHOT_SCHEMA_VERSION as CHOICE_SNAPSHOT_SCHEMA_VERSION
from .event_snapshot import (
    EVENT_SNAPSHOT_SCHEMA_VERSION,
    EVENTS_FILENAME,
    NO_EVENT_REQUIRED_MODE,
    OBSERVATIONS_FILENAME,
)
from .event_snapshot import MANIFEST_FILENAME as EVENT_MANIFEST_FILENAME

COMPOSITE_SNAPSHOT_SCHEMA_VERSION = "ashare-lab.composite-research-snapshot.v2"
COMPOSITE_MANIFEST_FILENAME = "snapshot_manifest.json"
_HASH_CHUNK_BYTES = 1024 * 1024
_PARQUET: Any = pq
_TECHNICAL_FILES = (
    "daily_ohlcv.parquet",
    "signal_daily_ohlcv.parquet",
    "instrument_sessions.parquet",
    "corporate_actions.parquet",
)


class CompositeSnapshotError(RuntimeError):
    """Technical and event snapshots cannot be composed without losing provenance."""


@dataclass(frozen=True, slots=True)
class CompositeSnapshotResult:
    snapshot_id: str
    path: Path
    manifest: Mapping[str, object]


def compose_choice_event_snapshot(
    *,
    choice_snapshot_path: Path,
    event_snapshot_path: Path,
    output_root: Path,
    composed_at: datetime,
) -> CompositeSnapshotResult:
    """Publish one content-addressed data root consumed by backtest workers."""

    if composed_at.tzinfo is None or composed_at.utcoffset() is None:
        raise CompositeSnapshotError("composed_at must include an explicit timezone")
    choice_root = choice_snapshot_path.expanduser().resolve()
    event_root = event_snapshot_path.expanduser().resolve()
    choice_manifest = _load_daily_source_manifest(choice_root)
    technical_source = choice_manifest.get("schemaVersion") == TECHNICAL_SNAPSHOT_SCHEMA_VERSION
    event_manifest = _load_source_manifest(
        event_root,
        filename=EVENT_MANIFEST_FILENAME,
        schema_version=EVENT_SNAPSHOT_SCHEMA_VERSION,
        snapshot_prefix="events",
    )
    _validate_choice_manifest(choice_manifest, choice_root=choice_root)
    event_data_available = _validate_event_manifest(event_manifest)
    _validate_event_document_text_artifacts(event_root)
    _validate_choice_event_alignment(choice_manifest, event_manifest)

    destination_root = output_root.expanduser().resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".composite-snapshot-", dir=destination_root))
    try:
        for filename in _TECHNICAL_FILES:
            _copy_registered_file(choice_root, choice_manifest, filename, temporary / filename)
        _copy_registered_file(
            event_root,
            event_manifest,
            EVENTS_FILENAME,
            temporary / EVENTS_FILENAME,
        )
        _copy_registered_file(
            event_root,
            event_manifest,
            OBSERVATIONS_FILENAME,
            temporary / OBSERVATIONS_FILENAME,
        )

        source_root = temporary / "source"
        source_root.mkdir(parents=True)
        choice_manifest_target = source_root / (
            "technical_snapshot_manifest.json"
            if technical_source
            else "choice_snapshot_manifest.json"
        )
        event_manifest_target = source_root / "event_snapshot_manifest.json"
        shutil.copy2(choice_root / COMPOSITE_MANIFEST_FILENAME, choice_manifest_target)
        shutil.copy2(event_root / EVENT_MANIFEST_FILENAME, event_manifest_target)

        audit_root = temporary / "raw" / ("technical" if technical_source else "choice")
        for relative in sorted(_manifest_files(choice_manifest)):
            if relative in _TECHNICAL_FILES:
                continue
            target = audit_root.joinpath(*PurePosixPath(relative).parts)
            _copy_registered_file(choice_root, choice_manifest, relative, target)

        quarantine = "event_quarantine.json"
        if quarantine in _manifest_files(event_manifest):
            _copy_registered_file(
                event_root,
                event_manifest,
                quarantine,
                temporary / quarantine,
            )

        files = {
            path.relative_to(temporary).as_posix(): {
                "bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
            for path in sorted(item for item in temporary.rglob("*") if item.is_file())
        }
        event_policy = _mapping(event_manifest, "policy")
        event_acquisition_coverage = _mapping(event_manifest, "acquisitionCoverage")
        event_rows = _mapping(event_manifest, "rowCounts")
        choice_rows = _mapping(choice_manifest, "rowCounts")
        manifest_body: dict[str, object] = {
            "schemaVersion": COMPOSITE_SNAPSHOT_SCHEMA_VERSION,
            "composedAt": composed_at.astimezone(UTC).isoformat(),
            "baseSnapshots": {
                ("technical" if technical_source else "choice"): {
                    "snapshotId": choice_manifest["snapshotId"],
                    "schemaVersion": choice_manifest["schemaVersion"],
                    "manifestSha256": _sha256_file(choice_root / COMPOSITE_MANIFEST_FILENAME),
                },
                "events": {
                    "snapshotId": event_manifest["snapshotId"],
                    "schemaVersion": event_manifest["schemaVersion"],
                    "manifestSha256": _sha256_file(event_root / EVENT_MANIFEST_FILENAME),
                },
            },
            "rowCounts": {
                "execution": choice_rows.get("execution"),
                "signal": choice_rows.get("signal"),
                "sessions": choice_rows.get("sessions"),
                "canonicalEvents": event_rows.get("canonicalEvents"),
                "eventObservations": event_rows.get("observations"),
                "corporateActions": choice_rows.get("corporateActions"),
            },
            "corporateActionCoverage": dict(_mapping(choice_manifest, "corporateActionCoverage")),
            "technicalSource": {
                "provider": choice_manifest.get("provider"),
                "dataset": choice_manifest.get("sourceDataset"),
                "schemaVersion": choice_manifest.get("schemaVersion"),
            },
            "eventPolicy": dict(event_policy),
            "eventAcquisitionCoverage": dict(event_acquisition_coverage),
            "capabilities": {
                "technicalDaily": "validated_for_demo",
                "events": ("validated_for_demo" if event_data_available else "not_required"),
                "corporateActions": "validated_for_demo",
                "corporateActionLedger": "point_in_time.v1",
                "runtimeNetworkAccess": "forbidden",
            },
            "files": files,
            "limitations": [
                "technical price inputs retain their declared research-source assumptions",
                "event APIs are acquisition-only and are never called during a backtest run",
                "provider authorization and redistribution terms must be approved for production",
            ],
        }
        event_requirement = event_manifest.get("eventRequirement")
        if event_requirement is not None:
            if not isinstance(event_requirement, Mapping):
                raise CompositeSnapshotError("event snapshot requirement must be an object")
            manifest_body["eventRequirement"] = dict(cast(Mapping[str, object], event_requirement))
        if not event_data_available:
            manifest_body["limitations"] = [
                *cast(list[str], manifest_body["limitations"]),
                "this composite is technical-only and does not advertise event capability",
            ]
        digest = hashlib.sha256(_canonical_json_bytes(manifest_body)).hexdigest()
        snapshot_id = f"composite:{digest}"
        manifest = {"snapshotId": snapshot_id, **manifest_body}
        _write_json(temporary / COMPOSITE_MANIFEST_FILENAME, manifest)

        final_path = destination_root / digest
        if final_path.exists():
            existing = final_path / COMPOSITE_MANIFEST_FILENAME
            if not existing.is_file() or json.loads(existing.read_text()) != manifest:
                raise CompositeSnapshotError(
                    f"composite snapshot destination contains different content: {final_path}"
                )
            _validate_composite(final_path, manifest)
            shutil.rmtree(temporary)
        else:
            temporary.replace(final_path)
        return CompositeSnapshotResult(
            snapshot_id=snapshot_id,
            path=final_path,
            manifest=manifest,
        )
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def _load_source_manifest(
    root: Path,
    *,
    filename: str,
    schema_version: str,
    snapshot_prefix: str,
) -> dict[str, object]:
    path = root / filename
    try:
        decoded: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CompositeSnapshotError(f"cannot read source snapshot manifest: {path}") from exc
    if not isinstance(decoded, dict):
        raise CompositeSnapshotError(f"source snapshot manifest must be an object: {path}")
    manifest = cast(dict[str, object], decoded)
    if manifest.get("schemaVersion") != schema_version:
        raise CompositeSnapshotError(f"source snapshot schema mismatch: {path}")
    snapshot_id = manifest.get("snapshotId")
    body = dict(manifest)
    body.pop("snapshotId", None)
    digest = hashlib.sha256(_canonical_json_bytes(body)).hexdigest()
    if snapshot_id != f"{snapshot_prefix}:{digest}" or root.name != digest:
        raise CompositeSnapshotError(f"source snapshot identity mismatch: {path}")
    for relative, metadata in _manifest_files(manifest).items():
        source = _safe_source_path(root, relative)
        typed_metadata = (
            cast(Mapping[object, object], metadata) if isinstance(metadata, Mapping) else None
        )
        if (
            typed_metadata is None
            or not source.is_file()
            or source.stat().st_size != typed_metadata.get("bytes")
            or _sha256_file(source) != typed_metadata.get("sha256")
        ):
            raise CompositeSnapshotError(f"source snapshot file mismatch: {relative}")
    return manifest


def _load_daily_source_manifest(root: Path) -> dict[str, object]:
    """Load one allowlisted Choice-v1 or provider-neutral technical manifest."""

    path = root / COMPOSITE_MANIFEST_FILENAME
    try:
        decoded: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CompositeSnapshotError(f"cannot read source snapshot manifest: {path}") from exc
    if not isinstance(decoded, dict):
        raise CompositeSnapshotError(f"source snapshot manifest must be an object: {path}")
    preview = cast(dict[str, object], decoded)
    schema_version = preview.get("schemaVersion")
    if schema_version == CHOICE_SNAPSHOT_SCHEMA_VERSION:
        snapshot_prefix = "choice"
    elif schema_version == TECHNICAL_SNAPSHOT_SCHEMA_VERSION:
        snapshot_prefix = "technical"
    else:
        raise CompositeSnapshotError(f"source snapshot schema mismatch: {path}")
    return _load_source_manifest(
        root,
        filename=COMPOSITE_MANIFEST_FILENAME,
        schema_version=cast(str, schema_version),
        snapshot_prefix=snapshot_prefix,
    )


def _validate_event_manifest(manifest: Mapping[str, object]) -> bool:
    policy = _mapping(manifest, "policy")
    row_counts = _mapping(manifest, "rowCounts")
    if not policy.get("strictDemoSecondsOnly"):
        raise CompositeSnapshotError("event snapshot is not strict-Demo validated")
    requirement = manifest.get("eventRequirement")
    typed_requirement = (
        cast(Mapping[str, object], requirement) if isinstance(requirement, Mapping) else None
    )
    if typed_requirement is not None and typed_requirement.get("mode") == NO_EVENT_REQUIRED_MODE:
        _validate_no_event_required_manifest(
            requirement=typed_requirement,
            coverage=_mapping(manifest, "acquisitionCoverage"),
            row_counts=row_counts,
        )
        return False
    canonical_events = row_counts.get("canonicalEvents")
    observations = row_counts.get("observations")
    quarantined = row_counts.get("quarantinedEvents")
    if (
        not isinstance(canonical_events, int)
        or isinstance(canonical_events, bool)
        or canonical_events < 0
        or not isinstance(observations, int)
        or isinstance(observations, bool)
        or observations < 0
        or quarantined != 0
    ):
        raise CompositeSnapshotError(
            "event snapshot row counts are invalid or contain quarantined rows"
        )
    coverage = _mapping(manifest, "acquisitionCoverage")
    if (
        coverage.get("status") != "complete"
        or coverage.get("querySucceeded") is not True
        or coverage.get("rowCount") != observations
        or coverage.get("zeroResult") is not (observations == 0)
    ):
        raise CompositeSnapshotError("event snapshot lacks complete acquisition coverage")
    required_providers = _string_sequence(coverage, "requiredProviders")
    if "eastmoney" not in required_providers:
        raise CompositeSnapshotError("event snapshot did not require the Eastmoney primary")
    source_statuses = coverage.get("sourceStatuses")
    if not isinstance(source_statuses, list):
        raise CompositeSnapshotError("event acquisition sourceStatuses must be a list")
    eastmoney_succeeded = any(
        _is_successful_eastmoney_status(item) for item in cast(list[object], source_statuses)
    )
    if not eastmoney_succeeded:
        raise CompositeSnapshotError("event snapshot primary Eastmoney query did not succeed")
    _coverage_date(coverage, "start")
    _coverage_date(coverage, "end")
    requested_codes = _string_sequence(coverage, "requestedEventCodes")
    if not requested_codes:
        raise CompositeSnapshotError("event snapshot requestedEventCodes cannot be empty")
    coverage_by_code = _mapping(coverage, "coverageByEventCode")
    if set(coverage_by_code) != set(requested_codes):
        raise CompositeSnapshotError(
            "event snapshot code-level coverage does not match requested event codes"
        )
    for code, raw_lane in coverage_by_code.items():
        if not isinstance(raw_lane, Mapping):
            raise CompositeSnapshotError(f"event snapshot lane coverage is invalid: {code}")
        lane = cast(Mapping[object, object], raw_lane)
        if lane.get("status") != "complete" or lane.get("querySucceeded") is not True:
            raise CompositeSnapshotError(
                f"event snapshot lacks complete interval coverage for {code}"
            )
    return True


def _validate_event_document_text_artifacts(event_root: Path) -> None:
    """Reject malformed document artifacts before a Composite can be published."""

    try:
        event_rows = cast(
            list[dict[str, object]],
            _PARQUET.read_table(
                event_root / EVENTS_FILENAME,
                columns=[
                    "stock_code",
                    "provider",
                    "source_event_id",
                    "revision_no",
                    "event_code",
                    "attributes_json",
                ],
            ).to_pylist(),
        )
        observation_rows = cast(
            list[dict[str, object]],
            _PARQUET.read_table(
                event_root / OBSERVATIONS_FILENAME,
                columns=[
                    "stock_code",
                    "provider",
                    "source_event_id",
                    "revision_no",
                    "document_sha256",
                ],
            ).to_pylist(),
        )
    except Exception as exc:
        raise CompositeSnapshotError(
            "event document text contract cannot read frozen event rows"
        ) from exc

    source_hashes: dict[tuple[object, object, object, object], object] = {}
    for observation in observation_rows:
        key = (
            observation.get("stock_code"),
            observation.get("provider"),
            observation.get("source_event_id"),
            observation.get("revision_no"),
        )
        if key in source_hashes:
            raise CompositeSnapshotError(
                "event document text contract found duplicate source observations"
            )
        source_hashes[key] = observation.get("document_sha256")

    for event in event_rows:
        raw_attributes = event.get("attributes_json")
        if not isinstance(raw_attributes, str):
            raise CompositeSnapshotError("event attributes_json must be frozen JSON text")
        try:
            decoded: object = json.loads(raw_attributes)
        except json.JSONDecodeError as exc:
            raise CompositeSnapshotError("event attributes_json is not valid JSON") from exc
        if not isinstance(decoded, dict):
            raise CompositeSnapshotError("event attributes_json must decode to an object")
        raw_mapping = cast(dict[object, object], decoded)
        if any(not isinstance(key, str) for key in raw_mapping):
            raise CompositeSnapshotError("event document attribute names must be text")
        attributes = cast(dict[str, object], raw_mapping)
        # Periodic-report identity metadata (for example
        # ``document_text_scope``) describes which source document would be
        # eligible for extraction.  It is not itself a frozen text artifact.
        # Only an actual text payload advertises the stronger capability.
        if "document_text" not in attributes:
            continue
        document_attributes = {
            name: value
            for name, value in attributes.items()
            if name.startswith("document_text") or name == "document_version_role"
        }
        if any(
            value is not None and not isinstance(value, str | int | bool)
            for value in document_attributes.values()
        ):
            raise CompositeSnapshotError("event document attributes must be scalar JSON values")

        key = (
            event.get("stock_code"),
            event.get("provider"),
            event.get("source_event_id"),
            event.get("revision_no"),
        )
        expected_source_sha256 = source_hashes.get(key)
        event_code = event.get("event_code")
        if not isinstance(event_code, str) or not event_code:
            event_code = "unknown_event"
        try:
            validate_document_text_artifact(
                cast(Mapping[str, str | int | bool | None], document_attributes),
                expected_source_sha256=(
                    expected_source_sha256 if isinstance(expected_source_sha256, str) else None
                ),
            )
        except DocumentMetricError as exc:
            raise CompositeSnapshotError(
                f"event document text artifact is invalid for {event_code}: {exc}"
            ) from exc
        if not isinstance(expected_source_sha256, str):
            raise CompositeSnapshotError(
                f"event document text artifact lacks a frozen source document for {event_code}"
            )


def _validate_no_event_required_manifest(
    *,
    requirement: Mapping[str, object],
    coverage: Mapping[str, object],
    row_counts: Mapping[str, object],
) -> None:
    if (
        requirement.get("eventDataAvailable") is not False
        or requirement.get("acquisitionPerformed") is not False
        or row_counts.get("canonicalEvents") != 0
        or row_counts.get("observations") != 0
        or row_counts.get("quarantinedEvents") != 0
    ):
        raise CompositeSnapshotError(
            "no-event-required snapshot contains event data or claims acquisition"
        )
    empty_mapping_fields = (
        "providerQueryEvidence",
        "coverageByEventCode",
    )
    empty_list_fields = (
        "requestedEventCodes",
        "requiredProviders",
        "sourceStatuses",
    )
    if (
        coverage.get("mode") != NO_EVENT_REQUIRED_MODE
        or coverage.get("status") != "not_required"
        or coverage.get("acquisitionPerformed") is not False
        or coverage.get("queriedAt") is not None
        or coverage.get("querySucceeded") is not False
        or coverage.get("rowCount") != 0
        or coverage.get("zeroResult") is not False
        or coverage.get("emptyByConstruction") is not True
        or any(coverage.get(name) != {} for name in empty_mapping_fields)
        or any(coverage.get(name) != [] for name in empty_list_fields)
    ):
        raise CompositeSnapshotError("no-event-required acquisition declaration is invalid")
    supplemental = _mapping(coverage, "supplementalEvidence")
    if supplemental != {"rowCount": 0, "observationsByProvider": {}}:
        raise CompositeSnapshotError("no-event-required supplemental evidence must be empty")
    instrument_id = coverage.get("instrumentId")
    if not isinstance(instrument_id, str) or not instrument_id:
        raise CompositeSnapshotError("no-event-required instrumentId is invalid")
    start = _coverage_date(coverage, "start")
    end = _coverage_date(coverage, "end")
    if start > end:
        raise CompositeSnapshotError("no-event-required coverage range is inverted")
    declared_at = coverage.get("declaredAt")
    if not isinstance(declared_at, str):
        raise CompositeSnapshotError("no-event-required declaredAt is invalid")
    try:
        parsed_declared_at = datetime.fromisoformat(declared_at)
    except ValueError as exc:
        raise CompositeSnapshotError("no-event-required declaredAt is invalid") from exc
    if parsed_declared_at.tzinfo is None or parsed_declared_at.utcoffset() is None:
        raise CompositeSnapshotError("no-event-required declaredAt needs a timezone")
    declaration: dict[str, object] = {
        "mode": NO_EVENT_REQUIRED_MODE,
        "instrumentId": instrument_id,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "declaredAt": declared_at,
        "requestedEventCodes": [],
    }
    empty_evidence: dict[str, object] = {
        "sourceStatuses": [],
        "providerQueryEvidence": {},
        "supplementalEvidence": {
            "rowCount": 0,
            "observationsByProvider": {},
        },
        "coverageByEventCode": {},
    }
    audit = _mapping(coverage, "auditSummary")
    if (
        audit.get("requestSha256") != hashlib.sha256(_canonical_json_bytes(declaration)).hexdigest()
        or audit.get("sourceStatusSha256")
        != hashlib.sha256(_canonical_json_bytes(empty_evidence)).hexdigest()
        or audit.get("requiredProvidersSatisfied") != []
        or audit.get("optionalUnavailableProviders") != []
        or audit.get("successfulProviders") != []
    ):
        raise CompositeSnapshotError("no-event-required audit declaration is inconsistent")


def _validate_choice_event_alignment(
    choice_manifest: Mapping[str, object],
    event_manifest: Mapping[str, object],
) -> None:
    coverage = _mapping(event_manifest, "acquisitionCoverage")
    symbol = choice_manifest.get("symbol")
    if coverage.get("instrumentId") != symbol:
        raise CompositeSnapshotError(
            "event acquisition coverage instrument does not match the Choice snapshot"
        )
    requested_range = choice_manifest.get("requestedRange")
    if not isinstance(requested_range, list):
        raise CompositeSnapshotError("Choice requestedRange must contain two ISO dates")
    typed_range = cast(list[object], requested_range)
    if len(typed_range) != 2 or any(not isinstance(value, str) for value in typed_range):
        raise CompositeSnapshotError("Choice requestedRange must contain two ISO dates")
    try:
        choice_start, choice_end = (
            date.fromisoformat(cast(str, typed_range[0])),
            date.fromisoformat(cast(str, typed_range[1])),
        )
    except ValueError as exc:
        raise CompositeSnapshotError("Choice requestedRange contains an invalid date") from exc
    event_start = _coverage_date(coverage, "start")
    event_end = _coverage_date(coverage, "end")
    if event_start > choice_start or event_end < choice_end:
        raise CompositeSnapshotError(
            "event acquisition coverage does not span the Choice requested range"
        )


def _validate_choice_manifest(
    manifest: Mapping[str, object],
    *,
    choice_root: Path,
) -> None:
    capabilities = _mapping(manifest, "capabilities")
    coverage = _mapping(manifest, "corporateActionCoverage")
    if capabilities.get("corporateActions") != "validated_for_demo":
        raise CompositeSnapshotError("Choice snapshot lacks validated corporate-action capability")
    requested_range = manifest.get("requestedRange")
    if not isinstance(requested_range, list):
        raise CompositeSnapshotError("Choice requestedRange must contain two ISO dates")
    typed_range = cast(list[object], requested_range)
    if len(typed_range) != 2 or any(not isinstance(value, str) for value in typed_range):
        raise CompositeSnapshotError("Choice requestedRange must contain two ISO dates")
    try:
        start, end = (
            date.fromisoformat(cast(str, typed_range[0])),
            date.fromisoformat(cast(str, typed_range[1])),
        )
    except ValueError as exc:
        raise CompositeSnapshotError("Choice requestedRange contains an invalid date") from exc

    action_path = choice_root / CORPORATE_ACTION_FILENAME
    try:
        values = cast(
            list[object],
            _PARQUET.read_table(action_path, columns=["action_type"])
            .column("action_type")
            .to_pylist(),
        )
    except Exception as exc:
        raise CompositeSnapshotError(
            "Choice corporate-action rows cannot be reconciled with coverage evidence"
        ) from exc
    counts = {category: 0 for category in STRICT_CORPORATE_ACTION_CATEGORIES}
    for value in values:
        if not isinstance(value, str) or value not in counts:
            raise CompositeSnapshotError("Choice corporate-action rows contain an unknown type")
        counts[value] += 1
    row_counts = _mapping(manifest, "rowCounts")
    if row_counts.get("corporateActions") != len(values):
        raise CompositeSnapshotError("Choice corporate-action row count is inconsistent")
    try:
        normalized_coverage = validate_corporate_action_coverage_evidence(
            coverage,
            start=start,
            end=end,
            action_type_counts=counts,
        )
    except ChoiceSnapshotError as exc:
        raise CompositeSnapshotError(
            "Choice snapshot lacks complete corporate-action coverage evidence"
        ) from exc
    if normalized_coverage != dict(coverage):
        raise CompositeSnapshotError(
            "Choice corporate-action coverage is not in canonical validated form"
        )


def _copy_registered_file(
    source_root: Path,
    manifest: Mapping[str, object],
    relative: str,
    target: Path,
) -> None:
    metadata = _manifest_files(manifest).get(relative)
    if not isinstance(metadata, Mapping):
        raise CompositeSnapshotError(f"source manifest did not register {relative}")
    source = _safe_source_path(source_root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _safe_source_path(root: Path, raw_relative: str) -> Path:
    relative = PurePosixPath(raw_relative)
    if (
        relative.is_absolute()
        or relative.as_posix() != raw_relative
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise CompositeSnapshotError(f"unsafe source manifest path: {raw_relative}")
    source = root.joinpath(*relative.parts).resolve()
    if not source.is_relative_to(root):
        raise CompositeSnapshotError(f"source manifest escaped snapshot root: {raw_relative}")
    return source


def _manifest_files(manifest: Mapping[str, object]) -> Mapping[str, object]:
    value = manifest.get("files")
    if not isinstance(value, Mapping):
        raise CompositeSnapshotError("source snapshot files must be an object")
    raw = cast(Mapping[object, object], value)
    if any(not isinstance(key, str) for key in raw):
        raise CompositeSnapshotError("source snapshot file names must be strings")
    return cast(Mapping[str, object], raw)


def _mapping(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    nested = value.get(key)
    if not isinstance(nested, Mapping):
        raise CompositeSnapshotError(f"snapshot manifest field {key!r} must be an object")
    raw = cast(Mapping[object, object], nested)
    if any(not isinstance(name, str) for name in raw):
        raise CompositeSnapshotError(f"snapshot manifest field {key!r} has invalid keys")
    return cast(Mapping[str, object], raw)


def _coverage_date(coverage: Mapping[str, object], key: str) -> date:
    value = coverage.get(key)
    if not isinstance(value, str):
        raise CompositeSnapshotError(f"event acquisition coverage {key} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CompositeSnapshotError(
            f"event acquisition coverage {key} must be an ISO date"
        ) from exc


def _string_sequence(coverage: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = coverage.get(key)
    if not isinstance(value, list):
        raise CompositeSnapshotError(f"event acquisition coverage {key} must be a text list")
    typed_values = cast(list[object], value)
    if any(not isinstance(item, str) for item in typed_values):
        raise CompositeSnapshotError(f"event acquisition coverage {key} must be a text list")
    return tuple(cast(list[str], typed_values))


def _is_successful_eastmoney_status(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    status = cast(Mapping[object, object], value)
    return (
        status.get("provider") == "eastmoney"
        and status.get("required") is True
        and status.get("querySucceeded") is True
        and status.get("status") in {"available", "empty"}
    )


def _validate_composite(path: Path, manifest: Mapping[str, object]) -> None:
    snapshot_id = manifest.get("snapshotId")
    body = {key: value for key, value in manifest.items() if key != "snapshotId"}
    digest = hashlib.sha256(_canonical_json_bytes(body)).hexdigest()
    if snapshot_id != f"composite:{digest}" or path.name != digest:
        raise CompositeSnapshotError("published composite identity does not match its manifest")
    for relative, metadata in _manifest_files(manifest).items():
        source = _safe_source_path(path, relative)
        typed_metadata = (
            cast(Mapping[object, object], metadata) if isinstance(metadata, Mapping) else None
        )
        if (
            typed_metadata is None
            or not source.is_file()
            or source.stat().st_size != typed_metadata.get("bytes")
            or _sha256_file(source) != typed_metadata.get("sha256")
        ):
            raise CompositeSnapshotError(f"published composite file mismatch: {relative}")


def _canonical_json_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _write_json(path: Path, payload: object) -> None:
    path.write_bytes(_canonical_json_bytes(payload) + b"\n")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()
