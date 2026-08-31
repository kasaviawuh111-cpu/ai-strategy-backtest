"""Small executable event release used by Strategy DSL v1.

This registry is intentionally narrower than the coverage Catalog.  Adding a
code here is a product capability claim and therefore requires an implemented
point-in-time evaluator and adapter contract.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ExecutableEventDefinition:
    event_code: str
    definition_version: str
    allowed_attributes: tuple[str, ...]


PUBLIC_ANNOUNCEMENT_EVENT_CODES = (
    "event.contracts_orders.contract_changed",
    "event.contracts_orders.contract_completed",
    "event.contracts_orders.contract_progress",
    "event.contracts_orders.contract_terminated",
    "event.contracts_orders.framework_agreement",
    "event.contracts_orders.major_contract_signed",
    "event.contracts_orders.purchase_order_received",
    "event.dividends_corporate_actions.capitalization_issue",
    "event.dividends_corporate_actions.cash_dividend_approved",
    "event.dividends_corporate_actions.cash_dividend_ex_date",
    "event.dividends_corporate_actions.cash_dividend_proposal",
    "event.dividends_corporate_actions.rights_issue",
    "event.dividends_corporate_actions.stock_dividend_ex_date",
    "event.dividends_corporate_actions.stock_dividend_proposal",
    "event.governance_personnel.board_secretary_change",
    "event.governance_personnel.ceo_change",
    "event.governance_personnel.cfo_change",
    "event.governance_personnel.chairman_change",
    "event.governance_personnel.director_resignation",
    "event.governance_personnel.equity_incentive_grant",
    "event.governance_personnel.equity_incentive_plan",
    "event.governance_personnel.supervisor_resignation",
    "event.litigation_credit.arbitration",
    "event.litigation_credit.bankruptcy_reorganization",
    "event.litigation_credit.credit_rating_downgrade",
    "event.litigation_credit.debt_default",
    "event.litigation_credit.litigation_progress",
    "event.litigation_credit.major_litigation",
    "event.m_and_a_restructuring.acquisition_plan",
    "event.m_and_a_restructuring.asset_sale_plan",
    "event.m_and_a_restructuring.restructuring_approved_board",
    "event.m_and_a_restructuring.restructuring_approved_shareholders",
    "event.m_and_a_restructuring.restructuring_completed",
    "event.m_and_a_restructuring.restructuring_regulatory_approval",
    "event.m_and_a_restructuring.restructuring_resumption",
    "event.m_and_a_restructuring.restructuring_suspension",
    "event.m_and_a_restructuring.restructuring_terminated",
    "event.m_and_a_restructuring.spin_off_listing",
    "event.regulation_risk.administrative_penalty",
    "event.regulation_risk.disciplinary_action",
    "event.regulation_risk.information_disclosure_violation",
    "event.regulation_risk.investigation_opened",
    "event.regulation_risk.public_censure",
    "event.repurchase_capital.repurchase_approved",
    "event.repurchase_capital.repurchase_cancellation",
    "event.repurchase_capital.repurchase_change",
    "event.repurchase_capital.repurchase_completion",
    "event.repurchase_capital.repurchase_first_execution",
    "event.repurchase_capital.repurchase_progress",
    "event.repurchase_capital.repurchase_proposal",
    "event.repurchase_capital.repurchase_termination",
    "event.restricted_shares_pledges.restricted_shares_unlock",
    "event.restricted_shares_pledges.unlock_schedule_change",
    "event.shareholder_holdings.actual_controller_change",
    "event.shareholder_holdings.executive_decrease",
    "event.shareholder_holdings.executive_increase",
    "event.shareholder_holdings.major_holder_decrease_plan",
    "event.shareholder_holdings.major_holder_decrease_progress",
    "event.shareholder_holdings.major_holder_increase_plan",
    "event.shareholder_holdings.major_holder_increase_progress",
    "event.shareholder_holdings.ownership_below_five_percent",
    "event.shareholder_holdings.ownership_reaches_five_percent",
)

# These issuer documents have a stable full-document acquisition path in the
# strict A-share event lane.  A text predicate on any other event remains
# fail-closed until its primary document contract is published.
DOCUMENT_TEXT_EVENT_CODES = frozenset(
    {
        "event.financial_results.earnings_forecast_published",
        "event.financial_results.earnings_flash_report",
        "event.financial_results.annual_report",
        "event.financial_results.semiannual_report",
        "event.financial_results.quarterly_report",
    }
)


_DEFINITIONS = (
    ExecutableEventDefinition(
        "event.financial_results.earnings_forecast_published",
        "1.0.0",
        ("forecast_type", "direction", "source"),
    ),
    ExecutableEventDefinition(
        "event.financial_results.earnings_flash_report",
        "1.0.0",
        ("report_type", "stat_date", "source"),
    ),
    ExecutableEventDefinition(
        "event.financial_results.annual_report",
        "1.0.0",
        ("report_type", "stat_date", "source"),
    ),
    ExecutableEventDefinition(
        "event.financial_results.semiannual_report",
        "1.0.0",
        ("report_type", "stat_date", "source"),
    ),
    ExecutableEventDefinition(
        "event.financial_results.quarterly_report",
        "1.0.0",
        ("report_type", "stat_date", "source"),
    ),
    ExecutableEventDefinition(
        "event.contracts_orders.major_contract_won",
        "1.0.0",
        (
            "award_stage",
            "contract_type",
            "counterparty",
            "is_consortium",
            "issuer_role",
            "materiality_basis",
            "materiality_status",
            "project_name",
            "source_kind",
        ),
    ),
    ExecutableEventDefinition(
        "event.macro_policy_industry.license_approval",
        "1.0.0",
        (
            "approval_status",
            "jurisdiction",
            "license_type",
            "product_or_scope",
            "regulator",
            "source_kind",
        ),
    ),
    # These codes are emitted only after the public-announcement adapter has
    # frozen a complete document, passed the second-level availability gate,
    # and matched an explicit lifecycle phrase.  Structured amount/ratio/entity
    # extraction is not implemented yet, so v1 deliberately exposes no filter
    # attributes rather than pretending title classification produced them.
    *(
        ExecutableEventDefinition(event_code, "1.0.0", ())
        for event_code in PUBLIC_ANNOUNCEMENT_EVENT_CODES
    ),
)

EXECUTABLE_EVENT_DEFINITIONS = {item.event_code: item for item in _DEFINITIONS}


def resolve_executable_event(event_code: str) -> ExecutableEventDefinition | None:
    return EXECUTABLE_EVENT_DEFINITIONS.get(event_code)
