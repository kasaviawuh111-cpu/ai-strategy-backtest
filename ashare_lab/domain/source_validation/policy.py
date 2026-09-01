"""Provider ordering and executable-candidate gates."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import (
    EventCoverageEvidence,
    EvidenceStatus,
    FieldValueOrigin,
    InterfaceKind,
    MetricFieldBinding,
    SourceCapabilityEvidence,
    SourceProvider,
)

_REQUIRED_PROVIDER_ORDER = (
    SourceProvider.CHOICE,
    SourceProvider.TUSHARE,
    SourceProvider.EASTMONEY,
    SourceProvider.EXCHANGE_CNINFO,
)


class SourceCandidatePolicy(BaseModel):
    """Fail-closed policy for phase-one provider evaluation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_order: tuple[SourceProvider, ...] = Field(
        default=_REQUIRED_PROVIDER_ORDER,
        min_length=4,
        max_length=4,
    )

    @model_validator(mode="after")
    def provider_order_is_fixed(self) -> Self:
        if self.provider_order != _REQUIRED_PROVIDER_ORDER:
            raise ValueError("provider order must be Choice, Tushare, Eastmoney, Exchange/CNInfo")
        return self

    def order_providers(
        self,
        providers: tuple[SourceProvider, ...],
    ) -> tuple[SourceProvider, ...]:
        """Return unique providers in the approved evaluation order."""

        rank = {provider: index for index, provider in enumerate(self.provider_order)}
        return tuple(sorted(set(providers), key=rank.__getitem__))

    def metric_is_executable(
        self,
        *,
        evidence: SourceCapabilityEvidence,
        binding: MetricFieldBinding,
        instrument_id: str,
    ) -> bool:
        """Gate one raw financial field; derived values always fail closed."""

        return (
            evidence.provider is binding.provider
            and evidence.status is EvidenceStatus.LIVE_SAMPLE_VERIFIED
            and evidence.interface_kind is InterfaceKind.FORMAL_API
            and evidence.historical_pit_available
            and evidence.verified_for(instrument_id)
            and binding.metric_id in evidence.verified_metric_ids
            and binding.dataset_id in evidence.verified_datasets
            and binding.provider_field in evidence.verified_fields
            and binding in evidence.field_bindings
            and binding.origin is FieldValueOrigin.DIRECT_API
            and binding.has_historical_pit
        )

    def event_is_executable(
        self,
        *,
        evidence: EventCoverageEvidence,
        event_code: str,
        instrument_id: str,
    ) -> bool:
        """Gate an event only inside its exact immutable coverage envelope."""

        return (
            evidence.status is EvidenceStatus.LIVE_SAMPLE_VERIFIED
            and evidence.interface_kind is InterfaceKind.FORMAL_API
            and evidence.snapshot_frozen
            and evidence.first_available_field is not None
            and evidence.verified_for(instrument_id)
            and evidence.covers_event(event_code)
        )


__all__ = ["SourceCandidatePolicy"]
