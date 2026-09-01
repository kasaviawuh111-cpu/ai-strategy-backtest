from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from ashare_lab.adapters.financial_sources.eastmoney_operator import (
    OPERATOR_READING_PROVIDER,
    OperatorDataset,
    OperatorReadingBatch,
    OperatorRequestAudit,
)
from ashare_lab.adapters.financial_sources.operator_normalize import (
    FinancialPublicationTime,
    OperatorReadingNormalizationError,
    normalize_latest_indicator_facts,
    normalize_valuation_facts,
)
from ashare_lab.domain.financials import (
    FinancialMetricId,
    FinancialPeriodBasis,
    FinancialUnit,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
RAW_HASH = "sha256:" + "a" * 64
SNAPSHOT_ID = "financial:" + "b" * 64
RETRIEVED_AT = datetime(2026, 9, 1, 12, 0, tzinfo=SHANGHAI)


def latest_batch() -> OperatorReadingBatch:
    rows = (
        {
            "SECUCODE": "300059.SZ",
            "REPORT_DATE": "2025-06-30 00:00:00",
            "TOTAL_OPERATE_INCOME": 7_000_000_000.0,
            "TOI_YOY_RATIO": 53.22,
            "PARENT_NETPROFIT": 5_000_000_000.0,
            "PNP_YOY_RATIO": 44.85,
            "ROE": 8.46,
            "ROTA": 1.79,
            "NPR": 76.76,
            "GROSS_PROFIT_RATIO": 74.39,
            "PER_NETCASH_OPERATE": None,
        },
    )
    audit = OperatorRequestAudit(
        dataset=OperatorDataset.LATEST_INDICATORS,
        page=1,
        raw_response_sha256=RAW_HASH,
        retrieved_at=RETRIEVED_AT,
        row_count=1,
    )
    return OperatorReadingBatch(
        dataset=OperatorDataset.LATEST_INDICATORS,
        instrument_id="300059.SZ",
        rows=rows,
        row_raw_response_sha256=(RAW_HASH,),
        row_retrieved_at=(RETRIEVED_AT,),
        requests=(audit,),
        canonical_rows_sha256="sha256:" + "c" * 64,
    )


def publication() -> FinancialPublicationTime:
    return FinancialPublicationTime(
        report_period=date(2025, 6, 30),
        announced_at=datetime(2025, 8, 15, 20, 3, 2, tzinfo=SHANGHAI),
        first_available_at=datetime(2025, 8, 15, 20, 3, 2, tzinfo=SHANGHAI),
        revision_id="AN202508151234567890",
    )


def test_latest_financial_values_are_direct_and_percentages_are_not_scaled() -> None:
    facts = normalize_latest_indicator_facts(
        latest_batch(),
        publication_times={date(2025, 6, 30): publication()},
        snapshot_id=SNAPSHOT_ID,
    )
    by_metric = {item.metric_id: item for item in facts}

    assert by_metric[FinancialMetricId.REVENUE_YOY].value == Decimal("53.22")
    assert by_metric[FinancialMetricId.REVENUE_YOY].unit is FinancialUnit.PERCENT
    assert by_metric[FinancialMetricId.ROE].value == Decimal("8.46")
    assert by_metric[FinancialMetricId.ROE].unit is FinancialUnit.PERCENT
    assert by_metric[FinancialMetricId.OPERATING_CASH_FLOW_PER_SHARE].value is None
    assert by_metric[FinancialMetricId.REVENUE].source_field == "TOTAL_OPERATE_INCOME"
    assert all(item.value_origin.value == "provider_raw" for item in facts)
    assert all(item.availability.source == OPERATOR_READING_PROVIDER for item in facts)


def test_missing_report_publication_time_fails_closed() -> None:
    with pytest.raises(OperatorReadingNormalizationError, match="publication time"):
        normalize_latest_indicator_facts(
            latest_batch(), publication_times={}, snapshot_id=SNAPSHOT_ID
        )


def test_valuation_date_is_only_available_after_that_market_close() -> None:
    audit = OperatorRequestAudit(
        dataset=OperatorDataset.VALUATION_TREND,
        page=1,
        raw_response_sha256=RAW_HASH,
        retrieved_at=RETRIEVED_AT,
        row_count=1,
    )
    batch = OperatorReadingBatch(
        dataset=OperatorDataset.VALUATION_TREND,
        instrument_id="300059.SZ",
        rows=(
            {
                "SECUCODE": "300059.SZ",
                "TRADE_DATE": "2025-08-29 00:00:00",
                "INDICATORTYPE": "1",
                "INDICATOR_VALUE": 21.03983572,
            },
        ),
        row_raw_response_sha256=(RAW_HASH,),
        row_retrieved_at=(RETRIEVED_AT,),
        requests=(audit,),
        canonical_rows_sha256="sha256:" + "c" * 64,
        indicator_type=1,
        statistics_cycle=4,
    )

    facts = normalize_valuation_facts((batch,), snapshot_id=SNAPSHOT_ID)

    assert len(facts) == 1
    assert facts[0].metric_id is FinancialMetricId.PE
    assert facts[0].value == Decimal("21.03983572")
    assert facts[0].period_basis is FinancialPeriodBasis.POINT_IN_TIME
    assert facts[0].availability.first_available_at == datetime(2025, 8, 29, 15, 0, tzinfo=SHANGHAI)
