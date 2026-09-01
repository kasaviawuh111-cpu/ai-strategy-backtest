from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from ashare_lab.domain.financials import (
    FinancialFactRecord,
    FinancialMetricId,
    FinancialPeriodBasis,
    FinancialReportType,
    FinancialStatementScope,
    FinancialUnit,
    FinancialValueOrigin,
)
from ashare_lab.domain.provenance import SourceKind, SourceRef
from ashare_lab.domain.signals import SignalRuntime
from ashare_lab.domain.strategy import FinancialConditionV1
from ashare_lab.domain.time import PointInTimeAvailability

from .conftest import make_bars

SHANGHAI = ZoneInfo("Asia/Shanghai")
RAW_HASH = "sha256:" + "a" * 64
SNAPSHOT_ID = "financial:" + "b" * 64


def condition() -> FinancialConditionV1:
    return FinancialConditionV1(
        metric_id=FinancialMetricId.REVENUE_YOY,
        report_type=FinancialReportType.ANNUAL,
        period_basis=FinancialPeriodBasis.FULL_YEAR,
        statement_scope=FinancialStatementScope.CONSOLIDATED,
        comparator="gt",
        value=Decimal("20"),
        unit=FinancialUnit.PERCENT,
    )


def fact(*, first_available_at: datetime, value: Decimal | None) -> FinancialFactRecord:
    revision_id = "eastmoney-operator:annual:2023"
    return FinancialFactRecord(
        instrument_id="300059.SZ",
        metric_id=FinancialMetricId.REVENUE_YOY,
        value=value,
        value_origin=FinancialValueOrigin.PROVIDER_RAW,
        provider="eastmoney-operator-reading",
        source_dataset="RPT_F10_FN_LATESTINDIC",
        source_field="TOI_YOY_RATIO",
        report_period=date(2023, 12, 31),
        report_type=FinancialReportType.ANNUAL,
        period_basis=FinancialPeriodBasis.FULL_YEAR,
        statement_scope=FinancialStatementScope.CONSOLIDATED,
        unit=FinancialUnit.PERCENT,
        availability=PointInTimeAvailability(
            observed_at=datetime(2023, 12, 31, tzinfo=SHANGHAI),
            announced_at=first_available_at,
            first_available_at=first_available_at,
            signal_at=None,
            execution_at=None,
            retrieved_at=datetime(2026, 9, 1, 12, tzinfo=SHANGHAI),
            timezone="Asia/Shanghai",
            source="eastmoney-operator-reading",
            revision_id=revision_id,
        ),
        revision_id=revision_id,
        raw_response_sha256=RAW_HASH,
        snapshot_id=SNAPSHOT_ID,
        source_refs=(
            SourceRef(
                provider="eastmoney-operator-reading",
                source_id="RPT_F10_FN_LATESTINDIC:300059.SZ:2023-12-31:TOI_YOY_RATIO",
                snapshot_id=SNAPSHOT_ID,
                schema_version="eastmoney-operator-reading.v1",
                content_sha256=RAW_HASH,
                source_kind=SourceKind.PROVIDER_RECORD,
            ),
        ),
    )


def test_financial_fact_is_invisible_before_first_available_at() -> None:
    bars = make_bars([10, 10, 10])
    available_at = datetime.combine(bars[1].session_date, time(20), tzinfo=SHANGHAI)

    aligned = SignalRuntime().evaluate_aligned(
        condition(),
        bars,
        financial_facts=(fact(first_available_at=available_at, value=Decimal("25")),),
    )

    assert aligned[:2] == (None, None)
    assert aligned[2] is not None
    assert aligned[2].triggered is True
    assert aligned[2].left_value == Decimal("25")
    assert aligned[2].evidence[0].raw_response_sha256 == RAW_HASH


def test_null_provider_value_stays_no_signal_instead_of_falling_back_or_zero() -> None:
    bars = make_bars([10, 10])
    available_at = datetime.combine(bars[0].session_date, time(9), tzinfo=SHANGHAI)

    aligned = SignalRuntime().evaluate_aligned(
        condition(),
        bars,
        financial_facts=(fact(first_available_at=available_at, value=None),),
    )

    assert aligned == (None, None)


def test_future_financial_suffix_cannot_change_existing_signal_prefix() -> None:
    bars = make_bars([10, 10, 10])
    first = fact(
        first_available_at=datetime.combine(bars[0].session_date, time(9), tzinfo=SHANGHAI),
        value=Decimal("25"),
    )

    full = SignalRuntime().evaluate_aligned(condition(), bars, financial_facts=(first,))

    for prefix_length in range(1, len(bars) + 1):
        assert (
            SignalRuntime().evaluate_aligned(
                condition(),
                bars[:prefix_length],
                financial_facts=(first,),
            )
            == full[:prefix_length]
        )
