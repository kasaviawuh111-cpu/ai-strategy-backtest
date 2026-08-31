"""Order aggregate errors."""

from ashare_lab.domain.shared import DomainValidationError


class OrderInvariantError(DomainValidationError):
    """Raised when an order or order event would violate a domain invariant."""


class InvalidOrderTransition(RuntimeError):
    """Raised when a command is not legal for the order's current status."""
