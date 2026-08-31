"""Immutable whole-share quantity value."""

from __future__ import annotations

from dataclasses import dataclass

from .errors import DomainValidationError


@dataclass(frozen=True, slots=True, order=True)
class Quantity:
    """A non-negative whole number of shares."""

    value: int

    def __post_init__(self) -> None:
        if type(self.value) is not int:
            raise DomainValidationError("quantity must be an integer")
        if self.value < 0:
            raise DomainValidationError("quantity cannot be negative")

    @classmethod
    def zero(cls) -> Quantity:
        return cls(0)

    def __add__(self, other: object) -> Quantity:
        if not isinstance(other, Quantity):
            return NotImplemented
        return Quantity(self.value + other.value)

    def __sub__(self, other: object) -> Quantity:
        if not isinstance(other, Quantity):
            return NotImplemented
        if other.value > self.value:
            raise DomainValidationError("quantity subtraction cannot underflow")
        return Quantity(self.value - other.value)

    def __int__(self) -> int:
        return self.value
