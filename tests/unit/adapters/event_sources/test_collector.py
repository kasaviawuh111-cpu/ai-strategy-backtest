from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from ashare_lab.adapters.event_sources import (
    EventCollectionRequest,
    EventFetchBatch,
    build_event_acquisition_coverage,
    collect_event_observations,
)
from ashare_lab.domain.events import EventObservation
from ashare_lab.domain.market_data import TimeQuality
from ashare_lab.domain.shared import InstrumentId
from tests.unit.adapters.event_sources.query_evidence import eastmoney_query_evidence

SHANGHAI = ZoneInfo("Asia/Shanghai")
INSTRUMENT = InstrumentId("300059.SZ")
RETRIEVED = datetime(2026, 8, 29, 14, 0, tzinfo=SHANGHAI)
REQUEST = EventCollectionRequest(
    instrument_id=INSTRUMENT,
    start=date(2025, 1, 1),
    end=date(2026, 8, 29),
    retrieved_at=RETRIEVED,
)


def _observation(provider: str) -> EventObservation:
    return EventObservation(
        provider=provider,
        provider_event_id=f"{provider}-1",
        instrument_id=INSTRUMENT,
        event_code="event.financial_results.annual_report",
        title="东方财富年度报告",
        occurred_at=None,
        source_released_at=datetime(2026, 3, 18, 18, 0, tzinfo=SHANGHAI),
        vendor_first_available_at=None,
        retrieved_at=RETRIEVED,
        time_quality=TimeQuality.EXACT,
        document_url=f"https://example.test/{provider}",
        raw_response_sha256="a" * 64,
        validation_status="validated",
    )


def _eastmoney_batch(
    observations: tuple[EventObservation, ...],
    *,
    event_codes: tuple[str, ...] = ("event.financial_results.annual_report",),
) -> EventFetchBatch:
    return EventFetchBatch(
        observations=observations,
        acquisition_evidence=eastmoney_query_evidence(
            instrument_id=INSTRUMENT,
            start=REQUEST.start,
            end=REQUEST.end,
            event_codes=event_codes,
            total_hits=len(observations),
            event_counts=Counter(item.event_code for item in observations),
        ),
    )


def test_collection_order_is_fixed_and_optional_failure_is_visible() -> None:
    def failed(_request: EventCollectionRequest):
        raise PermissionError("account lacks entitlement")

    result = collect_event_observations(
        REQUEST,
        {
            "tushare": lambda _request: (_observation("tushare"),),
            "eastmoney": lambda _request: _eastmoney_batch((_observation("eastmoney"),)),
            "ifind": failed,
        },
    )

    assert [item.provider for item in result.sources] == [
        "eastmoney",
        "ifind",
        "rqdata",
        "tushare",
    ]
    assert result.available_providers == ("eastmoney", "tushare")
    assert result.sources[1].status == "failed"
    assert result.sources[1].error_type == "PermissionError"
    assert [item.provider for item in result.observations] == ["eastmoney", "tushare"]

    coverage = build_event_acquisition_coverage(
        REQUEST,
        result,
        requested_event_codes=("event.financial_results.annual_report",),
    )
    assert coverage["status"] == "complete"
    assert coverage["querySucceeded"] is True
    assert coverage["rowCount"] == 2
    assert coverage["auditSummary"]["requiredProvidersSatisfied"] == ["eastmoney"]
    assert coverage["auditSummary"]["optionalUnavailableProviders"] == ["ifind", "rqdata"]
    assert coverage["auditSummary"]["successfulProviders"] == ["eastmoney", "tushare"]
    assert len(coverage["auditSummary"]["requestSha256"]) == 64
    assert len(coverage["auditSummary"]["sourceStatusSha256"]) == 64


def test_successful_empty_primary_proves_zero_result_coverage() -> None:
    result = collect_event_observations(
        REQUEST,
        {"eastmoney": lambda _request: _eastmoney_batch(())},
    )

    coverage = build_event_acquisition_coverage(
        REQUEST,
        result,
        requested_event_codes=("event.financial_results.annual_report",),
    )

    assert coverage["status"] == "complete"
    assert coverage["zeroResult"] is True
    assert coverage["rowCount"] == 0
    assert coverage["sourceStatuses"][0] == {
        "provider": "eastmoney",
        "required": True,
        "status": "empty",
        "querySucceeded": True,
        "rowCount": 0,
        "errorType": None,
        "errorMessageSha256": None,
    }
    assert coverage["coverageByEventCode"]["event.financial_results.annual_report"] == {
        "lane": "announcement",
        "status": "complete",
        "requiredSources": ["eastmoney"],
        "requiredSourceStatus": "empty",
        "querySucceeded": True,
        "rowCount": 0,
        "zeroResult": True,
        "observedProviders": [],
        "coverageBasis": "complete_announcement_interval_with_provider_column_classification",
    }


def test_license_lane_is_incomplete_without_web_archive_interval_query() -> None:
    result = collect_event_observations(
        REQUEST,
        {
            "eastmoney": lambda _request: _eastmoney_batch(
                (),
                event_codes=("event.macro_policy_industry.license_approval",),
            )
        },
    )

    coverage = build_event_acquisition_coverage(
        REQUEST,
        result,
        requested_event_codes=("event.macro_policy_industry.license_approval",),
    )
    lane = coverage["coverageByEventCode"]["event.macro_policy_industry.license_approval"]

    assert coverage["status"] == "incomplete"
    assert lane["requiredSources"] == ["web_archive"]
    assert lane["requiredSourceStatus"] == "not_queried"
    assert lane["querySucceeded"] is False


def test_primary_failure_is_not_silently_replaced() -> None:
    def failed(_request: EventCollectionRequest):
        raise RuntimeError("network down")

    with pytest.raises(RuntimeError, match="primary Eastmoney"):
        collect_event_observations(
            REQUEST,
            {
                "eastmoney": failed,
                "ifind": lambda _request: (_observation("ifind"),),
            },
        )


def test_fetcher_cannot_change_provider_or_retrieval_clock() -> None:
    wrong_clock = replace(
        _observation("eastmoney"),
        retrieved_at=datetime(2026, 8, 29, 14, 1, tzinfo=SHANGHAI),
    )

    with pytest.raises(RuntimeError, match="primary Eastmoney"):
        collect_event_observations(
            REQUEST,
            {"eastmoney": lambda _request: (wrong_clock,)},
        )
