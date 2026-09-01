"""Immutable evidence binding a direct financial row to its announcement clock.

The financial provider's collection time is deliberately absent.  A statement
value can be replayed only after this record proves which initial complete
periodic report made it available.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final

from ashare_lab.domain.market_data import TimeQuality
from ashare_lab.domain.shared import DomainValidationError, InstrumentId, require_aware

from .models import FinancialReportType

_SHA256: Final = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
_PRECISE_TIME_QUALITIES: Final = frozenset({TimeQuality.EXACT, TimeQuality.VENDOR_OBSERVED})
_REPORT_TYPE_BY_PERIOD_END: Final = {
    (3, 31): FinancialReportType.Q1,
    (6, 30): FinancialReportType.SEMIANNUAL,
    (9, 30): FinancialReportType.Q3,
    (12, 31): FinancialReportType.ANNUAL,
}


@dataclass(frozen=True, slots=True)
class FinancialPublicationEvidence:
    """A strict F10 row-to-announcement join that is safe for historical replay.

    ``notice_date`` and ``update_date`` are the direct F10 row fields.  A later
    update must be rejected by the resolver unless a future revision-chain
    policy proves which revised provider value belongs to which announcement.
    """

    instrument_id: InstrumentId
    report_period: date
    report_type: FinancialReportType
    notice_date: date
    update_date: date
    event_provider: str
    event_code: str
    provider_event_id: str
    event_revision_no: int
    announced_at: datetime
    vendor_first_available_at: datetime | None
    first_available_at: datetime
    time_quality: TimeQuality
    validation_status: str
    document_sha256: str | None
    raw_response_sha256: str

    def __post_init__(self) -> None:
        if type(self.instrument_id) is not InstrumentId:
            raise DomainValidationError("instrument_id must be an InstrumentId")
        for field_name in ("report_period", "notice_date", "update_date"):
            if type(getattr(self, field_name)) is not date:
                raise DomainValidationError(f"{field_name} must be a date")
        expected_report_type = _REPORT_TYPE_BY_PERIOD_END.get(
            (self.report_period.month, self.report_period.day)
        )
        if expected_report_type is None or self.report_type is not expected_report_type:
            raise DomainValidationError("report_type must match a standard A-share report period")
        if type(self.event_revision_no) is not int or self.event_revision_no < 0:
            raise DomainValidationError("event_revision_no must be a non-negative integer")
        for field_name in ("event_provider", "event_code", "provider_event_id"):
            object.__setattr__(
                self,
                field_name,
                _required_text(getattr(self, field_name), field_name),
            )
        if self.validation_status != "validated":
            raise DomainValidationError("validation_status must be validated")
        if self.time_quality not in _PRECISE_TIME_QUALITIES:
            raise DomainValidationError("time_quality must be exact or vendor_observed")

        require_aware(self.announced_at, "announced_at")
        require_aware(self.first_available_at, "first_available_at")
        if self.vendor_first_available_at is not None:
            require_aware(self.vendor_first_available_at, "vendor_first_available_at")
            if self.vendor_first_available_at < self.announced_at:
                raise DomainValidationError("vendor_first_available_at cannot precede announced_at")
        expected_first_available = max(
            self.announced_at,
            self.vendor_first_available_at or self.announced_at,
        )
        if self.first_available_at != expected_first_available:
            raise DomainValidationError(
                "first_available_at must equal the later source or vendor availability"
            )

        object.__setattr__(
            self,
            "document_sha256",
            _normalized_sha256(self.document_sha256, "document_sha256", allow_none=True),
        )
        object.__setattr__(
            self,
            "raw_response_sha256",
            _normalized_sha256(self.raw_response_sha256, "raw_response_sha256"),
        )

    @property
    def binding_hash(self) -> str:
        """Deterministic binding identity with collection time intentionally absent."""

        encoded = _canonical_json(self._binding_payload()).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    @property
    def revision_id(self) -> str:
        """Stable revision key for a financial fact derived from this binding."""

        return self.binding_hash

    def to_dict(self) -> dict[str, object]:
        """Return the explicit, JSON-compatible evidence representation."""

        return {
            **self._binding_payload(),
            "binding_hash": self.binding_hash,
        }

    def _binding_payload(self) -> dict[str, object]:
        return {
            "announced_at": self.announced_at.isoformat(),
            "document_sha256": self.document_sha256,
            "event_code": self.event_code,
            "event_provider": self.event_provider,
            "event_revision_no": self.event_revision_no,
            "first_available_at": self.first_available_at.isoformat(),
            "instrument_id": str(self.instrument_id),
            "notice_date": self.notice_date.isoformat(),
            "provider_event_id": self.provider_event_id,
            "raw_response_sha256": self.raw_response_sha256,
            "report_period": self.report_period.isoformat(),
            "report_type": self.report_type.value,
            "schema_version": "financial-publication-evidence.v1",
            "time_quality": self.time_quality.value,
            "update_date": self.update_date.isoformat(),
            "validation_status": self.validation_status,
            "vendor_first_available_at": _serialize_datetime(self.vendor_first_available_at),
        }


def _required_text(value: object, field_name: str) -> str:
    if type(value) is not str or not value.strip() or value != value.strip():
        raise DomainValidationError(f"{field_name} must be nonblank exact text")
    return value


def _normalized_sha256(
    value: object,
    field_name: str,
    *,
    allow_none: bool = False,
) -> str | None:
    if value is None and allow_none:
        return None
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise DomainValidationError(f"{field_name} must be a sha256 content hash")
    return "sha256:" + value.removeprefix("sha256:")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _serialize_datetime(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


__all__ = ["FinancialPublicationEvidence"]
