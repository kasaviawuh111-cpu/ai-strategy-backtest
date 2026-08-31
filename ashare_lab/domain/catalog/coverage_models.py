"""Auditable coverage catalog models.

The executable indicator catalog intentionally remains small.  This module
describes the wider research/product inventory without implying that an item
can be executed by the current signal runtime.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ashare_lab.domain.strategy.canonical import canonical_hash

type CapabilityStatus = Literal["stable", "research_only", "unavailable"]
type LicenseStatus = Literal["cleared", "internal_only", "unknown", "not_acquired"]


class FrozenCoverageModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class DataRequirement(FrozenCoverageModel):
    """One versioned input needed to evaluate a metric or event."""

    dataset_id: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
    fields: tuple[str, ...] = Field(min_length=1)
    frequency: Literal["tick", "1m", "5m", "1d", "event", "quarterly", "annual", "reference"]
    point_in_time_required: bool
    required_time_fields: tuple[
        Literal["fact_at", "available_at", "ingested_at", "revision_id"], ...
    ] = Field(min_length=1)
    license_status: LicenseStatus

    @model_validator(mode="after")
    def fields_are_unique(self) -> DataRequirement:
        if len(self.fields) != len(set(self.fields)):
            raise ValueError(f"dataset {self.dataset_id!r} contains duplicate fields")
        if len(self.required_time_fields) != len(set(self.required_time_fields)):
            raise ValueError(f"dataset {self.dataset_id!r} contains duplicate time fields")
        if self.point_in_time_required and "available_at" not in self.required_time_fields:
            raise ValueError("point-in-time data must declare available_at")
        return self


class CatalogTimeSemantics(FrozenCoverageModel):
    """Availability and default trading policy, independent of event fact time."""

    observation_basis: Literal[
        "bar_close",
        "bar_intraday",
        "available_at",
        "effective_at",
        "report_period",
    ]
    required_quality: Literal[
        "exact_timestamp",
        "exchange_session",
        "date_only",
        "report_period_with_publication_time",
    ]
    date_only_policy: Literal["conservative_after_close", "reject", "not_applicable"]
    revision_policy: Literal["as_known_at_cutoff", "first_seen_only", "reject_revisions"]
    default_order_time: Literal[
        "next_tradable_session_open",
        "next_continuous_match_after_available",
        "not_tradable",
    ]
    daily_bar_fallback: Literal["next_tradable_session_open", "reject"]
    notes: str = Field(min_length=1, max_length=500)


class MetricDefinition(FrozenCoverageModel):
    """A normalized indicator or raw/derived data series in the coverage inventory."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
    kind: Literal["indicator", "data_series"]
    family: Literal[
        "technical",
        "price_action",
        "volume_liquidity",
        "fundamental_valuation",
    ]
    name_zh: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=500)
    status: CapabilityStatus
    formula_summary: str | None = Field(default=None, max_length=1_000)
    parameters: tuple[str, ...] = ()
    triggers: tuple[str, ...] = Field(min_length=1)
    warmup_bars: int | None = Field(default=None, ge=0, le=100_000)
    data_requirements: tuple[DataRequirement, ...] = Field(min_length=1)
    time_semantics: CatalogTimeSemantics
    implementation_ref: str | None = Field(default=None, max_length=240)
    legacy_atom_ids: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()

    @model_validator(mode="after")
    def execution_claim_is_supported(self) -> MetricDefinition:
        if len(self.parameters) != len(set(self.parameters)):
            raise ValueError(f"metric {self.id!r} contains duplicate parameters")
        if len(self.triggers) != len(set(self.triggers)):
            raise ValueError(f"metric {self.id!r} contains duplicate triggers")
        if len(self.legacy_atom_ids) != len(set(self.legacy_atom_ids)):
            raise ValueError(f"metric {self.id!r} contains duplicate legacy atom ids")
        if self.status == "stable":
            if self.implementation_ref is None or self.formula_summary is None:
                raise ValueError("stable metric requires formula_summary and implementation_ref")
            if self.blockers:
                raise ValueError("stable metric cannot declare blockers")
            if self.time_semantics.default_order_time == "not_tradable":
                raise ValueError("stable metric must have a tradable default")
        elif not self.blockers:
            raise ValueError(f"{self.status} metric must explain its blockers")
        return self


class EventDefinition(FrozenCoverageModel):
    """A normalized A-share event code with point-in-time lifecycle semantics."""

    id: str = Field(pattern=r"^event\.[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
    category: Literal[
        "financial_results",
        "dividends_corporate_actions",
        "repurchase_capital",
        "shareholder_holdings",
        "restricted_shares_pledges",
        "trading_status",
        "regulation_risk",
        "market_activity",
        "financing_securities",
        "index_passive",
        "contracts_orders",
        "capacity_operations",
        "m_and_a_restructuring",
        "governance_personnel",
        "litigation_credit",
        "macro_policy_industry",
    ]
    name_zh: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=500)
    status: CapabilityStatus
    lifecycle_states: tuple[
        Literal[
            "announced",
            "amended",
            "approved",
            "effective",
            "completed",
            "cancelled",
        ],
        ...,
    ] = Field(min_length=2)
    dedupe_keys: tuple[str, ...] = Field(min_length=2)
    data_requirements: tuple[DataRequirement, ...] = Field(min_length=1)
    time_semantics: CatalogTimeSemantics
    implementation_ref: str | None = Field(default=None, max_length=240)
    blockers: tuple[str, ...] = ()

    @model_validator(mode="after")
    def lifecycle_and_claim_are_auditable(self) -> EventDefinition:
        if len(self.lifecycle_states) != len(set(self.lifecycle_states)):
            raise ValueError(f"event {self.id!r} contains duplicate lifecycle states")
        if len(self.dedupe_keys) != len(set(self.dedupe_keys)):
            raise ValueError(f"event {self.id!r} contains duplicate dedupe keys")
        if "available_at" not in self.dedupe_keys and "source_event_id" not in self.dedupe_keys:
            raise ValueError("event dedupe must include available_at or source_event_id")
        if self.status == "stable":
            if self.implementation_ref is None:
                raise ValueError("stable event requires implementation_ref")
            if self.blockers:
                raise ValueError("stable event cannot declare blockers")
        elif not self.blockers:
            raise ValueError(f"{self.status} event must explain its blockers")
        return self


class CoverageCatalogRelease(FrozenCoverageModel):
    schema_version: Literal["catalog-coverage.v1"] = "catalog-coverage.v1"
    catalog_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,63}$")
    release_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    state: Literal["active", "retired"]
    published_on: date
    metrics: tuple[MetricDefinition, ...] = Field(min_length=1)
    events: tuple[EventDefinition, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def definition_ids_are_unique(self) -> CoverageCatalogRelease:
        metric_ids = [item.id for item in self.metrics]
        event_ids = [item.id for item in self.events]
        if len(metric_ids) != len(set(metric_ids)):
            raise ValueError("coverage release contains duplicate metric ids")
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("coverage release contains duplicate event ids")
        legacy_ids = [legacy_id for item in self.metrics for legacy_id in item.legacy_atom_ids]
        if len(legacy_ids) != len(set(legacy_ids)):
            raise ValueError("legacy atom id is mapped by more than one metric")
        return self

    @property
    def content_hash(self) -> str:
        return canonical_hash(self)


class CoverageCatalogSnapshot(FrozenCoverageModel):
    releases: tuple[CoverageCatalogRelease, ...] = Field(min_length=1)
    metrics: tuple[MetricDefinition, ...] = Field(min_length=1)
    events: tuple[EventDefinition, ...] = Field(min_length=1)
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    def resolve_metric(self, metric_id: str) -> MetricDefinition | None:
        return next((item for item in self.metrics if item.id == metric_id), None)

    def resolve_event(self, event_id: str) -> EventDefinition | None:
        return next((item for item in self.events if item.id == event_id), None)
