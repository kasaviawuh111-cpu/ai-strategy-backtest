"""Fail-closed F10 statement-row to periodic-announcement resolver.

This adapter joins direct Eastmoney F10 period rows to the existing strict
announcement observations.  It does not fetch data, infer a report date, or
choose a later revision: any ambiguous or incomplete join is rejected.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Final, cast
from zoneinfo import ZoneInfo

from ashare_lab.domain.events import EventObservation
from ashare_lab.domain.financials import FinancialPublicationEvidence, FinancialReportType
from ashare_lab.domain.market_data import TimeQuality
from ashare_lab.domain.shared import InstrumentId

_SHANGHAI: Final = ZoneInfo("Asia/Shanghai")
_PRECISE_TIME_QUALITIES: Final = frozenset({TimeQuality.EXACT, TimeQuality.VENDOR_OBSERVED})


class FinancialPublicationResolutionError(ValueError):
    """A provider row lacks a single auditable historical publication clock."""


class _PeriodicDefinition:
    __slots__ = ("event_code", "event_report_type", "f10_report_types", "report_type")

    def __init__(
        self,
        *,
        report_type: FinancialReportType,
        event_code: str,
        event_report_type: str,
        f10_report_types: frozenset[str],
    ) -> None:
        self.report_type = report_type
        self.event_code = event_code
        self.event_report_type = event_report_type
        self.f10_report_types = f10_report_types


_PERIODIC_DEFINITIONS: Final = {
    (3, 31): _PeriodicDefinition(
        report_type=FinancialReportType.Q1,
        event_code="event.financial_results.quarterly_report",
        event_report_type="quarterly_report",
        f10_report_types=frozenset({"一季报", "第一季度报告"}),
    ),
    (6, 30): _PeriodicDefinition(
        report_type=FinancialReportType.SEMIANNUAL,
        event_code="event.financial_results.semiannual_report",
        event_report_type="semiannual_report",
        f10_report_types=frozenset({"中报", "半年报", "半年度报告"}),
    ),
    (9, 30): _PeriodicDefinition(
        report_type=FinancialReportType.Q3,
        event_code="event.financial_results.quarterly_report",
        event_report_type="quarterly_report",
        f10_report_types=frozenset({"三季报", "第三季度报告"}),
    ),
    (12, 31): _PeriodicDefinition(
        report_type=FinancialReportType.ANNUAL,
        event_code="event.financial_results.annual_report",
        event_report_type="annual_report",
        f10_report_types=frozenset({"年报", "年度报告"}),
    ),
}


def resolve_financial_publication_evidence(
    *,
    instrument_id: InstrumentId,
    f10_row: Mapping[str, object],
    observations: Sequence[EventObservation],
    event_provider: str = "eastmoney",
) -> FinancialPublicationEvidence:
    """Resolve exactly one initial complete periodic report for an F10 row.

    ``UPDATE_DATE`` later than ``NOTICE_DATE`` is not safe to replay from an
    initial report alone.  This first version therefore rejects it until a
    dedicated provider revision chain is introduced.
    """

    if type(instrument_id) is not InstrumentId:
        raise FinancialPublicationResolutionError("instrument_id must be an InstrumentId")
    provider = _required_text(event_provider, "event_provider")
    row = _row_mapping(f10_row)
    report_period = _provider_date(row, "REPORT_DATE")
    notice_date = _provider_date(row, "NOTICE_DATE")
    update_date = _provider_date(row, "UPDATE_DATE")
    definition = _PERIODIC_DEFINITIONS.get((report_period.month, report_period.day))
    if definition is None:
        raise FinancialPublicationResolutionError(
            "REPORT_DATE is not a standard A-share quarterly reporting period"
        )
    f10_report_type = _required_text(row.get("REPORT_TYPE"), "REPORT_TYPE")
    if f10_report_type not in definition.f10_report_types:
        raise FinancialPublicationResolutionError("REPORT_TYPE does not match REPORT_DATE")
    if update_date != notice_date:
        raise FinancialPublicationResolutionError(
            "UPDATE_DATE differs from NOTICE_DATE without a verified revision evidence chain"
        )

    matched = _matching_periodic_observations(
        instrument_id=instrument_id,
        definition=definition,
        report_period=report_period,
        observations=observations,
        event_provider=provider,
    )
    observation = _single_semantic_observation(matched, report_period=report_period)
    _validate_strict_observation(observation, notice_date=notice_date)
    observed_provider_time = observation.source_released_at or observation.vendor_first_available_at
    if observed_provider_time is None:
        raise AssertionError("strict observation validator must require an availability time")
    announced_at = observed_provider_time.astimezone(_SHANGHAI)
    vendor_first_available_at = (
        observation.vendor_first_available_at.astimezone(_SHANGHAI)
        if observation.vendor_first_available_at is not None
        else None
    )
    first_available_at = max(announced_at, vendor_first_available_at or announced_at)
    return FinancialPublicationEvidence(
        instrument_id=instrument_id,
        report_period=report_period,
        report_type=definition.report_type,
        notice_date=notice_date,
        update_date=update_date,
        event_provider=observation.provider,
        event_code=observation.event_code,
        provider_event_id=observation.provider_event_id,
        event_revision_no=observation.revision_no,
        announced_at=announced_at,
        vendor_first_available_at=vendor_first_available_at,
        first_available_at=first_available_at,
        time_quality=observation.time_quality,
        validation_status=observation.validation_status,
        document_sha256=observation.document_sha256,
        raw_response_sha256=observation.raw_response_sha256 or "",
    )


def _row_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise FinancialPublicationResolutionError("f10_row must be a mapping")
    row = cast(Mapping[object, object], value)
    if any(type(key) is not str for key in row):
        raise FinancialPublicationResolutionError("F10 row field names must be strings")
    return cast(Mapping[str, object], row)


def _provider_date(row: Mapping[str, object], field_name: str) -> date:
    value = row.get(field_name)
    if type(value) is not str or not value:
        raise FinancialPublicationResolutionError(f"{field_name} is missing")
    try:
        return datetime.fromisoformat(value).date()
    except ValueError as error:
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise FinancialPublicationResolutionError(f"{field_name} is invalid") from error


def _required_text(value: object, field_name: str) -> str:
    if type(value) is not str or not value.strip() or value != value.strip():
        raise FinancialPublicationResolutionError(f"{field_name} must be nonblank exact text")
    return value


def _matching_periodic_observations(
    *,
    instrument_id: InstrumentId,
    definition: _PeriodicDefinition,
    report_period: date,
    observations: Sequence[EventObservation],
    event_provider: str,
) -> tuple[EventObservation, ...]:
    matching: list[EventObservation] = []
    expected_stat_date = report_period.isoformat()
    for observation in observations:
        if type(observation) is not EventObservation:
            raise FinancialPublicationResolutionError("observations must contain EventObservation")
        if observation.instrument_id != instrument_id or observation.provider != event_provider:
            continue
        if observation.event_code != definition.event_code:
            continue
        if observation.attributes.get("report_type") != definition.event_report_type:
            continue
        if observation.attributes.get("stat_date") != expected_stat_date:
            continue
        matching.append(observation)
    if not matching:
        raise FinancialPublicationResolutionError(
            "no exact periodic announcement matches F10 REPORT_DATE and REPORT_TYPE"
        )
    return tuple(matching)


def _single_semantic_observation(
    observations: Sequence[EventObservation],
    *,
    report_period: date,
) -> EventObservation:
    unique: dict[tuple[str, str, int], EventObservation] = {}
    for observation in observations:
        existing = unique.get(observation.provider_revision_key)
        if existing is not None and _semantic_signature(existing) != _semantic_signature(
            observation
        ):
            raise FinancialPublicationResolutionError(
                "contradictory copies share one announcement revision identity"
            )
        unique[observation.provider_revision_key] = observation
    if len(unique) != 1:
        raise FinancialPublicationResolutionError(
            "ambiguous initial complete periodic announcements for " + report_period.isoformat()
        )
    return next(iter(unique.values()))


def _semantic_signature(observation: EventObservation) -> tuple[object, ...]:
    """Compare duplicate provider revisions without using collection time."""

    return (
        str(observation.instrument_id),
        observation.event_code,
        observation.title,
        observation.occurred_at,
        observation.source_released_at,
        observation.vendor_first_available_at,
        observation.time_quality,
        observation.document_url,
        observation.document_sha256,
        observation.raw_response_sha256,
        observation.external_fact_id,
        observation.validation_status,
        tuple(sorted((key, repr(value)) for key, value in observation.attributes.items())),
    )


def _validate_strict_observation(
    observation: EventObservation,
    *,
    notice_date: date,
) -> None:
    if observation.validation_status != "validated":
        raise FinancialPublicationResolutionError(
            "announcement validation_status must be validated"
        )
    if observation.time_quality not in _PRECISE_TIME_QUALITIES:
        raise FinancialPublicationResolutionError(
            "announcement time_quality must be exact or vendor_observed"
        )
    if observation.attributes.get("document_version_role") != "initial_complete":
        raise FinancialPublicationResolutionError(
            "announcement document_version_role must be initial_complete"
        )
    if observation.attributes.get("report_period_quality") != "title_exact":
        raise FinancialPublicationResolutionError(
            "announcement report_period_quality must be title_exact"
        )
    raw_notice_date = observation.attributes.get("raw_notice_date")
    if isinstance(raw_notice_date, str):
        try:
            provider_notice_date = date.fromisoformat(raw_notice_date)
        except ValueError as exc:
            raise FinancialPublicationResolutionError(
                "announcement raw_notice_date is invalid"
            ) from exc
        if provider_notice_date != notice_date:
            raise FinancialPublicationResolutionError(
                "F10 NOTICE_DATE does not match the announcement notice date"
            )
    elif observation.source_released_at is not None:
        source_released_at = observation.source_released_at.astimezone(_SHANGHAI)
        if source_released_at.date() != notice_date:
            raise FinancialPublicationResolutionError(
                "F10 NOTICE_DATE does not match the announcement source release date"
            )
    else:
        raise FinancialPublicationResolutionError(
            "announcement notice date is required when display time is unavailable"
        )
    if observation.source_released_at is None and observation.vendor_first_available_at is None:
        raise FinancialPublicationResolutionError("announcement availability time is required")
    if (
        observation.source_released_at is not None
        and observation.vendor_first_available_at is not None
        and observation.vendor_first_available_at < observation.source_released_at
    ):
        raise FinancialPublicationResolutionError(
            "vendor_first_available_at cannot precede source_released_at"
        )
    if observation.raw_response_sha256 is None:
        raise FinancialPublicationResolutionError("announcement raw_response_sha256 is required")


__all__ = [
    "FinancialPublicationResolutionError",
    "resolve_financial_publication_evidence",
]
