from __future__ import annotations

import pytest

from ashare_lab.domain.events.release_scope import (
    RELEASE_EVENT_CODES,
    require_release_event_code,
)


def test_release_scope_contains_only_repurchase_shareholder_changes_and_earnings() -> None:
    assert (
        frozenset(
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
        == RELEASE_EVENT_CODES
    )


@pytest.mark.parametrize(
    "event_code",
    [
        "event.market_activity.dragon_tiger_buy",
        "event.dividends_corporate_actions.cash_dividend_proposal",
        "event.trading_status.suspension",
        "event.regulation_risk.administrative_penalty",
        "event.financial_results.annual_report",
    ],
)
def test_out_of_scope_events_fail_closed(event_code: str) -> None:
    with pytest.raises(ValueError, match="capability_unavailable"):
        require_release_event_code(event_code)
