"""Narrow event execution scope for the first financial/event release."""

from __future__ import annotations

RELEASE_EVENT_CODES = frozenset(
    {
        "event.financial_results.earnings_forecast_published",
        "event.financial_results.earnings_flash_report",
        "event.repurchase_capital.repurchase_proposal",
        "event.repurchase_capital.repurchase_approved",
        "event.repurchase_capital.repurchase_first_execution",
        "event.repurchase_capital.repurchase_progress",
        "event.repurchase_capital.repurchase_completion",
        "event.repurchase_capital.repurchase_cancellation",
        "event.repurchase_capital.repurchase_change",
        "event.repurchase_capital.repurchase_termination",
        "event.shareholder_holdings.major_holder_increase_plan",
        "event.shareholder_holdings.major_holder_increase_progress",
        "event.shareholder_holdings.major_holder_decrease_plan",
        "event.shareholder_holdings.major_holder_decrease_progress",
        "event.shareholder_holdings.executive_increase",
        "event.shareholder_holdings.executive_decrease",
    }
)


def require_release_event_code(event_code: str) -> str:
    if event_code not in RELEASE_EVENT_CODES:
        raise ValueError(
            f"capability_unavailable: event code {event_code!r} is outside release scope"
        )
    return event_code


__all__ = ["RELEASE_EVENT_CODES", "require_release_event_code"]
