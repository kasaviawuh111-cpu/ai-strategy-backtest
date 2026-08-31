"""Deterministic acquisition orchestration kept outside the replay runtime."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import cast

from ashare_lab.domain.events import PROVIDER_PRIORITY, EventObservation, event_source_policy
from ashare_lab.domain.shared import InstrumentId


@dataclass(frozen=True, slots=True)
class EventCollectionRequest:
    instrument_id: InstrumentId
    start: date
    end: date
    retrieved_at: datetime

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError("event collection start must not exceed end")
        if self.retrieved_at.tzinfo is None or self.retrieved_at.utcoffset() is None:
            raise ValueError("event collection retrieved_at must include a timezone")


@dataclass(frozen=True, slots=True)
class EventFetchBatch:
    """Provider rows plus immutable evidence for the interval query itself."""

    observations: tuple[EventObservation, ...]
    acquisition_evidence: Mapping[str, object]


type EventFetcher = Callable[
    [EventCollectionRequest],
    Sequence[EventObservation] | EventFetchBatch,
]


@dataclass(frozen=True, slots=True)
class EventSourceCollection:
    provider: str
    status: str
    observations: tuple[EventObservation, ...]
    acquisition_evidence: Mapping[str, object] | None = None
    error_type: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class EventCollectionResult:
    observations: tuple[EventObservation, ...]
    sources: tuple[EventSourceCollection, ...]

    @property
    def available_providers(self) -> tuple[str, ...]:
        return tuple(item.provider for item in self.sources if item.status == "available")


def build_event_acquisition_coverage(
    request: EventCollectionRequest,
    collection: EventCollectionResult,
    *,
    requested_event_codes: Sequence[str],
    supplemental_observations: Sequence[EventObservation] = (),
) -> dict[str, object]:
    """Build immutable proof of what the acquisition step actually queried.

    Eastmoney is the required public announcement source for the current Demo.
    Optional licensed vendors remain visible in ``sourceStatuses`` but their
    absence does not silently change the required-source contract.  A
    successful empty response is distinct from a failed or skipped query.
    """

    codes = tuple(sorted(set(requested_event_codes)))
    if not codes or any(not item.startswith("event.") for item in codes):
        raise ValueError("requested event codes must contain canonical event.* codes")

    required_providers = (PROVIDER_PRIORITY[0],)
    successful_statuses = frozenset({"available", "empty"})
    source_statuses: list[dict[str, object]] = []
    provider_query_evidence: dict[str, object] = {}
    for source in collection.sources:
        query_succeeded = source.status in successful_statuses
        source_statuses.append(
            {
                "provider": source.provider,
                "required": source.provider in required_providers,
                "status": source.status,
                "querySucceeded": query_succeeded,
                "rowCount": len(source.observations),
                "errorType": source.error_type,
                "errorMessageSha256": (
                    hashlib.sha256(source.error_message.encode("utf-8")).hexdigest()
                    if source.error_message
                    else None
                ),
            }
        )
        if source.acquisition_evidence is not None:
            provider_query_evidence[source.provider] = dict(source.acquisition_evidence)

    status_by_provider = {item.provider: item.status for item in collection.sources}
    required_satisfied = tuple(
        provider
        for provider in required_providers
        if status_by_provider.get(provider) in successful_statuses
    )
    query_succeeded = required_satisfied == required_providers
    supplemental_counts: dict[str, int] = {}
    for observation in collection.observations:
        if observation.instrument_id != request.instrument_id:
            raise ValueError("collected event observation belongs to a different instrument")
        if observation.event_code not in codes:
            raise ValueError("collected event observation is outside requested event codes")
    for observation in supplemental_observations:
        if observation.instrument_id != request.instrument_id:
            raise ValueError("supplemental event observation belongs to a different instrument")
        if observation.event_code not in codes:
            raise ValueError("supplemental event observation is outside requested event codes")
        supplemental_counts[observation.provider] = (
            supplemental_counts.get(observation.provider, 0) + 1
        )

    row_count = len(collection.observations) + len(supplemental_observations)
    all_observations = (*collection.observations, *supplemental_observations)
    observations_by_code = Counter(item.event_code for item in all_observations)
    providers_by_code: dict[str, set[str]] = {code: set() for code in codes}
    for observation in all_observations:
        providers_by_code[observation.event_code].add(observation.provider)
    coverage_by_event_code: dict[str, object] = {}
    all_lanes_complete = True
    for code in codes:
        policy = event_source_policy(code)
        required_source = policy.provider_priority[0]
        source_status = status_by_provider.get(required_source, "not_queried")
        provider_evidence = provider_query_evidence.get(required_source)
        code_evidence = _event_code_query_evidence(provider_evidence, code)
        lane_query_succeeded = (
            source_status in successful_statuses
            and code_evidence is not None
            and code_evidence.get("querySucceeded") is True
        )
        all_lanes_complete = all_lanes_complete and lane_query_succeeded
        coverage_by_event_code[code] = {
            "lane": policy.lane,
            "status": "complete" if lane_query_succeeded else "incomplete",
            "requiredSources": [required_source],
            "requiredSourceStatus": source_status,
            "querySucceeded": lane_query_succeeded,
            "rowCount": observations_by_code[code],
            "zeroResult": observations_by_code[code] == 0,
            "observedProviders": sorted(providers_by_code[code]),
            "coverageBasis": (
                code_evidence.get("coverageBasis")
                if lane_query_succeeded and code_evidence is not None
                else "observation_only_no_interval_proof"
            ),
        }
    query_succeeded = query_succeeded and all_lanes_complete
    optional_unavailable = sorted(
        item.provider
        for item in collection.sources
        if item.provider not in required_providers and item.status not in successful_statuses
    )
    supplemental_evidence = {
        "rowCount": len(supplemental_observations),
        "observationsByProvider": dict(sorted(supplemental_counts.items())),
    }
    request_summary = {
        "instrumentId": request.instrument_id.value,
        "start": request.start.isoformat(),
        "end": request.end.isoformat(),
        "requestedEventCodes": list(codes),
        "queriedAt": request.retrieved_at.isoformat(),
    }
    request_sha256 = hashlib.sha256(_canonical_json_bytes(request_summary)).hexdigest()
    source_status_sha256 = hashlib.sha256(
        _canonical_json_bytes(
            {
                "sourceStatuses": source_statuses,
                "providerQueryEvidence": provider_query_evidence,
                "supplementalEvidence": supplemental_evidence,
                "coverageByEventCode": coverage_by_event_code,
            }
        )
    ).hexdigest()
    return {
        "status": "complete" if query_succeeded else "incomplete",
        "instrumentId": request.instrument_id.value,
        "start": request.start.isoformat(),
        "end": request.end.isoformat(),
        "queriedAt": request.retrieved_at.isoformat(),
        "requestedEventCodes": list(codes),
        "requiredProviders": list(required_providers),
        "querySucceeded": query_succeeded,
        "rowCount": row_count,
        "zeroResult": row_count == 0,
        "sourceStatuses": source_statuses,
        "providerQueryEvidence": provider_query_evidence,
        "supplementalEvidence": supplemental_evidence,
        "coverageByEventCode": coverage_by_event_code,
        "auditSummary": {
            "requestSha256": request_sha256,
            "sourceStatusSha256": source_status_sha256,
            "requiredProvidersSatisfied": list(required_satisfied),
            "optionalUnavailableProviders": optional_unavailable,
            "successfulProviders": [
                item.provider for item in collection.sources if item.status in successful_statuses
            ],
        },
    }


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _event_code_query_evidence(
    raw_provider_evidence: object,
    event_code: str,
) -> Mapping[str, object] | None:
    if not isinstance(raw_provider_evidence, Mapping):
        return None
    provider_evidence = cast(Mapping[object, object], raw_provider_evidence)
    raw_by_code = provider_evidence.get("eventCodeCoverage")
    if not isinstance(raw_by_code, Mapping):
        return None
    by_code = cast(Mapping[object, object], raw_by_code)
    raw_code_evidence = by_code.get(event_code)
    if not isinstance(raw_code_evidence, Mapping):
        return None
    return cast(Mapping[str, object], raw_code_evidence)


def collect_event_observations(
    request: EventCollectionRequest,
    fetchers: Mapping[str, EventFetcher],
    *,
    require_primary: bool = True,
) -> EventCollectionResult:
    """Collect configured sources in policy order without changing fusion policy.

    Missing optional vendors are represented explicitly.  A failed Eastmoney
    primary raises by default so a quiet fallback cannot silently change the
    backtest's data contract.
    """

    unknown = sorted(set(fetchers) - set(PROVIDER_PRIORITY))
    if unknown:
        raise ValueError("unsupported event providers: " + ", ".join(unknown))
    sources: list[EventSourceCollection] = []
    collected: list[EventObservation] = []
    for provider in PROVIDER_PRIORITY:
        fetcher = fetchers.get(provider)
        if fetcher is None:
            sources.append(
                EventSourceCollection(
                    provider=provider,
                    status="not_configured",
                    observations=(),
                )
            )
            continue
        try:
            fetched = fetcher(request)
            if isinstance(fetched, EventFetchBatch):
                observations = fetched.observations
                acquisition_evidence: Mapping[str, object] | None = fetched.acquisition_evidence
            else:
                observations = tuple(fetched)
                acquisition_evidence = None
            _validate_provider_result(provider, request, observations)
        except Exception as exc:
            sources.append(
                EventSourceCollection(
                    provider=provider,
                    status="failed",
                    observations=(),
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
            )
            if require_primary and provider == PROVIDER_PRIORITY[0]:
                raise RuntimeError("primary Eastmoney event collection failed") from exc
            continue
        status = "available" if observations else "empty"
        sources.append(
            EventSourceCollection(
                provider=provider,
                status=status,
                observations=observations,
                acquisition_evidence=acquisition_evidence,
            )
        )
        collected.extend(observations)
    collected.sort(
        key=lambda item: (
            item.instrument_id.value,
            item.event_code,
            PROVIDER_PRIORITY.index(item.provider),
            item.provider_event_id,
            item.revision_no,
        )
    )
    return EventCollectionResult(
        observations=tuple(collected),
        sources=tuple(sources),
    )


def _validate_provider_result(
    provider: str,
    request: EventCollectionRequest,
    observations: Sequence[EventObservation],
) -> None:
    for observation in observations:
        if observation.provider != provider:
            raise ValueError(
                f"{provider} fetcher returned an observation for {observation.provider}"
            )
        if observation.instrument_id != request.instrument_id:
            raise ValueError(f"{provider} fetcher returned a different instrument")
        if observation.retrieved_at != request.retrieved_at:
            raise ValueError(f"{provider} fetcher changed the pinned retrieval clock")
