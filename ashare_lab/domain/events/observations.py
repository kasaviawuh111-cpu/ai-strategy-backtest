"""Immutable provider observations used by the event-fusion boundary.

Provider records are deliberately kept separate from canonical events.  A later
vendor confirmation is another observation, not an in-place rewrite of the
event that a historical strategy could already see.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import cast

from ashare_lab.domain.market_data import TimeQuality
from ashare_lab.domain.shared import DomainValidationError, InstrumentId, require_aware

type EventAttribute = str | int | Decimal | bool | None

ANNOUNCEMENT_EVENT_PROVIDERS = ("eastmoney", "ifind", "rqdata", "tushare")
SUPPORTED_EVENT_PROVIDERS = frozenset({*ANNOUNCEMENT_EVENT_PROVIDERS, "web_archive"})
SUPPORTED_VALIDATION_STATUSES = frozenset(
    {"validated", "unverified", "blocked_time_quality", "rejected"}
)
_HEX_DIGITS = frozenset("0123456789abcdef")
_SEARCH_HIT_ATTRIBUTE_PREFIXES = ("search_hit", "search_result")


def _empty_attributes() -> dict[str, EventAttribute]:
    return {}


@dataclass(frozen=True, slots=True)
class EventObservation:
    """One immutable provider view of an event revision.

    ``retrieved_at`` is the real system-observation time.  It is retained for
    bitemporal audit and must never be substituted for market availability by
    the historical fusion policy.
    """

    provider: str
    provider_event_id: str
    instrument_id: InstrumentId
    event_code: str
    title: str
    occurred_at: datetime | None
    source_released_at: datetime | None
    vendor_first_available_at: datetime | None
    retrieved_at: datetime
    time_quality: TimeQuality
    document_url: str | None = None
    document_sha256: str | None = None
    raw_response_sha256: str | None = None
    external_fact_id: EventAttribute = None
    validation_status: str = "unverified"
    attributes: Mapping[str, EventAttribute] = field(default_factory=_empty_attributes)
    revision_no: int = 0

    def __post_init__(self) -> None:
        provider = _required_text(self.provider, "provider").lower()
        if provider not in SUPPORTED_EVENT_PROVIDERS:
            supported = ", ".join(sorted(SUPPORTED_EVENT_PROVIDERS))
            raise DomainValidationError(f"provider must be one of: {supported}")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(
            self,
            "provider_event_id",
            _required_text(self.provider_event_id, "provider_event_id"),
        )
        object.__setattr__(self, "event_code", _required_text(self.event_code, "event_code"))
        object.__setattr__(self, "title", _required_text(self.title, "title"))

        require_aware(self.retrieved_at, "retrieved_at")
        for field_name in (
            "occurred_at",
            "source_released_at",
            "vendor_first_available_at",
        ):
            value = getattr(self, field_name)
            if value is not None:
                require_aware(value, field_name)
        for field_name in ("source_released_at", "vendor_first_available_at"):
            value = getattr(self, field_name)
            if value is not None and self.retrieved_at < value:
                raise DomainValidationError(f"retrieved_at cannot precede {field_name}")

        if type(self.revision_no) is not int or self.revision_no < 0:
            raise DomainValidationError("revision_no must be a non-negative integer")
        if self.document_url is not None:
            object.__setattr__(
                self,
                "document_url",
                _required_text(self.document_url, "document_url"),
            )
        if self.document_sha256 is not None:
            object.__setattr__(
                self,
                "document_sha256",
                _normalized_sha256(self.document_sha256),
            )
        if self.raw_response_sha256 is not None:
            object.__setattr__(
                self,
                "raw_response_sha256",
                _normalized_sha256(self.raw_response_sha256),
            )
        validation_status = _required_text(
            self.validation_status,
            "validation_status",
        ).lower()
        if validation_status not in SUPPORTED_VALIDATION_STATUSES:
            supported = ", ".join(sorted(SUPPORTED_VALIDATION_STATUSES))
            raise DomainValidationError(f"validation_status must be one of: {supported}")
        object.__setattr__(self, "validation_status", validation_status)
        attributes = _immutable_attributes(self.attributes)
        fact_from_attributes = attributes.get("external_fact_id")
        if (
            self.external_fact_id is not None
            and fact_from_attributes is not None
            and _scalar_identity(self.external_fact_id) != _scalar_identity(fact_from_attributes)
        ):
            raise DomainValidationError(
                "external_fact_id conflicts with attributes.external_fact_id"
            )
        external_fact_id = self.external_fact_id
        if external_fact_id is None:
            external_fact_id = fact_from_attributes
        if external_fact_id is not None:
            _validate_scalar(external_fact_id, "external_fact_id")
            if isinstance(external_fact_id, str):
                external_fact_id = _required_text(external_fact_id, "external_fact_id")
        object.__setattr__(self, "external_fact_id", external_fact_id)
        object.__setattr__(self, "attributes", attributes)

    @property
    def provider_revision_key(self) -> tuple[str, str, int]:
        """Stable source-local identity used to reject contradictory duplicates."""

        return (self.provider, self.provider_event_id, self.revision_no)


def _required_text(value: object, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise DomainValidationError(f"{field_name} must be a non-empty string")
    return value.strip()


def _normalized_sha256(value: str) -> str:
    digest = _required_text(value, "document_sha256").lower().removeprefix("sha256:")
    if len(digest) != 64 or any(character not in _HEX_DIGITS for character in digest):
        raise DomainValidationError("document_sha256 must contain 64 hexadecimal digits")
    return digest


def _immutable_attributes(
    values: Mapping[str, EventAttribute],
) -> Mapping[str, EventAttribute]:
    normalized: dict[str, EventAttribute] = {}
    raw_values = cast(Mapping[object, object], values)
    for raw_name, value in raw_values.items():
        name = _required_text(raw_name, "attribute name")
        if name.casefold().startswith(_SEARCH_HIT_ATTRIBUTE_PREFIXES):
            raise DomainValidationError("search hits must not be stored in EventObservation")
        _validate_scalar(value, f"attribute {name!r}")
        normalized[name] = cast(EventAttribute, value)
    return MappingProxyType(normalized)


def _validate_scalar(value: object, field_name: str) -> None:
    if value is not None and not isinstance(value, str | int | Decimal | bool):
        raise DomainValidationError(f"{field_name} must be a scalar value")
    if isinstance(value, Decimal) and not value.is_finite():
        raise DomainValidationError(f"{field_name} Decimal must be finite")


def _scalar_identity(value: EventAttribute) -> tuple[str, str]:
    if value is None:
        return ("null", "")
    if isinstance(value, bool):
        return ("bool", "true" if value else "false")
    if isinstance(value, Decimal):
        return ("number", str(value.normalize()))
    if isinstance(value, int):
        return ("number", str(value))
    return ("text", value.strip())
