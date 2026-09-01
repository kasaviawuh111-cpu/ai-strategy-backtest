"""Normalize ``操盘必读`` direct fields into strict PIT financial facts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Final
from zoneinfo import ZoneInfo

from ashare_lab.domain.financials import (
    FinancialFactRecord,
    FinancialMetricId,
    FinancialPeriodBasis,
    FinancialPublicationEvidence,
    FinancialReportType,
    FinancialStatementScope,
    FinancialUnit,
    FinancialValueOrigin,
)
from ashare_lab.domain.provenance import SourceKind, SourceRef
from ashare_lab.domain.shared import require_aware
from ashare_lab.domain.time import PointInTimeAvailability

from .eastmoney_operator import (
    OPERATOR_READING_PROVIDER,
    OPERATOR_READING_SCHEMA_VERSION,
    OperatorDataset,
    OperatorReadingBatch,
)

_SHANGHAI: Final = ZoneInfo("Asia/Shanghai")


class OperatorReadingNormalizationError(ValueError):
    """A provider row cannot become a replay-safe financial fact."""


@dataclass(frozen=True, slots=True)
class FinancialPublicationTime:
    report_period: date
    announced_at: datetime
    first_available_at: datetime
    revision_id: str

    def __post_init__(self) -> None:
        require_aware(self.announced_at, "announced_at")
        require_aware(self.first_available_at, "first_available_at")
        if self.announced_at.tzinfo != _SHANGHAI or self.first_available_at.tzinfo != _SHANGHAI:
            raise OperatorReadingNormalizationError("publication times must use Asia/Shanghai")
        if self.announced_at > self.first_available_at:
            raise OperatorReadingNormalizationError(
                "first_available_at cannot precede announced_at"
            )
        if not self.revision_id.strip() or self.revision_id != self.revision_id.strip():
            raise OperatorReadingNormalizationError("revision_id must be nonblank exact text")


@dataclass(frozen=True, slots=True)
class _Binding:
    source_field: str
    metric_id: FinancialMetricId
    unit: FinancialUnit


_LATEST_BINDINGS = (
    _Binding("BASIC_EPS", FinancialMetricId.BASIC_EPS, FinancialUnit.CNY_PER_SHARE),
    _Binding("BVPS", FinancialMetricId.BOOK_VALUE_PER_SHARE, FinancialUnit.CNY_PER_SHARE),
    _Binding(
        "PER_CAPITAL_RESERVE",
        FinancialMetricId.CAPITAL_RESERVE_PER_SHARE,
        FinancialUnit.CNY_PER_SHARE,
    ),
    _Binding(
        "PER_UNASSIGN_PROFIT",
        FinancialMetricId.UNASSIGNED_PROFIT_PER_SHARE,
        FinancialUnit.CNY_PER_SHARE,
    ),
    _Binding(
        "PER_NETCASH_OPERATE",
        FinancialMetricId.OPERATING_CASH_FLOW_PER_SHARE,
        FinancialUnit.CNY_PER_SHARE,
    ),
    _Binding("TOTAL_OPERATE_INCOME", FinancialMetricId.REVENUE, FinancialUnit.CNY),
    _Binding("TOI_YOY_RATIO", FinancialMetricId.REVENUE_YOY, FinancialUnit.PERCENT),
    _Binding("PARENT_NETPROFIT", FinancialMetricId.NET_PROFIT_PARENT, FinancialUnit.CNY),
    _Binding(
        "PNP_YOY_RATIO",
        FinancialMetricId.NET_PROFIT_PARENT_YOY,
        FinancialUnit.PERCENT,
    ),
    _Binding("GROSS_PROFIT", FinancialMetricId.GROSS_PROFIT, FinancialUnit.CNY),
    _Binding("DEDUCT_PARENT_NETPROFIT", FinancialMetricId.DEDUCTED_NET_PROFIT, FinancialUnit.CNY),
    _Binding("DPNP_YOY_RATIO", FinancialMetricId.DEDUCTED_NET_PROFIT_YOY, FinancialUnit.RATIO),
    _Binding("ROE", FinancialMetricId.ROE, FinancialUnit.PERCENT),
    _Binding("NPR", FinancialMetricId.NET_MARGIN, FinancialUnit.PERCENT),
    _Binding("GROSS_PROFIT_RATIO", FinancialMetricId.GROSS_MARGIN, FinancialUnit.PERCENT),
    _Binding("TOTAL_ASSETS_TR", FinancialMetricId.TOTAL_ASSETS_TURNOVER, FinancialUnit.TIMES),
    _Binding("INVENTORY_TR", FinancialMetricId.INVENTORY_TURNOVER, FinancialUnit.TIMES),
    _Binding(
        "ACCOUNTS_RECE_TR",
        FinancialMetricId.ACCOUNTS_RECEIVABLE_TURNOVER,
        FinancialUnit.TIMES,
    ),
)

# These names are returned directly by RPT_F10_FINANCE_MAINFINADATA.  Keep the
# provider's value and unit as-is: this adapter must never derive a quarter,
# TTM, growth rate, margin, or ratio from another field.
_MAIN_FINANCIAL_BINDINGS = (
    _Binding("EPSJB", FinancialMetricId.BASIC_EPS, FinancialUnit.CNY_PER_SHARE),
    _Binding("BPS", FinancialMetricId.BOOK_VALUE_PER_SHARE, FinancialUnit.CNY_PER_SHARE),
    _Binding(
        "MGZBGJ",
        FinancialMetricId.CAPITAL_RESERVE_PER_SHARE,
        FinancialUnit.CNY_PER_SHARE,
    ),
    _Binding(
        "MGWFPLR",
        FinancialMetricId.UNASSIGNED_PROFIT_PER_SHARE,
        FinancialUnit.CNY_PER_SHARE,
    ),
    _Binding(
        "MGJYXJJE",
        FinancialMetricId.OPERATING_CASH_FLOW_PER_SHARE,
        FinancialUnit.CNY_PER_SHARE,
    ),
    _Binding("TOTALOPERATEREVE", FinancialMetricId.REVENUE, FinancialUnit.CNY),
    _Binding("TOTALOPERATEREVETZ", FinancialMetricId.REVENUE_YOY, FinancialUnit.PERCENT),
    _Binding("PARENTNETPROFIT", FinancialMetricId.NET_PROFIT_PARENT, FinancialUnit.CNY),
    _Binding(
        "PARENTNETPROFITTZ",
        FinancialMetricId.NET_PROFIT_PARENT_YOY,
        FinancialUnit.PERCENT,
    ),
    _Binding("ROEJQ", FinancialMetricId.ROE, FinancialUnit.PERCENT),
    _Binding("XSMLL", FinancialMetricId.GROSS_MARGIN, FinancialUnit.PERCENT),
    _Binding("XSJLL", FinancialMetricId.NET_MARGIN, FinancialUnit.PERCENT),
    _Binding("NETCASH_OPERATE_PK", FinancialMetricId.OPERATING_CASH_FLOW, FinancialUnit.CNY),
    _Binding("KCFJCXSYJLR", FinancialMetricId.DEDUCTED_NET_PROFIT, FinancialUnit.CNY),
)

_VALUATION_METRICS = {
    1: FinancialMetricId.PE,
    2: FinancialMetricId.PB,
    3: FinancialMetricId.PS,
    4: FinancialMetricId.PCF,
}


def normalize_latest_indicator_facts(
    batch: OperatorReadingBatch,
    *,
    publication_times: Mapping[date, FinancialPublicationTime],
    snapshot_id: str,
) -> tuple[FinancialFactRecord, ...]:
    """Normalize direct report fields after an explicit publication-time join."""

    _validate_batch(batch, OperatorDataset.LATEST_INDICATORS)
    facts: list[FinancialFactRecord] = []
    for index, row in enumerate(batch.rows):
        report_period = _provider_date(row.get("REPORT_DATE"), "REPORT_DATE")
        publication = publication_times.get(report_period)
        if publication is None:
            raise OperatorReadingNormalizationError(
                f"report {report_period.isoformat()} has no trusted publication time"
            )
        report_type, period_basis = _report_shape(report_period)
        revision_id = (
            f"{OPERATOR_READING_PROVIDER}:{batch.dataset.value}:{batch.instrument_id}:"
            f"{report_period.isoformat()}:{publication.revision_id}"
        )
        availability = PointInTimeAvailability(
            observed_at=datetime.combine(report_period, time.min, tzinfo=_SHANGHAI),
            announced_at=publication.announced_at,
            first_available_at=publication.first_available_at,
            signal_at=None,
            execution_at=None,
            retrieved_at=batch.row_retrieved_at[index],
            timezone="Asia/Shanghai",
            source=OPERATOR_READING_PROVIDER,
            revision_id=revision_id,
        )
        for binding in _LATEST_BINDINGS:
            if binding.source_field not in row:
                continue
            raw_hash = batch.row_raw_response_sha256[index]
            source_id = (
                f"{batch.dataset.value}:{batch.instrument_id}:{report_period.isoformat()}:"
                f"{binding.source_field}:{publication.revision_id}"
            )
            facts.append(
                FinancialFactRecord(
                    instrument_id=batch.instrument_id,
                    metric_id=binding.metric_id,
                    value=_provider_decimal(row[binding.source_field], binding.source_field),
                    value_origin=FinancialValueOrigin.PROVIDER_RAW,
                    provider=OPERATOR_READING_PROVIDER,
                    source_dataset=batch.dataset.value,
                    source_field=binding.source_field,
                    report_period=report_period,
                    report_type=report_type,
                    period_basis=period_basis,
                    statement_scope=FinancialStatementScope.CONSOLIDATED,
                    unit=binding.unit,
                    availability=availability,
                    revision_id=revision_id,
                    raw_response_sha256=raw_hash,
                    snapshot_id=snapshot_id,
                    source_refs=(
                        SourceRef(
                            provider=OPERATOR_READING_PROVIDER,
                            source_id=source_id,
                            snapshot_id=snapshot_id,
                            schema_version=OPERATOR_READING_SCHEMA_VERSION,
                            content_sha256=raw_hash,
                            source_kind=SourceKind.PROVIDER_RECORD,
                        ),
                    ),
                )
            )
    if not facts:
        raise OperatorReadingNormalizationError("no documented direct financial fields found")
    return tuple(facts)


def normalize_main_financial_facts(
    batch: OperatorReadingBatch,
    *,
    publication_evidence: Mapping[date, FinancialPublicationEvidence],
    snapshot_id: str,
) -> tuple[FinancialFactRecord, ...]:
    """Normalize direct F10 statement rows after a strict announcement join.

    The caller owns the F10-row-to-announcement resolver.  This function
    verifies the row still matches that evidence, then preserves only fields
    returned by the provider.  Missing values remain ``None``.
    """

    _validate_batch(batch, OperatorDataset.MAIN_FINANCIAL_DATA)
    facts: list[FinancialFactRecord] = []
    for index, row in enumerate(batch.rows):
        report_period = _provider_date(row.get("REPORT_DATE"), "REPORT_DATE")
        evidence = publication_evidence.get(report_period)
        if evidence is None:
            raise OperatorReadingNormalizationError(
                f"report {report_period.isoformat()} has no trusted publication evidence"
            )
        _validate_main_row_against_evidence(row, report_period=report_period, evidence=evidence)
        report_type, period_basis = _report_shape(report_period)
        revision_id = (
            f"{OPERATOR_READING_PROVIDER}:{batch.dataset.value}:{batch.instrument_id}:"
            f"{report_period.isoformat()}:{evidence.revision_id}"
        )
        availability = PointInTimeAvailability(
            observed_at=datetime.combine(report_period, time.min, tzinfo=_SHANGHAI),
            announced_at=evidence.announced_at,
            first_available_at=evidence.first_available_at,
            signal_at=None,
            execution_at=None,
            retrieved_at=batch.row_retrieved_at[index],
            timezone="Asia/Shanghai",
            source=OPERATOR_READING_PROVIDER,
            revision_id=revision_id,
        )
        raw_hash = batch.row_raw_response_sha256[index]
        for binding in _MAIN_FINANCIAL_BINDINGS:
            if binding.source_field not in row:
                continue
            source_id = (
                f"{batch.dataset.value}:{batch.instrument_id}:{report_period.isoformat()}:"
                f"{binding.source_field}:{evidence.revision_id}"
            )
            facts.append(
                FinancialFactRecord(
                    instrument_id=batch.instrument_id,
                    metric_id=binding.metric_id,
                    value=_provider_decimal(row[binding.source_field], binding.source_field),
                    value_origin=FinancialValueOrigin.PROVIDER_RAW,
                    provider=OPERATOR_READING_PROVIDER,
                    source_dataset=batch.dataset.value,
                    source_field=binding.source_field,
                    report_period=report_period,
                    report_type=report_type,
                    period_basis=period_basis,
                    statement_scope=FinancialStatementScope.CONSOLIDATED,
                    unit=binding.unit,
                    availability=availability,
                    revision_id=revision_id,
                    raw_response_sha256=raw_hash,
                    snapshot_id=snapshot_id,
                    source_refs=(
                        SourceRef(
                            provider=OPERATOR_READING_PROVIDER,
                            source_id=source_id,
                            snapshot_id=snapshot_id,
                            schema_version=OPERATOR_READING_SCHEMA_VERSION,
                            content_sha256=raw_hash,
                            source_kind=SourceKind.PROVIDER_RECORD,
                        ),
                    ),
                )
            )
    if not facts:
        raise OperatorReadingNormalizationError("no documented direct main financial fields found")
    return tuple(facts)


def normalize_valuation_facts(
    batches: Sequence[OperatorReadingBatch],
    *,
    snapshot_id: str,
) -> tuple[FinancialFactRecord, ...]:
    """Normalize provider PE/PB/PS/PCF values with date-only close availability."""

    facts: list[FinancialFactRecord] = []
    for batch in batches:
        _validate_batch(batch, OperatorDataset.VALUATION_TREND)
        if batch.indicator_type not in _VALUATION_METRICS:
            raise OperatorReadingNormalizationError("valuation indicator type is unsupported")
        metric_id = _VALUATION_METRICS[batch.indicator_type]
        for index, row in enumerate(batch.rows):
            trade_date = _provider_date(row.get("TRADE_DATE"), "TRADE_DATE")
            raw_indicator_type = row.get("INDICATORTYPE")
            if raw_indicator_type != str(batch.indicator_type):
                raise OperatorReadingNormalizationError("valuation indicator identity mismatch")
            available_at = datetime.combine(trade_date, time(15), tzinfo=_SHANGHAI)
            revision_id = (
                f"{OPERATOR_READING_PROVIDER}:{batch.dataset.value}:{batch.instrument_id}:"
                f"{trade_date.isoformat()}:{batch.indicator_type}"
            )
            raw_hash = batch.row_raw_response_sha256[index]
            source_id = (
                f"{batch.dataset.value}:{batch.instrument_id}:{trade_date.isoformat()}:"
                f"INDICATOR_VALUE:{batch.indicator_type}"
            )
            facts.append(
                FinancialFactRecord(
                    instrument_id=batch.instrument_id,
                    metric_id=metric_id,
                    value=_provider_decimal(row.get("INDICATOR_VALUE"), "INDICATOR_VALUE"),
                    value_origin=FinancialValueOrigin.PROVIDER_RAW,
                    provider=OPERATOR_READING_PROVIDER,
                    source_dataset=batch.dataset.value,
                    source_field="INDICATOR_VALUE",
                    report_period=None,
                    report_type=None,
                    period_basis=FinancialPeriodBasis.POINT_IN_TIME,
                    statement_scope=None,
                    unit=FinancialUnit.TIMES,
                    availability=PointInTimeAvailability(
                        observed_at=available_at,
                        announced_at=None,
                        first_available_at=available_at,
                        signal_at=None,
                        execution_at=None,
                        retrieved_at=batch.row_retrieved_at[index],
                        timezone="Asia/Shanghai",
                        source=OPERATOR_READING_PROVIDER,
                        revision_id=revision_id,
                    ),
                    revision_id=revision_id,
                    raw_response_sha256=raw_hash,
                    snapshot_id=snapshot_id,
                    source_refs=(
                        SourceRef(
                            provider=OPERATOR_READING_PROVIDER,
                            source_id=source_id,
                            snapshot_id=snapshot_id,
                            schema_version=OPERATOR_READING_SCHEMA_VERSION,
                            content_sha256=raw_hash,
                            source_kind=SourceKind.PROVIDER_RECORD,
                        ),
                    ),
                )
            )
    if not facts:
        raise OperatorReadingNormalizationError("no documented valuation values found")
    return tuple(facts)


def _validate_batch(batch: OperatorReadingBatch, dataset: OperatorDataset) -> None:
    if batch.dataset is not dataset:
        raise OperatorReadingNormalizationError(f"expected {dataset.value} batch")
    row_count = len(batch.rows)
    if row_count != len(batch.row_raw_response_sha256) or row_count != len(batch.row_retrieved_at):
        raise OperatorReadingNormalizationError("row provenance is incomplete")


def _validate_main_row_against_evidence(
    row: Mapping[str, object],
    *,
    report_period: date,
    evidence: FinancialPublicationEvidence,
) -> None:
    if evidence.report_period != report_period:
        raise OperatorReadingNormalizationError("publication evidence report period mismatch")
    if _provider_date(row.get("NOTICE_DATE"), "NOTICE_DATE") != evidence.notice_date:
        raise OperatorReadingNormalizationError("publication evidence NOTICE_DATE mismatch")
    if _provider_date(row.get("UPDATE_DATE"), "UPDATE_DATE") != evidence.update_date:
        raise OperatorReadingNormalizationError("publication evidence UPDATE_DATE mismatch")
    expected_report_type, _ = _report_shape(report_period)
    if evidence.report_type is not expected_report_type:
        raise OperatorReadingNormalizationError("publication evidence report type mismatch")


def _provider_date(value: object, field_name: str) -> date:
    if type(value) is not str:
        raise OperatorReadingNormalizationError(f"{field_name} is missing")
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").date()
    except ValueError as error:
        raise OperatorReadingNormalizationError(f"{field_name} is invalid") from error


def _provider_decimal(value: object, field_name: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise OperatorReadingNormalizationError(f"{field_name} is not a provider number")
    try:
        converted = Decimal(str(value))
    except InvalidOperation as error:
        raise OperatorReadingNormalizationError(f"{field_name} is not a provider number") from error
    if not converted.is_finite():
        raise OperatorReadingNormalizationError(f"{field_name} is not finite")
    return converted


def _report_shape(report_period: date) -> tuple[FinancialReportType, FinancialPeriodBasis]:
    month_day = (report_period.month, report_period.day)
    if month_day == (3, 31):
        return FinancialReportType.Q1, FinancialPeriodBasis.YTD_CUMULATIVE
    if month_day == (6, 30):
        return FinancialReportType.SEMIANNUAL, FinancialPeriodBasis.YTD_CUMULATIVE
    if month_day == (9, 30):
        return FinancialReportType.Q3, FinancialPeriodBasis.YTD_CUMULATIVE
    if month_day == (12, 31):
        return FinancialReportType.ANNUAL, FinancialPeriodBasis.FULL_YEAR
    raise OperatorReadingNormalizationError("report period is not a standard A-share quarter end")


__all__ = [
    "FinancialPublicationTime",
    "OperatorReadingNormalizationError",
    "normalize_latest_indicator_facts",
    "normalize_main_financial_facts",
    "normalize_valuation_facts",
]
