"""Two independently owned plan legs, sharing one account configuration.

StrategySpec can preserve this contract through stock binding and persistence.
Execution requires the shared minute, calendar and corporate-action data route.
"""
from decimal import Decimal
from pydantic import BaseModel, ConfigDict, model_validator

from .price_plans import PricePlan, ConditionalPlan, ScheduledPlan


class IndependentPlanPair(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    entry_plan: PricePlan
    exit_plan: PricePlan

    @model_validator(mode="after")
    def single_account_and_side_ownership(self):
        for plan, side in ((self.entry_plan, "buy"), (self.exit_plan, "sell")):
            p = plan.parameters
            if isinstance(plan, ConditionalPlan) and any(rule.side != side for rule in p.rules):
                raise ValueError("independent plan contains an opposite-side condition")
            if isinstance(plan, ScheduledPlan):
                if p.side != side or p.exit_rules or (side == "sell" and p.buy_on_start):
                    raise ValueError("independent schedule must own only its declared side")
        entry, exit = self.entry_plan.parameters, self.exit_plan.parameters
        # Reuse persisted plan parameters but never silently choose one of two
        # conflicting accounts. Per-order amounts and trigger settings differ.
        shared = {
            "initial_cash_cny": None, "initial_shares": 0, "opening_shares": 0,
            "initial_capital_scope": "total_equity", "min_shares": 0,
            "max_shares": None, "max_position_cny": None,
            "commission_rate": None, "minimum_commission_cny": None,
            "slippage_bps": None, "slippage_cny": 0,
            "stamp_tax_rate": Decimal("0.0005"), "transfer_fee_rate": Decimal("0.00001"),
        }
        for name, default in shared.items():
            if getattr(entry, name, default) != getattr(exit, name, default):
                raise ValueError(f"independent plans disagree on shared account field: {name}")
        return self
