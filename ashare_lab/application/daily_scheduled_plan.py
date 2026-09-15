"""Known-before-open calendar orders using daily O/C, not synthetic minutes."""
from datetime import datetime, time
from dataclasses import asdict, replace
from decimal import Decimal
from hashlib import sha256
import json
from zoneinfo import ZoneInfo

from ashare_lab.application.fixed_grid_orders import CellOrder
from ashare_lab.application.minute_grid_replay import GridReplayEvent, GridReplayResult, GridEquityPoint
from ashare_lab.application.minute_scheduled_plan import schedule_orders
from ashare_lab.application.minute_replay_input import MinuteReplayDataError
from ashare_lab.application.scheduled_execution import ScheduledOrder, execute_scheduled
from ashare_lab.application.backtest_submission import BacktestRunConfig
from ashare_lab.application.skill_backtest import _execution_capacity
from ashare_lab.domain.execution import CapacityMode, LimitHandling
from ashare_lab.domain.execution.bar_prices import BarPrices
from ashare_lab.domain.execution.fees import AshareExchange, FeeCalculator, FeePolicy
from ashare_lab.domain.market_data import InstrumentSession, standard_buy_quantity_rule
from ashare_lab.domain.orders import OrderSide
from ashare_lab.domain.portfolio import PortfolioState, apply_buy
from ashare_lab.domain.shared import InstrumentId, Money, Price


def execute_sourced_daily_schedule(strategy, history, *, calendar_input, corporate=None, config=None):
    """Reuse daily execution and provenance without enabling minute acquisition."""
    dates, calendar, calendar_bytes = calendar_input
    start, end = strategy.backtest.start, strategy.backtest.end
    control_start = start
    if config is not None and config.capacity_mode is CapacityMode.POINT_IN_TIME_VOLUME:
        first_index = next((i for i, day in enumerate(dates) if start <= day <= end), None)
        if first_index is not None and first_index > 0:
            control_start = dates[first_index - 1]
    controls = [asdict(row) for row in history.rows if control_start <= row.session_date <= end]
    controls_json = json.dumps(controls, default=str, sort_keys=True, separators=(",", ":"))
    source_id = "mx-daily:" + sha256(controls_json.encode()).hexdigest()
    action_options = dict(corporate_actions=corporate.applier, price_rebases=corporate.price_rebases) if corporate else {}
    result = execute_daily_schedule(strategy.trading_plan.parameters, history=history, market_sessions=dates,
        exchange=AshareExchange(history.instrument_id.rsplit(".", 1)[1]), start=start, end=end, config=config, **action_options)
    return result, source_id, (), dict(
        executionPeriod="daily",
        corporateActions=corporate.evidence if corporate else dict(status="not_connected"),
        calendar=dict(provider=calendar["provider"], sourceSha256=calendar["sourceSha256"],
                      fileSha256=sha256(calendar_bytes).hexdigest(), sessions=calendar["sessions"]),
        dailyControls=dict(provider=history.provider, instrumentId=history.instrument_id,
            retrievedAt=history.retrieved_at, cacheStatus=history.cache_status or "unknown",
            sha256=source_id.split(":", 1)[1], rows=controls))


def execute_daily_schedule(params, *, history, market_sessions, exchange, start, end,
                           corporate_actions=None, price_rebases=(), config=None):
    config = config or BacktestRunConfig(capacity_mode=CapacityMode.UNLIMITED)
    zone = ZoneInfo("Asia/Shanghai")
    days = [day for day in market_sessions if start <= day <= end]
    rows = {row.session_date: row for row in history.rows}
    if not days or any(day not in rows for day in days):
        raise MinuteReplayDataError("daily_schedule_session_data_missing")
    if market_sessions[-1] <= days[-1]:
        raise MinuteReplayDataError("daily_schedule_next_market_session_missing")
    orders = schedule_orders(params, market_sessions=market_sessions, start=start, end=end)
    if params.initial_shares:
        orders = (ScheduledOrder("schedule-initial-build", days[0], datetime.combine(start, time(), zone),
                                  quantity=params.initial_shares), *orders)
    portfolio = PortfolioState(Money(params.initial_cash_cny))
    benchmark = portfolio
    benchmark_entered = False
    instrument = InstrumentId(history.instrument_id)
    rebases = {item.ex_date: item for item in price_rebases}
    if len(rebases) != len(price_rebases) or any(item.instrument_id != instrument for item in price_rebases):
        raise MinuteReplayDataError("invalid_daily_price_rebases")
    if price_rebases and corporate_actions is None:
        raise MinuteReplayDataError("corporate_action_source_missing")
    if corporate_actions is not None:
        if any(action.instrument_id != instrument for action in corporate_actions.actions):
            raise MinuteReplayDataError("corporate_action_instrument_mismatch")
        action_ex_dates = {action.ex_date for action in corporate_actions.actions}
        if any(day not in action_ex_dates for day in rebases):
            raise MinuteReplayDataError("corporate_action_rebase_event_missing")
        if any(start <= action.ex_date <= end and action.instrument_id == instrument and action.ex_date not in rebases
               for action in corporate_actions.actions):
            raise MinuteReplayDataError("daily_corporate_action_price_rebase_missing")
    minimum, increment = standard_buy_quantity_rule(history.board)
    fees = FeeCalculator(FeePolicy(exchange, params.commission_rate, Money(params.minimum_commission_cny)))
    events, equity = [], []
    closed = 0
    for index, day in enumerate(days):
        row = rows[day]
        calendar_index = market_sessions.index(day)
        previous = rows.get(market_sessions[calendar_index - 1]) if calendar_index else None
        used_capacity = 0
        session = InstrumentSession(instrument, day, history.board, row.trading_status, Price(row.raw_preclose),
                                    Price(row.upper_limit) if row.upper_limit is not None else None,
                                    Price(row.lower_limit) if row.lower_limit is not None else None,
                                    minimum, increment, is_st=row.is_st)
        prices = BarPrices(row.raw_open, row.raw_high, row.raw_low, row.raw_close)
        next_day = market_sessions[market_sessions.index(day) + 1]
        opening, closing = datetime.combine(day, time(9, 30), zone), datetime.combine(day, time(15), zone)
        if corporate_actions is not None:
            rebase = rebases.get(day)
            if rebase is not None:
                multiplier = Decimal(1)
                for action in corporate_actions.actions:
                    if action.instrument_id != instrument or action.ex_date != day:
                        continue
                    if action.available_at is None or action.available_at > opening:
                        raise MinuteReplayDataError("corporate_action_not_known_before_open")
                    if action.share_multiplier is not None:
                        multiplier *= action.share_multiplier
                def rebased_cost(state):
                    lots = []
                    for lot in state.lots:
                        if lot.instrument_id != instrument:
                            lots.append(lot)
                        elif lot.acquisition_principal is None:
                            raise MinuteReplayDataError("corporate_action_cost_provenance_missing")
                        else:
                            lots.append(replace(lot, acquisition_principal=Money(lot.acquisition_principal.amount * rebase.factor * multiplier)))
                    return replace(state, lots=tuple(lots))
                portfolio, benchmark = rebased_cost(portfolio), rebased_cost(benchmark)
                orders = tuple(replace(order, limit_price=rebase.price(order.limit_price, order.side.value, session.price_tick))
                               if order.session_date >= day else order for order in orders)
            portfolio = corporate_actions.apply_before_session(portfolio=portfolio, instrument_id=instrument,
                session=session, as_of=opening).portfolio
            benchmark = corporate_actions.apply_before_session(portfolio=benchmark, instrument_id=instrument,
                session=session, as_of=opening).portfolio
        # Stable sorting keeps initial build first among opening plans.
        for order in sorted((o for o in orders if o.session_date == day), key=lambda o: o.at == "close"):
            when = datetime.combine(day, time(15) if order.at == "close" else time(9, 30), zone)
            observed_at = when
            time_quality = "daily_bar_available_at_proxy" if order.at == "close" else "daily_bar_open_proxy"
            intrabar_limit = order.at == "open" and order.limit_price is not None and (
                prices.open > order.limit_price if order.side is OrderSide.BUY
                else prices.open < order.limit_price
            )
            if intrabar_limit:
                # The order starts at open, but only the completed daily range
                # proves a later touch (or no touch). Never backdate that fill.
                observed_at = closing
                time_quality = "daily_bar_available_at_proxy"
            ticket = CellOrder(order.order_id, "scheduled", order.side.value, order.quantity or 0,
                               order.limit_price, index - 1, index)
            events.append(GridReplayEvent(ticket, index, when, "submitted", "scheduled_plan",
                                          signal_at=order.known_at, effective_at=when))
            sizing_details: dict[str, str | int] = {}
            capacity_reason, notional_capacity = _execution_capacity(
                row=row, previous_row=previous, side=order.side.value,
                config=replace(config, limit_handling=LimitHandling.ALLOW_LIMIT_VOLUME))
            available = (max(0, int(notional_capacity / row.raw_open) - used_capacity)
                         if notional_capacity is not None and row.raw_open > 0 else None)
            if capacity_reason:
                updated, fill, reason = portfolio, None, capacity_reason
            else:
                updated, fill, reason = execute_scheduled(order, portfolio=portfolio, prices=prices, session=session,
                    ended_at=observed_at, next_session=next_day, fees=fees, slippage_bps=params.slippage_bps,
                    slippage_cny=params.slippage_cny, sizing_details=sizing_details, max_quantity=available,
                    allow_adverse_limit_volume=config.limit_handling is LimitHandling.ALLOW_LIMIT_VOLUME)
            if fill is not None:
                shares = updated.position_quantity(instrument).value
                if order.side is OrderSide.BUY and shares > params.max_shares:
                    sizing_details.update(currentPositionQuantity=portfolio.position_quantity(instrument).value,
                                          proposedFillQuantity=fill.quantity.value,
                                          projectedPositionQuantity=shares,
                                          maximumPositionQuantity=params.max_shares)
                    fill, reason = None, "maximum_inventory_exceeded"
                elif order.side is OrderSide.BUY and params.max_position_cny is not None and shares * fill.price.amount > params.max_position_cny:
                    fill, reason = None, "maximum_position_value_exceeded"
                elif order.side is OrderSide.SELL and shares < params.min_shares:
                    fill, reason = None, "minimum_inventory_breached"
                else:
                    if (portfolio.position_quantity(instrument).value + portfolio.pending_share_delta(instrument) > 0
                            and shares + updated.pending_share_delta(instrument) == 0):
                        closed += 1
                    portfolio = updated
                    used_capacity += fill.quantity.value
                    if not benchmark_entered and order.side is OrderSide.BUY:
                        benchmark = apply_buy(benchmark, fill, sellable_on=next_day)
                        benchmark_entered = True
            status = "filled" if fill else "cancelled" if reason == "limit_not_reached" else "skipped"
            if fill and reason == "participation_partial_fill":
                status = "partially_filled"
            if reason == "limit_not_reached":
                observed_at = closing
                time_quality = "daily_bar_available_at_proxy"
            events.append(GridReplayEvent(ticket, index, observed_at, status, reason, fill,
                                          signal_at=order.known_at, effective_at=when,
                                          time_quality=time_quality,
                                          sizing_details=sizing_details or None))
            if status == "partially_filled":
                remaining = int(sizing_details["requestedQuantity"]) - fill.quantity.value
                events.append(GridReplayEvent(replace(ticket, quantity=remaining), index, closing,
                    "cancelled", "day_order_expired", signal_at=order.known_at, effective_at=when,
                    time_quality="daily_bar_available_at_proxy", sizing_details={
                        "requestedQuantity": int(sizing_details["requestedQuantity"]),
                        "filledQuantity": fill.quantity.value, "cancelledQuantity": remaining}))
        if corporate_actions is not None:
            portfolio = corporate_actions.apply_after_session(portfolio=portfolio, instrument_id=instrument,
                session=session, as_of=closing).portfolio
            benchmark = corporate_actions.apply_after_session(portfolio=benchmark, instrument_id=instrument,
                session=session, as_of=closing).portfolio
        shares = portfolio.position_quantity(instrument).value
        receivable = portfolio.dividend_receivable(instrument).amount
        pending = portfolio.pending_share_delta(instrument)
        equity.append(GridEquityPoint(datetime.combine(day, time(15), zone), portfolio.cash.amount, shares,
            row.raw_close, portfolio.cash.amount + receivable + (shares + pending) * row.raw_close,
            dividend_receivable=receivable, pending_share_delta=pending,
            benchmark_equity=benchmark.cash.amount + benchmark.dividend_receivable(instrument).amount
            + (benchmark.position_quantity(instrument).value + benchmark.pending_share_delta(instrument)) * row.raw_close
            if benchmark_entered else None))
    return GridReplayResult(portfolio, tuple(events), execution_time_quality="daily_bar_proxy",
                            capacity_assumption=("unlimited_ohlc_research" if config.capacity_mode is CapacityMode.UNLIMITED
                                                 else f"previous_completed_session_volume:{config.participation_rate}"),
                            equity=tuple(equity), benchmark_portfolio=benchmark if benchmark_entered else None,
                            closed_position_cycles=closed, corporate_action_policy=corporate_actions.policy_id if corporate_actions else None,
                            price_rebases=price_rebases)
