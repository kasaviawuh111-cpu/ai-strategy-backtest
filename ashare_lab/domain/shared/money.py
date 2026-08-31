"""Immutable decimal money and price values."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from .errors import CurrencyMismatchError, DomainValidationError

_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")


def _require_decimal(value: object, field_name: str) -> None:
    if not isinstance(value, Decimal):
        raise DomainValidationError(f"{field_name} must be Decimal; binary floats are not accepted")
    if not value.is_finite():
        raise DomainValidationError(f"{field_name} must be finite")


@dataclass(frozen=True, slots=True)
class Money:
    """An exact amount in one ISO-style three-letter currency."""

    amount: Decimal
    currency: str = "CNY"

    def __post_init__(self) -> None:
        _require_decimal(self.amount, "money amount")
        if type(self.currency) is not str or not _CURRENCY_PATTERN.fullmatch(self.currency):
            raise DomainValidationError("currency must be a three-letter uppercase code")

    @classmethod
    def zero(cls, currency: str = "CNY") -> Money:
        return cls(Decimal("0"), currency)

    def _require_same_currency(self, other: Money) -> None:
        if self.currency != other.currency:
            raise CurrencyMismatchError(f"cannot mix {self.currency} and {other.currency}")

    def __add__(self, other: object) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: object) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return Money(self.amount - other.amount, self.currency)

    def __mul__(self, multiplier: object) -> Money:
        if isinstance(multiplier, bool) or not isinstance(multiplier, (Decimal, int)):
            return NotImplemented
        return Money(self.amount * Decimal(multiplier), self.currency)

    def __rmul__(self, multiplier: object) -> Money:
        return self * multiplier


@dataclass(frozen=True, slots=True)
class Price(Money):
    """A strictly positive monetary price."""

    def __post_init__(self) -> None:
        Money.__post_init__(self)
        if self.amount <= 0:
            raise DomainValidationError("price must be greater than zero")
