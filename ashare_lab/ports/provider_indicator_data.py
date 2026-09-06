"""Provider-supplied historical indicator series used by backtests.

The values in this contract are returned by the configured data provider.  A
consumer may compare or combine them, but must never derive the indicator from
OHLCV as a silent fallback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Protocol

from ashare_lab.domain.shared import DomainValidationError, require_aware

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_INSTRUMENT = re.compile(r"^[0-9]{6}\.(?:SH|SZ|BJ)$")
_INDICATOR_ID = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")


@dataclass(frozen=True, slots=True)
class ProviderIndicatorValue:
    """One exact numeric field returned by the provider."""

    field_code: str
    field_name: str
    value: Decimal
    unit: str | None = None
    source_field_name: str | None = None
    source_unit: str | None = None
    source_parameters: str | None = None

    def __post_init__(self) -> None:
        if not self.field_code.strip() or not self.field_name.strip():
            raise DomainValidationError("provider indicator field identity cannot be blank")
        if not self.value.is_finite():
            raise DomainValidationError("provider indicator value must be finite")
        if self.unit is not None and not self.unit.strip():
            raise DomainValidationError("provider indicator unit cannot be blank")


@dataclass(frozen=True, slots=True)
class ProviderIndicatorPoint:
    """Provider values for one completed market session."""

    session_date: date
    observed_at: datetime
    first_available_at: datetime
    values: tuple[ProviderIndicatorValue, ...]

    def __post_init__(self) -> None:
        require_aware(self.observed_at, "provider_indicator.observed_at")
        require_aware(self.first_available_at, "provider_indicator.first_available_at")
        if self.first_available_at < self.observed_at:
            raise DomainValidationError(
                "provider indicator cannot be available before it was observed"
            )
        if self.observed_at.date() != self.session_date:
            raise DomainValidationError("provider indicator observation date is inconsistent")
        if not self.values:
            raise DomainValidationError("provider indicator point must contain values")
        names = tuple(value.field_name.casefold() for value in self.values)
        if len(names) != len(set(names)):
            raise DomainValidationError("provider indicator fields must be unique")


@dataclass(frozen=True, slots=True)
class ProviderIndicatorSeries:
    """Immutable, auditable provider response for one historical indicator."""

    provider: str
    instrument_id: str
    indicator_id: str
    requested_start: date
    requested_end: date
    points: tuple[ProviderIndicatorPoint, ...]
    response_sha256: str
    retrieved_at: datetime
    schema_version: str
    query: str
    cache_status: Literal["memory", "disk", "live", "forced"] | None = None

    def __post_init__(self) -> None:
        if not self.provider.strip() or not self.schema_version.strip() or not self.query.strip():
            raise DomainValidationError("provider indicator provenance cannot be blank")
        if _INSTRUMENT.fullmatch(self.instrument_id) is None:
            raise DomainValidationError("provider indicator instrument_id is invalid")
        if _INDICATOR_ID.fullmatch(self.indicator_id) is None:
            raise DomainValidationError("provider indicator id is invalid")
        if self.requested_start > self.requested_end:
            raise DomainValidationError("provider indicator requested range is inverted")
        if _SHA256.fullmatch(self.response_sha256) is None:
            raise DomainValidationError("provider indicator response hash must be SHA-256")
        require_aware(self.retrieved_at, "provider_indicator.retrieved_at")
        if self.cache_status not in (None, "memory", "disk", "live", "forced"):
            raise DomainValidationError("provider indicator cache status is invalid")
        dates = tuple(point.session_date for point in self.points)
        if not dates or dates != tuple(sorted(set(dates))):
            raise DomainValidationError(
                "provider indicator points must have unique ascending session dates"
            )
        if any(
            point.session_date < self.requested_start or point.session_date > self.requested_end
            for point in self.points
        ):
            raise DomainValidationError("provider indicator point falls outside requested range")


class HistoricalIndicatorData(Protocol):
    """Exact historical indicator values supplied by a server-owned provider."""

    async def query_indicator_history(
        self,
        *,
        instrument_id: str,
        indicator_id: str,
        provider_indicator_name: str,
        value_names: tuple[str, ...],
        start: date,
        end: date,
    ) -> ProviderIndicatorSeries: ...
