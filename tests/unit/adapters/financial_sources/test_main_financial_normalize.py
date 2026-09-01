from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from ashare_lab.adapters.financial_sources.eastmoney_operator import (
    OperatorDataset,
    OperatorReadingBatch,
    OperatorRequestAudit,
)
from ashare_lab.adapters.financial_sources.operator_normalize import (
    normalize_main_financial_facts,
)
from ashare_lab.domain.financials import (
    FinancialMetricId,
    FinancialPublicationEvidence,
    FinancialReportType,
    FinancialUnit,
)
from ashare_lab.domain.market_data import TimeQuality
from ashare_lab.domain.shared import InstrumentId

SHANGHAI = ZoneInfo("Asia/Shanghai")
RAW_HASH = "sha256:" + "a" * 64
EVENT_HASH = "sha256:" + "b" * 64
SNAPSHOT_ID = "financial:" + "c" * 64
RETRIEVED_AT = datetime(2026, 9, 1, 12, 0, tzinfo=SHANGHAI)
REPORT_PERIOD = date(2025, 6, 30)


def _publication() -> FinancialPublicationEvidence:
    return FinancialPublicationEvidence(
        instrument_id=InstrumentId("300059.SZ"),
        report_period=REPORT_PERIOD,
        report_type=FinancialReportType.SEMIANNUAL,
        notice_date=date(2025, 8, 15),
        update_date=date(2025, 8, 15),
        event_provider="eastmoney",
        event_code="event.financial_results.semiannual_report",
        provider_event_id="AN202508151234567890",
        event_revision_no=0,
        announced_at=datetime(2025, 8, 15, 20, 3, 2, tzinfo=SHANGHAI),
        vendor_first_available_at=datetime(2025, 8, 15, 20, 3, 5, tzinfo=SHANGHAI),
        first_available_at=datetime(2025, 8, 15, 20, 3, 5, tzinfo=SHANGHAI),
        time_quality=TimeQuality.VENDOR_OBSERVED,
        validation_status="validated",
        document_sha256=EVENT_HASH,
        raw_response_sha256=EVENT_HASH,
    )


def _batch() -> OperatorReadingBatch:
    row = {
        "SECUCODE": "300059.SZ",
        "REPORT_DATE": "2025-06-30 00:00:00",
        "REPORT_TYPE": "中报",
        "NOTICE_DATE": "2025-08-15 00:00:00",
        "UPDATE_DATE": "2025-08-15 00:00:00",
        "TOTALOPERATEREVE": 7_000_000_000.0,
        "TOTALOPERATEREVETZ": 53.22,
        "PARENTNETPROFIT": 5_000_000_000.0,
        "PARENTNETPROFITTZ": 44.85,
        "ROEJQ": 8.46,
        "XSMLL": 74.39,
        "XSJLL": 76.76,
        "NETCASH_OPERATE_PK": 4_500_000_000.0,
        "EPSJB": 1.27,
        "MGJYXJJE": None,
    }
    return OperatorReadingBatch(
        dataset=OperatorDataset.MAIN_FINANCIAL_DATA,
        instrument_id="300059.SZ",
        rows=(row,),
        row_raw_response_sha256=(RAW_HASH,),
        row_retrieved_at=(RETRIEVED_AT,),
        requests=(
            OperatorRequestAudit(
                dataset=OperatorDataset.MAIN_FINANCIAL_DATA,
                page=1,
                raw_response_sha256=RAW_HASH,
                retrieved_at=RETRIEVED_AT,
                row_count=1,
            ),
        ),
        canonical_rows_sha256="sha256:" + "d" * 64,
    )


def test_main_financial_fields_are_direct_and_bound_to_the_announcement_clock() -> None:
    publication = _publication()

    facts = normalize_main_financial_facts(
        _batch(),
        publication_evidence={REPORT_PERIOD: publication},
        snapshot_id=SNAPSHOT_ID,
    )

    by_metric = {item.metric_id: item for item in facts}
    assert by_metric[FinancialMetricId.REVENUE].value == Decimal("7000000000.0")
    assert by_metric[FinancialMetricId.REVENUE_YOY].value == Decimal("53.22")
    assert by_metric[FinancialMetricId.NET_PROFIT_PARENT].value == Decimal("5000000000.0")
    assert by_metric[FinancialMetricId.NET_PROFIT_PARENT_YOY].value == Decimal("44.85")
    assert by_metric[FinancialMetricId.ROE].value == Decimal("8.46")
    assert by_metric[FinancialMetricId.GROSS_MARGIN].value == Decimal("74.39")
    assert by_metric[FinancialMetricId.NET_MARGIN].value == Decimal("76.76")
    assert all(
        by_metric[metric_id].unit is FinancialUnit.PERCENT
        for metric_id in (
            FinancialMetricId.REVENUE_YOY,
            FinancialMetricId.NET_PROFIT_PARENT_YOY,
            FinancialMetricId.ROE,
            FinancialMetricId.GROSS_MARGIN,
            FinancialMetricId.NET_MARGIN,
        )
    )
    assert by_metric[FinancialMetricId.OPERATING_CASH_FLOW].value == Decimal("4500000000.0")
    assert by_metric[FinancialMetricId.BASIC_EPS].value == Decimal("1.27")
    assert by_metric[FinancialMetricId.OPERATING_CASH_FLOW_PER_SHARE].value is None
    assert all(item.value_origin.value == "provider_raw" for item in facts)
    assert all(
        item.availability.first_available_at == publication.first_available_at for item in facts
    )
    assert all(item.revision_id.endswith(publication.revision_id) for item in facts)
