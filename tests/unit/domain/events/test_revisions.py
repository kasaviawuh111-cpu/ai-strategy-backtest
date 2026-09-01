from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from ashare_lab.domain.events.observations import EventAttribute, EventObservation
from ashare_lab.domain.events.revisions import (
    RevisionResolutionState,
    resolve_revision_chain,
)
from ashare_lab.domain.market_data import TimeQuality
from ashare_lab.domain.shared import InstrumentId

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 31, hour, minute, tzinfo=SHANGHAI)


def _observation(
    *,
    provider: str = "eastmoney",
    provider_event_id: str = "fact-1:r0",
    revision_no: int = 0,
    revision_type: str = "initial",
    available_at: datetime | None = None,
    title: str = "初始公告",
    document_sha256: str | None = "b" * 64,
    semantic_attributes: dict[str, EventAttribute] | None = None,
    validation_status: str = "validated",
    time_quality: TimeQuality = TimeQuality.EXACT,
) -> EventObservation:
    attributes: dict[str, EventAttribute] = {
        "provider": provider,
        "revision_id": f"fact-1:r{revision_no}",
        "revision_type": revision_type,
        "source_event_id": provider_event_id,
    }
    attributes.update(semantic_attributes or {})
    return EventObservation(
        provider=provider,
        provider_event_id=provider_event_id,
        instrument_id=InstrumentId("300059.SZ"),
        event_code="event.financial_results.earnings_forecast_published",
        title=title,
        occurred_at=None,
        source_released_at=available_at,
        vendor_first_available_at=None,
        retrieved_at=_at(23),
        time_quality=time_quality,
        document_sha256=document_sha256,
        raw_response_sha256="a" * 64,
        external_fact_id="fact-1",
        validation_status=validation_status,
        attributes=attributes,
        revision_no=revision_no,
    )


def test_exact_source_duplicate_is_deduplicated() -> None:
    initial = _observation(available_at=_at(9))

    result = resolve_revision_chain((initial, initial), decision_at=_at(10))

    assert result.state is RevisionResolutionState.ACTIVE
    assert result.active_revision_no == 0
    assert result.deduplicated_count == 1
    assert result.visible_observations == (initial,)
    assert result.can_emit_signal
    assert result.permits_retry(0)


def test_contradictory_source_duplicate_is_quarantined() -> None:
    initial = _observation(available_at=_at(9))
    contradiction = _observation(available_at=_at(9), title="同键但内容不同")

    result = resolve_revision_chain((initial, contradiction), decision_at=_at(10))

    assert result.state is RevisionResolutionState.QUARANTINED
    assert result.conflicts[0].code == "contradictory_provider_revision"
    assert not result.can_emit_signal
    assert not result.permits_retry(0)


def test_multi_source_revision_role_conflict_is_quarantined() -> None:
    initial = _observation(available_at=_at(9))
    amendment = _observation(
        provider="ifind",
        provider_event_id="ifind-fact-1:r1",
        revision_no=1,
        revision_type="amendment",
        available_at=_at(9, 30),
        title="修订公告",
    )
    cancellation = _observation(
        provider="rqdata",
        provider_event_id="rqdata-fact-1:r1",
        revision_no=1,
        revision_type="cancellation",
        available_at=_at(9, 31),
        title="撤回公告",
    )

    result = resolve_revision_chain(
        (initial, amendment, cancellation),
        decision_at=_at(10),
    )

    assert result.state is RevisionResolutionState.QUARANTINED
    assert result.conflicts[0].code == "multi_source_revision_conflict"


@pytest.mark.parametrize(
    ("left_attributes", "right_attributes"),
    [
        ({"forecast_direction": "increase"}, {"forecast_direction": "decrease"}),
        ({"forecast_value": 100}, {"forecast_value": 200}),
        ({"withdrawal": False}, {"withdrawal": True}),
    ],
)
def test_multi_source_same_role_structured_semantic_conflict_is_quarantined(
    left_attributes: dict[str, EventAttribute],
    right_attributes: dict[str, EventAttribute],
) -> None:
    initial = _observation(available_at=_at(9))
    left = _observation(
        provider="ifind",
        provider_event_id="ifind-fact-1:r1",
        revision_no=1,
        revision_type="amendment",
        available_at=_at(9, 30),
        title="修订公告",
        semantic_attributes=left_attributes,
    )
    right = _observation(
        provider="rqdata",
        provider_event_id="rqdata-fact-1:r1",
        revision_no=1,
        revision_type="amendment",
        available_at=_at(9, 31),
        title="修订公告",
        semantic_attributes=right_attributes,
    )

    result = resolve_revision_chain((initial, left, right), decision_at=_at(10))

    assert result.state is RevisionResolutionState.QUARANTINED
    assert result.conflicts[0].code == "multi_source_revision_semantic_conflict"


@pytest.mark.parametrize(
    ("left_title", "right_title", "left_hash", "right_hash"),
    [
        ("修订公告甲", "修订公告乙", "b" * 64, "b" * 64),
        ("修订公告", "修订公告", "b" * 64, "c" * 64),
    ],
)
def test_multi_source_same_role_title_or_document_conflict_is_quarantined(
    left_title: str,
    right_title: str,
    left_hash: str,
    right_hash: str,
) -> None:
    initial = _observation(available_at=_at(9))
    left = _observation(
        provider="ifind",
        provider_event_id="ifind-fact-1:r1",
        revision_no=1,
        revision_type="amendment",
        available_at=_at(9, 30),
        title=left_title,
        document_sha256=left_hash,
    )
    right = _observation(
        provider="rqdata",
        provider_event_id="rqdata-fact-1:r1",
        revision_no=1,
        revision_type="amendment",
        available_at=_at(9, 31),
        title=right_title,
        document_sha256=right_hash,
    )

    result = resolve_revision_chain((initial, left, right), decision_at=_at(10))

    assert result.state is RevisionResolutionState.QUARANTINED
    assert result.conflicts[0].code == "multi_source_revision_semantic_conflict"


def test_multi_source_same_semantics_coexist_despite_provider_audit_fields() -> None:
    initial = _observation(available_at=_at(9))
    left = _observation(
        provider="ifind",
        provider_event_id="ifind-fact-1:r1",
        revision_no=1,
        revision_type="amendment",
        available_at=_at(9, 30),
        title="修订公告",
        semantic_attributes={"forecast_direction": "increase", "forecast_value": 100},
    )
    right = _observation(
        provider="rqdata",
        provider_event_id="rqdata-fact-1:r1",
        revision_no=1,
        revision_type="amendment",
        available_at=_at(9, 31),
        title="修订公告",
        semantic_attributes={"forecast_direction": "increase", "forecast_value": 100},
    )

    result = resolve_revision_chain((initial, left, right), decision_at=_at(10))

    assert result.state is RevisionResolutionState.ACTIVE
    assert result.active_observations == (left, right)
    assert result.permits_retry(1)


def test_late_same_session_withdrawal_is_not_backfilled_before_decision() -> None:
    initial = _observation(available_at=_at(9))
    cancellation = _observation(
        provider_event_id="fact-1:r1",
        revision_no=1,
        revision_type="cancellation",
        available_at=_at(10, 30),
        title="撤回公告",
    )

    before = resolve_revision_chain(
        (initial, cancellation),
        decision_at=_at(10),
    )
    after = resolve_revision_chain(
        (initial, cancellation),
        decision_at=_at(11),
    )

    assert before.state is RevisionResolutionState.ACTIVE
    assert before.visible_observations == (initial,)
    assert before.permits_retry(0)
    assert after.state is RevisionResolutionState.WITHDRAWN
    assert after.active_revision_no is None
    assert not after.can_emit_signal
    assert not after.permits_retry(0)


def test_amendment_supersedes_old_signal_and_requires_new_revision() -> None:
    initial = _observation(available_at=_at(9))
    amendment = _observation(
        provider_event_id="fact-1:r1",
        revision_no=1,
        revision_type="amendment",
        available_at=_at(10, 30),
        title="修订公告",
    )

    result = resolve_revision_chain((initial, amendment), decision_at=_at(11))

    assert result.state is RevisionResolutionState.ACTIVE
    assert result.active_revision_no == 1
    assert result.superseded_revision_nos == (0,)
    assert not result.permits_retry(0)
    assert result.permits_retry(1)


def test_missing_reliable_first_available_at_fails_closed() -> None:
    unreliable = _observation(
        available_at=None,
        validation_status="blocked_time_quality",
        time_quality=TimeQuality.DATE_ONLY_CONSERVATIVE,
    )

    result = resolve_revision_chain((unreliable,), decision_at=_at(10))

    assert result.state is RevisionResolutionState.QUARANTINED
    assert result.conflicts[0].code == "missing_reliable_first_available_at"
    assert not result.can_emit_signal


def test_revision_gap_fails_closed() -> None:
    initial = _observation(available_at=_at(9))
    revision_two = _observation(
        provider_event_id="fact-1:r2",
        revision_no=2,
        revision_type="correction",
        available_at=_at(9, 30),
        title="更正公告",
    )

    result = resolve_revision_chain((initial, revision_two), decision_at=_at(10))

    assert result.state is RevisionResolutionState.QUARANTINED
    assert result.conflicts[0].code == "revision_gap"


def test_naive_decision_time_is_rejected() -> None:
    with pytest.raises(ValueError, match="decision_at"):
        resolve_revision_chain(
            (_observation(available_at=_at(9)),),
            decision_at=datetime(2026, 8, 31, 10),
        )
