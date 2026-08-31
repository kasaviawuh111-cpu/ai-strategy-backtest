"""Immutable facts emitted by the pure signal runtime."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from ashare_lab.domain.shared import DomainValidationError, InstrumentId, require_aware


@dataclass(frozen=True, slots=True)
class SignalEvidence:
    """Auditable source fact that caused a signal, without provider execution data."""

    evidence_type: str
    evidence_id: str
    available_at: datetime
    source_event_id: str | None = None
    provider: str | None = None
    source_url: str | None = None
    time_quality: str | None = None
    timestamp_precision: str | None = None
    validation_status: str | None = None
    raw_response_sha256: str | None = None

    def __post_init__(self) -> None:
        require_aware(self.available_at, "evidence.available_at")
        if not self.evidence_type.strip() or not self.evidence_id.strip():
            raise DomainValidationError("signal evidence type and id cannot be blank")
        for field_name in ("source_event_id", "provider", "source_url", "time_quality"):
            value = getattr(self, field_name)
            if value is not None and not value.strip():
                raise DomainValidationError(f"signal evidence {field_name} cannot be blank")
        if self.timestamp_precision not in {None, "second", "minute", "hour", "date"}:
            raise DomainValidationError("signal evidence timestamp_precision is invalid")
        if self.raw_response_sha256 is not None:
            digest = self.raw_response_sha256.removeprefix("sha256:")
            if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise DomainValidationError("signal evidence raw hash must be SHA-256")


@dataclass(frozen=True, slots=True)
class SignalFact:
    """The reproducible result of evaluating one condition at one bar close.

    ``observed_at`` is the market observation time (15:00 Asia/Shanghai for the
    daily runtime). ``available_at`` is the earliest time the complete evidence
    set was available to the strategy.  Keeping both prevents a late data record
    from being silently treated as if it had been known at the close.
    """

    instrument_id: InstrumentId
    session_date: date
    condition_ref: str
    triggered: bool
    observed_at: datetime
    available_at: datetime
    reason: str
    left_value: Decimal | None = None
    right_value: Decimal | None = None
    children: tuple[SignalFact, ...] = ()
    evidence: tuple[SignalEvidence, ...] = ()

    def __post_init__(self) -> None:
        require_aware(self.observed_at, "observed_at")
        require_aware(self.available_at, "available_at")
        if self.available_at < self.observed_at:
            raise DomainValidationError("signal available_at cannot precede observed_at")
        if not self.condition_ref:
            raise DomainValidationError("condition_ref cannot be empty")
        if not self.reason:
            raise DomainValidationError("signal reason cannot be empty")
        for field_name in ("left_value", "right_value"):
            value = getattr(self, field_name)
            if value is not None and (not isinstance(value, Decimal) or not value.is_finite()):
                raise DomainValidationError(f"{field_name} must be a finite Decimal")
        for child in self.children:
            if child.instrument_id != self.instrument_id or child.session_date != self.session_date:
                raise DomainValidationError("child signal facts must describe the same bar")
        evidence_keys = tuple(
            (item.evidence_type, item.evidence_id, item.source_event_id) for item in self.evidence
        )
        if len(evidence_keys) != len(set(evidence_keys)):
            raise DomainValidationError("signal evidence must be unique")
