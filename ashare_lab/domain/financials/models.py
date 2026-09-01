"""Strict point-in-time contracts for provider-supplied financial facts.

The models in this module are intentionally data-source neutral and are not an
execution implementation.  They preserve provider values without deriving
single-quarter or TTM figures, distinguish statement facts from daily
valuation facts, and bind every value to immutable provenance.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from ashare_lab.domain.provenance import DataEnvelope, SourceKind, SourceRef
from ashare_lab.domain.shared import require_aware
from ashare_lab.domain.time import PointInTimeAvailability

_INSTRUMENT_ID = re.compile(r"[0-9]{6}\.(SH|SZ|BJ)")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_FINANCIAL_SNAPSHOT_ID = re.compile(r"financial:[0-9a-f]{64}")


class FinancialMetricId(StrEnum):
    """The closed first tranche of executable-candidate financial metrics."""

    REVENUE = "financial.revenue"
    NET_PROFIT_PARENT = "financial.net_profit_parent"
    REVENUE_YOY = "financial.revenue_yoy"
    NET_PROFIT_PARENT_YOY = "financial.net_profit_parent_yoy"
    ROE = "financial.roe"
    GROSS_MARGIN = "financial.gross_margin"
    NET_MARGIN = "financial.net_margin"
    OPERATING_CASH_FLOW = "financial.operating_cash_flow"
    BASIC_EPS = "financial.basic_eps"
    BOOK_VALUE_PER_SHARE = "financial.book_value_per_share"
    CAPITAL_RESERVE_PER_SHARE = "financial.capital_reserve_per_share"
    UNASSIGNED_PROFIT_PER_SHARE = "financial.unassigned_profit_per_share"
    OPERATING_CASH_FLOW_PER_SHARE = "financial.operating_cash_flow_per_share"
    GROSS_PROFIT = "financial.gross_profit"
    DEDUCTED_NET_PROFIT = "financial.deducted_net_profit"
    DEDUCTED_NET_PROFIT_YOY = "financial.deducted_net_profit_yoy"
    ROTA = "financial.rota"
    TOTAL_ASSETS_TURNOVER = "financial.total_assets_turnover"
    INVENTORY_TURNOVER = "financial.inventory_turnover"
    ACCOUNTS_RECEIVABLE_TURNOVER = "financial.accounts_receivable_turnover"
    PE_TTM = "valuation.pe_ttm"
    PB_MRQ = "valuation.pb_mrq"
    DIVIDEND_YIELD_TTM = "valuation.dividend_yield_ttm"
    PE = "valuation.pe"
    PB = "valuation.pb"
    PS = "valuation.ps"
    PCF = "valuation.pcf"


class FinancialDataKind(StrEnum):
    STATEMENT = "statement"
    VALUATION = "valuation"


class FinancialPeriodBasis(StrEnum):
    SINGLE_QUARTER = "single_quarter"
    YTD_CUMULATIVE = "ytd_cumulative"
    FULL_YEAR = "full_year"
    TTM = "ttm"
    MRQ = "mrq"
    POINT_IN_TIME = "point_in_time"


class FinancialReportType(StrEnum):
    Q1 = "q1"
    SEMIANNUAL = "semiannual"
    Q3 = "q3"
    ANNUAL = "annual"


class FinancialStatementScope(StrEnum):
    CONSOLIDATED = "consolidated"
    PARENT_COMPANY = "parent_company"


class FinancialUnit(StrEnum):
    CNY = "CNY"
    CNY_PER_SHARE = "CNY_PER_SHARE"
    PERCENT = "PERCENT"
    RATIO = "RATIO"
    TIMES = "TIMES"


class FinancialValueOrigin(StrEnum):
    """Only provider-supplied values may cross this contract."""

    PROVIDER_RAW = "provider_raw"


class _FrozenFinancialModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)


class FinancialMetricDefinition(_FrozenFinancialModel):
    metric_id: FinancialMetricId
    data_kind: FinancialDataKind
    unit: FinancialUnit
    allowed_period_bases: tuple[FinancialPeriodBasis, ...] = Field(min_length=1)
    allowed_statement_scopes: tuple[FinancialStatementScope, ...] = ()

    @model_validator(mode="after")
    def kind_and_shape_are_coherent(self) -> Self:
        if len(set(self.allowed_period_bases)) != len(self.allowed_period_bases):
            raise ValueError("allowed_period_bases must be unique")
        if len(set(self.allowed_statement_scopes)) != len(self.allowed_statement_scopes):
            raise ValueError("allowed_statement_scopes must be unique")
        if self.data_kind is FinancialDataKind.STATEMENT:
            if not self.allowed_statement_scopes:
                raise ValueError("statement metric requires allowed_statement_scopes")
            if any(
                basis in {FinancialPeriodBasis.TTM, FinancialPeriodBasis.MRQ}
                for basis in self.allowed_period_bases
            ):
                raise ValueError("statement metrics cannot declare TTM or MRQ")
        elif self.allowed_statement_scopes:
            raise ValueError("valuation metrics cannot declare statement scopes")
        return self


_STATEMENT_BASES = (
    FinancialPeriodBasis.SINGLE_QUARTER,
    FinancialPeriodBasis.YTD_CUMULATIVE,
    FinancialPeriodBasis.FULL_YEAR,
)
_STATEMENT_SCOPES = (
    FinancialStatementScope.CONSOLIDATED,
    FinancialStatementScope.PARENT_COMPANY,
)


def _statement_definition(
    metric_id: FinancialMetricId,
    unit: FinancialUnit,
    *,
    scopes: tuple[FinancialStatementScope, ...] = _STATEMENT_SCOPES,
) -> FinancialMetricDefinition:
    return FinancialMetricDefinition(
        metric_id=metric_id,
        data_kind=FinancialDataKind.STATEMENT,
        unit=unit,
        allowed_period_bases=_STATEMENT_BASES,
        allowed_statement_scopes=scopes,
    )


_FIRST_METRIC_DEFINITIONS = (
    FinancialMetricDefinition(
        metric_id=FinancialMetricId.REVENUE,
        data_kind=FinancialDataKind.STATEMENT,
        unit=FinancialUnit.CNY,
        allowed_period_bases=_STATEMENT_BASES,
        allowed_statement_scopes=_STATEMENT_SCOPES,
    ),
    FinancialMetricDefinition(
        metric_id=FinancialMetricId.NET_PROFIT_PARENT,
        data_kind=FinancialDataKind.STATEMENT,
        unit=FinancialUnit.CNY,
        allowed_period_bases=_STATEMENT_BASES,
        allowed_statement_scopes=(FinancialStatementScope.CONSOLIDATED,),
    ),
    FinancialMetricDefinition(
        metric_id=FinancialMetricId.REVENUE_YOY,
        data_kind=FinancialDataKind.STATEMENT,
        unit=FinancialUnit.RATIO,
        allowed_period_bases=_STATEMENT_BASES,
        allowed_statement_scopes=_STATEMENT_SCOPES,
    ),
    FinancialMetricDefinition(
        metric_id=FinancialMetricId.NET_PROFIT_PARENT_YOY,
        data_kind=FinancialDataKind.STATEMENT,
        unit=FinancialUnit.RATIO,
        allowed_period_bases=_STATEMENT_BASES,
        allowed_statement_scopes=(FinancialStatementScope.CONSOLIDATED,),
    ),
    FinancialMetricDefinition(
        metric_id=FinancialMetricId.ROE,
        data_kind=FinancialDataKind.STATEMENT,
        unit=FinancialUnit.RATIO,
        allowed_period_bases=_STATEMENT_BASES,
        allowed_statement_scopes=_STATEMENT_SCOPES,
    ),
    FinancialMetricDefinition(
        metric_id=FinancialMetricId.GROSS_MARGIN,
        data_kind=FinancialDataKind.STATEMENT,
        unit=FinancialUnit.RATIO,
        allowed_period_bases=_STATEMENT_BASES,
        allowed_statement_scopes=_STATEMENT_SCOPES,
    ),
    FinancialMetricDefinition(
        metric_id=FinancialMetricId.NET_MARGIN,
        data_kind=FinancialDataKind.STATEMENT,
        unit=FinancialUnit.RATIO,
        allowed_period_bases=_STATEMENT_BASES,
        allowed_statement_scopes=_STATEMENT_SCOPES,
    ),
    FinancialMetricDefinition(
        metric_id=FinancialMetricId.OPERATING_CASH_FLOW,
        data_kind=FinancialDataKind.STATEMENT,
        unit=FinancialUnit.CNY,
        allowed_period_bases=_STATEMENT_BASES,
        allowed_statement_scopes=_STATEMENT_SCOPES,
    ),
    _statement_definition(FinancialMetricId.BASIC_EPS, FinancialUnit.CNY_PER_SHARE),
    _statement_definition(FinancialMetricId.BOOK_VALUE_PER_SHARE, FinancialUnit.CNY_PER_SHARE),
    _statement_definition(FinancialMetricId.CAPITAL_RESERVE_PER_SHARE, FinancialUnit.CNY_PER_SHARE),
    _statement_definition(
        FinancialMetricId.UNASSIGNED_PROFIT_PER_SHARE, FinancialUnit.CNY_PER_SHARE
    ),
    _statement_definition(
        FinancialMetricId.OPERATING_CASH_FLOW_PER_SHARE, FinancialUnit.CNY_PER_SHARE
    ),
    _statement_definition(FinancialMetricId.GROSS_PROFIT, FinancialUnit.CNY),
    _statement_definition(FinancialMetricId.DEDUCTED_NET_PROFIT, FinancialUnit.CNY),
    _statement_definition(FinancialMetricId.DEDUCTED_NET_PROFIT_YOY, FinancialUnit.RATIO),
    _statement_definition(FinancialMetricId.ROTA, FinancialUnit.RATIO),
    _statement_definition(FinancialMetricId.TOTAL_ASSETS_TURNOVER, FinancialUnit.TIMES),
    _statement_definition(FinancialMetricId.INVENTORY_TURNOVER, FinancialUnit.TIMES),
    _statement_definition(FinancialMetricId.ACCOUNTS_RECEIVABLE_TURNOVER, FinancialUnit.TIMES),
    FinancialMetricDefinition(
        metric_id=FinancialMetricId.PE_TTM,
        data_kind=FinancialDataKind.VALUATION,
        unit=FinancialUnit.TIMES,
        allowed_period_bases=(FinancialPeriodBasis.TTM,),
    ),
    FinancialMetricDefinition(
        metric_id=FinancialMetricId.PB_MRQ,
        data_kind=FinancialDataKind.VALUATION,
        unit=FinancialUnit.TIMES,
        allowed_period_bases=(FinancialPeriodBasis.MRQ,),
    ),
    FinancialMetricDefinition(
        metric_id=FinancialMetricId.DIVIDEND_YIELD_TTM,
        data_kind=FinancialDataKind.VALUATION,
        unit=FinancialUnit.PERCENT,
        allowed_period_bases=(FinancialPeriodBasis.TTM,),
    ),
    *(
        FinancialMetricDefinition(
            metric_id=metric_id,
            data_kind=FinancialDataKind.VALUATION,
            unit=FinancialUnit.TIMES,
            allowed_period_bases=(FinancialPeriodBasis.POINT_IN_TIME,),
        )
        for metric_id in (
            FinancialMetricId.PE,
            FinancialMetricId.PB,
            FinancialMetricId.PS,
            FinancialMetricId.PCF,
        )
    ),
)


class FinancialMetricCatalog(_FrozenFinancialModel):
    """Closed catalog whose semantics cannot be replaced by provider input."""

    schema_version: Literal["financial-metric-catalog.v1"] = "financial-metric-catalog.v1"
    definitions: tuple[FinancialMetricDefinition, ...] = Field(
        default_factory=lambda: _FIRST_METRIC_DEFINITIONS
    )

    @model_validator(mode="after")
    def contains_only_the_exact_first_metric_definitions(self) -> Self:
        if self.definitions != _FIRST_METRIC_DEFINITIONS:
            raise ValueError("catalog must contain the exact first metric definitions")
        return self

    def definition_for(self, metric_id: FinancialMetricId) -> FinancialMetricDefinition:
        if type(metric_id) is not FinancialMetricId:
            raise ValueError("metric_id must be a FinancialMetricId")
        for definition in self.definitions:
            if definition.metric_id is metric_id:
                return definition
        raise ValueError(f"metric is not registered: {metric_id.value}")


FIRST_FINANCIAL_METRIC_CATALOG = FinancialMetricCatalog()


class FinancialFactRecord(_FrozenFinancialModel):
    """One immutable provider fact, gated by historical first availability."""

    schema_version: Literal["financial-fact.v1"] = "financial-fact.v1"
    instrument_id: str
    metric_id: FinancialMetricId
    value: Decimal | None
    value_origin: Literal[FinancialValueOrigin.PROVIDER_RAW]
    provider: str
    source_dataset: str
    source_field: str
    report_period: date | None
    report_type: FinancialReportType | None
    period_basis: FinancialPeriodBasis
    statement_scope: FinancialStatementScope | None
    unit: FinancialUnit
    availability: PointInTimeAvailability
    revision_id: str
    raw_response_sha256: str
    snapshot_id: str
    source_refs: tuple[SourceRef, ...] = Field(min_length=1)

    @field_validator("value", mode="before")
    @classmethod
    def value_is_provider_decimal_or_null(cls, value: object) -> object:
        if value is None:
            return None
        if type(value) is not Decimal or not value.is_finite():
            raise ValueError("value must be a finite Decimal or null")
        return value

    @field_validator("availability", mode="before")
    @classmethod
    def availability_is_the_shared_pit_value(cls, value: object) -> object:
        if type(value) is not PointInTimeAvailability:
            raise ValueError("availability must be a PointInTimeAvailability")
        return value

    @field_validator("source_refs", mode="before")
    @classmethod
    def source_refs_are_explicit_values(cls, value: object) -> object:
        if type(value) is not tuple:
            raise ValueError("source_refs must be an explicit tuple of SourceRef values")
        raw_values = cast(tuple[object, ...], value)
        if any(type(item) is not SourceRef for item in raw_values):
            raise ValueError("source_refs must be an explicit tuple of SourceRef values")
        return raw_values

    @field_validator("provider", "source_dataset", "source_field", "revision_id")
    @classmethod
    def text_fields_are_exact_nonblank_values(cls, value: str) -> str:
        if not value.strip() or value != value.strip() or _has_control(value):
            raise ValueError("financial provenance text must be nonblank and exact")
        return value

    @model_validator(mode="after")
    def validate_metric_shape_and_provenance(self) -> Self:
        if _INSTRUMENT_ID.fullmatch(self.instrument_id) is None:
            raise ValueError("instrument_id must be a canonical A-share symbol")
        _require_sha256(self.raw_response_sha256, "raw_response_sha256")
        if _FINANCIAL_SNAPSHOT_ID.fullmatch(self.snapshot_id) is None:
            raise ValueError("snapshot_id must be a financial content identity")
        if self.availability.first_available_at is None:
            raise ValueError("financial fact requires first_available_at")
        if self.availability.signal_at is not None or self.availability.execution_at is not None:
            raise ValueError("financial fact availability cannot contain signal or execution time")
        if self.availability.revision_id != self.revision_id:
            raise ValueError("revision_id must match availability.revision_id")
        if self.availability.source != self.provider:
            raise ValueError("provider must match availability.source")

        if len(set(self.source_refs)) != len(self.source_refs):
            raise ValueError("source_refs must be unique")
        for source_ref in self.source_refs:
            if source_ref.source_kind is not SourceKind.PROVIDER_RECORD:
                raise ValueError("financial facts require provider_record source refs")
            if source_ref.provider != self.provider:
                raise ValueError("provider must match every source_refs provider")
            if source_ref.snapshot_id != self.snapshot_id:
                raise ValueError("snapshot_id must match every source_refs snapshot_id")
        if self.raw_response_sha256 not in {
            source_ref.content_sha256 for source_ref in self.source_refs
        }:
            raise ValueError("raw_response_sha256 must be present in source_refs")

        definition = FIRST_FINANCIAL_METRIC_CATALOG.definition_for(self.metric_id)
        self._validate_shape(definition)
        if self.period_basis not in definition.allowed_period_bases:
            raise ValueError("period_basis is not allowed for metric")
        if self.unit is not definition.unit:
            raise ValueError("unit does not match metric catalog")
        if (
            self.statement_scope is not None
            and self.statement_scope not in definition.allowed_statement_scopes
        ):
            raise ValueError("statement_scope is not allowed for metric")
        return self

    def _validate_shape(self, definition: FinancialMetricDefinition) -> None:
        if definition.data_kind is FinancialDataKind.STATEMENT:
            if self.report_period is None:
                raise ValueError("statement fact requires report_period")
            if self.report_type is None:
                raise ValueError("statement fact requires report_type")
            if self.statement_scope is None:
                raise ValueError("statement fact requires statement_scope")
            if self.availability.announced_at is None:
                raise ValueError("statement fact requires announced_at")
            if self.availability.observed_at is None:
                raise ValueError("statement fact requires observed_at")
            if self.availability.observed_at.date() != self.report_period:
                raise ValueError("report_period must match availability.observed_at date")
            if (
                self.report_type is FinancialReportType.Q1
                and self.period_basis is FinancialPeriodBasis.FULL_YEAR
            ):
                raise ValueError("q1 report cannot use full_year period_basis")
            return

        if (
            self.report_period is not None
            and self.report_type is not None
            and self.statement_scope is not None
        ):
            raise ValueError("data shape does not match metric catalog kind")
        if self.report_period is not None:
            raise ValueError("valuation fact cannot carry report_period")
        if self.report_type is not None:
            raise ValueError("valuation fact cannot carry report_type")
        if self.statement_scope is not None:
            raise ValueError("valuation fact cannot carry statement_scope")
        if self.availability.observed_at is None:
            raise ValueError("valuation fact requires observed_at")

    def to_data_envelope(self) -> DataEnvelope:
        """Project the provider fact into the shared provenance contract."""

        return DataEnvelope(
            data_id=self.metric_id.value,
            value=self.value,
            unit=self.unit.value,
            availability=self.availability,
            source_refs=self.source_refs,
        )


class FinancialMetricCoverage(_FrozenFinancialModel):
    """Observed row coverage for one exact provider field and metric."""

    metric_id: FinancialMetricId
    coverage_start: date
    coverage_end: date
    non_null_count: StrictInt = Field(ge=0)
    null_count: StrictInt = Field(ge=0)
    source_dataset: str
    source_field: str

    @field_validator("source_dataset", "source_field")
    @classmethod
    def source_identity_is_exact_nonblank_text(cls, value: str) -> str:
        if not value.strip() or value != value.strip() or _has_control(value):
            raise ValueError("coverage source identity must be nonblank exact text")
        return value

    @model_validator(mode="after")
    def validate_coverage(self) -> Self:
        if self.coverage_start > self.coverage_end:
            raise ValueError("coverage_start cannot follow coverage_end")
        if self.non_null_count + self.null_count < 1:
            raise ValueError("metric coverage requires at least one observed row")
        return self


class FinancialSnapshotManifest(_FrozenFinancialModel):
    """Content-addressed identity and coverage of one financial snapshot."""

    schema_version: Literal["ashare-lab.financial-snapshot.v1"] = "ashare-lab.financial-snapshot.v1"
    snapshot_id: str
    provider: str
    content_sha256: str
    catalog_version: Literal["financial-metric-catalog.v1"]
    generated_at: datetime
    coverage_start: date
    coverage_end: date
    source_datasets: tuple[str, ...] = Field(min_length=1)
    metric_ids: tuple[FinancialMetricId, ...] = Field(min_length=1)
    metric_coverage: tuple[FinancialMetricCoverage, ...] = Field(min_length=1)
    fact_count: StrictInt = Field(ge=1)

    @field_validator("provider")
    @classmethod
    def provider_is_exact_nonblank_text(cls, value: str) -> str:
        if not value.strip() or value != value.strip() or _has_control(value):
            raise ValueError("provider must be nonblank exact text")
        return value

    @field_validator("source_datasets")
    @classmethod
    def source_datasets_are_explicit_and_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() or item != item.strip() or _has_control(item) for item in values):
            raise ValueError("source_datasets must contain nonblank exact text")
        if len(set(values)) != len(values):
            raise ValueError("source_datasets must be unique")
        return values

    @field_validator("metric_ids")
    @classmethod
    def metric_ids_are_unique(
        cls, values: tuple[FinancialMetricId, ...]
    ) -> tuple[FinancialMetricId, ...]:
        if len(set(values)) != len(values):
            raise ValueError("metric_ids must be unique")
        return values

    @model_validator(mode="after")
    def validate_snapshot_identity_and_coverage(self) -> Self:
        if _FINANCIAL_SNAPSHOT_ID.fullmatch(self.snapshot_id) is None:
            raise ValueError("snapshot_id must be a financial content identity")
        _require_sha256(self.content_sha256, "content_sha256")
        if self.snapshot_id.removeprefix("financial:") != self.content_sha256.removeprefix(
            "sha256:"
        ):
            raise ValueError("snapshot_id must match content_sha256")
        require_aware(self.generated_at, "generated_at")
        if self.coverage_start > self.coverage_end:
            raise ValueError("coverage_start cannot follow coverage_end")

        coverage_keys = tuple(
            (item.metric_id, item.source_dataset, item.source_field)
            for item in self.metric_coverage
        )
        if len(set(coverage_keys)) != len(coverage_keys):
            raise ValueError("metric_coverage entries must have unique source identities")
        if set(self.metric_ids) != {item.metric_id for item in self.metric_coverage}:
            raise ValueError("metric_ids must exactly match metric_coverage")
        if set(self.source_datasets) != {item.source_dataset for item in self.metric_coverage}:
            raise ValueError("source_datasets must exactly match metric_coverage")
        expected_fact_count = sum(
            item.non_null_count + item.null_count for item in self.metric_coverage
        )
        if self.fact_count != expected_fact_count:
            raise ValueError("fact_count must equal metric_coverage row counts")
        if self.coverage_start != min(
            item.coverage_start for item in self.metric_coverage
        ) or self.coverage_end != max(item.coverage_end for item in self.metric_coverage):
            raise ValueError("global coverage must equal metric_coverage bounds")
        return self


def _require_sha256(value: object, field_name: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a sha256 content hash")


def _has_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


__all__ = [
    "FIRST_FINANCIAL_METRIC_CATALOG",
    "FinancialDataKind",
    "FinancialFactRecord",
    "FinancialMetricCatalog",
    "FinancialMetricCoverage",
    "FinancialMetricDefinition",
    "FinancialMetricId",
    "FinancialPeriodBasis",
    "FinancialReportType",
    "FinancialSnapshotManifest",
    "FinancialStatementScope",
    "FinancialUnit",
    "FinancialValueOrigin",
]
