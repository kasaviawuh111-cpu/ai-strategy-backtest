"""Adapt independent plan pairs into a single existing minute ledger."""
from dataclasses import replace
from ashare_lab.domain.strategy.independent_plans import IndependentPlanPair
from ashare_lab.domain.strategy.price_plans import GridPlan, ScheduledPlan, ConditionalPlan
from ashare_lab.application.minute_grid_plan import execute_minute_grid, MinuteGridCapabilityError
from ashare_lab.application.minute_scheduled_plan import schedule_orders, execute_minute_schedule


def execute_independent_plans(pair: IndependentPlanPair, *, prepared, exchange,
                              start, end, corporate_actions=None, price_rebases=(),
                              execution_config=None):
    if all(isinstance(plan, (GridPlan, ConditionalPlan)) for plan in (pair.entry_plan, pair.exit_plan)):
        return _execute_trigger_pair(pair, prepared=prepared, exchange=exchange,
            corporate_actions=corporate_actions, price_rebases=price_rebases,
            execution_config=execution_config)
    if (isinstance(pair.entry_plan, ConditionalPlan) and isinstance(pair.exit_plan, ScheduledPlan)
            or isinstance(pair.exit_plan, ConditionalPlan) and isinstance(pair.entry_plan, ScheduledPlan)):
        condition = pair.entry_plan if isinstance(pair.entry_plan, ConditionalPlan) else pair.exit_plan
        schedule = pair.exit_plan if condition is pair.entry_plan else pair.entry_plan
        from ashare_lab.application.minute_conditional_orders import execute_minute_conditions
        orders = schedule_orders(schedule.parameters, market_sessions=prepared.market_sessions,
                                 start=start, end=end)
        if condition.parameters.observation == "daily_close":
            return _execute_trigger_pair(pair, prepared=prepared, exchange=exchange,
                corporate_actions=corporate_actions, price_rebases=price_rebases,
                execution_config=execution_config, scheduled_orders=orders)
        return execute_minute_conditions(condition.parameters, prepared=prepared, exchange=exchange,
            scheduled_orders=orders, corporate_actions=corporate_actions,
            price_rebases=price_rebases, execution_config=execution_config)
    if isinstance(pair.entry_plan, ScheduledPlan) and isinstance(pair.exit_plan, ScheduledPlan):
        exits = tuple(replace(order, order_id=f"exit-leg:{order.order_id}") for order in
            schedule_orders(pair.exit_plan.parameters, market_sessions=prepared.market_sessions,
                            start=start, end=end))
        return execute_minute_schedule(pair.entry_plan.parameters, prepared=prepared,
            exchange=exchange, start=start, end=end, additional_orders=exits,
            corporate_actions=corporate_actions, price_rebases=price_rebases,
            execution_config=execution_config)
    grid, scheduled, side = (
        (pair.entry_plan, pair.exit_plan, "buy") if isinstance(pair.entry_plan, GridPlan)
        else (pair.exit_plan, pair.entry_plan, "sell")
    )
    if not isinstance(grid, GridPlan) or not isinstance(scheduled, ScheduledPlan):
        raise MinuteGridCapabilityError("independent_plan_pair_adapter_not_connected")
    orders = schedule_orders(scheduled.parameters, market_sessions=prepared.market_sessions,
                             start=start, end=end)
    if grid.parameters.observation == 'daily_close':
        return _execute_trigger_pair(pair, prepared=prepared, exchange=exchange,
            corporate_actions=corporate_actions, price_rebases=price_rebases,
            execution_config=execution_config, scheduled_orders=orders)
    # The grid adapter alone initializes shared cash/holdings/initial build.
    # Schedule parameters contribute orders, never a second portfolio.
    return execute_minute_grid(grid.parameters, prepared=prepared, exchange=exchange,
        corporate_actions=corporate_actions, price_rebases=price_rebases,
        execution_config=execution_config, active_sides=(side,), scheduled_orders=orders)


def _execute_trigger_pair(pair, *, prepared, exchange, corporate_actions, price_rebases, execution_config,
                          scheduled_orders=()):
    from datetime import time
    from zoneinfo import ZoneInfo
    from ashare_lab.application.minute_replay_input import replay_start
    from ashare_lab.application.opening_portfolio import opening_portfolio
    from ashare_lab.application.minute_grid_plan import LazyFixedGridOrders, _resolve_parameters
    from ashare_lab.application.trigger_grid_orders import TriggerGridOrders
    from ashare_lab.application.minute_conditional_orders import MinuteConditionalOrders
    from ashare_lab.application.independent_order_routing import IndependentOrderRouting
    from ashare_lab.application.minute_grid_replay import replay_grid
    from ashare_lab.application.scheduled_execution import ScheduledOrder
    from ashare_lab.domain.execution.fees import FeeCalculator, FeePolicy
    from ashare_lab.domain.shared import Money
    first_day, first_price, first_at = replay_start(prepared)
    p = (pair.exit_plan if isinstance(pair.entry_plan, ScheduledPlan) else pair.entry_plan).parameters
    fees = FeeCalculator(FeePolicy(exchange, p.commission_rate, Money(p.minimum_commission_cny)))
    observations = {leg: getattr(plan.parameters, 'observation', 'minute_bar') for leg, plan in
                    (("entry", pair.entry_plan), ("exit", pair.exit_plan))}
    if 'daily_close' in observations.values():
        observed_days = {bar.session.session_date for bar in prepared.bars}
        for clock in (time(9, 31), time(15)):
            covered = {bar.session.session_date for bar in prepared.bars
                       if bar.ended_at.astimezone(ZoneInfo('Asia/Shanghai')).time() == clock}
            if covered != observed_days:
                raise MinuteGridCapabilityError('independent_daily_open_or_close_missing')

    def policy(plan, side):
        if isinstance(plan, ScheduledPlan):
            from ashare_lab.application.fixed_grid_orders import FixedGridOrders
            return FixedGridOrders([])
        if isinstance(plan, GridPlan) and plan.parameters.observation != 'minute_bar':
            from ashare_lab.application.daily_grid_orders import DailyGridOrders
            return DailyGridOrders(plan.parameters, first_open=first_price, side=side)
        if isinstance(plan, GridPlan):
            params = plan.parameters.model_copy(update={'initial_shares': 0})
            return (TriggerGridOrders(_resolve_parameters(params, first_price), fees, active_sides=(side,))
                if params.anchor_update == 'last_trigger' else
                LazyFixedGridOrders(params, first_open=first_price, active_sides=(side,)))
        return MinuteConditionalOrders(plan.parameters.rules, plan.parameters.repeat_cycles,
            market_sessions=prepared.market_sessions, minimum_shares=p.min_shares,
            external_entries=side == 'sell')

    router = IndependentOrderRouting(entry=policy(pair.entry_plan, 'buy'), exit=policy(pair.exit_plan, 'sell'),
                                     minimum_shares=p.min_shares, observations=observations)
    portfolio, equity = opening_portfolio(p, prepared, first_price=first_price)
    initial = (ScheduledOrder('pair-initial-build', first_day, first_at, quantity=p.initial_shares),) if p.initial_shares else ()
    return replay_grid(router, list(prepared.bars), portfolio, fees, scheduled_orders=(*initial, *scheduled_orders),
        initial_equity_cny=equity, market_sessions=prepared.market_sessions,
        minimum_shares=p.min_shares, maximum_shares=p.max_shares, maximum_position_cny=p.max_position_cny,
        slippage_bps=p.slippage_bps, slippage_cny=p.slippage_cny,
        corporate_actions=corporate_actions, price_rebases=price_rebases,
        nontrading_closes=getattr(prepared, 'nontrading_closes', ()),
        **(dict(capacity_mode=execution_config.capacity_mode, participation_rate=execution_config.participation_rate,
                limit_handling=execution_config.limit_handling) if execution_config is not None else {}))
