from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from ashare_lab.adapters.financial_sources.publication_resolver import (
    FinancialPublicationResolutionError,
    resolve_financial_publication_evidence,
)
from ashare_lab.domain.events import EventObservation
from ashare_lab.domain.financials import FinancialReportType
from ashare_lab.domain.market_data import TimeQuality
from ashare_lab.domain.shared import InstrumentId

SHANGHAI = ZoneInfo("Asia/Shanghai")
INSTRUMENT = InstrumentId("300059.SZ")
RAW_HASH = "a" * 64
DOCUMENT_HASH = "b" * 64


def _shape(report_period: date) -> tuple[str, str, str]:
    if (report_period.month, report_period.day) == (3, 31):
        return (
            "一季报",
            "quarterly_report",
            "event.financial_results.quarterly_report",
        )
    if (report_period.month, report_period.day) == (6, 30):
        return (
            "中报",
            "semiannual_report",
            "event.financial_results.semiannual_report",
        )
    if (report_period.month, report_period.day) == (9, 30):
        return (
            "三季报",
            "quarterly_report",
            "event.financial_results.quarterly_report",
        )
    if (report_period.month, report_period.day) == (12, 31):
        return (
            "年报",
            "annual_report",
            "event.financial_results.annual_report",
        )
    raise AssertionError("test data must use a standard A-share reporting period")


def _f10_row(
    report_period: date = date(2025, 12, 31),
    *,
    notice_date: date = date(2026, 3, 14),
    update_date: date | None = None,
    report_type: str | None = None,
) -> Mapping[str, object]:
    f10_report_type, _, _ = _shape(report_period)
    return {
        "REPORT_DATE": f"{report_period.isoformat()} 00:00:00",
        "REPORT_TYPE": report_type or f10_report_type,
        "NOTICE_DATE": f"{notice_date.isoformat()} 00:00:00",
        "UPDATE_DATE": f"{(update_date or notice_date).isoformat()} 00:00:00",
    }


def _observation(
    report_period: date = date(2025, 12, 31),
    *,
    source_released_at: datetime | None = datetime(2026, 3, 14, 20, 57, 30, tzinfo=SHANGHAI),
    vendor_first_available_at: datetime | None = datetime(2026, 3, 14, 20, 57, 33, tzinfo=SHANGHAI),
    provider_event_id: str = "AN202603141626765931",
    revision_no: int = 0,
    validation_status: str = "validated",
    time_quality: TimeQuality = TimeQuality.VENDOR_OBSERVED,
    attributes: Mapping[str, object] | None = None,
    retrieved_at: datetime = datetime(2026, 8, 31, 12, 0, tzinfo=SHANGHAI),
) -> EventObservation:
    _, event_report_type, event_code = _shape(report_period)
    merged_attributes: dict[str, object] = {
        "report_type": event_report_type,
        "stat_date": report_period.isoformat(),
        "report_period_quality": "title_exact",
        "document_version_role": "initial_complete",
        "raw_notice_date": "2026-03-14",
    }
    merged_attributes.update(attributes or {})
    return EventObservation(
        provider="eastmoney",
        provider_event_id=provider_event_id,
        instrument_id=INSTRUMENT,
        event_code=event_code,
        title=f"东方财富{report_period.year}年定期报告",
        occurred_at=None,
        source_released_at=source_released_at,
        vendor_first_available_at=vendor_first_available_at,
        retrieved_at=retrieved_at,
        time_quality=time_quality,
        document_sha256=DOCUMENT_HASH,
        raw_response_sha256=RAW_HASH,
        validation_status=validation_status,
        attributes=merged_attributes,  # type: ignore[arg-type]
        revision_no=revision_no,
    )


def test_binds_f10_row_to_exact_initial_complete_annual_report() -> None:
    evidence = resolve_financial_publication_evidence(
        instrument_id=INSTRUMENT,
        f10_row=_f10_row(),
        observations=(_observation(),),
    )

    assert evidence.report_period == date(2025, 12, 31)
    assert evidence.report_type is FinancialReportType.ANNUAL
    assert evidence.provider_event_id == "AN202603141626765931"
    assert evidence.announced_at == datetime(2026, 3, 14, 20, 57, 30, tzinfo=SHANGHAI)
    assert evidence.first_available_at == datetime(2026, 3, 14, 20, 57, 33, tzinfo=SHANGHAI)
    assert evidence.binding_hash.startswith("sha256:")
    assert evidence.revision_id == evidence.binding_hash
    assert "retrieved_at" not in evidence.to_dict()


@pytest.mark.parametrize(
    ("report_period", "expected_type"),
    (
        (date(2025, 3, 31), FinancialReportType.Q1),
        (date(2025, 9, 30), FinancialReportType.Q3),
    ),
)
def test_binds_q1_and_q3_to_the_matching_quarterly_report(
    report_period: date,
    expected_type: FinancialReportType,
) -> None:
    evidence = resolve_financial_publication_evidence(
        instrument_id=INSTRUMENT,
        f10_row=_f10_row(report_period),
        observations=(_observation(report_period),),
    )

    assert evidence.report_type is expected_type
    assert evidence.report_period == report_period


def test_binding_hash_excludes_collection_time() -> None:
    first = resolve_financial_publication_evidence(
        instrument_id=INSTRUMENT,
        f10_row=_f10_row(),
        observations=(_observation(retrieved_at=datetime(2026, 8, 31, 12, 0, tzinfo=SHANGHAI)),),
    )
    repeated = resolve_financial_publication_evidence(
        instrument_id=INSTRUMENT,
        f10_row=_f10_row(),
        observations=(_observation(retrieved_at=datetime(2026, 9, 1, 12, 0, tzinfo=SHANGHAI)),),
    )

    assert first.binding_hash == repeated.binding_hash


def test_vendor_observed_time_is_usable_without_a_display_time() -> None:
    evidence = resolve_financial_publication_evidence(
        instrument_id=INSTRUMENT,
        f10_row=_f10_row(),
        observations=(
            _observation(
                source_released_at=None,
                vendor_first_available_at=datetime(2026, 3, 14, 19, 56, 49, tzinfo=SHANGHAI),
            ),
        ),
    )

    assert evidence.announced_at == datetime(2026, 3, 14, 19, 56, 49, tzinfo=SHANGHAI)
    assert evidence.first_available_at == evidence.announced_at


def test_rejects_missing_both_source_release_and_vendor_time() -> None:
    with pytest.raises(FinancialPublicationResolutionError, match="availability time"):
        resolve_financial_publication_evidence(
            instrument_id=INSTRUMENT,
            f10_row=_f10_row(),
            observations=(_observation(source_released_at=None, vendor_first_available_at=None),),
        )


@pytest.mark.parametrize(
    ("validation_status", "time_quality", "message"),
    (
        ("unverified", TimeQuality.EXACT, "validated"),
        ("validated", TimeQuality.DATE_ONLY_CONSERVATIVE, "time_quality"),
    ),
)
def test_rejects_unvalidated_or_non_precise_announcement_time(
    validation_status: str,
    time_quality: TimeQuality,
    message: str,
) -> None:
    with pytest.raises(FinancialPublicationResolutionError, match=message):
        resolve_financial_publication_evidence(
            instrument_id=INSTRUMENT,
            f10_row=_f10_row(),
            observations=(
                _observation(validation_status=validation_status, time_quality=time_quality),
            ),
        )


def test_rejects_f10_notice_date_that_does_not_match_announcement_day() -> None:
    with pytest.raises(FinancialPublicationResolutionError, match="NOTICE_DATE"):
        resolve_financial_publication_evidence(
            instrument_id=INSTRUMENT,
            f10_row=_f10_row(notice_date=date(2026, 3, 13)),
            observations=(_observation(),),
        )


def test_rejects_duplicate_initial_complete_reports_for_one_period() -> None:
    with pytest.raises(FinancialPublicationResolutionError, match="ambiguous"):
        resolve_financial_publication_evidence(
            instrument_id=INSTRUMENT,
            f10_row=_f10_row(),
            observations=(
                _observation(provider_event_id="AN202603141626765931"),
                _observation(provider_event_id="AN202603141626765932"),
            ),
        )


def test_rejects_non_initial_or_unresolved_periodic_document() -> None:
    with pytest.raises(FinancialPublicationResolutionError, match="initial_complete"):
        resolve_financial_publication_evidence(
            instrument_id=INSTRUMENT,
            f10_row=_f10_row(),
            observations=(_observation(attributes={"document_version_role": "revision"}),),
        )

    with pytest.raises(FinancialPublicationResolutionError, match="report_period_quality"):
        resolve_financial_publication_evidence(
            instrument_id=INSTRUMENT,
            f10_row=_f10_row(),
            observations=(_observation(attributes={"report_period_quality": "unresolved"}),),
        )


def test_rejects_later_f10_update_without_a_revision_evidence_chain() -> None:
    with pytest.raises(FinancialPublicationResolutionError, match="UPDATE_DATE"):
        resolve_financial_publication_evidence(
            instrument_id=INSTRUMENT,
            f10_row=_f10_row(update_date=date(2026, 3, 15)),
            observations=(_observation(),),
        )
