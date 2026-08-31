"""Publish normalized multi-provider events as immutable replay snapshots."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pyarrow.parquet as pq

from ashare_lab.adapters.event_sources.eastmoney import (
    ANNOUNCEMENT_CLASSIFIER_VERSION,
    eastmoney_event_coverage_contract,
    eastmoney_preparable_event_codes,
)
from ashare_lab.domain.events.fusion import (
    EVENT_LANE_SOURCE_POLICIES,
    LICENSE_APPROVAL_EVENT_CODE,
    PROVIDER_PRIORITY,
    EventFusionResult,
    EventProvenance,
    FusedEvent,
    event_source_policy,
    fuse_event_observations,
)
from ashare_lab.domain.events.observations import EventObservation
from ashare_lab.domain.market_data import TimeQuality
from ashare_lab.domain.shared import InstrumentId

EVENT_SNAPSHOT_SCHEMA_VERSION = "ashare-lab.event-snapshot.v2"
NO_EVENT_REQUIRED_MODE = "no_event_required"
_EASTMONEY_QUERY_EVIDENCE_SCHEMA = "ashare-lab.eastmoney-announcement-query-evidence.v2"
_EASTMONEY_ANNOUNCEMENT_LIST_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
EVENTS_FILENAME = "events.parquet"
OBSERVATIONS_FILENAME = "event_observations.parquet"
QUARANTINE_FILENAME = "event_quarantine.json"
MANIFEST_FILENAME = "event_snapshot_manifest.json"
_HASH_CHUNK_BYTES = 1024 * 1024
_WEB_FACT_EVENT_CODES = frozenset({LICENSE_APPROVAL_EVENT_CODE})
_WEB_FACT_PROVENANCE_ATTRIBUTES = frozenset(
    {
        "canonical_url",
        "classifier_id",
        "classifier_version",
        "entity_match_method",
        "extractor_id",
        "extractor_version",
        "mapping_confidence",
        "publisher_authority",
        "publisher_name",
        "publisher_type",
        "review_status",
        "revision_id",
        "revision_type",
        "timestamp_precision",
        "timing_basis",
    }
)
_ARROW: Any = pa
_PARQUET: Any = pq


class EventSnapshotError(RuntimeError):
    """Provider observations cannot be promoted into a replayable event snapshot."""


@dataclass(frozen=True, slots=True)
class EventSnapshotResult:
    snapshot_id: str
    path: Path
    manifest: Mapping[str, object]
    fusion: EventFusionResult


def build_event_snapshot(
    *,
    observations: Sequence[EventObservation],
    acquisition_coverage: Mapping[str, object] | None = None,
    output_root: Path,
    captured_at: datetime,
    strict_demo: bool = True,
) -> EventSnapshotResult:
    """Validate, fuse and atomically publish one multi-source event snapshot.

    ``retrieved_at`` remains the real collection time in the observation file.
    Canonical ``replay_available_at`` is derived only from the selected fixed-
    priority provider's historical source/vendor clocks.
    """

    _require_aware(captured_at, "captured_at")
    ordered = tuple(sorted(observations, key=_observation_sort_key))
    if any(item.retrieved_at > captured_at for item in ordered):
        raise EventSnapshotError("captured_at cannot precede an observation retrieved_at")
    validated_acquisition_coverage = _validate_acquisition_coverage(
        acquisition_coverage,
        observations=ordered,
        captured_at=captured_at,
        strict_demo=strict_demo,
    )

    fusion = fuse_event_observations(ordered)
    if strict_demo:
        _validate_strict_demo(fusion, ordered)

    return _publish_event_snapshot(
        ordered=ordered,
        fusion=fusion,
        acquisition_coverage=validated_acquisition_coverage,
        output_root=output_root,
        captured_at=captured_at,
        strict_demo=strict_demo,
    )


def build_no_event_required_snapshot(
    *,
    instrument_id: InstrumentId,
    start: date,
    end: date,
    output_root: Path,
    captured_at: datetime,
) -> EventSnapshotResult:
    """Publish an explicit empty Event v2 input for a technical-only run.

    This is not a zero-result event query.  It records that the submitted
    strategy requested no event lanes, no event provider was contacted, and
    event capability must therefore not be advertised by the composite.
    """

    _require_aware(captured_at, "captured_at")
    if start > end:
        raise EventSnapshotError("no-event-required range start must not exceed end")
    fusion = fuse_event_observations(())
    coverage = _no_event_required_coverage(
        instrument_id=instrument_id.value,
        start=start,
        end=end,
        declared_at=captured_at,
    )
    return _publish_event_snapshot(
        ordered=(),
        fusion=fusion,
        acquisition_coverage=coverage,
        output_root=output_root,
        captured_at=captured_at,
        strict_demo=True,
        event_requirement={
            "mode": NO_EVENT_REQUIRED_MODE,
            "eventDataAvailable": False,
            "acquisitionPerformed": False,
        },
    )


def _publish_event_snapshot(
    *,
    ordered: Sequence[EventObservation],
    fusion: EventFusionResult,
    acquisition_coverage: Mapping[str, object],
    output_root: Path,
    captured_at: datetime,
    strict_demo: bool,
    event_requirement: Mapping[str, object] | None = None,
) -> EventSnapshotResult:
    destination_root = output_root.expanduser().resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".event-snapshot-", dir=destination_root))
    try:
        _write_events(temporary / EVENTS_FILENAME, fusion.events)
        _write_observations(temporary / OBSERVATIONS_FILENAME, ordered)
        _write_json(temporary / QUARANTINE_FILENAME, _quarantine_payload(fusion))

        data_paths = (
            temporary / EVENTS_FILENAME,
            temporary / OBSERVATIONS_FILENAME,
            temporary / QUARANTINE_FILENAME,
        )
        files = {
            path.name: {"bytes": path.stat().st_size, "sha256": _sha256_file(path)}
            for path in data_paths
        }
        provider_counts = Counter(item.provider for item in ordered)
        selected_counts = Counter(item.selected_provider for item in fusion.events)
        local_times = [
            value for item in fusion.events if (value := item.market_available_at) is not None
        ]
        manifest_body: dict[str, object] = {
            "schemaVersion": EVENT_SNAPSHOT_SCHEMA_VERSION,
            "capturedAt": captured_at.astimezone(UTC).isoformat(),
            "policy": {
                "providerPriority": list(PROVIDER_PRIORITY),
                "eventLanePolicies": {
                    event_code: {
                        "lane": policy.lane,
                        "evidencePreference": list(policy.evidence_preference),
                        "providerPriority": list(policy.provider_priority),
                    }
                    for event_code, policy in sorted(EVENT_LANE_SOURCE_POLICIES.items())
                },
                "selection": "fixed_priority_never_earliest_timestamp",
                "marketAvailability": "max_selected_source_and_vendor_time",
                "retrievalClockUsedForReplay": False,
                "strictDemoSecondsOnly": strict_demo,
                "timezone": "Asia/Shanghai",
                "validationStatusesAllowed": ["validated"],
                "timeQualitiesAllowed": [
                    TimeQuality.EXACT.value,
                    TimeQuality.VENDOR_OBSERVED.value,
                ],
            },
            "rowCounts": {
                "canonicalEvents": len(fusion.events),
                "observations": len(ordered),
                "quarantinedEvents": len(fusion.quarantined),
            },
            "acquisitionCoverage": dict(acquisition_coverage),
            "coverage": {
                "status": fusion.coverage_status.value,
                "observationsByProvider": dict(sorted(provider_counts.items())),
                "selectedEventsByProvider": dict(sorted(selected_counts.items())),
                "firstMarketAvailableAt": (min(local_times).isoformat() if local_times else None),
                "lastMarketAvailableAt": (max(local_times).isoformat() if local_times else None),
            },
            "files": files,
            "limitations": [
                "public or account-scoped vendor APIs have no shared service-level agreement",
                "fixed source priority is used; timestamps never choose the winning provider",
                "date-only, unverified and conflicting observations are not tradable",
                "document bodies are represented by hashes when the provider exposes them",
                "each Eastmoney announcement-list page is frozen by raw-response hash; "
                "the response bytes themselves are not archived in this local snapshot",
                "deterministic title classification proves only the frozen classifier contract; "
                "ambiguous, revised, withdrawn, or body-only semantics remain unclassified",
                "individual supplemental web observations do not prove interval completeness",
            ],
        }
        if event_requirement is not None:
            manifest_body["eventRequirement"] = dict(event_requirement)
            manifest_body["limitations"] = [
                *cast(list[str], manifest_body["limitations"]),
                "no event provider was queried because the frozen strategy requires no events",
            ]

        digest = hashlib.sha256(_canonical_json_bytes(manifest_body)).hexdigest()
        snapshot_id = f"events:{digest}"
        manifest = {"snapshotId": snapshot_id, **manifest_body}
        _write_json(temporary / MANIFEST_FILENAME, manifest)

        final_path = destination_root / digest
        if final_path.exists():
            existing_manifest = final_path / MANIFEST_FILENAME
            if (
                not existing_manifest.is_file()
                or json.loads(existing_manifest.read_text(encoding="utf-8")) != manifest
            ):
                raise EventSnapshotError(
                    f"event snapshot destination contains different content: {final_path}"
                )
            _validate_published_snapshot(final_path, manifest)
            shutil.rmtree(temporary)
        else:
            temporary.replace(final_path)
        return EventSnapshotResult(
            snapshot_id=snapshot_id,
            path=final_path,
            manifest=manifest,
            fusion=fusion,
        )
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def _no_event_required_coverage(
    *,
    instrument_id: str,
    start: date,
    end: date,
    declared_at: datetime,
) -> dict[str, object]:
    declaration: dict[str, object] = {
        "mode": NO_EVENT_REQUIRED_MODE,
        "instrumentId": instrument_id,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "declaredAt": declared_at.astimezone(UTC).isoformat(),
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
    return {
        **declaration,
        "status": "not_required",
        "acquisitionPerformed": False,
        "queriedAt": None,
        "requiredProviders": [],
        "querySucceeded": False,
        "rowCount": 0,
        "zeroResult": False,
        "emptyByConstruction": True,
        **empty_evidence,
        "auditSummary": {
            "requestSha256": hashlib.sha256(_canonical_json_bytes(declaration)).hexdigest(),
            "sourceStatusSha256": hashlib.sha256(_canonical_json_bytes(empty_evidence)).hexdigest(),
            "requiredProvidersSatisfied": [],
            "optionalUnavailableProviders": [],
            "successfulProviders": [],
        },
    }


def _validate_strict_demo(
    fusion: EventFusionResult,
    observations: Sequence[EventObservation],
) -> None:
    if observations and not fusion.events:
        raise EventSnapshotError(
            "strict Demo observations produced no validated second-level canonical event"
        )
    if fusion.quarantined:
        raise EventSnapshotError(
            "strict Demo refuses quarantined or ambiguous events; review the source conflicts"
        )
    for event in fusion.events:
        selected = next(item for item in event.provenance if item.selected)
        if (
            event.market_available_at is None
            or event.market_available_at.microsecond != 0
            or event.time_quality not in {TimeQuality.EXACT, TimeQuality.VENDOR_OBSERVED}
            or selected.validation_status != "validated"
            or selected.raw_response_sha256 is None
            or selected.document_url is None
        ):
            raise EventSnapshotError(
                "strict Demo event is missing validated second-level time or source provenance"
            )
        if event.event_code in _WEB_FACT_EVENT_CODES:
            selected_observation = next(
                item
                for item in observations
                if item.instrument_id == event.instrument_id
                and item.event_code == event.event_code
                and item.provider == selected.provider
                and item.provider_event_id == selected.provider_event_id
                and item.revision_no == selected.revision_no
            )
            _validate_web_fact_provenance(event, selected, selected_observation)


def _validate_acquisition_coverage(
    raw_coverage: Mapping[str, object] | None,
    *,
    observations: Sequence[EventObservation],
    captured_at: datetime,
    strict_demo: bool,
) -> dict[str, object]:
    if raw_coverage is None:
        if strict_demo:
            raise EventSnapshotError(
                "strict Demo requires complete event acquisition coverage evidence"
            )
        instruments = sorted({item.instrument_id.value for item in observations})
        return {
            "status": "unproven",
            "instrumentId": instruments[0] if len(instruments) == 1 else None,
            "start": None,
            "end": None,
            "queriedAt": None,
            "requestedEventCodes": sorted({item.event_code for item in observations}),
            "requiredProviders": [PROVIDER_PRIORITY[0]],
            "querySucceeded": False,
            "rowCount": len(observations),
            "zeroResult": not observations,
            "sourceStatuses": [],
            "providerQueryEvidence": {},
            "coverageByEventCode": {},
            "supplementalEvidence": {
                "rowCount": len(observations),
                "observationsByProvider": dict(
                    sorted(Counter(item.provider for item in observations).items())
                ),
            },
            "auditSummary": {
                "requestSha256": None,
                "sourceStatusSha256": None,
                "requiredProvidersSatisfied": [],
                "optionalUnavailableProviders": [],
                "successfulProviders": [],
            },
        }

    instrument_id = raw_coverage.get("instrumentId")
    if not isinstance(instrument_id, str) or not instrument_id.strip():
        raise EventSnapshotError("event acquisition coverage instrumentId is required")
    start = _coverage_date(raw_coverage.get("start"), "start")
    end = _coverage_date(raw_coverage.get("end"), "end")
    if start > end:
        raise EventSnapshotError("event acquisition coverage start must not exceed end")
    queried_at = _coverage_datetime(raw_coverage.get("queriedAt"), "queriedAt")
    if queried_at > captured_at:
        raise EventSnapshotError("event acquisition queriedAt cannot follow captured_at")

    requested_codes = _string_list(
        raw_coverage.get("requestedEventCodes"),
        "requestedEventCodes",
    )
    if not requested_codes or any(not item.startswith("event.") for item in requested_codes):
        raise EventSnapshotError(
            "event acquisition coverage requires canonical requested event codes"
        )
    required_providers = _string_list(
        raw_coverage.get("requiredProviders"),
        "requiredProviders",
    )
    if not required_providers:
        raise EventSnapshotError("event acquisition coverage requires a provider")

    raw_statuses = raw_coverage.get("sourceStatuses")
    if not isinstance(raw_statuses, list):
        raise EventSnapshotError("event acquisition coverage sourceStatuses must be a list")
    source_statuses: list[dict[str, object]] = []
    source_counts: Counter[str] = Counter()
    seen_providers: set[str] = set()
    successful_statuses = frozenset({"available", "empty"})
    allowed_statuses = successful_statuses | {"failed", "not_configured"}
    for raw_status in cast(list[object], raw_statuses):
        if not isinstance(raw_status, Mapping):
            raise EventSnapshotError("event acquisition source status must be an object")
        status = cast(Mapping[object, object], raw_status)
        provider = status.get("provider")
        state = status.get("status")
        row_count = status.get("rowCount")
        if (
            not isinstance(provider, str)
            or not provider
            or provider in seen_providers
            or state not in allowed_statuses
            or not isinstance(row_count, int)
            or isinstance(row_count, bool)
            or row_count < 0
        ):
            raise EventSnapshotError("event acquisition source status is invalid")
        seen_providers.add(provider)
        query_succeeded = state in successful_statuses
        if status.get("querySucceeded") is not query_succeeded:
            raise EventSnapshotError("event acquisition source query status is inconsistent")
        is_required = provider in required_providers
        if status.get("required") is not is_required:
            raise EventSnapshotError("event acquisition required-provider flag is inconsistent")
        error_type = status.get("errorType")
        error_hash = status.get("errorMessageSha256")
        if error_type is not None and not isinstance(error_type, str):
            raise EventSnapshotError("event acquisition errorType must be text or null")
        if error_hash is not None and not _is_sha256(error_hash):
            raise EventSnapshotError("event acquisition error message hash is invalid")
        source_counts[provider] += row_count
        source_statuses.append(
            {
                "provider": provider,
                "required": is_required,
                "status": state,
                "querySucceeded": query_succeeded,
                "rowCount": row_count,
                "errorType": error_type,
                "errorMessageSha256": error_hash,
            }
        )

    missing_required = sorted(set(required_providers) - seen_providers)
    if missing_required:
        raise EventSnapshotError(
            "event acquisition coverage is missing required source status: "
            + ", ".join(missing_required)
        )
    required_satisfied: list[str] = sorted(
        cast(str, item["provider"])
        for item in source_statuses
        if item["required"] and item["querySucceeded"]
    )
    primary_query_succeeded = set(required_satisfied) == set(required_providers)
    provider_query_evidence = _validate_provider_query_evidence(
        raw_coverage.get("providerQueryEvidence"),
        instrument_id=instrument_id,
        start=start,
        end=end,
        requested_codes=requested_codes,
        source_statuses=source_statuses,
    )

    supplemental = raw_coverage.get("supplementalEvidence")
    if not isinstance(supplemental, Mapping):
        raise EventSnapshotError("event acquisition supplementalEvidence must be an object")
    supplemental_map = cast(Mapping[object, object], supplemental)
    supplemental_row_count = supplemental_map.get("rowCount")
    if (
        not isinstance(supplemental_row_count, int)
        or isinstance(supplemental_row_count, bool)
        or supplemental_row_count < 0
    ):
        raise EventSnapshotError("event acquisition supplemental rowCount is invalid")
    raw_supplemental_counts = supplemental_map.get("observationsByProvider")
    if not isinstance(raw_supplemental_counts, Mapping):
        raise EventSnapshotError("event acquisition supplemental provider counts must be an object")
    supplemental_counts: Counter[str] = Counter()
    for provider, count in cast(Mapping[object, object], raw_supplemental_counts).items():
        if (
            not isinstance(provider, str)
            or not provider
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count < 1
        ):
            raise EventSnapshotError("event acquisition supplemental provider count is invalid")
        supplemental_counts[provider] = count
    if sum(supplemental_counts.values()) != supplemental_row_count:
        raise EventSnapshotError("event acquisition supplemental row counts do not reconcile")

    row_count = raw_coverage.get("rowCount")
    if (
        not isinstance(row_count, int)
        or isinstance(row_count, bool)
        or row_count < 0
        or row_count != len(observations)
        or row_count != sum(source_counts.values()) + supplemental_row_count
        or raw_coverage.get("zeroResult") is not (row_count == 0)
    ):
        raise EventSnapshotError("event acquisition row counts do not reconcile")

    actual_counts = Counter(item.provider for item in observations)
    if actual_counts != source_counts + supplemental_counts:
        raise EventSnapshotError("event acquisition provider counts do not match observations")
    if any(item.instrument_id.value != instrument_id for item in observations):
        raise EventSnapshotError("event observation is outside acquisition instrument coverage")
    if any(item.event_code not in requested_codes for item in observations):
        raise EventSnapshotError("event observation is outside requested event-code coverage")
    if any(item.retrieved_at != queried_at for item in observations):
        raise EventSnapshotError("event observation retrieval clock differs from acquisition query")
    normalized_code_coverage, all_lanes_complete = _validate_coverage_by_event_code(
        raw_coverage.get("coverageByEventCode"),
        requested_codes=requested_codes,
        observations=observations,
        source_statuses=source_statuses,
        provider_query_evidence=provider_query_evidence,
    )
    query_succeeded = primary_query_succeeded and all_lanes_complete
    expected_status = "complete" if query_succeeded else "incomplete"
    if (
        raw_coverage.get("querySucceeded") is not query_succeeded
        or raw_coverage.get("status") != expected_status
    ):
        raise EventSnapshotError("event acquisition coverage completion status is inconsistent")
    if strict_demo and (
        not primary_query_succeeded or PROVIDER_PRIORITY[0] not in required_providers
    ):
        raise EventSnapshotError(
            "strict Demo requires a successful primary Eastmoney acquisition query"
        )
    if strict_demo and not all_lanes_complete:
        raise EventSnapshotError(
            "strict Demo requires complete interval coverage for every requested event code"
        )

    optional_unavailable = sorted(
        cast(str, item["provider"])
        for item in source_statuses
        if not item["required"] and not item["querySucceeded"]
    )
    successful_providers = sorted(
        cast(str, item["provider"]) for item in source_statuses if item["querySucceeded"]
    )
    normalized_source_statuses = sorted(
        source_statuses,
        key=lambda item: cast(str, item["provider"]),
    )
    normalized_supplemental = {
        "rowCount": supplemental_row_count,
        "observationsByProvider": dict(sorted(supplemental_counts.items())),
    }
    request_summary = {
        "instrumentId": instrument_id,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "requestedEventCodes": sorted(requested_codes),
        "queriedAt": queried_at.isoformat(),
    }
    request_sha256 = hashlib.sha256(_canonical_json_bytes(request_summary)).hexdigest()
    source_status_sha256 = hashlib.sha256(
        _canonical_json_bytes(
            {
                "sourceStatuses": normalized_source_statuses,
                "providerQueryEvidence": provider_query_evidence,
                "supplementalEvidence": normalized_supplemental,
                "coverageByEventCode": normalized_code_coverage,
            }
        )
    ).hexdigest()
    audit_summary = raw_coverage.get("auditSummary")
    if not isinstance(audit_summary, Mapping):
        raise EventSnapshotError("event acquisition auditSummary must be an object")
    typed_audit_summary = cast(Mapping[object, object], audit_summary)
    if (
        typed_audit_summary.get("requestSha256") != request_sha256
        or typed_audit_summary.get("sourceStatusSha256") != source_status_sha256
    ):
        raise EventSnapshotError("event acquisition audit summary hash is inconsistent")
    return {
        "status": expected_status,
        "instrumentId": instrument_id,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "queriedAt": queried_at.isoformat(),
        "requestedEventCodes": sorted(requested_codes),
        "requiredProviders": sorted(required_providers),
        "querySucceeded": query_succeeded,
        "rowCount": row_count,
        "zeroResult": row_count == 0,
        "sourceStatuses": normalized_source_statuses,
        "providerQueryEvidence": provider_query_evidence,
        "supplementalEvidence": normalized_supplemental,
        "coverageByEventCode": normalized_code_coverage,
        "auditSummary": {
            "requestSha256": request_sha256,
            "sourceStatusSha256": source_status_sha256,
            "requiredProvidersSatisfied": required_satisfied,
            "optionalUnavailableProviders": optional_unavailable,
            "successfulProviders": successful_providers,
        },
    }


def _validate_provider_query_evidence(
    raw_evidence: object,
    *,
    instrument_id: str,
    start: date,
    end: date,
    requested_codes: tuple[str, ...],
    source_statuses: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    if not isinstance(raw_evidence, Mapping):
        raise EventSnapshotError("event acquisition providerQueryEvidence must be an object")
    typed = cast(Mapping[object, object], raw_evidence)
    if any(not isinstance(provider, str) for provider in typed):
        raise EventSnapshotError("event provider query evidence has invalid provider keys")
    status_by_provider = {
        cast(str, item["provider"]): cast(str, item["status"]) for item in source_statuses
    }
    normalized: dict[str, object] = {}
    for provider, raw_provider in sorted(cast(Mapping[str, object], typed).items()):
        if provider not in status_by_provider:
            raise EventSnapshotError("event provider query evidence has no matching source status")
        if provider != PROVIDER_PRIORITY[0]:
            raise EventSnapshotError(
                "only Eastmoney interval-query evidence is supported in strict Demo"
            )
        normalized[provider] = _validate_eastmoney_query_evidence(
            raw_provider,
            instrument_id=instrument_id,
            start=start,
            end=end,
            requested_codes=requested_codes,
            source_status=status_by_provider[provider],
        )
    return normalized


def validate_persisted_acquisition_query_evidence(
    raw_coverage: Mapping[str, object],
) -> Mapping[str, object]:
    """Validate manifest-level interval, pagination and raw-response evidence.

    The replay loader and readiness probe call this public validator before a
    snapshot can be pinned.  Observation/row-count reconciliation remains in
    :func:`build_event_snapshot`; this function intentionally validates the
    evidence that is available from a persisted manifest alone.
    """

    instrument_id = raw_coverage.get("instrumentId")
    if not isinstance(instrument_id, str) or not instrument_id:
        raise EventSnapshotError("persisted event coverage instrumentId is required")
    start = _coverage_date(raw_coverage.get("start"), "start")
    end = _coverage_date(raw_coverage.get("end"), "end")
    if start > end:
        raise EventSnapshotError("persisted event coverage start must not exceed end")
    requested_codes = _string_list(
        raw_coverage.get("requestedEventCodes"),
        "requestedEventCodes",
    )
    if not requested_codes:
        raise EventSnapshotError("persisted event coverage needs requested event codes")
    raw_statuses = raw_coverage.get("sourceStatuses")
    if not isinstance(raw_statuses, list):
        raise EventSnapshotError("persisted event coverage sourceStatuses must be a list")
    source_statuses: list[Mapping[str, object]] = []
    for raw_status in cast(list[object], raw_statuses):
        if not isinstance(raw_status, Mapping):
            raise EventSnapshotError("persisted event source status must be an object")
        status = cast(Mapping[object, object], raw_status)
        provider = status.get("provider")
        state = status.get("status")
        if not isinstance(provider, str) or state not in {
            "available",
            "empty",
            "failed",
            "not_configured",
        }:
            raise EventSnapshotError("persisted event source status is invalid")
        source_statuses.append({"provider": provider, "status": state})
    normalized = _validate_provider_query_evidence(
        raw_coverage.get("providerQueryEvidence"),
        instrument_id=instrument_id,
        start=start,
        end=end,
        requested_codes=requested_codes,
        source_statuses=source_statuses,
    )
    for code in requested_codes:
        required_source = event_source_policy(code).provider_priority[0]
        code_evidence = _provider_code_query_evidence(
            normalized,
            required_source,
            code,
        )
        if code_evidence is None or code_evidence.get("querySucceeded") is not True:
            raise EventSnapshotError(
                f"persisted event coverage lacks interval query evidence for {code}"
            )
    return normalized


def _validate_eastmoney_query_evidence(
    raw_evidence: object,
    *,
    instrument_id: str,
    start: date,
    end: date,
    requested_codes: tuple[str, ...],
    source_status: str,
) -> dict[str, object]:
    if not isinstance(raw_evidence, Mapping):
        raise EventSnapshotError("Eastmoney query evidence must be an object")
    evidence = cast(Mapping[object, object], raw_evidence)
    if (
        evidence.get("schemaVersion") != _EASTMONEY_QUERY_EVIDENCE_SCHEMA
        or evidence.get("provider") != "eastmoney"
        or evidence.get("instrumentId") != instrument_id
        or evidence.get("start") != start.isoformat()
        or evidence.get("end") != end.isoformat()
        or evidence.get("endpoint") != _EASTMONEY_ANNOUNCEMENT_LIST_URL
        or evidence.get("querySucceeded") is not True
        or source_status not in {"available", "empty"}
    ):
        raise EventSnapshotError("Eastmoney interval-query evidence scope is inconsistent")

    request_parameters = evidence.get("requestParameters")
    if not isinstance(request_parameters, Mapping):
        raise EventSnapshotError("Eastmoney query requestParameters must be an object")
    normalized_request = dict(cast(Mapping[str, object], request_parameters))
    expected_stock = instrument_id.split(".", maxsplit=1)[0]
    if (
        normalized_request.get("stockList") != expected_stock
        or normalized_request.get("beginTime") != start.isoformat()
        or normalized_request.get("endTime") != end.isoformat()
        or not isinstance(normalized_request.get("pageSize"), int)
    ):
        raise EventSnapshotError("Eastmoney query parameters do not match coverage scope")
    request_sha256 = hashlib.sha256(_canonical_json_bytes(normalized_request)).hexdigest()
    if evidence.get("requestSha256") != request_sha256:
        raise EventSnapshotError("Eastmoney query request hash is inconsistent")

    raw_pagination = evidence.get("pagination")
    if not isinstance(raw_pagination, Mapping):
        raise EventSnapshotError("Eastmoney pagination evidence must be an object")
    pagination = cast(Mapping[object, object], raw_pagination)
    page_size = pagination.get("pageSize")
    page_count = pagination.get("pageCount")
    total_hits = pagination.get("totalHits")
    unique_art_codes = pagination.get("uniqueArtCodes")
    returned_rows = pagination.get("returnedRows")
    if (
        not isinstance(page_size, int)
        or isinstance(page_size, bool)
        or page_size < 1
        or page_size != normalized_request.get("pageSize")
        or not isinstance(page_count, int)
        or isinstance(page_count, bool)
        or page_count < 1
        or not isinstance(total_hits, int)
        or isinstance(total_hits, bool)
        or total_hits < 0
        or unique_art_codes != total_hits
        or not isinstance(returned_rows, int)
        or isinstance(returned_rows, bool)
        or returned_rows < total_hits
        or pagination.get("complete") is not True
        or pagination.get("zeroResult") is not (total_hits == 0)
    ):
        raise EventSnapshotError("Eastmoney pagination summary is incomplete or inconsistent")
    raw_pages = pagination.get("pages")
    if not isinstance(raw_pages, list):
        raise EventSnapshotError("Eastmoney pagination page evidence is incomplete")
    typed_pages = cast(list[object], raw_pages)
    if len(typed_pages) != page_count:
        raise EventSnapshotError("Eastmoney pagination page evidence is incomplete")
    pages: list[dict[str, object]] = []
    for expected_index, raw_page in enumerate(typed_pages, start=1):
        if not isinstance(raw_page, Mapping):
            raise EventSnapshotError("Eastmoney page evidence must be an object")
        page = cast(Mapping[object, object], raw_page)
        row_count = page.get("rowCount")
        response_hash = page.get("responseSha256")
        if (
            page.get("pageIndex") != expected_index
            or not isinstance(row_count, int)
            or isinstance(row_count, bool)
            or row_count < 0
            or not _is_sha256(response_hash)
        ):
            raise EventSnapshotError("Eastmoney page evidence is invalid")
        pages.append(
            {
                "pageIndex": expected_index,
                "rowCount": row_count,
                "responseSha256": response_hash,
            }
        )
    if sum(cast(int, item["rowCount"]) for item in pages) != returned_rows:
        raise EventSnapshotError("Eastmoney pagination row counts do not reconcile")
    pagination_body: dict[str, object] = {
        "pageSize": page_size,
        "pageCount": page_count,
        "totalHits": total_hits,
        "uniqueArtCodes": unique_art_codes,
        "returnedRows": returned_rows,
        "complete": True,
        "zeroResult": total_hits == 0,
        "pages": pages,
    }
    pagination_hash = hashlib.sha256(_canonical_json_bytes(pagination_body)).hexdigest()
    if pagination.get("evidenceSha256") != pagination_hash:
        raise EventSnapshotError("Eastmoney pagination evidence hash is inconsistent")
    pagination_body["evidenceSha256"] = pagination_hash

    classification_summary = _validate_classification_summary(
        evidence.get("classificationSummary"),
        total_hits=total_hits,
    )
    rows_by_event_code = cast(
        Mapping[str, int],
        classification_summary["rowsByEventCode"],
    )

    raw_by_code = evidence.get("eventCodeCoverage")
    if not isinstance(raw_by_code, Mapping):
        raise EventSnapshotError("Eastmoney eventCodeCoverage must be an object")
    typed_by_code = cast(Mapping[object, object], raw_by_code)
    if set(typed_by_code) != set(requested_codes):
        raise EventSnapshotError("Eastmoney event-code evidence must match requested event codes")
    normalized_by_code: dict[str, object] = {}
    for code in sorted(requested_codes):
        raw_code = typed_by_code[code]
        if not isinstance(raw_code, Mapping):
            raise EventSnapshotError("Eastmoney event-code query evidence is invalid")
        code_evidence = cast(Mapping[object, object], raw_code)
        provider_columns = _string_list(
            code_evidence.get("providerColumnCodes"),
            "providerColumnCodes",
        )
        title_rule_ids = _string_list(code_evidence.get("titleRuleIds"), "titleRuleIds")
        query_succeeded = code_evidence.get("querySucceeded") is True
        coverage_basis = code_evidence.get("coverageBasis")
        selected_record_count = code_evidence.get("selectedRecordCount")
        contract = eastmoney_event_coverage_contract(code)
        if (
            query_succeeded is not contract.query_succeeded
            or coverage_basis != contract.coverage_basis
            or provider_columns != contract.provider_column_codes
            or title_rule_ids != contract.title_rule_ids
            or code_evidence.get("classifierVersion") != ANNOUNCEMENT_CLASSIFIER_VERSION
            or code_evidence.get("classifierSha256") != contract.classifier_sha256
            or not isinstance(selected_record_count, int)
            or isinstance(selected_record_count, bool)
            or selected_record_count < 0
            or selected_record_count != rows_by_event_code.get(code, 0)
        ):
            raise EventSnapshotError(
                "Eastmoney event-code evidence lacks interval query evidence or does not "
                f"match the fixed classifier contract: {code}"
            )
        normalized_by_code[code] = {
            "querySucceeded": query_succeeded,
            "coverageBasis": coverage_basis,
            "providerColumnCodes": list(provider_columns),
            "titleRuleIds": list(title_rule_ids),
            "classifierVersion": ANNOUNCEMENT_CLASSIFIER_VERSION,
            "classifierSha256": contract.classifier_sha256,
            "selectedRecordCount": selected_record_count,
        }

    normalized: dict[str, object] = {
        "schemaVersion": _EASTMONEY_QUERY_EVIDENCE_SCHEMA,
        "provider": "eastmoney",
        "instrumentId": instrument_id,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "endpoint": _EASTMONEY_ANNOUNCEMENT_LIST_URL,
        "requestParameters": normalized_request,
        "requestSha256": request_sha256,
        "querySucceeded": True,
        "pagination": pagination_body,
        "classificationSummary": classification_summary,
        "eventCodeCoverage": normalized_by_code,
    }
    evidence_hash = hashlib.sha256(_canonical_json_bytes(normalized)).hexdigest()
    if evidence.get("evidenceSha256") != evidence_hash:
        raise EventSnapshotError("Eastmoney query evidence hash is inconsistent")
    normalized["evidenceSha256"] = evidence_hash
    return normalized


def _validate_classification_summary(
    raw_summary: object,
    *,
    total_hits: int,
) -> dict[str, object]:
    if not isinstance(raw_summary, Mapping):
        raise EventSnapshotError("Eastmoney classification summary must be an object")
    summary = cast(Mapping[object, object], raw_summary)
    total_rows = summary.get("totalRows")
    classified_rows = summary.get("classifiedRows")
    unclassified_rows = summary.get("unclassifiedRows")
    if (
        summary.get("classifierVersion") != ANNOUNCEMENT_CLASSIFIER_VERSION
        or total_rows != total_hits
        or not isinstance(classified_rows, int)
        or isinstance(classified_rows, bool)
        or classified_rows < 0
        or not isinstance(unclassified_rows, int)
        or isinstance(unclassified_rows, bool)
        or unclassified_rows < 0
        or classified_rows + unclassified_rows != total_hits
        or not _is_sha256(summary.get("classifiedRowsSha256"))
    ):
        raise EventSnapshotError("Eastmoney classification summary is inconsistent")
    raw_counts = summary.get("rowsByEventCode")
    if not isinstance(raw_counts, Mapping):
        raise EventSnapshotError("Eastmoney classification counts must be an object")
    allowed_codes = eastmoney_preparable_event_codes()
    counts: dict[str, int] = {}
    for raw_code, raw_count in cast(Mapping[object, object], raw_counts).items():
        if (
            not isinstance(raw_code, str)
            or raw_code not in allowed_codes
            or not isinstance(raw_count, int)
            or isinstance(raw_count, bool)
            or raw_count <= 0
        ):
            raise EventSnapshotError("Eastmoney classification counts are invalid")
        counts[raw_code] = raw_count
    if sum(counts.values()) != classified_rows:
        raise EventSnapshotError("Eastmoney classification row counts do not reconcile")
    normalized: dict[str, object] = {
        "classifierVersion": ANNOUNCEMENT_CLASSIFIER_VERSION,
        "totalRows": total_rows,
        "classifiedRows": classified_rows,
        "unclassifiedRows": unclassified_rows,
        "rowsByEventCode": dict(sorted(counts.items())),
        "classifiedRowsSha256": summary["classifiedRowsSha256"],
    }
    expected_hash = hashlib.sha256(_canonical_json_bytes(normalized)).hexdigest()
    if summary.get("evidenceSha256") != expected_hash:
        raise EventSnapshotError("Eastmoney classification evidence hash is inconsistent")
    normalized["evidenceSha256"] = expected_hash
    return normalized


def _provider_code_query_evidence(
    provider_query_evidence: Mapping[str, object],
    provider: str,
    event_code: str,
) -> Mapping[str, object] | None:
    raw_provider = provider_query_evidence.get(provider)
    if not isinstance(raw_provider, Mapping):
        return None
    typed_provider = cast(Mapping[object, object], raw_provider)
    raw_by_code = typed_provider.get("eventCodeCoverage")
    if not isinstance(raw_by_code, Mapping):
        return None
    typed_by_code = cast(Mapping[object, object], raw_by_code)
    raw_code = typed_by_code.get(event_code)
    if not isinstance(raw_code, Mapping):
        return None
    return cast(Mapping[str, object], raw_code)


def _validate_coverage_by_event_code(
    raw_coverage: object,
    *,
    requested_codes: tuple[str, ...],
    observations: Sequence[EventObservation],
    source_statuses: Sequence[Mapping[str, object]],
    provider_query_evidence: Mapping[str, object],
) -> tuple[dict[str, object], bool]:
    if not isinstance(raw_coverage, Mapping):
        raise EventSnapshotError("event acquisition coverageByEventCode must be an object")
    typed_coverage = cast(Mapping[object, object], raw_coverage)
    if any(not isinstance(key, str) for key in typed_coverage):
        raise EventSnapshotError("event acquisition coverageByEventCode has invalid keys")
    by_code = cast(Mapping[str, object], typed_coverage)
    if set(by_code) != set(requested_codes):
        raise EventSnapshotError(
            "event acquisition coverageByEventCode must exactly match requested event codes"
        )

    status_by_provider = {
        cast(str, item["provider"]): cast(str, item["status"]) for item in source_statuses
    }
    observations_by_code = Counter(item.event_code for item in observations)
    primary_observations_by_code = Counter(
        item.event_code for item in observations if item.provider == PROVIDER_PRIORITY[0]
    )
    providers_by_code: dict[str, set[str]] = {code: set() for code in requested_codes}
    for observation in observations:
        providers_by_code[observation.event_code].add(observation.provider)

    normalized: dict[str, object] = {}
    all_complete = True
    for code in sorted(requested_codes):
        raw_lane = by_code[code]
        if not isinstance(raw_lane, Mapping):
            raise EventSnapshotError("event-code acquisition coverage must be an object")
        lane = cast(Mapping[object, object], raw_lane)
        policy = event_source_policy(code)
        required_source = policy.provider_priority[0]
        source_status = status_by_provider.get(required_source, "not_queried")
        code_query_evidence = _provider_code_query_evidence(
            provider_query_evidence,
            required_source,
            code,
        )
        lane_query_succeeded = (
            source_status in {"available", "empty"}
            and code_query_evidence is not None
            and code_query_evidence.get("querySucceeded") is True
        )
        if (
            code_query_evidence is not None
            and code_query_evidence.get("selectedRecordCount") != primary_observations_by_code[code]
        ):
            raise EventSnapshotError(
                f"event acquisition selected-row count is inconsistent: {code}"
            )
        expected_status = "complete" if lane_query_succeeded else "incomplete"
        expected_basis = (
            code_query_evidence.get("coverageBasis")
            if lane_query_succeeded and code_query_evidence is not None
            else "observation_only_no_interval_proof"
        )
        observed_providers = sorted(providers_by_code[code])
        if (
            lane.get("lane") != policy.lane
            or lane.get("status") != expected_status
            or lane.get("requiredSources") != [required_source]
            or lane.get("requiredSourceStatus") != source_status
            or lane.get("querySucceeded") is not lane_query_succeeded
            or lane.get("rowCount") != observations_by_code[code]
            or lane.get("zeroResult") is not (observations_by_code[code] == 0)
            or lane.get("observedProviders") != observed_providers
            or lane.get("coverageBasis") != expected_basis
        ):
            raise EventSnapshotError(f"event acquisition lane coverage is inconsistent: {code}")
        normalized[code] = {
            "lane": policy.lane,
            "status": expected_status,
            "requiredSources": [required_source],
            "requiredSourceStatus": source_status,
            "querySucceeded": lane_query_succeeded,
            "rowCount": observations_by_code[code],
            "zeroResult": observations_by_code[code] == 0,
            "observedProviders": observed_providers,
            "coverageBasis": expected_basis,
        }
        all_complete = all_complete and lane_query_succeeded
    return normalized, all_complete


def _coverage_date(value: object, field_name: str) -> date:
    if not isinstance(value, str):
        raise EventSnapshotError(f"event acquisition coverage {field_name} must be a date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise EventSnapshotError(
            f"event acquisition coverage {field_name} must be an ISO date"
        ) from exc


def _coverage_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise EventSnapshotError(f"event acquisition coverage {field_name} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise EventSnapshotError(
            f"event acquisition coverage {field_name} must be an ISO timestamp"
        ) from exc
    _require_aware(parsed, f"event acquisition coverage {field_name}")
    return parsed


def _string_list(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise EventSnapshotError(f"event acquisition coverage {field_name} is invalid")
    typed_values = cast(list[object], value)
    if any(not isinstance(item, str) or not item for item in typed_values):
        raise EventSnapshotError(f"event acquisition coverage {field_name} is invalid")
    strings = cast(list[str], typed_values)
    if len(set(strings)) != len(strings):
        raise EventSnapshotError(f"event acquisition coverage {field_name} is invalid")
    return tuple(strings)


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_web_fact_provenance(
    event: FusedEvent,
    selected: EventProvenance,
    selected_observation: EventObservation,
) -> None:
    if (
        not isinstance(event.external_fact_id, str)
        or not event.external_fact_id.strip()
        or selected.document_sha256 is None
        or selected_observation.external_fact_id != event.external_fact_id
    ):
        raise EventSnapshotError(
            "strict Demo web event requires an external fact ID and frozen document hash"
        )
    missing = sorted(
        name
        for name in _WEB_FACT_PROVENANCE_ATTRIBUTES
        if selected_observation.attributes.get(name) is None
    )
    if missing:
        raise EventSnapshotError(
            "strict Demo web event is missing evidence provenance: " + ", ".join(missing)
        )
    if (
        selected_observation.attributes.get("review_status") != "accepted"
        or selected_observation.attributes.get("timestamp_precision") != "second"
        or selected_observation.attributes.get("timing_basis")
        not in {
            "licensed_vendor_first_available",
            "prospective_web_archive_capture",
        }
        or (
            selected_observation.attributes.get("evidence_span") is None
            and selected_observation.attributes.get("evidence_sha256") is None
        )
    ):
        raise EventSnapshotError(
            "strict Demo web event failed review, timing-basis, or evidence validation"
        )


def _write_events(path: Path, events: Sequence[FusedEvent]) -> None:
    schema = _ARROW.schema(
        [
            ("stock_code", _ARROW.string()),
            ("event_id", _ARROW.string()),
            ("source_event_id", _ARROW.string()),
            ("external_fact_id", _ARROW.string()),
            ("event_code", _ARROW.string()),
            ("occurred_at", _ARROW.string()),
            ("source_released_at", _ARROW.string()),
            ("vendor_first_available_at", _ARROW.string()),
            ("ingested_at", _ARROW.string()),
            ("replay_available_at", _ARROW.string()),
            ("revision_no", _ARROW.int32()),
            ("provider", _ARROW.string()),
            ("source_url", _ARROW.string()),
            ("raw_response_sha256", _ARROW.string()),
            ("time_quality", _ARROW.string()),
            ("validation_status", _ARROW.string()),
            ("attributes_json", _ARROW.string()),
        ]
    )
    rows: list[dict[str, object]] = []
    for event in events:
        selected = next(item for item in event.provenance if item.selected)
        source_url = event.attributes.get("source_url")
        if not isinstance(source_url, str) or not source_url:
            source_url = selected.document_url
        attributes = dict(event.attributes)
        attributes.update(
            {
                "canonical_title": event.title,
                "coverage_status": event.coverage_status.value,
                "provider": event.selected_provider,
                "source_event_id": selected.provider_event_id,
                "source_url": source_url,
                "external_fact_id": event.external_fact_id,
            }
        )
        rows.append(
            {
                "stock_code": event.instrument_id.value.split(".", maxsplit=1)[0],
                "event_id": event.canonical_event_id,
                "source_event_id": selected.provider_event_id,
                "external_fact_id": _external_fact_id_text(event.external_fact_id),
                "event_code": event.event_code,
                "occurred_at": _iso(event.occurred_at),
                "source_released_at": _iso(event.source_released_at),
                "vendor_first_available_at": _iso(event.vendor_first_available_at),
                "ingested_at": selected.retrieved_at.isoformat(),
                "replay_available_at": _iso(event.market_available_at),
                "revision_no": event.revision_no,
                "provider": event.selected_provider,
                "source_url": source_url,
                "raw_response_sha256": selected.raw_response_sha256,
                "time_quality": event.time_quality.value,
                "validation_status": selected.validation_status,
                "attributes_json": _canonical_json_text(attributes),
            }
        )
    _PARQUET.write_table(_ARROW.Table.from_pylist(rows, schema=schema), path)


def _write_observations(path: Path, observations: Sequence[EventObservation]) -> None:
    schema = _ARROW.schema(
        [
            ("provider", _ARROW.string()),
            ("source_event_id", _ARROW.string()),
            ("external_fact_id", _ARROW.string()),
            ("stock_code", _ARROW.string()),
            ("instrument_id", _ARROW.string()),
            ("event_code", _ARROW.string()),
            ("title", _ARROW.string()),
            ("occurred_at", _ARROW.string()),
            ("source_released_at", _ARROW.string()),
            ("vendor_first_available_at", _ARROW.string()),
            ("ingested_at", _ARROW.string()),
            ("revision_no", _ARROW.int32()),
            ("source_url", _ARROW.string()),
            ("document_url", _ARROW.string()),
            ("document_sha256", _ARROW.string()),
            ("raw_response_sha256", _ARROW.string()),
            ("time_quality", _ARROW.string()),
            ("validation_status", _ARROW.string()),
            ("attributes_json", _ARROW.string()),
        ]
    )
    rows = [
        {
            "provider": item.provider,
            "source_event_id": item.provider_event_id,
            "external_fact_id": _external_fact_id_text(item.external_fact_id),
            "stock_code": item.instrument_id.value.split(".", maxsplit=1)[0],
            "instrument_id": item.instrument_id.value,
            "event_code": item.event_code,
            "title": item.title,
            "occurred_at": _iso(item.occurred_at),
            "source_released_at": _iso(item.source_released_at),
            "vendor_first_available_at": _iso(item.vendor_first_available_at),
            "ingested_at": item.retrieved_at.isoformat(),
            "revision_no": item.revision_no,
            "source_url": (
                item.attributes.get("source_url")
                if isinstance(item.attributes.get("source_url"), str)
                else item.document_url
            ),
            "document_url": item.document_url,
            "document_sha256": item.document_sha256,
            "raw_response_sha256": item.raw_response_sha256,
            "time_quality": item.time_quality.value,
            "validation_status": item.validation_status,
            "attributes_json": _canonical_json_text(
                {
                    **item.attributes,
                    "external_fact_id": item.external_fact_id,
                }
            ),
        }
        for item in observations
    ]
    _PARQUET.write_table(_ARROW.Table.from_pylist(rows, schema=schema), path)


def _quarantine_payload(fusion: EventFusionResult) -> dict[str, object]:
    return {
        "coverageStatus": fusion.coverage_status.value,
        "quarantined": [
            {
                "canonicalEventId": item.canonical_event_id,
                "coverageStatus": item.coverage_status.value,
                "eventCode": item.event_code,
                "instrumentId": item.instrument_id.value,
                "externalFactId": item.external_fact_id,
                "providers": [source.provider for source in item.provenance],
            }
            for item in fusion.quarantined
        ],
        "conflicts": [
            {
                "code": item.code,
                "critical": item.critical,
                "message": item.message,
                "observationKeys": [list(key) for key in item.observation_keys],
            }
            for item in fusion.conflicts
        ],
    }


def _validate_published_snapshot(path: Path, manifest: Mapping[str, object]) -> None:
    snapshot_id = manifest.get("snapshotId")
    body = {key: value for key, value in manifest.items() if key != "snapshotId"}
    digest = hashlib.sha256(_canonical_json_bytes(body)).hexdigest()
    if snapshot_id != f"events:{digest}" or path.name != digest:
        raise EventSnapshotError("published event snapshot identity does not match its manifest")
    raw_files = manifest.get("files")
    if not isinstance(raw_files, Mapping):
        raise EventSnapshotError("published event snapshot files must be an object")
    typed_files = cast(Mapping[object, object], raw_files)
    for relative, metadata in typed_files.items():
        if not isinstance(relative, str) or not isinstance(metadata, Mapping):
            raise EventSnapshotError("published event snapshot file entry is invalid")
        typed_metadata = cast(Mapping[object, object], metadata)
        file_path = path / relative
        if (
            not file_path.is_file()
            or file_path.stat().st_size != typed_metadata.get("bytes")
            or _sha256_file(file_path) != typed_metadata.get("sha256")
        ):
            raise EventSnapshotError(f"published event snapshot file mismatch: {relative}")


def _observation_sort_key(item: EventObservation) -> tuple[str, str, str, int, str]:
    return (
        item.instrument_id.value,
        item.event_code,
        item.provider,
        item.revision_no,
        item.provider_event_id,
    )


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise EventSnapshotError(f"{field_name} must include an explicit timezone")


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _external_fact_id_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return str(value.normalize())
    return str(value).strip()


def _canonical_json_text(value: object) -> str:
    return _canonical_json_bytes(value).decode("utf-8")


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        default=_json_default,
    ).encode("utf-8")


def _json_default(value: object) -> object:
    if isinstance(value, Mapping):
        raw = cast(Mapping[object, object], value)
        if any(not isinstance(key, str) for key in raw):
            raise TypeError("JSON mapping keys must be strings")
        return {cast(str, key): nested for key, nested in raw.items()}
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _write_json(path: Path, payload: object) -> None:
    path.write_bytes(_canonical_json_bytes(payload) + b"\n")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()
