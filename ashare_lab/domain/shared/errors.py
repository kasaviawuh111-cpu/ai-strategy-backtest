"""Errors shared by small, dependency-free domain value objects."""


class DomainValidationError(ValueError):
    """Raised when a value cannot exist in the domain model."""


class CurrencyMismatchError(DomainValidationError):
    """Raised when arithmetic mixes values denominated in different currencies."""
