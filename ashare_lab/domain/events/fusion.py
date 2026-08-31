"""Deterministic, point-in-time-safe fusion of provider event observations."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from zoneinfo import ZoneInfo

from ashare_lab.domain.market_data import TimeQuality
from ashare_lab.domain.shared import InstrumentId

from .observations import ANNOUNCEMENT_EVENT_PROVIDERS, EventAttribute, EventObservation

SHANGHAI = ZoneInfo("Asia/Shanghai")
PROVIDER_PRIORITY = ANNOUNCEMENT_EVENT_PROVIDERS
_DEFAULT_ACQUISITION_PRIORITY = (*PROVIDER_PRIORITY, "web_archive")
_TITLE_NOISE = re.compile(r"[^\w\u3400-\u9fff]+", re.UNICODE)
_PROVENANCE_ATTRIBUTE_NAMES = frozenset(
    {
        "document_url",
        "ingested_at",
        "provider",
        "source_event_id",
        "source_url",
        "time_quality",
        "validation_status",
        "external_fact_id",
    }
)
_PROVENANCE_ATTRIBUTE_PREFIXES = ("publisher", "extractor", "discovery")

MAJOR_CONTRACT_EVENT_CODE = "event.contracts_orders.major_contract_won"
LICENSE_APPROVAL_EVENT_CODE = "event.macro_policy_industry.license_approval"


@dataclass(frozen=True, slots=True)
class EventLaneSourcePolicy:
    """A source order fixed before observations or timestamps are inspected."""

    event_code: str
    lane: str
    evidence_preference: tuple[str, ...]
    provider_priority: tuple[str, ...]


DEFAULT_EVENT_SOURCE_POLICY = EventLaneSourcePolicy(
    event_code="*",
    lane="announcement",
    evidence_preference=("issuer_announcement_history", "news_history"),
    provider_priority=_DEFAULT_ACQUISITION_PRIORITY,
)
EVENT_LANE_SOURCE_POLICIES: Mapping[str, EventLaneSourcePolicy] = MappingProxyType(
    {
        MAJOR_CONTRACT_EVENT_CODE: EventLaneSourcePolicy(
            event_code=MAJOR_CONTRACT_EVENT_CODE,
            lane="major_contract",
            evidence_preference=("issuer_announcement_history", "news_history"),
            provider_priority=_DEFAULT_ACQUISITION_PRIORITY,
        ),
        LICENSE_APPROVAL_EVENT_CODE: EventLaneSourcePolicy(
            event_code=LICENSE_APPROVAL_EVENT_CODE,
            lane="license_approval",
            evidence_preference=("regulator_history", "news_history"),
            provider_priority=("web_archive", *PROVIDER_PRIORITY),
        ),
    }
)


def event_source_policy(event_code: str) -> EventLaneSourcePolicy:
    return EVENT_LANE_SOURCE_POLICIES.get(event_code, DEFAULT_EVENT_SOURCE_POLICY)


class CoverageStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    CONFLICTED = "conflicted"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class FusionConflict:
    code: str
    message: str
    critical: bool
    observation_keys: tuple[tuple[str, str, int], ...]


@dataclass(frozen=True, slots=True)
class EventProvenance:
    provider: str
    provider_event_id: str
    revision_no: int
    retrieved_at: datetime
    market_available_at: datetime | None
    selected: bool
    document_url: str | None
    document_sha256: str | None
    raw_response_sha256: str | None
    external_fact_id: EventAttribute
    validation_status: str


@dataclass(frozen=True, slots=True)
class FusedEvent:
    canonical_event_id: str
    external_fact_id: EventAttribute
    instrument_id: InstrumentId
    event_code: str
    title: str
    occurred_at: datetime | None
    source_released_at: datetime | None
    vendor_first_available_at: datetime | None
    market_available_at: datetime | None
    time_quality: TimeQuality
    document_url: str | None
    document_sha256: str | None
    attributes: Mapping[str, EventAttribute]
    revision_no: int
    selected_provider: str
    provenance: tuple[EventProvenance, ...]
    conflicts: tuple[FusionConflict, ...]
    coverage_status: CoverageStatus

    @property
    def is_tradable(self) -> bool:
        return self.market_available_at is not None and self.coverage_status not in {
            CoverageStatus.CONFLICTED,
            CoverageStatus.UNAVAILABLE,
        }


@dataclass(frozen=True, slots=True)
class EventFusionResult:
    events: tuple[FusedEvent, ...]
    quarantined: tuple[FusedEvent, ...]
    conflicts: tuple[FusionConflict, ...]
    coverage_status: CoverageStatus


def fuse_event_observations(
    observations: Sequence[EventObservation],
) -> EventFusionResult:
    """Fuse observations under a fixed provider policy.

    The default announcement lane keeps Eastmoney, iFinD, RQData and Tushare
    in that fixed order.  Explicit event-lane policies may prefer a validated
    regulator archive, but timestamps never influence source selection.
    """

    unique, duplicate_groups = _source_local_deduplication(observations)
    groups, isolated_groups = _identity_groups(unique)

    fused: list[FusedEvent] = []
    quarantined: list[FusedEvent] = []
    for identity, items in groups:
        event = _fuse_group(identity, items)
        (fused if event.is_tradable else quarantined).append(event)
    for identity, items, conflicts in (*duplicate_groups, *isolated_groups):
        quarantined.append(_fuse_group(identity, items, initial_conflicts=conflicts))

    fused.sort(key=_event_sort_key)
    quarantined.sort(key=_event_sort_key)
    all_conflicts = tuple(
        conflict for event in (*fused, *quarantined) for conflict in event.conflicts
    )
    if any(event.coverage_status is CoverageStatus.CONFLICTED for event in quarantined):
        status = CoverageStatus.CONFLICTED
    elif not fused:
        status = CoverageStatus.UNAVAILABLE
    elif quarantined or any(event.coverage_status is CoverageStatus.PARTIAL for event in fused):
        status = CoverageStatus.PARTIAL
    else:
        status = CoverageStatus.COMPLETE
    return EventFusionResult(
        events=tuple(fused),
        quarantined=tuple(quarantined),
        conflicts=all_conflicts,
        coverage_status=status,
    )


def market_available_at(observation: EventObservation) -> datetime | None:
    """Return historical market availability without using ``retrieved_at``."""

    if observation.validation_status != "validated" or observation.time_quality not in {
        TimeQuality.EXACT,
        TimeQuality.VENDOR_OBSERVED,
    }:
        return None
    candidates = tuple(
        canonical
        for value in (observation.source_released_at, observation.vendor_first_available_at)
        if value is not None
        if (canonical := _canonical_time(value)) is not None
    )
    if not candidates:
        return None
    return max(candidates)


def normalize_event_title(title: str) -> str:
    normalized = unicodedata.normalize("NFKC", title).casefold()
    return _TITLE_NOISE.sub("", normalized)


type _Identity = tuple[str, ...]
type _Group = tuple[_Identity, tuple[EventObservation, ...]]
type _IsolatedGroup = tuple[_Identity, tuple[EventObservation, ...], tuple[FusionConflict, ...]]


def _source_local_deduplication(
    observations: Sequence[EventObservation],
) -> tuple[tuple[EventObservation, ...], tuple[_IsolatedGroup, ...]]:
    by_key: dict[tuple[str, str, int], list[EventObservation]] = defaultdict(list)
    for observation in observations:
        by_key[observation.provider_revision_key].append(observation)

    unique: list[EventObservation] = []
    isolated: list[_IsolatedGroup] = []
    for key in sorted(by_key):
        values = by_key[key]
        first = values[0]
        if all(item == first for item in values[1:]):
            unique.append(first)
            continue
        conflict = _conflict(
            "contradictory_provider_revision",
            "one provider revision produced contradictory immutable observations",
            values,
        )
        isolated.append((("source-conflict", *map(str, key)), tuple(values), (conflict,)))
    return tuple(unique), tuple(isolated)


def _identity_groups(
    observations: Sequence[EventObservation],
) -> tuple[tuple[_Group, ...], tuple[_IsolatedGroup, ...]]:
    fact_groups: dict[_Identity, list[EventObservation]] = defaultdict(list)
    hashed: dict[_Identity, list[EventObservation]] = defaultdict(list)
    fallback: dict[_Identity, list[EventObservation]] = defaultdict(list)
    isolated: list[_IsolatedGroup] = []

    for observation in observations:
        fact_identity = _external_fact_identity(observation)
        if fact_identity is not None:
            fact_groups[fact_identity].append(observation)

    document_to_facts: dict[_Identity, set[_Identity]] = defaultdict(set)
    fallback_to_facts: dict[_Identity, set[_Identity]] = defaultdict(set)
    for fact_identity, items in fact_groups.items():
        for item in items:
            if item.document_sha256 is not None:
                document_to_facts[_document_identity(item)].add(fact_identity)
            fallback_identity = _fallback_identity(item)
            if fallback_identity is not None:
                fallback_to_facts[fallback_identity].add(fact_identity)

    fact_conflicts: dict[_Identity, list[FusionConflict]] = defaultdict(list)
    for document_identity, fact_identities in document_to_facts.items():
        if len(fact_identities) <= 1:
            continue
        conflicting = tuple(
            item
            for fact_identity in sorted(fact_identities)
            for item in fact_groups[fact_identity]
            if item.document_sha256 is not None and _document_identity(item) == document_identity
        )
        conflict = _conflict(
            "external_fact_id_conflict",
            "one document hash is assigned to multiple external facts",
            conflicting,
        )
        for fact_identity in fact_identities:
            fact_conflicts[fact_identity].append(conflict)

    unmatched_without_hash: list[EventObservation] = []
    for observation in observations:
        if observation.external_fact_id is not None:
            continue
        document_identity = (
            _document_identity(observation) if observation.document_sha256 is not None else None
        )
        fallback_identity = _fallback_identity(observation)
        document_candidates: set[_Identity] = set()
        if document_identity is not None and document_identity in document_to_facts:
            document_candidates = document_to_facts[document_identity]
        if len(document_candidates) == 1:
            fact_groups[next(iter(document_candidates))].append(observation)
            continue
        if len(document_candidates) > 1:
            conflict = _conflict(
                "ambiguous_external_fact_match",
                "document identity maps to more than one external fact",
                (observation,),
            )
            isolated.append((document_identity or ("document",), (observation,), (conflict,)))
            continue
        fallback_candidates: set[_Identity] = set()
        if fallback_identity is not None and fallback_identity in fallback_to_facts:
            fallback_candidates = fallback_to_facts[fallback_identity]
        if len(fallback_candidates) == 1:
            fact_groups[next(iter(fallback_candidates))].append(observation)
            continue
        if len(fallback_candidates) > 1:
            conflict = _conflict(
                "ambiguous_external_fact_match",
                "title/date identity maps to more than one external fact",
                (observation,),
            )
            isolated.append((fallback_identity or ("title-date",), (observation,), (conflict,)))
            continue
        if document_identity is not None:
            hashed[document_identity].append(observation)
        else:
            unmatched_without_hash.append(observation)

    fallback_to_documents: dict[_Identity, set[_Identity]] = defaultdict(set)
    for identity, items in hashed.items():
        for item in items:
            fallback_identity = _fallback_identity(item)
            if fallback_identity is not None:
                fallback_to_documents[fallback_identity].add(identity)

    # Different providers commonly publish byte-different mirrors of the same
    # issuer announcement.  A shared title/date identity is not strong enough
    # to prove those documents are the same fact, but emitting every document
    # group would be worse: one real event could trigger the strategy twice.
    # Isolate every involved document until an external fact ID or another
    # deterministic revision identity resolves the ambiguity.
    ambiguous_documents: set[_Identity] = set()
    for _title_date_identity, document_identities in sorted(fallback_to_documents.items()):
        if len(document_identities) <= 1:
            continue
        ambiguous_documents.update(document_identities)
        conflicting = tuple(
            item
            for document_identity in sorted(document_identities)
            for item in hashed[document_identity]
        )
        conflict = _conflict(
            "ambiguous_document_match",
            "title/date identity maps to more than one document hash",
            conflicting,
        )
        for document_identity in sorted(document_identities):
            isolated.append(
                (
                    document_identity,
                    tuple(hashed[document_identity]),
                    (conflict,),
                )
            )

    for observation in unmatched_without_hash:
        fallback_identity = _fallback_identity(observation)
        if fallback_identity is None:
            conflict = _conflict(
                "missing_cross_source_identity",
                "observation has neither a document hash nor a usable title/date identity",
                (observation,),
            )
            isolated.append(
                (
                    ("missing-identity", *map(str, observation.provider_revision_key)),
                    (observation,),
                    (conflict,),
                )
            )
            continue
        document_candidates = fallback_to_documents.get(fallback_identity, set())
        if len(document_candidates) == 1:
            hashed[next(iter(document_candidates))].append(observation)
        elif len(document_candidates) > 1:
            conflict = _conflict(
                "ambiguous_document_match",
                "title/date identity maps to more than one document hash",
                (observation,),
            )
            isolated.append((fallback_identity, (observation,), (conflict,)))
        else:
            fallback[fallback_identity].append(observation)

    groups: list[_Group] = []
    for identity, items in sorted(fact_groups.items()):
        conflicts = tuple(fact_conflicts.get(identity, ()))
        if conflicts:
            isolated.append((identity, tuple(items), conflicts))
        else:
            groups.append((identity, tuple(items)))
    groups.extend(
        (identity, tuple(items))
        for identity, items in sorted((*hashed.items(), *fallback.items()))
        if identity not in ambiguous_documents
    )
    return tuple(groups), tuple(isolated)


def _external_fact_identity(observation: EventObservation) -> _Identity | None:
    if observation.external_fact_id is None:
        return None
    kind, value = _scalar_identity(observation.external_fact_id)
    return (
        "external-fact",
        observation.instrument_id.value,
        observation.event_code,
        kind,
        value,
    )


def _scalar_identity(value: EventAttribute) -> tuple[str, str]:
    if value is None:
        return ("null", "")
    if isinstance(value, bool):
        return ("bool", "true" if value else "false")
    if isinstance(value, Decimal):
        return ("number", str(value.normalize()))
    if isinstance(value, int):
        return ("number", str(value))
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    return ("text", normalized)


def _document_identity(observation: EventObservation) -> _Identity:
    assert observation.document_sha256 is not None
    return (
        "document",
        observation.instrument_id.value,
        observation.event_code,
        observation.document_sha256,
    )


def _fallback_identity(observation: EventObservation) -> _Identity | None:
    announced_at = observation.source_released_at or observation.vendor_first_available_at
    normalized_title = normalize_event_title(observation.title)
    if announced_at is None or not normalized_title:
        return None
    labelled_date = _provider_labelled_date(observation)
    local_date = (labelled_date or announced_at.astimezone(SHANGHAI).date()).isoformat()
    return (
        "title-date",
        observation.instrument_id.value,
        observation.event_code,
        normalized_title,
        local_date,
    )


def _provider_labelled_date(observation: EventObservation) -> date | None:
    """Prefer the provider's announcement-date label for cross-source identity.

    Some A-share feeds label an evening announcement with the next trading
    date while their precise first-seen timestamp remains on the prior evening.
    The label is useful only for identity; signal timing still comes exclusively
    from the validated timestamp fields.
    """

    for name in ("raw_notice_date", "info_date", "ann_date", "reportDate"):
        value = observation.attributes.get(name)
        if not isinstance(value, str):
            continue
        compact = value.strip().replace("/", "-")
        if len(compact) == 8 and compact.isdigit():
            compact = f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"
        try:
            return date.fromisoformat(compact[:10])
        except ValueError:
            continue
    return None


def _fuse_group(
    identity: _Identity,
    observations: Sequence[EventObservation],
    *,
    initial_conflicts: Sequence[FusionConflict] = (),
) -> FusedEvent:
    ordered = tuple(sorted(observations, key=_observation_sort_key))
    selected = next((item for item in ordered if market_available_at(item) is not None), ordered[0])
    conflicts = [*initial_conflicts, *_critical_conflicts(ordered)]
    if not any(market_available_at(item) is not None for item in ordered):
        conflicts.append(
            FusionConflict(
                code="no_validated_second_precision_time",
                message=(
                    "no observation has validated, second-precision Asia/Shanghai market time"
                ),
                critical=False,
                observation_keys=tuple(sorted(item.provider_revision_key for item in ordered)),
            )
        )
    has_critical_conflict = any(conflict.critical for conflict in conflicts)
    selected_market_time = None if has_critical_conflict else market_available_at(selected)
    source_policy = event_source_policy(selected.event_code)
    if has_critical_conflict:
        coverage = CoverageStatus.CONFLICTED
    elif selected_market_time is None:
        coverage = CoverageStatus.UNAVAILABLE
    elif selected.provider == source_policy.provider_priority[0]:
        coverage = CoverageStatus.COMPLETE
    else:
        coverage = CoverageStatus.PARTIAL

    attributes: dict[str, EventAttribute] = dict(selected.attributes)
    for observation in ordered:
        for name, value in observation.attributes.items():
            attributes.setdefault(name, value)
    provenance = tuple(
        EventProvenance(
            provider=item.provider,
            provider_event_id=item.provider_event_id,
            revision_no=item.revision_no,
            retrieved_at=item.retrieved_at,
            market_available_at=market_available_at(item),
            selected=item is selected,
            document_url=item.document_url,
            document_sha256=item.document_sha256,
            raw_response_sha256=item.raw_response_sha256,
            external_fact_id=item.external_fact_id,
            validation_status=item.validation_status,
        )
        for item in ordered
    )
    return FusedEvent(
        canonical_event_id=_canonical_event_id(identity),
        external_fact_id=_first_non_null(
            selected.external_fact_id,
            (item.external_fact_id for item in ordered),
        ),
        instrument_id=selected.instrument_id,
        event_code=selected.event_code,
        title=selected.title,
        occurred_at=_canonical_time(
            _first_non_null(selected.occurred_at, (item.occurred_at for item in ordered))
        ),
        source_released_at=_canonical_time(selected.source_released_at),
        vendor_first_available_at=_canonical_time(selected.vendor_first_available_at),
        market_available_at=selected_market_time,
        time_quality=selected.time_quality,
        document_url=_first_non_null(
            selected.document_url, (item.document_url for item in ordered)
        ),
        document_sha256=_first_non_null(
            selected.document_sha256, (item.document_sha256 for item in ordered)
        ),
        attributes=MappingProxyType(attributes),
        revision_no=selected.revision_no,
        selected_provider=selected.provider,
        provenance=provenance,
        conflicts=tuple(conflicts),
        coverage_status=coverage,
    )


def _critical_conflicts(observations: Sequence[EventObservation]) -> tuple[FusionConflict, ...]:
    conflicts: list[FusionConflict] = []
    external_fact_ids = {
        _scalar_identity(item.external_fact_id)
        for item in observations
        if item.external_fact_id is not None
    }
    if len(external_fact_ids) > 1:
        conflicts.append(
            _conflict(
                "external_fact_id_conflict",
                "matched observations disagree on external fact identity",
                observations,
            )
        )
    if len({item.instrument_id for item in observations}) > 1:
        conflicts.append(
            _conflict(
                "instrument_conflict",
                "matched observations disagree on instrument",
                observations,
            )
        )
    if len({item.event_code for item in observations}) > 1:
        conflicts.append(
            _conflict(
                "event_code_conflict",
                "matched observations disagree on event code",
                observations,
            )
        )
    occurred = {item.occurred_at for item in observations if item.occurred_at is not None}
    if len(occurred) > 1:
        conflicts.append(
            _conflict(
                "occurred_at_conflict",
                "matched observations disagree on fact time",
                observations,
            )
        )
    local_release_dates = {
        _provider_labelled_date(item) or item.source_released_at.astimezone(SHANGHAI).date()
        for item in observations
        if item.source_released_at is not None
    }
    if len(local_release_dates) > 1 and len(external_fact_ids) != 1:
        conflicts.append(
            _conflict(
                "announcement_date_conflict",
                "matched observations disagree on the local announcement date",
                observations,
            )
        )
    names = {
        name for item in observations for name in item.attributes if _is_semantic_attribute(name)
    }
    for name in sorted(names):
        values = {item.attributes[name] for item in observations if name in item.attributes}
        if len(values) > 1:
            conflicts.append(
                _conflict(
                    "attribute_conflict",
                    f"matched observations disagree on attribute {name!r}",
                    observations,
                )
            )
    return tuple(conflicts)


def _is_semantic_attribute(name: str) -> bool:
    folded = name.casefold()
    if folded in _PROVENANCE_ATTRIBUTE_NAMES or folded.startswith("raw_"):
        return False
    return not folded.startswith(_PROVENANCE_ATTRIBUTE_PREFIXES)


def _conflict(
    code: str,
    message: str,
    observations: Sequence[EventObservation],
) -> FusionConflict:
    return FusionConflict(
        code=code,
        message=message,
        critical=True,
        observation_keys=tuple(sorted(item.provider_revision_key for item in observations)),
    )


def _canonical_event_id(identity: _Identity) -> str:
    encoded = json.dumps(identity, ensure_ascii=True, separators=(",", ":")).encode()
    return f"canonical:{hashlib.sha256(encoded).hexdigest()}"


def _observation_sort_key(observation: EventObservation) -> tuple[int, str, str, int]:
    priority = event_source_policy(observation.event_code).provider_priority
    provider_rank = priority.index(observation.provider)
    return (
        provider_rank,
        observation.event_code,
        observation.provider_event_id,
        observation.revision_no,
    )


def _event_sort_key(event: FusedEvent) -> tuple[str, str, str]:
    return (event.instrument_id.value, event.event_code, event.canonical_event_id)


def _first_non_null[T](preferred: T | None, values: Iterable[T | None]) -> T | None:
    if preferred is not None:
        return preferred
    for value in values:
        if value is not None:
            return value
    return None


def _canonical_time(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    local = value.astimezone(SHANGHAI)
    if local.microsecond == 0:
        return local
    # The Demo contract stores seconds.  Ceil finer provider timestamps rather
    # than truncating them, otherwise normalization would move availability
    # into the past by up to one second.
    return (local + timedelta(seconds=1)).replace(microsecond=0)
