"""Snapshot-backed trading calendar with no weekday or holiday inference."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date

from ashare_lab.domain.shared import DomainValidationError


class NoNextSessionError(DomainValidationError):
    """Raised when a calendar snapshot contains no later trading session."""


@dataclass(frozen=True, slots=True)
class TradingCalendar:
    """An immutable, versioned, strictly ordered set of exchange sessions.

    ``next_session`` performs a lookup only in ``sessions``.  A missing weekday
    remains missing, so weekends, exchange holidays, and exceptional closures
    can never be guessed by generic date arithmetic.
    """

    version: str
    sessions: tuple[date, ...]

    def __post_init__(self) -> None:
        if type(self.version) is not str or not self.version.strip():
            raise DomainValidationError("calendar version must be non-empty")
        if type(self.sessions) is not tuple or not self.sessions:
            raise DomainValidationError("sessions must be a non-empty tuple")
        if any(type(session) is not date for session in self.sessions):
            raise DomainValidationError("every session must be a date")
        if any(
            current >= following
            for current, following in zip(self.sessions, self.sessions[1:], strict=False)
        ):
            raise DomainValidationError("sessions must be unique and in strictly increasing order")

    def is_session(self, day: date) -> bool:
        """Return whether ``day`` is explicitly present in this snapshot."""

        self._require_date(day)
        index = bisect_right(self.sessions, day)
        return index > 0 and self.sessions[index - 1] == day

    def next_session(self, after: date) -> date:
        """Return the first explicit session strictly later than ``after``."""

        self._require_date(after)
        index = bisect_right(self.sessions, after)
        if index == len(self.sessions):
            raise NoNextSessionError(
                f"calendar {self.version!r} has no session after {after.isoformat()}"
            )
        return self.sessions[index]

    @staticmethod
    def _require_date(value: date) -> None:
        if type(value) is not date:
            raise DomainValidationError("calendar lookup value must be a date")
