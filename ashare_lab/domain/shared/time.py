"""Time validation helpers for deterministic domain events."""

from datetime import datetime

from .errors import DomainValidationError


def require_aware(value: datetime, field_name: str) -> datetime:
    """Return ``value`` after rejecting naive or invalid datetimes."""

    if type(value) is not datetime:
        raise DomainValidationError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise DomainValidationError(f"{field_name} must be timezone-aware")
    return value
