"""Point-in-time event condition evaluation aligned to A-share daily sessions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from ashare_lab.domain.market_data import DailyBar, EventEnvelope
from ashare_lab.domain.shared import InstrumentId
from ashare_lab.domain.signals.models import SignalEvidence
from ashare_lab.domain.strategy.models import EventCondition, JsonScalar

from .catalog import resolve_executable_event
from .document_metrics import DocumentMetricError, evaluate_document_text_predicate

SHANGHAI = ZoneInfo("Asia/Shanghai")


class EventRuntimeError(ValueError):
    """Raised when an event condition cannot be replayed deterministically."""


@dataclass(frozen=True, slots=True)
class EventSignalPoint:
    instrument_id: InstrumentId
    session_date: date
    triggered: bool
    observed_at: datetime
    available_at: datetime
    reason: str
    evidence: tuple[SignalEvidence, ...] = ()


def evaluate_event_condition_aligned(
    condition: EventCondition,
    bars: Sequence[DailyBar],
    events: Sequence[EventEnvelope],
) -> tuple[EventSignalPoint | None, ...]:
    """Evaluate a one-shot ``published`` trigger without revising old facts.

    Revisions share ``event_id``.  The first point-in-time-visible revision that
    matches the exact attribute filters wins; later revisions cannot rewrite a
    signal already emitted from information that was then public.
    """

    canonical_bars = tuple(bars)
    if not canonical_bars:
        return ()
    definition = resolve_executable_event(condition.event_code)
    if definition is None:
        raise EventRuntimeError(f"unsupported event code: {condition.event_code!r}")
    if condition.definition_version != definition.definition_version:
        raise EventRuntimeError(
            f"unsupported {condition.event_code} definition version "
            f"{condition.definition_version!r}"
        )
    if condition.trigger != "published":
        raise EventRuntimeError(f"unsupported event trigger: {condition.trigger!r}")

    instrument_id = canonical_bars[0].instrument_id
    selected = _first_matching_revisions(condition, events, instrument_id)
    matches_by_index: dict[int, list[EventEnvelope]] = {}
    for envelope in selected:
        available_at = envelope.available_at
        assert available_at is not None
        index = _first_session_on_or_after(canonical_bars, available_at)
        if index is not None:
            matches_by_index.setdefault(index, []).append(envelope)

    result: list[EventSignalPoint | None] = []
    for index, bar in enumerate(canonical_bars):
        matches = sorted(
            matches_by_index.get(index, ()),
            key=lambda item: (
                _required_available_at(item),
                item.event.event_id.value,
                item.revision_no,
            ),
        )
        if not matches:
            observed_at = datetime.combine(
                bar.session_date,
                time(hour=15),
                tzinfo=SHANGHAI,
            )
            result.append(
                EventSignalPoint(
                    instrument_id=bar.instrument_id,
                    session_date=bar.session_date,
                    triggered=False,
                    observed_at=observed_at,
                    available_at=max(observed_at, bar.available_at),
                    reason="no_matching_event_published => false",
                )
            )
            continue

        first_available = min(_required_available_at(item) for item in matches)
        # A daily slot can only preserve one causal decision point.  Evidence
        # published later in the same session was not available when the first
        # event made the condition true and must never be merged backwards into
        # that signal.  Equal-timestamp observations remain a deterministic set.
        causal_matches = tuple(
            item for item in matches if _required_available_at(item) == first_available
        )
        identities = ",".join(item.event.event_id.value for item in causal_matches)
        reason = f"published_events[{identities}] => true"
        if condition.document_text is not None:
            counts = tuple(
                evaluate_document_text_predicate(
                    condition.document_text,
                    item.event.attributes,
                ).count
                for item in causal_matches
            )
            count_text = ",".join(str(value) for value in counts)
            reason = (
                f"published_events[{identities}] and "
                f"document.literal_mention_count({condition.document_text.term!r})"
                f"=[{count_text}] {condition.document_text.comparator} "
                f"{condition.document_text.value} => true"
            )
        result.append(
            EventSignalPoint(
                instrument_id=bar.instrument_id,
                session_date=bar.session_date,
                triggered=True,
                observed_at=first_available,
                available_at=first_available,
                reason=reason,
                evidence=tuple(_signal_evidence(item) for item in causal_matches),
            )
        )
    return tuple(result)


def _signal_evidence(envelope: EventEnvelope) -> SignalEvidence:
    available_at = _required_available_at(envelope)
    return SignalEvidence(
        evidence_type="event",
        evidence_id=envelope.event.event_id.value,
        available_at=available_at,
        source_event_id=envelope.source_event_id,
        provider=envelope.provider,
        source_url=envelope.source_url,
        time_quality=envelope.time_quality.value,
        validation_status=envelope.validation_status,
        raw_response_sha256=envelope.raw_response_sha256,
    )


def _first_matching_revisions(
    condition: EventCondition,
    events: Sequence[EventEnvelope],
    instrument_id: InstrumentId,
) -> tuple[EventEnvelope, ...]:
    ordered = sorted(
        (
            item
            for item in events
            if item.event.instrument_id == instrument_id
            and item.event.event_code == condition.event_code
            and item.available_at is not None
        ),
        key=lambda item: (
            _required_available_at(item),
            item.event.event_id.value,
            item.revision_no,
        ),
    )
    selected: dict[str, EventEnvelope] = {}
    for envelope in ordered:
        event_id = envelope.event.event_id.value
        if event_id in selected:
            continue
        if not _attributes_match(envelope.event.attributes, condition.attributes):
            continue
        if condition.document_text is not None:
            try:
                document_result = evaluate_document_text_predicate(
                    condition.document_text,
                    envelope.event.attributes,
                )
            except DocumentMetricError as exc:
                raise EventRuntimeError(
                    "event document metric cannot be replayed: " + str(exc)
                ) from exc
            if not document_result.matched:
                continue
        selected[event_id] = envelope
    return tuple(selected[key] for key in sorted(selected))


def _attributes_match(
    actual: Mapping[str, str | int | Decimal | bool | None],
    expected: Mapping[str, JsonScalar],
) -> bool:
    return all(
        name in actual and _scalar_equal(actual[name], value) for name, value in expected.items()
    )


def _scalar_equal(
    actual: str | int | Decimal | bool | None,
    expected: JsonScalar,
) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    if isinstance(actual, int | Decimal) and isinstance(expected, int | float):
        try:
            return Decimal(actual) == Decimal(str(expected))
        except InvalidOperation:
            return False
    return actual == expected


def _first_session_on_or_after(
    bars: tuple[DailyBar, ...],
    available_at: datetime,
) -> int | None:
    local_date = available_at.astimezone(SHANGHAI).date()
    return next(
        (index for index, bar in enumerate(bars) if bar.session_date >= local_date),
        None,
    )


def _required_available_at(envelope: EventEnvelope) -> datetime:
    available_at = envelope.available_at
    if available_at is None:
        raise EventRuntimeError("research-only estimated event has no tradable availability")
    return available_at
