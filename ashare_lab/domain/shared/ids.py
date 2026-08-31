"""Strong, immutable identifiers used by domain aggregates and events."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import DomainValidationError

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


@dataclass(frozen=True, slots=True)
class StrongId:
    """A non-empty identifier that does not silently normalize user input."""

    value: str

    def __post_init__(self) -> None:
        if type(self.value) is not str:
            raise DomainValidationError("identifier value must be a string")
        if not _ID_PATTERN.fullmatch(self.value):
            raise DomainValidationError(
                "identifier must be 1-128 characters and contain only "
                "letters, digits, '.', '_', ':', or '-'"
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class OrderId(StrongId):
    """Identity of an order aggregate."""


@dataclass(frozen=True, slots=True)
class OrderEventId(StrongId):
    """Identity of one immutable order-domain event."""


@dataclass(frozen=True, slots=True)
class DecisionId(StrongId):
    """Identity of the strategy decision that created an order."""


@dataclass(frozen=True, slots=True)
class InstrumentId(StrongId):
    """Canonical security identity, for example ``300059.SZ``."""


@dataclass(frozen=True, slots=True)
class FillId(StrongId):
    """Identity of one execution fill, used to reject duplicate application."""


@dataclass(frozen=True, slots=True)
class RunId(StrongId):
    """Identity of a reproducible backtest run."""
