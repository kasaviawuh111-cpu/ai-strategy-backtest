from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from ashare_lab.domain.financials import (
    FIRST_FINANCIAL_METRIC_CATALOG,
    FinancialDataKind,
    FinancialFactRecord,
    FinancialMetricCatalog,
    FinancialMetricCoverage,
    FinancialMetricId,
    FinancialPeriodBasis,
    FinancialReportType,
    FinancialSnapshotManifest,
    FinancialStatementScope,
    FinancialUnit,
    FinancialValueOrigin,
)
from ashare_lab.domain.provenance import SourceKind, SourceRef
from ashare_lab.domain.time import PointInTimeAvailability

SHANGHAI = ZoneInfo("Asia/Shanghai")
RAW_HASH = "sha256:" + "a" * 64
SNAPSHOT_ID = "financial:" + "b" * 64
SNAPSHOT_HASH = "sha256:" + "b" * 64

EXPECTED_METRIC_IDS = (
    "financial.revenue",
    "financial.net_profit_parent",
    "financial.revenue_yoy",
    "financial.net_profit_parent_yoy",
    "financial.roe",
    "financial.gross_margin",
    "financial.net_margin",
    "financial.operating_cash_flow",
    "valuation.pe_ttm",
    "valuation.pb_mrq",
    "valuation.dividend_yield_ttm",
)


def moment(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2025, 4, day, hour, minute, tzinfo=SHANGHAI)


def availability(**overrides: object) -> PointInTimeAvailability:
    values: dict[str, object] = {
        "observed_at": datetime(2025, 3, 31, 0, 0, tzinfo=SHANGHAI),
        "announced_at": moment(25, 19, 30),
        "first_available_at": moment(25, 19, 30),
        "signal_at": None,
        "execution_at": None,
        "retrieved_at": moment(26, 10, 0),
        "timezone": "Asia/Shanghai",
        "source": "tushare",
        "revision_id": "tushare:income:300059.SZ:20250331:rev-1",
    }
    values.update(overrides)
    return PointInTimeAvailability(**values)  # type: ignore[arg-type]


def source_ref(**overrides: object) -> SourceRef:
    values: dict[str, object] = {
        "provider": "tushare",
        "source_id": "tushare:income:300059.SZ:20250331:row-1",
        "snapshot_id": SNAPSHOT_ID,
        "schema_version": "tushare.income.v1",
        "content_sha256": RAW_HASH,
        "source_kind": SourceKind.PROVIDER_RECORD,
    }
    values.update(overrides)
    return SourceRef(**values)  # type: ignore[arg-type]


def statement_fact(**overrides: object) -> FinancialFactRecord:
    values: dict[str, object] = {
        "instrument_id": "300059.SZ",
        "metric_id": FinancialMetricId.REVENUE,
        "value": Decimal("3810000000.25"),
        "value_origin": FinancialValueOrigin.PROVIDER_RAW,
        "provider": "tushare",
        "source_dataset": "income",
        "source_field": "revenue",
        "report_period": date(2025, 3, 31),
        "report_type": FinancialReportType.Q1,
        "period_basis": FinancialPeriodBasis.YTD_CUMULATIVE,
        "statement_scope": FinancialStatementScope.CONSOLIDATED,
        "unit": FinancialUnit.CNY,
        "availability": availability(),
        "revision_id": "tushare:income:300059.SZ:20250331:rev-1",
        "raw_response_sha256": RAW_HASH,
        "snapshot_id": SNAPSHOT_ID,
        "source_refs": (source_ref(),),
    }
    values.update(overrides)
    return FinancialFactRecord(**values)  # type: ignore[arg-type]


def valuation_fact(**overrides: object) -> FinancialFactRecord:
    observed = moment(25, 15, 0)
    values: dict[str, object] = {
        "instrument_id": "300059.SZ",
        "metric_id": FinancialMetricId.PE_TTM,
        "value": Decimal("31.47"),
        "value_origin": FinancialValueOrigin.PROVIDER_RAW,
        "provider": "tushare",
        "source_dataset": "daily_basic",
        "source_field": "pe_ttm",
        "report_period": None,
        "report_type": None,
        "period_basis": FinancialPeriodBasis.TTM,
        "statement_scope": None,
        "unit": FinancialUnit.TIMES,
        "availability": availability(
            observed_at=observed,
            announced_at=None,
            first_available_at=moment(26, 9, 30),
            revision_id="tushare:daily_basic:300059.SZ:20250425",
        ),
        "revision_id": "tushare:daily_basic:300059.SZ:20250425",
        "raw_response_sha256": RAW_HASH,
        "snapshot_id": SNAPSHOT_ID,
        "source_refs": (
            source_ref(
                source_id="tushare:daily_basic:300059.SZ:20250425",
                schema_version="tushare.daily_basic.v1",
            ),
        ),
    }
    values.update(overrides)
    return FinancialFactRecord(**values)  # type: ignore[arg-type]


def test_catalog_is_closed_to_the_exact_first_eleven_metrics() -> None:
    catalog = FIRST_FINANCIAL_METRIC_CATALOG

    assert catalog.schema_version == "financial-metric-catalog.v1"
    assert tuple(item.metric_id.value for item in catalog.definitions) == EXPECTED_METRIC_IDS
    assert catalog.definition_for(FinancialMetricId.PE_TTM).data_kind is FinancialDataKind.VALUATION
    assert (
        catalog.definition_for(FinancialMetricId.REVENUE).data_kind is FinancialDataKind.STATEMENT
    )


def test_catalog_rejects_missing_or_changed_definitions() -> None:
    with pytest.raises(ValidationError, match="exact first metric definitions"):
        FinancialMetricCatalog(definitions=FIRST_FINANCIAL_METRIC_CATALOG.definitions[:-1])

    changed = FIRST_FINANCIAL_METRIC_CATALOG.definitions[0].model_copy(
        update={"unit": FinancialUnit.PERCENT}
    )
    with pytest.raises(ValidationError, match="exact first metric definitions"):
        FinancialMetricCatalog(
            definitions=(changed, *FIRST_FINANCIAL_METRIC_CATALOG.definitions[1:])
        )


def test_statement_fact_preserves_provider_raw_decimal_and_pit_provenance() -> None:
    fact = statement_fact()

    assert fact.value == Decimal("3810000000.25")
    assert type(fact.value) is Decimal
    assert fact.source_dataset == "income"
    assert fact.source_field == "revenue"
    assert fact.report_period == date(2025, 3, 31)
    assert fact.availability.announced_at == moment(25, 19, 30)
    assert fact.availability.first_available_at == moment(25, 19, 30)
    assert fact.source_refs[0].content_sha256 == fact.raw_response_sha256
    assert fact.source_refs[0].snapshot_id == fact.snapshot_id


def test_missing_value_remains_none_and_is_not_silently_zero_filled() -> None:
    missing = statement_fact(value=None)
    real_zero = statement_fact(value=Decimal("0"))

    assert missing.value is None
    assert missing.model_dump(mode="json")["value"] is None
    assert real_zero.value == Decimal("0")
    assert real_zero.model_dump(mode="json")["value"] == "0"


@pytest.mark.parametrize("value", [0, 1.5, "1.5", Decimal("NaN"), Decimal("Infinity")])
def test_fact_accepts_only_finite_decimal_or_null(value: object) -> None:
    with pytest.raises(ValidationError, match="value must be a finite Decimal or null"):
        statement_fact(value=value)


def test_derived_values_and_transform_sources_are_forbidden() -> None:
    with pytest.raises(ValidationError):
        statement_fact(value_origin="derived")

    with pytest.raises(ValidationError, match="provider_record"):
        statement_fact(source_refs=(source_ref(source_kind=SourceKind.DETERMINISTIC_TRANSFORM),))


def test_statement_and_valuation_shapes_cannot_be_interchanged() -> None:
    with pytest.raises(ValidationError, match="statement fact requires report_period"):
        statement_fact(report_period=None)
    with pytest.raises(ValidationError, match="statement fact requires report_type"):
        statement_fact(report_type=None)
    with pytest.raises(ValidationError, match="statement fact requires statement_scope"):
        statement_fact(statement_scope=None)
    with pytest.raises(ValidationError, match="valuation fact cannot carry report_period"):
        valuation_fact(report_period=date(2025, 3, 31))
    with pytest.raises(ValidationError, match="valuation fact cannot carry statement_scope"):
        valuation_fact(statement_scope=FinancialStatementScope.CONSOLIDATED)
    with pytest.raises(ValidationError, match="valuation fact cannot carry report_type"):
        valuation_fact(report_type=FinancialReportType.ANNUAL)


def test_report_type_keeps_only_minimum_structural_period_rules() -> None:
    with pytest.raises(ValidationError, match="q1 report cannot use full_year"):
        statement_fact(
            report_type=FinancialReportType.Q1,
            period_basis=FinancialPeriodBasis.FULL_YEAR,
        )

    annual = statement_fact(
        report_period=date(2024, 12, 31),
        report_type=FinancialReportType.ANNUAL,
        period_basis=FinancialPeriodBasis.FULL_YEAR,
        availability=availability(
            observed_at=datetime(2024, 12, 31, 0, 0, tzinfo=SHANGHAI),
        ),
    )
    assert annual.report_type is FinancialReportType.ANNUAL


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"unit": FinancialUnit.PERCENT}, "unit does not match metric catalog"),
        ({"period_basis": FinancialPeriodBasis.TTM}, "period_basis is not allowed"),
        ({"metric_id": FinancialMetricId.PE_TTM}, "data shape does not match metric"),
    ],
)
def test_fact_must_match_catalog_semantics(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        statement_fact(**overrides)


def test_pe_pb_and_dividend_yield_have_distinct_valuation_bases() -> None:
    assert valuation_fact().period_basis is FinancialPeriodBasis.TTM
    assert (
        valuation_fact(
            metric_id=FinancialMetricId.PB_MRQ,
            source_field="pb",
            period_basis=FinancialPeriodBasis.MRQ,
        ).period_basis
        is FinancialPeriodBasis.MRQ
    )
    assert (
        valuation_fact(
            metric_id=FinancialMetricId.DIVIDEND_YIELD_TTM,
            source_field="dv_ttm",
            unit=FinancialUnit.PERCENT,
        ).metric_id
        is FinancialMetricId.DIVIDEND_YIELD_TTM
    )


def test_fact_requires_historical_first_availability_and_source_alignment() -> None:
    with pytest.raises(ValidationError, match="first_available_at"):
        statement_fact(
            availability=availability(first_available_at=None),
        )
    with pytest.raises(ValidationError, match="revision_id must match"):
        statement_fact(revision_id="different-revision")
    with pytest.raises(ValidationError, match="provider must match"):
        statement_fact(provider="choice")
    with pytest.raises(ValidationError, match="snapshot_id must match"):
        statement_fact(source_refs=(source_ref(snapshot_id="financial:" + "c" * 64),))
    with pytest.raises(ValidationError, match="raw_response_sha256"):
        statement_fact(raw_response_sha256="sha256:" + "d" * 64)


def test_fact_rejects_naive_times_through_shared_pit_contract() -> None:
    with pytest.raises(Exception, match="announced_at must be timezone-aware"):
        statement_fact(
            availability=availability(
                announced_at=datetime(2025, 4, 25, 19, 30),
            )
        )


def test_snapshot_manifest_is_content_addressed_and_timezone_aware() -> None:
    coverage = (
        FinancialMetricCoverage(
            metric_id=FinancialMetricId.REVENUE,
            coverage_start=date(2021, 1, 4),
            coverage_end=date(2025, 4, 25),
            non_null_count=1,
            null_count=0,
            source_dataset="income",
            source_field="revenue",
        ),
        FinancialMetricCoverage(
            metric_id=FinancialMetricId.PE_TTM,
            coverage_start=date(2021, 1, 4),
            coverage_end=date(2025, 4, 25),
            non_null_count=1,
            null_count=0,
            source_dataset="daily_basic",
            source_field="pe_ttm",
        ),
    )
    manifest = FinancialSnapshotManifest(
        snapshot_id=SNAPSHOT_ID,
        provider="tushare",
        content_sha256=SNAPSHOT_HASH,
        catalog_version=FIRST_FINANCIAL_METRIC_CATALOG.schema_version,
        generated_at=moment(26, 12, 0),
        coverage_start=date(2021, 1, 4),
        coverage_end=date(2025, 4, 25),
        source_datasets=("income", "daily_basic"),
        metric_ids=(FinancialMetricId.REVENUE, FinancialMetricId.PE_TTM),
        metric_coverage=coverage,
        fact_count=2,
    )

    assert manifest.schema_version == "ashare-lab.financial-snapshot.v1"
    assert manifest.snapshot_id.removeprefix("financial:") == manifest.content_sha256.removeprefix(
        "sha256:"
    )
    assert manifest.metric_ids == (FinancialMetricId.REVENUE, FinancialMetricId.PE_TTM)
    assert manifest.metric_coverage == coverage


def test_metric_coverage_preserves_nulls_and_cannot_be_empty() -> None:
    coverage = FinancialMetricCoverage(
        metric_id=FinancialMetricId.PE_TTM,
        coverage_start=date(2025, 4, 1),
        coverage_end=date(2025, 4, 25),
        non_null_count=17,
        null_count=1,
        source_dataset="daily_basic",
        source_field="pe_ttm",
    )

    assert coverage.non_null_count == 17
    assert coverage.null_count == 1
    with pytest.raises(ValidationError, match="at least one observed row"):
        FinancialMetricCoverage(
            metric_id=FinancialMetricId.PE_TTM,
            coverage_start=date(2025, 4, 1),
            coverage_end=date(2025, 4, 25),
            non_null_count=0,
            null_count=0,
            source_dataset="daily_basic",
            source_field="pe_ttm",
        )


def test_manifest_cannot_use_one_metric_sample_to_claim_other_coverage() -> None:
    revenue_only = FinancialMetricCoverage(
        metric_id=FinancialMetricId.REVENUE,
        coverage_start=date(2025, 4, 25),
        coverage_end=date(2025, 4, 25),
        non_null_count=1,
        null_count=0,
        source_dataset="income",
        source_field="revenue",
    )
    values = _manifest_values(
        metric_ids=(FinancialMetricId.REVENUE, FinancialMetricId.PE_TTM),
        source_datasets=("income",),
        metric_coverage=(revenue_only,),
        fact_count=1,
        coverage_start=date(2025, 4, 25),
    )

    with pytest.raises(ValidationError, match="metric_ids must exactly match metric_coverage"):
        FinancialSnapshotManifest(**values)  # type: ignore[arg-type]


def test_manifest_totals_and_global_window_must_match_metric_coverage() -> None:
    coverage = FinancialMetricCoverage(
        metric_id=FinancialMetricId.REVENUE,
        coverage_start=date(2025, 4, 25),
        coverage_end=date(2025, 4, 25),
        non_null_count=1,
        null_count=1,
        source_dataset="income",
        source_field="revenue",
    )

    with pytest.raises(ValidationError, match="fact_count must equal metric_coverage row counts"):
        FinancialSnapshotManifest.model_validate(
            _manifest_values(
                coverage_start=date(2025, 4, 25),
                metric_coverage=(coverage,),
                fact_count=1,
            )
        )
    with pytest.raises(ValidationError, match="global coverage must equal metric_coverage bounds"):
        FinancialSnapshotManifest.model_validate(
            _manifest_values(
                metric_coverage=(coverage,),
                fact_count=2,
            )
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"snapshot_id": "financial:" + "c" * 64}, "snapshot_id must match content_sha256"),
        ({"generated_at": datetime(2025, 4, 26, 12, 0)}, "timezone-aware"),
        ({"coverage_start": date(2025, 5, 1)}, "coverage_start cannot follow coverage_end"),
        ({"source_datasets": ()}, "source_datasets"),
        ({"metric_ids": ()}, "metric_ids"),
        ({"fact_count": 0}, "greater than or equal to 1"),
    ],
)
def test_snapshot_manifest_fails_closed(overrides: dict[str, object], message: str) -> None:
    values = _manifest_values()
    values.update(overrides)

    with pytest.raises(ValidationError, match=message):
        FinancialSnapshotManifest(**values)  # type: ignore[arg-type]


def _manifest_values(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "snapshot_id": SNAPSHOT_ID,
        "provider": "tushare",
        "content_sha256": SNAPSHOT_HASH,
        "catalog_version": FIRST_FINANCIAL_METRIC_CATALOG.schema_version,
        "generated_at": moment(26, 12, 0),
        "coverage_start": date(2021, 1, 4),
        "coverage_end": date(2025, 4, 25),
        "source_datasets": ("income",),
        "metric_ids": (FinancialMetricId.REVENUE,),
        "metric_coverage": (
            FinancialMetricCoverage(
                metric_id=FinancialMetricId.REVENUE,
                coverage_start=date(2021, 1, 4),
                coverage_end=date(2025, 4, 25),
                non_null_count=1,
                null_count=0,
                source_dataset="income",
                source_field="revenue",
            ),
        ),
        "fact_count": 1,
    }
    values.update(overrides)
    return values
