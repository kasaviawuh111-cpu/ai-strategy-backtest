from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from ashare_lab.domain.source_validation import (
    CHOICE_ACTIVATION_PENDING,
    EASTMONEY_300059_LIVE_SAMPLE,
    EASTMONEY_F10_2020_SCHEMA,
    EASTMONEY_FROZEN_FORECAST_FLASH_EVENTS,
    TUSHARE_SCHEMA_ONLY,
    DatasetResponseHash,
    EvidenceScope,
    EvidenceStatus,
    FieldValueOrigin,
    InterfaceKind,
    MetricFieldBinding,
    SourceCandidatePolicy,
    SourceCapabilityEvidence,
    SourceProvider,
)

CHECKED_AT = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
SAMPLE_HASH = "sha256:" + ("1" * 64)


def _metric_binding(
    *,
    provider: SourceProvider = SourceProvider.EASTMONEY,
    metric_id: str = "financial.net_profit",
    dataset_id: str = "RPT_F10_FINANCE_GINCOME",
    provider_field: str = "NETPROFIT",
    origin: FieldValueOrigin = FieldValueOrigin.DIRECT_API,
    first_available_field: str | None = "NOTICE_DATE",
) -> MetricFieldBinding:
    return MetricFieldBinding(
        metric_id=metric_id,
        provider=provider,
        dataset_id=dataset_id,
        provider_field=provider_field,
        origin=origin,
        unit="CNY",
        report_period_field="REPORT_DATE",
        first_available_field=first_available_field,
        revision_field="UPDATE_DATE",
        derivation_id=("transform.net_profit.v1" if origin is FieldValueOrigin.DERIVED else None),
    )


def _metric_evidence(
    *,
    provider: SourceProvider = SourceProvider.EASTMONEY,
    status: EvidenceStatus = EvidenceStatus.LIVE_SAMPLE_VERIFIED,
    scope: EvidenceScope = EvidenceScope.EXACT_INSTRUMENT_SAMPLE,
    interface_kind: InterfaceKind = InterfaceKind.FORMAL_API,
    historical_pit_available: bool = True,
    verified_instruments: tuple[str, ...] = ("300059.SZ",),
    binding: MetricFieldBinding | None = None,
) -> SourceCapabilityEvidence:
    selected_binding = binding or _metric_binding(provider=provider)
    is_live = status is EvidenceStatus.LIVE_SAMPLE_VERIFIED
    return SourceCapabilityEvidence(
        provider=provider,
        capability_id="financial.provider_probe",
        status=status,
        scope=scope,
        interface_kind=interface_kind,
        historical_pit_available=historical_pit_available,
        checked_at=CHECKED_AT,
        endpoint_origin="https://example.invalid/securities/api/data",
        response_hashes=(
            DatasetResponseHash(
                dataset_id=selected_binding.dataset_id,
                response_sha256=SAMPLE_HASH,
            ),
        )
        if is_live
        else (),
        verified_datasets=(selected_binding.dataset_id,) if is_live else (),
        verified_fields=(
            selected_binding.provider_field,
            selected_binding.report_period_field,
            selected_binding.first_available_field or "NOTICE_DATE",
            selected_binding.revision_field or "UPDATE_DATE",
        )
        if is_live
        else (),
        verified_metric_ids=(selected_binding.metric_id,) if is_live else (),
        field_bindings=(selected_binding,) if is_live else (),
        verified_instruments=verified_instruments,
        blocker=None if is_live else "not live verified",
    )


def test_evidence_statuses_are_explicit_and_non_interchangeable() -> None:
    assert {item.value for item in EvidenceStatus} == {
        "live_sample_verified",
        "official_schema_verified",
        "configured_unverified",
        "unavailable",
    }


def test_current_source_baselines_do_not_overclaim_runtime_coverage() -> None:
    policy = SourceCandidatePolicy()
    binding = _metric_binding()

    assert CHOICE_ACTIVATION_PENDING.status is EvidenceStatus.CONFIGURED_UNVERIFIED
    assert CHOICE_ACTIVATION_PENDING.blocker == "activation_pending"
    assert TUSHARE_SCHEMA_ONLY.status is EvidenceStatus.OFFICIAL_SCHEMA_VERIFIED
    assert TUSHARE_SCHEMA_ONLY.verified_instruments == ()
    assert EASTMONEY_F10_2020_SCHEMA.status is EvidenceStatus.OFFICIAL_SCHEMA_VERIFIED
    assert EASTMONEY_F10_2020_SCHEMA.response_hashes == ()

    assert EASTMONEY_300059_LIVE_SAMPLE.status is EvidenceStatus.LIVE_SAMPLE_VERIFIED
    assert EASTMONEY_300059_LIVE_SAMPLE.interface_kind is InterfaceKind.PUBLIC_WEB_API
    assert EASTMONEY_300059_LIVE_SAMPLE.checked_at.utcoffset() is not None
    assert {
        item.dataset_id: item.response_sha256
        for item in EASTMONEY_300059_LIVE_SAMPLE.response_hashes
    } == {
        "RPT_F10_FINANCE_MAINFINADATA": (
            "sha256:57bb0db0a3f7882b7af4989aed764e24191b85af50895dfb3ac0239e46d84c8d"
        ),
        "RPT_F10_FINANCE_GINCOMEQUARTER": (
            "sha256:0344a1c346a07aed9d28f5faad82dc033918fce9803c18b3c855eb772809be9a"
        ),
    }
    assert EASTMONEY_300059_LIVE_SAMPLE.verified_metric_ids == ()
    assert EASTMONEY_300059_LIVE_SAMPLE.field_bindings == ()
    assert EASTMONEY_300059_LIVE_SAMPLE.verified_for("300059.SZ")
    assert not EASTMONEY_300059_LIVE_SAMPLE.verified_for("600519.SH")
    assert not policy.metric_is_executable(
        evidence=CHOICE_ACTIVATION_PENDING,
        binding=binding,
        instrument_id="300059.SZ",
    )
    assert not policy.metric_is_executable(
        evidence=TUSHARE_SCHEMA_ONLY,
        binding=binding,
        instrument_id="300059.SZ",
    )
    assert not policy.metric_is_executable(
        evidence=EASTMONEY_300059_LIVE_SAMPLE,
        binding=binding,
        instrument_id="300059.SZ",
    )


def test_single_instrument_sample_cannot_claim_wildcard_or_another_stock() -> None:
    with pytest.raises(ValidationError, match="verified_instruments"):
        _metric_evidence(verified_instruments=("*",))

    policy = SourceCandidatePolicy()
    assert policy.metric_is_executable(
        evidence=_metric_evidence(),
        binding=_metric_binding(),
        instrument_id="300059.SZ",
    )
    assert not policy.metric_is_executable(
        evidence=_metric_evidence(),
        binding=_metric_binding(),
        instrument_id="600519.SH",
    )


@pytest.mark.parametrize(
    ("status", "scope"),
    [
        (EvidenceStatus.OFFICIAL_SCHEMA_VERIFIED, EvidenceScope.OFFICIAL_SCHEMA),
        (EvidenceStatus.CONFIGURED_UNVERIFIED, EvidenceScope.CONFIGURATION_ONLY),
        (EvidenceStatus.UNAVAILABLE, EvidenceScope.UNAVAILABLE),
    ],
)
def test_non_live_evidence_never_becomes_executable(
    status: EvidenceStatus,
    scope: EvidenceScope,
) -> None:
    evidence = _metric_evidence(
        status=status,
        scope=scope,
        verified_instruments=(),
    )

    assert not SourceCandidatePolicy().metric_is_executable(
        evidence=evidence,
        binding=_metric_binding(),
        instrument_id="300059.SZ",
    )


def test_metric_candidate_requires_formal_direct_field_and_historical_pit() -> None:
    policy = SourceCandidatePolicy()

    assert policy.metric_is_executable(
        evidence=_metric_evidence(),
        binding=_metric_binding(),
        instrument_id="300059.SZ",
    )
    assert not policy.metric_is_executable(
        evidence=_metric_evidence(),
        binding=_metric_binding(origin=FieldValueOrigin.DERIVED),
        instrument_id="300059.SZ",
    )
    assert not policy.metric_is_executable(
        evidence=_metric_evidence(historical_pit_available=False),
        binding=_metric_binding(),
        instrument_id="300059.SZ",
    )
    assert not policy.metric_is_executable(
        evidence=_metric_evidence(interface_kind=InterfaceKind.PUBLIC_WEB_API),
        binding=_metric_binding(),
        instrument_id="300059.SZ",
    )
    assert not policy.metric_is_executable(
        evidence=_metric_evidence(),
        binding=_metric_binding(first_available_field=None),
        instrument_id="300059.SZ",
    )


def test_metric_binding_and_evidence_must_refer_to_same_provider_and_metric() -> None:
    policy = SourceCandidatePolicy()
    evidence = _metric_evidence()

    assert not policy.metric_is_executable(
        evidence=evidence,
        binding=_metric_binding(provider=SourceProvider.TUSHARE),
        instrument_id="300059.SZ",
    )
    assert not policy.metric_is_executable(
        evidence=evidence,
        binding=_metric_binding(metric_id="financial.revenue"),
        instrument_id="300059.SZ",
    )
    assert not policy.metric_is_executable(
        evidence=evidence,
        binding=_metric_binding(dataset_id="RPT_F10_FINANCE_GINCOMEQUARTER"),
        instrument_id="300059.SZ",
    )
    assert not policy.metric_is_executable(
        evidence=evidence,
        binding=_metric_binding(provider_field="PARENTNETPROFIT"),
        instrument_id="300059.SZ",
    )


def test_metric_binding_accepts_financial_and_valuation_namespaces() -> None:
    assert _metric_binding(metric_id="financial.net_profit").metric_id == "financial.net_profit"
    assert (
        _metric_binding(metric_id="valuation.price_to_earnings").metric_id
        == "valuation.price_to_earnings"
    )


def test_capability_evidence_requires_aware_check_time_and_valid_response_hashes() -> None:
    payload = _metric_evidence().model_dump()
    payload["checked_at"] = datetime(2026, 8, 31, 12, 0)
    with pytest.raises(ValidationError, match="checked_at"):
        SourceCapabilityEvidence.model_validate(payload)

    payload = _metric_evidence().model_dump()
    payload["response_hashes"] = [
        {"dataset_id": "RPT_F10_FINANCE_GINCOME", "response_sha256": "sha256:bad"}
    ]
    with pytest.raises(ValidationError, match="response_sha256"):
        SourceCapabilityEvidence.model_validate(payload)


def test_candidate_order_is_choice_tushare_eastmoney_then_exchange_cninfo() -> None:
    policy = SourceCandidatePolicy()
    unordered = (
        SourceProvider.EXCHANGE_CNINFO,
        SourceProvider.EASTMONEY,
        SourceProvider.CHOICE,
        SourceProvider.TUSHARE,
    )

    assert policy.order_providers(unordered) == (
        SourceProvider.CHOICE,
        SourceProvider.TUSHARE,
        SourceProvider.EASTMONEY,
        SourceProvider.EXCHANGE_CNINFO,
    )


def test_frozen_eastmoney_event_baseline_is_exact_not_universe_wide() -> None:
    evidence = EASTMONEY_FROZEN_FORECAST_FLASH_EVENTS
    policy = SourceCandidatePolicy()

    assert evidence.snapshot_frozen
    assert evidence.interface_kind is InterfaceKind.PUBLIC_WEB_API
    assert evidence.snapshot_id == (
        "events:399ba57af95e1f837b6d4dcdf13f5b9e148524cc0cad88e4ee08fb9837c7d32a"
    )
    assert evidence.event_codes == (
        "event.financial_results.earnings_flash_report",
        "event.financial_results.earnings_forecast_published",
    )
    assert evidence.coverage_start == date(2021, 2, 7)
    assert evidence.coverage_end == date(2026, 8, 20)
    assert not policy.event_is_executable(
        evidence=evidence,
        event_code="event.financial_results.earnings_forecast_published",
        instrument_id="300059.SZ",
    )
    assert not policy.event_is_executable(
        evidence=evidence,
        event_code="event.financial_results.annual_report",
        instrument_id="300059.SZ",
    )
    assert not policy.event_is_executable(
        evidence=evidence,
        event_code="event.financial_results.earnings_flash_report",
        instrument_id="600519.SH",
    )


def test_event_candidate_requires_frozen_formal_pit_evidence() -> None:
    evidence = EASTMONEY_FROZEN_FORECAST_FLASH_EVENTS
    policy = SourceCandidatePolicy()
    common = {
        "event_code": "event.financial_results.earnings_flash_report",
        "instrument_id": "300059.SZ",
    }

    formal = evidence.model_copy(update={"interface_kind": InterfaceKind.FORMAL_API})
    assert policy.event_is_executable(evidence=formal, **common)
    assert not policy.event_is_executable(
        evidence=formal.model_copy(update={"snapshot_frozen": False}),
        **common,
    )
    assert not policy.event_is_executable(
        evidence=formal.model_copy(update={"first_available_field": None}),
        **common,
    )
    assert not policy.event_is_executable(
        evidence=evidence.model_copy(update={"interface_kind": InterfaceKind.PUBLIC_WEB_API}),
        **common,
    )
