"""Conservative local evidence baselines verified before phase-one integration."""

from datetime import UTC, date, datetime

from .models import (
    DatasetResponseHash,
    EventCoverageEvidence,
    EvidenceScope,
    EvidenceStatus,
    InterfaceKind,
    SourceCapabilityEvidence,
    SourceProvider,
)

_CHECKED_AT = datetime(2026, 8, 31, tzinfo=UTC)
_EASTMONEY_F10_DATASETS = (
    "RPT_F10_FINANCE_GINCOMEQUARTER",
    "RPT_F10_FINANCE_MAINFINADATA",
)
_EASTMONEY_REPORTED_FIELDS = (
    "CURRENCY",
    "NOTICE_DATE",
    "PARENTNETPROFIT",
    "PARENTNETPROFITTZ",
    "REPORT_DATE",
    "REPORT_TYPE",
    "ROEJQ",
    "TOTALOPERATEREVE",
    "TOTALOPERATEREVETZ",
    "UPDATE_DATE",
    "XSJLL",
    "XSMLL",
)

CHOICE_ACTIVATION_PENDING = SourceCapabilityEvidence(
    provider=SourceProvider.CHOICE,
    capability_id="financial.provider_api",
    status=EvidenceStatus.CONFIGURED_UNVERIFIED,
    scope=EvidenceScope.CONFIGURATION_ONLY,
    interface_kind=InterfaceKind.FORMAL_API,
    historical_pit_available=False,
    checked_at=_CHECKED_AT,
    endpoint_origin="choice.quant.api",
    blocker="activation_pending",
)

TUSHARE_SCHEMA_ONLY = SourceCapabilityEvidence(
    provider=SourceProvider.TUSHARE,
    capability_id="financial.provider_schema",
    status=EvidenceStatus.OFFICIAL_SCHEMA_VERIFIED,
    scope=EvidenceScope.OFFICIAL_SCHEMA,
    interface_kind=InterfaceKind.FORMAL_API,
    historical_pit_available=True,
    checked_at=_CHECKED_AT,
    endpoint_origin="https://tushare.pro/document/2",
    blocker="Tushare official schema is verified but no live sample is verified",
)

EASTMONEY_F10_2020_SCHEMA = SourceCapabilityEvidence(
    provider=SourceProvider.EASTMONEY,
    capability_id="financial.f10_schema",
    status=EvidenceStatus.OFFICIAL_SCHEMA_VERIFIED,
    scope=EvidenceScope.OFFICIAL_SCHEMA,
    interface_kind=InterfaceKind.PUBLIC_WEB_API,
    historical_pit_available=True,
    checked_at=_CHECKED_AT,
    endpoint_origin="/securities/api/data",
    verified_datasets=_EASTMONEY_F10_DATASETS,
    verified_fields=_EASTMONEY_REPORTED_FIELDS,
    blocker="2020 F10 document is schema evidence only; production authorization is unverified",
)

EASTMONEY_300059_LIVE_SAMPLE = SourceCapabilityEvidence(
    provider=SourceProvider.EASTMONEY,
    capability_id="financial.provider_live_sample",
    status=EvidenceStatus.LIVE_SAMPLE_VERIFIED,
    scope=EvidenceScope.EXACT_INSTRUMENT_SAMPLE,
    interface_kind=InterfaceKind.PUBLIC_WEB_API,
    historical_pit_available=True,
    checked_at=_CHECKED_AT,
    endpoint_origin="https://datacenter.eastmoney.com/securities/api/data/v1/get",
    response_hashes=(
        DatasetResponseHash(
            dataset_id="RPT_F10_FINANCE_GINCOMEQUARTER",
            response_sha256=(
                "sha256:0344a1c346a07aed9d28f5faad82dc033918fce9803c18b3c855eb772809be9a"
            ),
        ),
        DatasetResponseHash(
            dataset_id="RPT_F10_FINANCE_MAINFINADATA",
            response_sha256=(
                "sha256:57bb0db0a3f7882b7af4989aed764e24191b85af50895dfb3ac0239e46d84c8d"
            ),
        ),
    ),
    verified_datasets=_EASTMONEY_F10_DATASETS,
    verified_fields=_EASTMONEY_REPORTED_FIELDS,
    verified_instruments=("300059.SZ",),
)

EASTMONEY_FROZEN_FORECAST_FLASH_EVENTS = EventCoverageEvidence(
    provider=SourceProvider.EASTMONEY,
    status=EvidenceStatus.LIVE_SAMPLE_VERIFIED,
    scope=EvidenceScope.FROZEN_EXACT_INSTRUMENT_SNAPSHOT,
    interface_kind=InterfaceKind.PUBLIC_WEB_API,
    snapshot_id="events:399ba57af95e1f837b6d4dcdf13f5b9e148524cc0cad88e4ee08fb9837c7d32a",
    snapshot_frozen=True,
    instrument_ids=("300059.SZ",),
    event_codes=(
        "event.financial_results.earnings_flash_report",
        "event.financial_results.earnings_forecast_published",
    ),
    first_available_field="vendor_first_available_at",
    coverage_start=date(2021, 2, 7),
    coverage_end=date(2026, 8, 20),
)

__all__ = [
    "CHOICE_ACTIVATION_PENDING",
    "EASTMONEY_300059_LIVE_SAMPLE",
    "EASTMONEY_F10_2020_SCHEMA",
    "EASTMONEY_FROZEN_FORECAST_FLASH_EVENTS",
    "TUSHARE_SCHEMA_ONLY",
]
