"""Fail-closed point-in-time primitives for future v2 data contracts.

This module is deliberately independent from the current v1 signal and
backtest runtimes.  It records when a fact was knowable and maps that instant
to an explicit A-share session schedule without guessing holidays or fills.
"""

from __future__ import annotations

import json
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from itertools import pairwise
from typing import TypeVar, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ashare_lab.domain.shared import DomainValidationError, require_aware

_Value = TypeVar("_Value")
_DATETIME_FIELDS = (
    "observed_at",
    "announced_at",
    "first_available_at",
    "signal_at",
    "execution_at",
    "retrieved_at",
)
_SERIALIZED_FIELDS = frozenset(
    {
        *_DATETIME_FIELDS,
        "timezone",
        "source",
        "revision_id",
    }
)


@dataclass(frozen=True, slots=True)
class PointInTimeAvailability:
    """Immutable visibility and execution clocks for one data revision.

    ``observed_at`` describes the time represented by the data.
    ``announced_at`` is the source publication time when one is known.
    ``first_available_at`` is the only clock used to gate a historical replay.
    ``retrieved_at`` is audit provenance only and never supplies a missing
    historical availability time.
    """

    observed_at: datetime | None
    announced_at: datetime | None
    first_available_at: datetime | None
    signal_at: datetime | None
    execution_at: datetime | None
    retrieved_at: datetime | None
    timezone: str
    source: str
    revision_id: str

    def __post_init__(self) -> None:
        timezone_name, zone = _validated_timezone(self.timezone)
        object.__setattr__(self, "timezone", timezone_name)
        object.__setattr__(self, "source", _required_text(self.source, "source"))
        object.__setattr__(
            self,
            "revision_id",
            _required_text(self.revision_id, "revision_id"),
        )

        for field_name in _DATETIME_FIELDS:
            value = getattr(self, field_name)
            if value is not None:
                _require_datetime_in_zone(value, field_name, zone, timezone_name)

        if self.signal_at is not None and self.first_available_at is None:
            raise DomainValidationError("signal_at requires first_available_at")
        if self.execution_at is not None and self.signal_at is None:
            raise DomainValidationError("execution_at requires signal_at")
        if (
            self.announced_at is not None
            and self.first_available_at is not None
            and self.announced_at > self.first_available_at
        ):
            raise DomainValidationError("first_available_at cannot precede announced_at")
        if (
            self.first_available_at is not None
            and self.signal_at is not None
            and self.first_available_at > self.signal_at
        ):
            raise DomainValidationError("first_available_at cannot follow signal_at")
        if (
            self.signal_at is not None
            and self.execution_at is not None
            and self.signal_at > self.execution_at
        ):
            raise DomainValidationError("signal_at cannot follow execution_at")

    def is_available_at(self, as_of: datetime) -> bool:
        """Whether the revision was historically usable at ``as_of``.

        A missing ``first_available_at`` fails closed even if ``retrieved_at``
        is present.  This is the core no-look-ahead boundary.
        """

        _, zone = _validated_timezone(self.timezone)
        _require_datetime_in_zone(as_of, "as_of", zone, self.timezone)
        return self.first_available_at is not None and self.first_available_at <= as_of

    def value_as_of(self, value: _Value | None, as_of: datetime) -> _Value | None:
        """Return a value only when it was available, preserving missingness."""

        if value is None or not self.is_available_at(as_of):
            return None
        return value

    def to_dict(self) -> dict[str, str | None]:
        """Return the canonical JSON-compatible representation."""

        return {
            "observed_at": _serialize_datetime(self.observed_at),
            "announced_at": _serialize_datetime(self.announced_at),
            "first_available_at": _serialize_datetime(self.first_available_at),
            "signal_at": _serialize_datetime(self.signal_at),
            "execution_at": _serialize_datetime(self.execution_at),
            "retrieved_at": _serialize_datetime(self.retrieved_at),
            "timezone": self.timezone,
            "source": self.source,
            "revision_id": self.revision_id,
        }

    def to_json(self) -> str:
        """Serialize with stable key order and no insignificant whitespace."""

        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_dict(cls, payload: object) -> PointInTimeAvailability:
        """Parse the canonical representation, rejecting omissions and extras."""

        if not isinstance(payload, Mapping):
            raise DomainValidationError("point-in-time payload must be an object")
        raw_payload = cast(Mapping[object, object], payload)
        if any(type(key) is not str for key in raw_payload):
            raise DomainValidationError("point-in-time field names must be strings")
        fields = cast(set[str], set(raw_payload))
        extras = fields - _SERIALIZED_FIELDS
        missing = _SERIALIZED_FIELDS - fields
        if extras:
            raise DomainValidationError(
                f"point-in-time payload has unexpected fields: {', '.join(sorted(extras))}"
            )
        if missing:
            raise DomainValidationError(
                f"point-in-time payload is missing fields: {', '.join(sorted(missing))}"
            )

        typed_payload = cast(Mapping[str, object], payload)
        timezone_name = _required_payload_text(typed_payload["timezone"], "timezone")
        source = _required_payload_text(typed_payload["source"], "source")
        revision_id = _required_payload_text(typed_payload["revision_id"], "revision_id")
        return cls(
            observed_at=_parse_datetime(typed_payload["observed_at"], "observed_at"),
            announced_at=_parse_datetime(typed_payload["announced_at"], "announced_at"),
            first_available_at=_parse_datetime(
                typed_payload["first_available_at"], "first_available_at"
            ),
            signal_at=_parse_datetime(typed_payload["signal_at"], "signal_at"),
            execution_at=_parse_datetime(typed_payload["execution_at"], "execution_at"),
            retrieved_at=_parse_datetime(typed_payload["retrieved_at"], "retrieved_at"),
            timezone=timezone_name,
            source=source,
            revision_id=revision_id,
        )

    @classmethod
    def from_json(cls, payload: str) -> PointInTimeAvailability:
        """Parse canonical JSON into the validated immutable model."""

        if type(payload) is not str:
            raise DomainValidationError("point-in-time JSON must be a string")
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as error:
            raise DomainValidationError("point-in-time JSON is invalid") from error
        if type(decoded) is not dict:
            raise DomainValidationError("point-in-time JSON must contain an object")
        return cls.from_dict(cast(dict[str, object], decoded))


@dataclass(frozen=True, slots=True)
class AShareSessionSchedule:
    """Explicit daily continuous-trading windows for an A-share session.

    The 09:30 default is a conservative continuous-session execution point,
    not a claim about a 09:25 auction print, queue position, or actual fill.
    """

    session_open: time = time(9, 30)
    morning_close: time = time(11, 30)
    afternoon_open: time = time(13, 0)
    session_close: time = time(15, 0)
    timezone: str = "Asia/Shanghai"

    def __post_init__(self) -> None:
        timezone_name, _ = _validated_timezone(self.timezone)
        if timezone_name != "Asia/Shanghai":
            raise DomainValidationError("A-share schedule timezone must be Asia/Shanghai")
        object.__setattr__(self, "timezone", timezone_name)
        fields = (
            ("session_open", self.session_open),
            ("morning_close", self.morning_close),
            ("afternoon_open", self.afternoon_open),
            ("session_close", self.session_close),
        )
        for field_name, value in fields:
            if type(value) is not time or value.tzinfo is not None:
                raise DomainValidationError(f"{field_name} must be a timezone-naive time")
        if not (self.session_open < self.morning_close < self.afternoon_open < self.session_close):
            raise DomainValidationError("A-share session times must be strictly increasing")


def earliest_tradable_at(
    available_at: datetime,
    *,
    sessions: Sequence[date],
    schedule: AShareSessionSchedule | None = None,
) -> datetime:
    """Map availability to the earliest continuous-trading instant.

    Session dates are injected from a versioned exchange calendar.  The
    function never infers weekdays, holidays, exceptional closures, auction
    prints, order acceptance, or an actual fill.
    """

    active_schedule = schedule or AShareSessionSchedule()
    _, zone = _validated_timezone(active_schedule.timezone)
    _require_datetime_in_zone(
        available_at,
        "available_at",
        zone,
        active_schedule.timezone,
    )
    validated_sessions = _validated_sessions(sessions)
    local_available = available_at.astimezone(zone)
    session_index = bisect_left(validated_sessions, local_available.date())

    if (
        session_index == len(validated_sessions)
        or validated_sessions[session_index] != local_available.date()
    ):
        return _session_open_at_or_fail(
            validated_sessions,
            session_index,
            active_schedule,
            zone,
            local_available.date(),
        )

    local_time = local_available.timetz().replace(tzinfo=None)
    if local_time < active_schedule.session_open:
        return _combine(
            local_available.date(),
            active_schedule.session_open,
            zone,
        )
    if active_schedule.session_open <= local_time < active_schedule.morning_close:
        return local_available
    if active_schedule.morning_close <= local_time < active_schedule.afternoon_open:
        return _combine(
            local_available.date(),
            active_schedule.afternoon_open,
            zone,
        )
    if active_schedule.afternoon_open <= local_time < active_schedule.session_close:
        return local_available

    return _session_open_at_or_fail(
        validated_sessions,
        session_index + 1,
        active_schedule,
        zone,
        local_available.date(),
    )


def _session_open_at_or_fail(
    sessions: tuple[date, ...],
    index: int,
    schedule: AShareSessionSchedule,
    zone: ZoneInfo,
    reference_date: date,
) -> datetime:
    if index >= len(sessions):
        raise DomainValidationError(f"no trading session at or after {reference_date.isoformat()}")
    return _combine(sessions[index], schedule.session_open, zone)


def _combine(day: date, clock: time, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, clock, tzinfo=zone)


def _validated_sessions(values: object) -> tuple[date, ...]:
    if not isinstance(values, Sequence) or isinstance(values, str | bytes):
        raise DomainValidationError("sessions must be a sequence of dates")
    raw_sessions = tuple(cast(Sequence[object], values))
    if not raw_sessions:
        raise DomainValidationError("sessions must be non-empty")
    if any(type(session) is not date for session in raw_sessions):
        raise DomainValidationError("sessions must contain only dates")
    sessions = tuple(cast(date, session) for session in raw_sessions)
    if any(current >= following for current, following in pairwise(sessions)):
        raise DomainValidationError("sessions must be strictly increasing")
    return sessions


def _validated_timezone(value: object) -> tuple[str, ZoneInfo]:
    timezone_name = _required_text(value, "timezone")
    try:
        return timezone_name, ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise DomainValidationError("timezone must be a valid IANA timezone") from error


def _require_datetime_in_zone(
    value: datetime,
    field_name: str,
    zone: ZoneInfo,
    timezone_name: str,
) -> None:
    require_aware(value, field_name)
    if value.utcoffset() != value.astimezone(zone).utcoffset():
        raise DomainValidationError(f"{field_name} must use timezone {timezone_name}")


def _required_text(value: object, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise DomainValidationError(f"{field_name} must be a non-empty string")
    return value.strip()


def _required_payload_text(value: object, field_name: str) -> str:
    return _required_text(value, field_name)


def _serialize_datetime(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_datetime(value: object, field_name: str) -> datetime | None:
    if value is None:
        return None
    if type(value) is not str:
        raise DomainValidationError(f"{field_name} must be an ISO 8601 string or null")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise DomainValidationError(f"{field_name} must be a valid ISO 8601 datetime") from error
    return parsed
