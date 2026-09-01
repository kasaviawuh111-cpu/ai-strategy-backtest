"""Fail-closed evidence contracts for provider capability validation."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SourceProvider(StrEnum):
    """Provider identities in the approved evaluation order."""

    CHOICE = "choice"
    TUSHARE = "tushare"
    EASTMONEY = "eastmoney"
    EXCHANGE_CNINFO = "exchange_cninfo"


class EvidenceStatus(StrEnum):
    """What has actually been verified, never what a provider might support."""

    LIVE_SAMPLE_VERIFIED = "live_sample_verified"
    OFFICIAL_SCHEMA_VERIFIED = "official_schema_verified"
    CONFIGURED_UNVERIFIED = "configured_unverified"
    UNAVAILABLE = "unavailable"


class EvidenceScope(StrEnum):
    """Bounded scope of the evidence.

    No universe-wide value is present on purpose: one instrument sample cannot
    be promoted into a full-market coverage claim.
    """

    EXACT_INSTRUMENT_SAMPLE = "exact_instrument_sample"
    FROZEN_EXACT_INSTRUMENT_SNAPSHOT = "frozen_exact_instrument_snapshot"
    OFFICIAL_SCHEMA = "official_schema"
    CONFIGURATION_ONLY = "configuration_only"
    UNAVAILABLE = "unavailable"


class InterfaceKind(StrEnum):
    """How facts are obtained from a source."""

    FORMAL_API = "formal_api"
    PUBLIC_WEB_API = "public_web_api"
    CONFIGURATION = "configuration"
    NONE = "none"


class FieldValueOrigin(StrEnum):
    """Whether the provider returned the field or local code derived it."""

    DIRECT_API = "direct_api"
    DERIVED = "derived"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class DatasetResponseHash(_FrozenModel):
    """Hash of one exact provider response, labelled by its requested dataset."""

    dataset_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    response_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class MetricFieldBinding(_FrozenModel):
    """Exact mapping from one normalized metric to one provider dataset field."""

    metric_id: str = Field(pattern=r"^(financial|valuation)\.[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
    provider: SourceProvider
    dataset_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    provider_field: str = Field(min_length=1, max_length=128)
    origin: FieldValueOrigin
    unit: str = Field(min_length=1, max_length=64)
    report_period_field: str = Field(min_length=1, max_length=128)
    first_available_field: str | None = Field(default=None, min_length=1, max_length=128)
    revision_field: str | None = Field(default=None, min_length=1, max_length=128)
    derivation_id: str | None = Field(default=None, min_length=1, max_length=160)

    @model_validator(mode="after")
    def origin_is_explicit(self) -> Self:
        if self.origin is FieldValueOrigin.DIRECT_API and self.derivation_id is not None:
            raise ValueError("direct API field cannot declare a derivation_id")
        if self.origin is FieldValueOrigin.DERIVED and self.derivation_id is None:
            raise ValueError("derived field requires a derivation_id")
        return self

    @property
    def has_historical_pit(self) -> bool:
        return self.first_available_field is not None


class SourceCapabilityEvidence(_FrozenModel):
    """Bounded evidence for one provider capability.

    ``verified_instruments`` is an exact allow-list.  An empty list never means
    all instruments, and wildcards are structurally invalid.
    """

    provider: SourceProvider
    capability_id: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
    status: EvidenceStatus
    scope: EvidenceScope
    interface_kind: InterfaceKind
    historical_pit_available: bool
    checked_at: datetime
    endpoint_origin: str = Field(min_length=1, max_length=500)
    response_hashes: tuple[DatasetResponseHash, ...] = ()
    verified_datasets: tuple[str, ...] = ()
    verified_fields: tuple[str, ...] = ()
    verified_metric_ids: tuple[str, ...] = ()
    field_bindings: tuple[MetricFieldBinding, ...] = ()
    verified_instruments: tuple[str, ...] = ()
    blocker: str | None = Field(default=None, min_length=1, max_length=300)

    @field_validator("checked_at")
    @classmethod
    def checked_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("checked_at must be timezone-aware")
        return value

    @field_validator("verified_datasets", "verified_fields", "verified_metric_ids")
    @classmethod
    def string_evidence_is_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item for item in value):
            raise ValueError("verified evidence values must be non-empty")
        if len(value) != len(set(value)):
            raise ValueError("verified evidence values must be unique")
        return tuple(sorted(value))

    @field_validator("response_hashes")
    @classmethod
    def response_datasets_are_unique(
        cls,
        value: tuple[DatasetResponseHash, ...],
    ) -> tuple[DatasetResponseHash, ...]:
        datasets = tuple(item.dataset_id for item in value)
        if len(datasets) != len(set(datasets)):
            raise ValueError("response_hashes must contain one hash per dataset")
        return tuple(sorted(value, key=lambda item: item.dataset_id))

    @field_validator("field_bindings")
    @classmethod
    def field_bindings_are_unique(
        cls,
        value: tuple[MetricFieldBinding, ...],
    ) -> tuple[MetricFieldBinding, ...]:
        keys = tuple(
            (item.provider, item.metric_id, item.dataset_id, item.provider_field) for item in value
        )
        if len(keys) != len(set(keys)):
            raise ValueError("field_bindings must be unique")
        return tuple(
            sorted(
                value,
                key=lambda item: (
                    item.provider.value,
                    item.metric_id,
                    item.dataset_id,
                    item.provider_field,
                ),
            )
        )

    @field_validator("verified_instruments")
    @classmethod
    def instruments_are_canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("verified_instruments must be unique")
        for item in value:
            if (
                len(item) != 9
                or item[:6].isdigit() is False
                or item[6:]
                not in {
                    ".SH",
                    ".SZ",
                    ".BJ",
                }
            ):
                raise ValueError("verified_instruments must contain canonical A-share symbols")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def status_matches_bounded_scope(self) -> Self:
        expected_scopes: dict[EvidenceStatus, tuple[EvidenceScope, ...]] = {
            EvidenceStatus.LIVE_SAMPLE_VERIFIED: (
                EvidenceScope.EXACT_INSTRUMENT_SAMPLE,
                EvidenceScope.FROZEN_EXACT_INSTRUMENT_SNAPSHOT,
            ),
            EvidenceStatus.OFFICIAL_SCHEMA_VERIFIED: (EvidenceScope.OFFICIAL_SCHEMA,),
            EvidenceStatus.CONFIGURED_UNVERIFIED: (EvidenceScope.CONFIGURATION_ONLY,),
            EvidenceStatus.UNAVAILABLE: (EvidenceScope.UNAVAILABLE,),
        }
        if self.scope not in expected_scopes[self.status]:
            raise ValueError("evidence status and scope are inconsistent")
        response_datasets = {item.dataset_id for item in self.response_hashes}
        if not response_datasets.issubset(set(self.verified_datasets)):
            raise ValueError("response_hashes datasets must be declared in verified_datasets")
        bound_metrics = {item.metric_id for item in self.field_bindings}
        if bound_metrics != set(self.verified_metric_ids):
            raise ValueError("verified_metric_ids must exactly match field_bindings")
        for binding in self.field_bindings:
            if binding.provider is not self.provider:
                raise ValueError("field binding provider must match capability evidence provider")
            if binding.dataset_id not in self.verified_datasets:
                raise ValueError("field binding dataset must be verified")
            if binding.provider_field not in self.verified_fields:
                raise ValueError("field binding provider field must be verified")
        if self.status is EvidenceStatus.LIVE_SAMPLE_VERIFIED:
            if not self.verified_instruments:
                raise ValueError("live sample evidence requires exact verified_instruments")
            if not self.response_hashes:
                raise ValueError("live sample evidence requires response_hashes")
            if not self.verified_datasets or not self.verified_fields:
                raise ValueError("live sample evidence requires verified datasets and fields")
            if self.blocker is not None:
                raise ValueError("live sample evidence cannot declare a blocker")
        else:
            if self.verified_instruments:
                raise ValueError("non-live evidence cannot claim verified instruments")
            if self.blocker is None:
                raise ValueError("non-live evidence must explain its blocker")
        return self

    def verified_for(self, instrument_id: str) -> bool:
        """Return true only for an explicitly sampled instrument."""

        return instrument_id in self.verified_instruments


class EventCoverageEvidence(_FrozenModel):
    """Exact frozen event coverage for named codes and instruments."""

    provider: SourceProvider
    status: EvidenceStatus
    scope: EvidenceScope
    interface_kind: InterfaceKind
    snapshot_id: str = Field(pattern=r"^events:[0-9a-f]{64}$")
    snapshot_frozen: bool
    instrument_ids: tuple[str, ...] = Field(min_length=1)
    event_codes: tuple[str, ...] = Field(min_length=1)
    first_available_field: str | None = Field(default=None, min_length=1, max_length=128)
    coverage_start: date
    coverage_end: date

    @field_validator("instrument_ids")
    @classmethod
    def event_instruments_are_canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("instrument_ids must be unique")
        for item in value:
            if (
                len(item) != 9
                or item[:6].isdigit() is False
                or item[6:]
                not in {
                    ".SH",
                    ".SZ",
                    ".BJ",
                }
            ):
                raise ValueError("instrument_ids must contain canonical A-share symbols")
        return tuple(sorted(value))

    @field_validator("event_codes")
    @classmethod
    def event_codes_are_canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("event_codes must be unique")
        for item in value:
            parts = item.split(".")
            if len(parts) < 3 or parts[0] != "event" or any(not part for part in parts):
                raise ValueError("event_codes must be normalized event identifiers")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def frozen_sample_is_bounded(self) -> Self:
        if self.status is not EvidenceStatus.LIVE_SAMPLE_VERIFIED:
            raise ValueError("frozen event coverage requires live_sample_verified evidence")
        if self.scope is not EvidenceScope.FROZEN_EXACT_INSTRUMENT_SNAPSHOT:
            raise ValueError("event evidence must use a frozen exact-instrument scope")
        if self.coverage_end < self.coverage_start:
            raise ValueError("coverage_end cannot precede coverage_start")
        return self

    def verified_for(self, instrument_id: str) -> bool:
        return instrument_id in self.instrument_ids

    def covers_event(self, event_code: str) -> bool:
        return event_code in self.event_codes


__all__ = [
    "DatasetResponseHash",
    "EventCoverageEvidence",
    "EvidenceScope",
    "EvidenceStatus",
    "FieldValueOrigin",
    "InterfaceKind",
    "MetricFieldBinding",
    "SourceCapabilityEvidence",
    "SourceProvider",
]
