"""Translate calendar plans into existing known-before-open scheduled orders."""
from datetime import datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from ashare_lab.application.fixed_grid_orders import FixedGridOrders
from ashare_lab.application.minute_grid_replay import replay_grid
from ashare_lab.application.scheduled_execution import ScheduledOrder
from ashare_lab.application.trading_schedule import investment_schedule
from ashare_lab.domain.execution.fees import FeeCalculator, FeePolicy
from ashare_lab.domain.orders import OrderSide
from ashare_lab.domain.portfolio import PortfolioState
from ashare_lab.domain.shared import Money


def schedule_orders(params, *, market_sessions, start, end):
    known = datetime.combine(start, time(0), tzinfo=ZoneInfo("Asia/Shanghai"))
    size = params.budget_cny if params.sizing_mode == "amount" else Decimal(params.quantity)
    if params.frequency == "once":
        target = next((day for day in market_sessions if start <= day <= end), None)
        dates = {} if target is None else {target: size}
    else:
        dates = investment_schedule(sessions=market_sessions, start=start, end=end,
                                    frequency=params.frequency, day=params.day, budget=size)
    if params.buy_on_start:
        first = next((day for day in market_sessions if start <= day <= end), None)
        if first is not None:
            dates.setdefault(first, size)
    dates = dict(sorted(dates.items()))
    orders = tuple(ScheduledOrder(f"scheduled:{day.isoformat()}", day, known, side=OrderSide(params.side),
                                   at=params.at, limit_price=params.limit_price,
                                   budget=amount if params.sizing_mode == "amount" else None,
                                   quantity=int(amount) if params.sizing_mode == "shares" else None)
                   for day, amount in dates.items())
    return orders


def execute_minute_schedule(params, *, prepared, exchange, start, end, exit_rules=(),
                            corporate_actions=None, price_rebases=(), execution_config=None,
                            daily_signals=(), protection=None, holding_sessions=None,
                            holding_anchor="each_entry_fill", daily_position_risk=None,
                            additional_orders=()):
    if daily_signals and any(
        intent.enter if params.side == "buy" else intent.exit for intent in daily_signals
    ):
        raise ValueError("calendar and daily signals must own opposite sides")
    known = datetime.combine(start, time(0), tzinfo=ZoneInfo("Asia/Shanghai"))
    orders = schedule_orders(params, market_sessions=prepared.market_sessions, start=start, end=end)
    orders = (*orders, *additional_orders)
    if params.initial_shares:
        orders = (ScheduledOrder("schedule-initial-build", prepared.bars[0].session.session_date,
                                  known, quantity=params.initial_shares), *orders)
    from ashare_lab.application.minute_conditional_orders import MinuteConditionalOrders
    policy = (MinuteConditionalOrders(exit_rules, recurring_exits=True,
        market_sessions=prepared.market_sessions, minimum_shares=params.min_shares)
        if exit_rules else FixedGridOrders([]))
    return replay_grid(policy, list(prepared.bars), PortfolioState(Money(params.initial_cash_cny)),
                       FeeCalculator(FeePolicy(exchange, params.commission_rate, Money(params.minimum_commission_cny))),
                       scheduled_orders=orders, daily_signals=daily_signals,
                       daily_position_risk=daily_position_risk,
                       protection=protection, resume_after_exit=protection is not None,
                       holding_sessions=holding_sessions, holding_anchor=holding_anchor,
                       market_sessions=prepared.market_sessions,
                       slippage_bps=params.slippage_bps, slippage_cny=params.slippage_cny,
                       minimum_shares=params.min_shares, maximum_shares=params.max_shares,
                       maximum_position_cny=params.max_position_cny,
                       corporate_actions=corporate_actions, price_rebases=price_rebases,
                       nontrading_closes=getattr(prepared, "nontrading_closes", ()),
                       preceding_bar=prepared.preceding_bar,
                       **(dict(capacity_mode=execution_config.capacity_mode,
                               participation_rate=execution_config.participation_rate,
                               limit_handling=execution_config.limit_handling)
                          if execution_config is not None else {}))
