"""Point-in-time revision semantics for one logical company event.

This module deliberately stays below API and backtest orchestration.  Callers
must re-resolve a chain at every decision or retry time; an earlier signal is
not authority to ignore a later correction or withdrawal.
"""

from __future__ import annotations

import unicodedata
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from ashare_lab.domain.shared import require_aware

from .fusion import market_available_at
from .observations import EventObservation


class RevisionResolutionState(StrEnum):
    """Whether the logical event can influence a decision at ``decision_at``."""

    ACTIVE = "active"
    WITHDRAWN = "withdrawn"
    NOT_YET_AVAILABLE = "not_yet_available"
    QUARANTINED = "quarantined"


class RevisionRole(StrEnum):
    INITIAL = "initial"
    SUPERSESSION = "supersession"
    WITHDRAWAL = "withdrawal"


@dataclass(frozen=True, slots=True)
class RevisionConflict:
    code: str
    message: str
    observation_keys: tuple[tuple[str, str, int], ...]


@dataclass(frozen=True, slots=True)
class RevisionResolution:
    state: RevisionResolutionState
    decision_at: datetime
    visible_observations: tuple[EventObservation, ...]
    active_observations: tuple[EventObservation, ...]
    active_revision_no: int | None
    superseded_revision_nos: tuple[int, ...]
    conflicts: tuple[RevisionConflict, ...]
    deduplicated_count: int = 0

    @property
    def can_emit_signal(self) -> bool:
        return self.state is RevisionResolutionState.ACTIVE

    def permits_retry(self, signal_revision_no: int) -> bool:
        """Permit retry only while the signal's exact revision remains active."""

        return (
            type(signal_revision_no) is int
            and self.state is RevisionResolutionState.ACTIVE
            and self.active_revision_no == signal_revision_no
        )


_INITIAL_ROLES = frozenset({"initial", "initial_or_explicit_lifecycle", "original"})
_SUPERSESSION_ROLES = frozenset(
    {"amendment", "amended", "correction", "corrected", "revision", "supersession"}
)
_WITHDRAWAL_ROLES = frozenset(
    {"cancellation", "cancelled", "delete", "revoked", "withdrawal", "withdrawn"}
)
_AUDIT_ATTRIBUTE_NAMES = frozenset(
    {
        "announcement_revision_role",
        "capture_mode",
        "conservative_available_at",
        "document_hash_basis",
        "document_text_scope",
        "document_url",
        "document_version_role",
        "external_fact_id",
        "ingested_at",
        "mapping_confidence",
        "provider",
        "retrieved_at",
        "review_status",
        "revision_id",
        "revision_policy",
        "revision_type",
        "search_indexed_at",
        "source",
        "source_entity_id",
        "source_event_id",
        "source_url",
        "timestamp_precision",
        "time_quality",
        "timing_basis",
        "timing_policy",
        "validation_status",
    }
)
_AUDIT_ATTRIBUTE_PREFIXES = (
    "classification_",
    "classifier_",
    "entity_mapping_",
    "evidence_",
    "extractor_",
    "provider_",
    "publisher_",
    "raw_",
    "source_",
)


def resolve_revision_chain(
    observations: Sequence[EventObservation],
    *,
    decision_at: datetime,
) -> RevisionResolution:
    """Resolve one logical event chain using only information known by a cutoff.

    The function fails closed for unreliable availability, missing revision
    links, contradictory source duplicates or disagreeing multi-source roles.
    Exact duplicates are harmlessly collapsed.  Later observations in the same
    session are excluded until their own ``first_available_at`` is reached.
    """

    require_aware(decision_at, "decision_at")
    supplied = tuple(observations)
    if not supplied:
        return _resolution(
            RevisionResolutionState.NOT_YET_AVAILABLE,
            decision_at,
        )

    unique, duplicate_count, duplicate_conflicts = _deduplicate(supplied)
    if duplicate_conflicts:
        return _resolution(
            RevisionResolutionState.QUARANTINED,
            decision_at,
            conflicts=duplicate_conflicts,
            deduplicated_count=duplicate_count,
        )

    identity_conflict = _identity_conflict(unique)
    if identity_conflict is not None:
        return _resolution(
            RevisionResolutionState.QUARANTINED,
            decision_at,
            conflicts=(identity_conflict,),
            deduplicated_count=duplicate_count,
        )

    unavailable = tuple(item for item in unique if market_available_at(item) is None)
    if unavailable:
        return _resolution(
            RevisionResolutionState.QUARANTINED,
            decision_at,
            conflicts=(
                _conflict(
                    "missing_reliable_first_available_at",
                    "every revision requires validated point-in-time first availability",
                    unavailable,
                ),
            ),
            deduplicated_count=duplicate_count,
        )

    visible_candidates = (item for item in unique if _required_available_at(item) <= decision_at)
    visible = tuple(sorted(visible_candidates, key=_observation_order))
    if not visible:
        return _resolution(
            RevisionResolutionState.NOT_YET_AVAILABLE,
            decision_at,
            deduplicated_count=duplicate_count,
        )

    by_revision: dict[int, list[EventObservation]] = defaultdict(list)
    for observation in visible:
        by_revision[observation.revision_no].append(observation)

    revision_nos = tuple(sorted(by_revision))
    expected = tuple(range(revision_nos[-1] + 1))
    if revision_nos != expected:
        return _resolution(
            RevisionResolutionState.QUARANTINED,
            decision_at,
            visible=visible,
            conflicts=(
                _conflict(
                    "revision_gap",
                    "visible revision chain must be contiguous from revision zero",
                    visible,
                ),
            ),
            deduplicated_count=duplicate_count,
        )

    roles: dict[int, RevisionRole] = {}
    for revision_no in revision_nos:
        revision_items = tuple(by_revision[revision_no])
        resolved_roles = {_revision_role(item) for item in revision_items}
        if None in resolved_roles:
            return _resolution(
                RevisionResolutionState.QUARANTINED,
                decision_at,
                visible=visible,
                conflicts=(
                    _conflict(
                        "unknown_revision_role",
                        "revision role is absent or unsupported",
                        revision_items,
                    ),
                ),
                deduplicated_count=duplicate_count,
            )
        if len(resolved_roles) != 1:
            return _resolution(
                RevisionResolutionState.QUARANTINED,
                decision_at,
                visible=visible,
                conflicts=(
                    _conflict(
                        "multi_source_revision_conflict",
                        "providers disagree on the semantic role of one revision",
                        revision_items,
                    ),
                ),
                deduplicated_count=duplicate_count,
            )
        semantic_conflict = _multi_source_semantic_conflict(revision_items)
        if semantic_conflict is not None:
            return _resolution(
                RevisionResolutionState.QUARANTINED,
                decision_at,
                visible=visible,
                conflicts=(semantic_conflict,),
                deduplicated_count=duplicate_count,
            )
        role = next(iter(resolved_roles))
        assert role is not None
        if (revision_no == 0 and role is not RevisionRole.INITIAL) or (
            revision_no > 0 and role is RevisionRole.INITIAL
        ):
            return _resolution(
                RevisionResolutionState.QUARANTINED,
                decision_at,
                visible=visible,
                conflicts=(
                    _conflict(
                        "invalid_revision_sequence",
                        "revision zero must be initial and later revisions must replace it",
                        revision_items,
                    ),
                ),
                deduplicated_count=duplicate_count,
            )
        roles[revision_no] = role

    latest_revision = revision_nos[-1]
    latest_role = roles[latest_revision]
    superseded = tuple(number for number in revision_nos if number < latest_revision)
    if latest_role is RevisionRole.WITHDRAWAL:
        return _resolution(
            RevisionResolutionState.WITHDRAWN,
            decision_at,
            visible=visible,
            superseded=superseded,
            deduplicated_count=duplicate_count,
        )

    active = tuple(sorted(by_revision[latest_revision], key=_observation_order))
    return _resolution(
        RevisionResolutionState.ACTIVE,
        decision_at,
        visible=visible,
        active=active,
        active_revision_no=latest_revision,
        superseded=superseded,
        deduplicated_count=duplicate_count,
    )


def _deduplicate(
    observations: Sequence[EventObservation],
) -> tuple[tuple[EventObservation, ...], int, tuple[RevisionConflict, ...]]:
    by_key: dict[tuple[str, str, int], list[EventObservation]] = defaultdict(list)
    for observation in observations:
        by_key[observation.provider_revision_key].append(observation)

    unique: list[EventObservation] = []
    conflicts: list[RevisionConflict] = []
    duplicate_count = 0
    for key in sorted(by_key):
        values = by_key[key]
        first = values[0]
        if all(item == first for item in values[1:]):
            unique.append(first)
            duplicate_count += len(values) - 1
            continue
        conflicts.append(
            _conflict(
                "contradictory_provider_revision",
                "one provider revision produced contradictory immutable observations",
                values,
            )
        )
    return tuple(unique), duplicate_count, tuple(conflicts)


def _identity_conflict(observations: Sequence[EventObservation]) -> RevisionConflict | None:
    instruments = {item.instrument_id for item in observations}
    event_codes = {item.event_code for item in observations}
    if len(instruments) != 1 or len(event_codes) != 1:
        return _conflict(
            "mixed_event_chain",
            "one revision chain cannot mix instruments or event codes",
            observations,
        )

    identified = (item for item in observations if item.external_fact_id)
    fact_ids = {str(item.external_fact_id).strip() for item in identified}
    if fact_ids:
        if len(fact_ids) != 1 or any(item.external_fact_id is None for item in observations):
            return _conflict(
                "conflicting_revision_chain_identity",
                "all provider revisions must share one explicit external_fact_id",
                observations,
            )
        return None

    source_identities = {(item.provider, item.provider_event_id) for item in observations}
    if len(source_identities) != 1:
        return _conflict(
            "missing_revision_chain_identity",
            "multiple revisions require a shared external_fact_id",
            observations,
        )
    return None


def _revision_role(observation: EventObservation) -> RevisionRole | None:
    raw = observation.attributes.get("revision_type")
    if raw is None:
        raw = observation.attributes.get("announcement_revision_role")
    if raw is None and observation.revision_no == 0:
        return RevisionRole.INITIAL
    if not isinstance(raw, str):
        return None
    normalized = raw.strip().casefold()
    if normalized in _INITIAL_ROLES:
        return RevisionRole.INITIAL
    if normalized in _SUPERSESSION_ROLES:
        return RevisionRole.SUPERSESSION
    if normalized in _WITHDRAWAL_ROLES:
        return RevisionRole.WITHDRAWAL
    return None


def _multi_source_semantic_conflict(
    observations: Sequence[EventObservation],
) -> RevisionConflict | None:
    if len({item.provider for item in observations}) < 2:
        return None

    titles = {_normalized_title(item.title) for item in observations}
    document_hashes = {
        item.document_sha256 for item in observations if item.document_sha256 is not None
    }
    attribute_sets = tuple(_semantic_attributes(item) for item in observations)
    conflicting_names: set[str] = set()
    for index, left in enumerate(attribute_sets):
        for right in attribute_sets[index + 1 :]:
            for name in left.keys() & right.keys():
                if left[name] != right[name]:
                    conflicting_names.add(name)

    if len(titles) <= 1 and len(document_hashes) <= 1 and not conflicting_names:
        return None
    reasons: list[str] = []
    if len(titles) > 1:
        reasons.append("title")
    if len(document_hashes) > 1:
        reasons.append("document_sha256")
    reasons.extend(sorted(conflicting_names))
    return _conflict(
        "multi_source_revision_semantic_conflict",
        "providers disagree on revision semantics: " + ", ".join(reasons),
        observations,
    )


def _semantic_attributes(observation: EventObservation) -> dict[str, tuple[str, str]]:
    return {
        name: _semantic_value(value)
        for name, value in observation.attributes.items()
        if not _is_audit_attribute(name)
    }


def _is_audit_attribute(name: str) -> bool:
    normalized = name.strip().casefold()
    return normalized in _AUDIT_ATTRIBUTE_NAMES or normalized.startswith(_AUDIT_ATTRIBUTE_PREFIXES)


def _semantic_value(value: object) -> tuple[str, str]:
    if value is None:
        return ("null", "")
    if isinstance(value, bool):
        return ("bool", "true" if value else "false")
    if isinstance(value, Decimal):
        return ("number", str(value.normalize()))
    if isinstance(value, int):
        return ("number", str(value))
    if isinstance(value, str):
        return ("text", unicodedata.normalize("NFKC", value).strip().casefold())
    return (type(value).__name__, repr(value))


def _normalized_title(title: str) -> str:
    normalized = unicodedata.normalize("NFKC", title).strip().casefold()
    return " ".join(normalized.split())


def _required_available_at(observation: EventObservation) -> datetime:
    available_at = market_available_at(observation)
    if available_at is None:  # guarded by the caller; keeps sorting total.
        raise RuntimeError("revision observation has no reliable first_available_at")
    return available_at


def _observation_order(observation: EventObservation) -> tuple[datetime, int, str, str]:
    return (
        _required_available_at(observation),
        observation.revision_no,
        observation.provider,
        observation.provider_event_id,
    )


def _conflict(
    code: str,
    message: str,
    observations: Sequence[EventObservation],
) -> RevisionConflict:
    return RevisionConflict(
        code=code,
        message=message,
        observation_keys=tuple(sorted(item.provider_revision_key for item in observations)),
    )


def _resolution(
    state: RevisionResolutionState,
    decision_at: datetime,
    *,
    visible: tuple[EventObservation, ...] = (),
    active: tuple[EventObservation, ...] = (),
    active_revision_no: int | None = None,
    superseded: tuple[int, ...] = (),
    conflicts: tuple[RevisionConflict, ...] = (),
    deduplicated_count: int = 0,
) -> RevisionResolution:
    return RevisionResolution(
        state=state,
        decision_at=decision_at,
        visible_observations=visible,
        active_observations=active,
        active_revision_no=active_revision_no,
        superseded_revision_nos=superseded,
        conflicts=conflicts,
        deduplicated_count=deduplicated_count,
    )


__all__ = [
    "RevisionConflict",
    "RevisionResolution",
    "RevisionResolutionState",
    "RevisionRole",
    "resolve_revision_chain",
]
