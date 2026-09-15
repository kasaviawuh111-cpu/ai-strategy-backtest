"""Project raw entry facts into causal occurrences without changing exit facts."""
from ashare_lab.domain.signals.models import SignalFact
from ashare_lab.domain.strategy import EventCondition, IndicatorCondition

ACCUMULATE_ON_NEW_ENTRY = "accumulate_on_new_entry_signal"


def entry_occurrences(timeline, *, condition):
    """Keep pulses; for states retain only the first true of each known episode.

    Call with the complete warm-up timeline. Missing observations do not rearm
    an episode. Original facts remain available to callers for diagnostics.
    """
    if isinstance(condition, IndicatorCondition) and "cross" in condition.trigger:
        return tuple(timeline)
    result: list[SignalFact | None] = []
    active = False
    seen_events = set()
    for fact in timeline:
        if fact is None:
            result.append(None)
            continue
        if isinstance(condition, EventCondition):
            keys = {(item.evidence_type, item.source_event_id or item.evidence_id)
                    for item in fact.evidence}
            fresh = fact.triggered and (not keys or bool(keys - seen_events))
            if fact.triggered:
                seen_events.update(keys)
            result.append(fact if not fact.triggered or fresh else None)
            continue
        fresh = fact.triggered and not active
        active = fact.triggered
        result.append(fact if not fact.triggered or fresh else None)
    return tuple(result)
