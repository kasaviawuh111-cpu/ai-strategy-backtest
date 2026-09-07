"""Log-safe correlation shared by request handlers and model adapters."""

from contextvars import ContextVar

# Bound only to the validated/generated HTTP ID, never user input or model text.
# Tasks inherit context, and concurrent requests keep independent values.
request_id: ContextVar[str] = ContextVar("request_id", default="-")
candidate_attempt: ContextVar[int] = ContextVar("candidate_attempt", default=0)


def current_request_id() -> str:
    """Return the active HTTP correlation ID, or '-' outside a request."""
    return request_id.get()


def current_candidate_attempt() -> int:
    """Return the bounded candidate attempt; zero denotes other model calls."""
    return candidate_attempt.get()
