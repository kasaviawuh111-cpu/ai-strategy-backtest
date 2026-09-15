"""Route independent legs through existing signal and inventory adapters."""
from ashare_lab.application.minute_grid_plan import MinuteGridCapabilityError
from ashare_lab.domain.strategy import (
    HoldingPeriodExit, MinuteProtectionExit, PositionReturnExit, TrailingDrawdownExit,
)
from ashare_lab.domain.strategy.price_plans import ConditionalPlan, ScheduledPlan, GridPlan


def validate_composed_route(strategy):
    """Reject ambiguous ownership or unwired clocks before any data/fills.

    Each side has one owner: the signal tree or a plan. No side is silently
    removed from the submitted strategy. More plan families will use this same
    boundary as their state-sharing adapters are connected.
    """
    plan = strategy.trading_plan
    if strategy.independent_plans is not None:
        if plan is not None or strategy.entry is not None or strategy.exit is not None:
            raise MinuteGridCapabilityError('composed_leg_ownership_conflict')
        for leg in (strategy.independent_plans.entry_plan, strategy.independent_plans.exit_plan):
            if isinstance(leg, GridPlan):
                p = leg.parameters
                if p.observation == 'minute_bar' and p.anchor_update == 'last_fill':
                    raise MinuteGridCapabilityError('moving_anchor_execution_not_connected')
                if p.observation == 'daily_close' and p.anchor_update == 'last_trigger':
                    raise MinuteGridCapabilityError('trigger_grid_requires_minute_execution')
        return
    risk_rules = tuple(rule for rule in strategy.exit.children if isinstance(rule, (
        PositionReturnExit, TrailingDrawdownExit,
    ))) if strategy.exit else ()
    if risk_rules and strategy.exit.op != "first_of":
        raise MinuteGridCapabilityError("composed_position_exit_ordering_not_connected")
    protections = tuple(rule for rule in strategy.exit.children
                        if isinstance(rule, MinuteProtectionExit)) if strategy.exit else ()
    holding = tuple(rule for rule in strategy.exit.children
                    if isinstance(rule, HoldingPeriodExit)) if strategy.exit else ()
    if holding and (len(holding) > 1 or strategy.exit.op != "first_of"):
        raise MinuteGridCapabilityError("composed_holding_ordering_not_connected")
    if protections and (len(protections) > 1 or strategy.exit.op != "first_of"):
        raise MinuteGridCapabilityError("composed_protection_ordering_not_connected")
    if isinstance(plan, ScheduledPlan):
        if plan.parameters.side == "buy":
            valid = strategy.entry is None and strategy.exit is not None
        else:
            valid = strategy.entry is not None and strategy.exit is None
        if not valid:
            raise MinuteGridCapabilityError("composed_leg_ownership_conflict")
        return
    if isinstance(plan, ConditionalPlan):
        if plan.parameters.observation not in {"minute_bar", "daily_close"}:
            raise MinuteGridCapabilityError("composed_daily_price_plan_not_connected")
        sides = {rule.side for rule in plan.parameters.rules}
        if (strategy.entry is not None and "buy" in sides
                or strategy.exit is not None and "sell" in sides
                or strategy.entry is None and "buy" not in sides
                or strategy.exit is None and "sell" not in sides):
            raise MinuteGridCapabilityError("composed_leg_ownership_conflict")
        return
    if isinstance(plan, GridPlan):
        if plan.parameters.observation not in {"minute_bar", "daily_close"}:
            raise MinuteGridCapabilityError("composed_daily_grid_not_connected")
        if (strategy.entry is None) == (strategy.exit is None):
            raise MinuteGridCapabilityError("composed_leg_ownership_conflict")
        return
    raise MinuteGridCapabilityError("composed_grid_execution_not_connected")


def composed_protection(strategy):
    """Reuse the existing minute protection clock and weighted-cost anchor."""
    from ashare_lab.application.minute_grid_replay import Protection
    rules = tuple(rule for rule in strategy.exit.children
                  if isinstance(rule, MinuteProtectionExit)) if strategy.exit else ()
    if not rules:
        return None
    rule, = rules
    return Protection(
        take_profit=rule.take_profit_pct / 100 if rule.take_profit_pct is not None else None,
        stop_loss=rule.stop_loss_pct / 100 if rule.stop_loss_pct is not None else None,
        trailing_drawdown=rule.trailing_drawdown_pct / 100 if rule.trailing_drawdown_pct is not None else None,
        limit_price=rule.limit_price_cny)


def composed_holding_sessions(strategy):
    rules = tuple(rule for rule in strategy.exit.children
                  if isinstance(rule, HoldingPeriodExit)) if strategy.exit else ()
    return rules[0].sessions if rules else None


def composed_daily_position_risk(strategy, history, market_sessions):
    from datetime import datetime, time
    from zoneinfo import ZoneInfo
    from ashare_lab.application.daily_signal_execution import DailyPositionRiskObserver
    from ashare_lab.domain.market_data import DailyBar
    from ashare_lab.domain.shared import InstrumentId, Price, Quantity
    rules = tuple(rule for rule in strategy.exit.children
                  if isinstance(rule, (PositionReturnExit, TrailingDrawdownExit))) if strategy.exit else ()
    if not rules:
        return None
    bars = tuple(DailyBar(InstrumentId(history.instrument_id), row.session_date,
        *(Price(getattr(row, f"raw_{name}")) for name in ("open", "high", "low", "close")),
        Quantity(row.volume), row.amount,
        datetime.combine(row.session_date, time(15), ZoneInfo("Asia/Shanghai")))
        for row in history.rows)
    return DailyPositionRiskObserver(rules=rules, bars=bars, market_sessions=market_sessions)
