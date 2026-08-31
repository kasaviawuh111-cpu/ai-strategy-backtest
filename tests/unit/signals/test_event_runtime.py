from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from ashare_lab.domain.market_data import EventEnvelope, MarketEvent, TimeQuality
from ashare_lab.domain.shared import InstrumentId, StrongId
from ashare_lab.domain.signals import SignalRuntime
from ashare_lab.domain.strategy import EventCondition

from .conftest import make_bars

SHANGHAI = ZoneInfo("Asia/Shanghai")
INSTRUMENT = InstrumentId("300059.SZ")
EVENT_CODE = "event.financial_results.earnings_forecast_published"


def condition(**attributes: str | int | float | bool) -> EventCondition:
    return EventCondition(
        event_code=EVENT_CODE,
        definition_version="1.0.0",
        attributes=attributes,
    )


def event(
    event_id: str,
    available_at: datetime,
    *,
    revision_no: int = 0,
    attributes: dict[str, str | int | Decimal | bool | None] | None = None,
    quality: TimeQuality = TimeQuality.EXACT,
) -> EventEnvelope:
    return EventEnvelope(
        event=MarketEvent(
            event_id=StrongId(event_id),
            event_code=EVENT_CODE,
            instrument_id=INSTRUMENT,
            attributes=attributes or {},
        ),
        occurred_at=None,
        source_released_at=available_at,
        vendor_first_available_at=available_at,
        ingested_at=available_at,
        revision_no=revision_no,
        time_quality=quality,
        source_event_id=f"source-{event_id}",
        provider="eastmoney",
        source_url=f"https://example.test/{event_id}",
        raw_response_sha256="a" * 64,
        validation_status="validated",
    )


def test_event_is_aligned_to_its_session_without_changing_available_at() -> None:
    bars = make_bars([10, 11, 12])
    pre_open = datetime(2024, 1, 3, 8, 0, tzinfo=SHANGHAI)

    aligned = SignalRuntime().evaluate_aligned(
        condition(),
        bars,
        (event("event-1", pre_open),),
    )

    fact = aligned[1]
    assert fact is not None and fact.triggered
    assert fact.session_date == bars[1].session_date
    assert fact.observed_at == pre_open
    assert fact.available_at == pre_open
    assert len(fact.evidence) == 1
    assert fact.evidence[0].evidence_id == "event-1"
    assert fact.evidence[0].source_event_id == "source-event-1"
    assert fact.evidence[0].provider == "eastmoney"
    assert fact.evidence[0].time_quality == "exact"
    assert fact.evidence[0].available_at == pre_open
    assert aligned[0] is not None and not aligned[0].triggered


def test_later_revision_does_not_retrigger_an_event_already_seen() -> None:
    bars = make_bars([10, 11, 12, 13])
    first_time = datetime(2024, 1, 3, 8, 0, tzinfo=SHANGHAI)
    revised_time = first_time + timedelta(days=1)
    revisions = (
        event(
            "event-1",
            first_time,
            revision_no=0,
            attributes={"direction": "increase"},
        ),
        event(
            "event-1",
            revised_time,
            revision_no=1,
            attributes={"direction": "decrease"},
        ),
    )

    unfiltered = SignalRuntime().evaluate_aligned(condition(), bars, revisions)
    corrected = SignalRuntime().evaluate_aligned(
        condition(direction="decrease"),
        bars,
        revisions,
    )

    assert [index for index, fact in enumerate(unfiltered) if fact and fact.triggered] == [1]
    assert [index for index, fact in enumerate(corrected) if fact and fact.triggered] == [2]


def test_estimated_research_only_event_never_triggers() -> None:
    bars = make_bars([10, 11, 12])
    timestamp = datetime(2024, 1, 3, 8, 0, tzinfo=SHANGHAI)
    estimated = event(
        "event-estimated",
        timestamp,
        quality=TimeQuality.ESTIMATED_RESEARCH_ONLY,
    )

    aligned = SignalRuntime().evaluate_aligned(condition(), bars, (estimated,))

    assert not any(fact is not None and fact.triggered for fact in aligned)


def test_event_timeline_is_prefix_invariant_when_future_events_are_added() -> None:
    bars = make_bars([10, 11, 12, 13, 14])
    events = (
        event("event-1", datetime(2024, 1, 3, 8, 0, tzinfo=SHANGHAI)),
        event("event-2", datetime(2024, 1, 5, 16, 0, tzinfo=SHANGHAI)),
    )
    runtime = SignalRuntime()
    full = runtime.evaluate_aligned(condition(), bars, events)

    for prefix_length in range(1, len(bars) + 1):
        assert (
            runtime.evaluate_aligned(condition(), bars[:prefix_length], events)
            == full[:prefix_length]
        )
