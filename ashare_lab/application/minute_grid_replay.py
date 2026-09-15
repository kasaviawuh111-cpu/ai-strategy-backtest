"""Fixed-grid bar replay using the existing exact-money portfolio ledger.

This executor consumes already validated raw bars/session rules. Its OHLC
fills are research estimates, not reconstructed exchange executions. Data
admission, corporate-action rebasing and API/report adapters remain upstream.
"""
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from ashare_lab.application.fixed_grid_orders import CellOrder, FixedGridOrders
from ashare_lab.application.trading_schedule import holding_due_session
from ashare_lab.application.scheduled_execution import ScheduledOrder, execute_scheduled
from ashare_lab.application.daily_signal_execution import DailySignalIntent
from ashare_lab.application.corporate_action_timeline import TimelineCorporateActionApplier
from ashare_lab.application.minute_price_rebase import MinutePriceRebase
from ashare_lab.domain.execution.bar_prices import BarPrices, match_bar_price, protective_exit
from ashare_lab.domain.execution.fees import FeeCalculator
from ashare_lab.domain.execution import CapacityMode, LimitHandling
from ashare_lab.domain.market_data import InstrumentSession, TradingStatus
from ashare_lab.domain.orders import OrderSide
from ashare_lab.domain.portfolio import (
    FillRecord, PortfolioState, apply_buy, apply_sell,
    InsufficientCashError, InsufficientSellableQuantityError,
)
from ashare_lab.domain.shared import FillId, OrderId, Price, Quantity, Money, require_aware


@dataclass(frozen=True)
class ReplayBar:
    ended_at: datetime
    prices: BarPrices
    session: InstrumentSession
    next_session: date
    volume_shares: int

    def __post_init__(self) -> None:
        require_aware(self.ended_at, "ended_at")
        if type(self.volume_shares) is not int or self.volume_shares < 0:
            raise ValueError("minute volume must be nonnegative whole shares")
        if self.ended_at.astimezone(ZoneInfo("Asia/Shanghai")).date() != self.session.session_date:
            raise ValueError("bar date differs from security session")
        if self.next_session <= self.session.session_date:
            raise ValueError("next_session must be a later market session")


@dataclass(frozen=True)
class GridReplayEvent:
    order: CellOrder
    bar_index: int
    observed_at: datetime
    status: str
    reason: str
    fill: FillRecord | None = None
    ambiguous_intrabar_order: bool = False
    signal_at: datetime | None = None
    effective_at: datetime | None = None
    time_quality: str | None = None
    sizing_details: dict[str, str | int] | None = None


@dataclass(frozen=True)
class Protection:
    """Fractions of fee-exclusive average acquisition price, e.g. .05 = 5%."""
    take_profit: Decimal | None = None
    stop_loss: Decimal | None = None
    trailing_drawdown: Decimal | None = None
    # None denotes market; a price is a distinct fixed sell limit, not the
    # trigger threshold and never silently converted to a market order.
    limit_price: Decimal | None = None

    def __post_init__(self) -> None:
        for value in (self.take_profit, self.stop_loss, self.trailing_drawdown):
            if value is not None and (not value.is_finite() or value <= 0):
                raise ValueError("protection fractions must be finite and positive")
        if self.stop_loss is not None and self.stop_loss >= 1:
            raise ValueError("stop loss must be below 100 percent")
        if self.trailing_drawdown is not None and self.trailing_drawdown >= 1:
            raise ValueError("trailing drawdown must be below 100 percent")
        if self.limit_price is not None and (not self.limit_price.is_finite() or self.limit_price <= 0):
            raise ValueError("exit limit must be finite and positive")

    def observe(self, portfolio: PortfolioState, bar: ReplayBar) -> tuple[str | None, bool]:
        lots = [lot for lot in portfolio.lots if lot.instrument_id == bar.session.instrument_id]
        if not lots:
            return None, False
        # Initial lots without fill provenance need an explicit raw cost source.
        fills = {fill.fill_id: fill for fill in portfolio.fills}
        if any(lot.acquisition_principal is None and lot.opened_by_fill_id not in fills for lot in lots):
            raise ValueError("protection requires rebased fee-exclusive lot cost provenance")
        quantity = sum(lot.remaining_quantity.value for lot in lots) + portfolio.pending_share_delta(bar.session.instrument_id)
        if quantity <= 0:
            return None, False
        cost = (sum(lot.acquisition_principal.amount if lot.acquisition_principal is not None
                   else fills[lot.opened_by_fill_id].price.amount * lot.remaining_quantity.value for lot in lots)
                + portfolio.pending_share_principal(bar.session.instrument_id)) / quantity
        return protective_exit(
            bar.prices,
            take_profit=cost * (1 + self.take_profit) if self.take_profit is not None else None,
            stop_loss=cost * (1 - self.stop_loss) if self.stop_loss is not None else None,
        )


@dataclass(frozen=True)
class GridEquityPoint:
    observed_at: datetime
    cash: Decimal
    shares: int
    close: Decimal
    equity: Decimal
    dividend_receivable: Decimal = Decimal(0)
    pending_share_delta: int = 0
    benchmark_equity: Decimal | None = None


@dataclass(frozen=True)
class GridReplayResult:
    portfolio: PortfolioState
    events: tuple[GridReplayEvent, ...]
    # Not an exchange timestamp: OHLC cannot establish exact intrabar order.
    execution_time_quality: str = "minute_bar_end_proxy"
    capacity_assumption: str = "unlimited_ohlc_research"
    unfinished_exit_quantity: int = 0
    equity: tuple[GridEquityPoint, ...] = ()
    unfinished_holding_exit_quantity: int = 0
    corporate_action_policy: str | None = None
    price_rebases: tuple[MinutePriceRebase, ...] = ()
    benchmark_portfolio: PortfolioState | None = None
    closed_position_cycles: int = 0
    initial_equity_cny: Decimal | None = None
    grid_policy: str | None = None
    monitoring_events: tuple[dict, ...] = ()




def replay_grid(
    policy: FixedGridOrders, bars: list[ReplayBar], portfolio: PortfolioState,
    fees: FeeCalculator, *, slippage_bps: Decimal = Decimal(5),
    slippage_cny: Decimal = Decimal(0),
    protection: Protection | None = None,
    holding_sessions: int | None = None,
    holding_anchor: str = "each_entry_fill",
    market_sessions: tuple[date, ...] = (),
    scheduled_orders: tuple[ScheduledOrder, ...] = (),
    daily_signals: tuple[DailySignalIntent, ...] = (),
    daily_position_risk=None,
    grid_composition: bool = False,
    resume_after_exit: bool = False,
    minimum_shares: int = 0,
    maximum_shares: int | None = None,
    maximum_position_cny: Decimal | None = None,
    corporate_actions: TimelineCorporateActionApplier | None = None,
    price_rebases: tuple[MinutePriceRebase, ...] = (),
    nontrading_closes: tuple = (),
    initial_equity_cny: Decimal | None = None,
    capacity_mode: CapacityMode = CapacityMode.UNLIMITED,
    participation_rate: Decimal = Decimal("0.05"),
    limit_handling: LimitHandling = LimitHandling.STRICT_NO_FILL_AT_LIMIT,
    preceding_bar: ReplayBar | None = None,
) -> GridReplayResult:
    """Match pre-existing tickets, update ledger, then observe completed bar.

Distinct cells are intentionally processed in declaration order; they never
net. Account validation failures end the affected intent. Untouched limits
remain working until DAY expiry and the next session rebuilds their intent.
"""
    if initial_equity_cny is None:
        if portfolio.lots or portfolio.corporate_action_entitlements:
            raise ValueError("opening portfolio requires sourced initial equity")
        initial_equity_cny = portfolio.cash.amount
    if daily_position_risk is not None and (portfolio.lots or portfolio.corporate_action_entitlements):
        raise ValueError("daily position risk requires a sourced first entry fill, not imported inventory")
    if not initial_equity_cny.is_finite() or initial_equity_cny <= 0:
        raise ValueError("initial equity must be finite and positive")
    if not isinstance(capacity_mode, CapacityMode) or not isinstance(limit_handling, LimitHandling):
        raise ValueError("invalid minute execution policy")
    if not participation_rate.is_finite() or not 0 < participation_rate <= 1:
        raise ValueError("participation rate must be in (0, 1]")
    if not bars and not nontrading_closes:
        raise ValueError("minute replay requires bars or sourced nontrading sessions")
    if minimum_shares < 0 or (maximum_shares is not None and maximum_shares < minimum_shares):
        raise ValueError("invalid inventory bounds")
    if policy.last_observed != -1 or policy.pending:
        raise ValueError("replay requires a fresh grid policy")
    closed_sessions = sorted(nontrading_closes, key=lambda item: item[0].session_date)
    first_session = bars[0].session if bars else closed_sessions[0][0]
    instrument = first_session.instrument_id
    if holding_anchor not in {"each_entry_fill", "first_entry_fill"}:
        raise ValueError("unknown holding-period anchor")
    first_entry_session = min((lot.acquired_on for lot in portfolio.lots
                               if lot.instrument_id == instrument), default=None)
    trading_days = {b.session.session_date for b in bars}
    seen_days = set(trading_days)
    for session, daily in closed_sessions:
        if (session.instrument_id != instrument or daily.instrument_id != instrument
                or session.session_date != daily.session_date or session.session_date in seen_days
                or session.status is TradingStatus.TRADING or daily.volume.value or daily.turnover):
            raise ValueError("invalid nontrading session")
        seen_days.add(session.session_date)
    final_day = max(seen_days)
    daily_by_session = {}
    for intent in daily_signals:
        intent.validate_calendar(market_sessions)
        if intent.instrument_id != instrument:
            raise ValueError("daily signal instrument differs from replay")
        if intent.session_date not in seen_days:
            raise ValueError("daily signal target requires a sourced session")
        target_first = next((b for b in bars if b.session.session_date == intent.session_date), None)
        if target_first is not None and target_first.ended_at.astimezone(ZoneInfo("Asia/Shanghai")).time() != time(9, 31):
            raise ValueError("daily signal target requires the opening minute")
        if intent.session_date in daily_by_session:
            raise ValueError("daily signals must be combined per session")
        daily_by_session[intent.session_date] = intent
    if len({o.order_id for o in scheduled_orders}) != len(scheduled_orders):
        raise ValueError("scheduled order IDs must be unique")
    if (len({intent.intent_id for intent in daily_signals}) != len(daily_signals)
            or {intent.intent_id for intent in daily_signals} & {o.order_id for o in scheduled_orders}):
        raise ValueError("daily signal order IDs must be unique")
    if grid_composition and (len(policy.active_sides) != 1 or any(
        intent.enter and "buy" in policy.active_sides or intent.exit and "sell" in policy.active_sides
        for intent in daily_signals
    )):
        raise ValueError("composed grid and daily signals must own opposite sides")
    if daily_signals and not grid_composition and (policy.cells or (
        any(intent.enter for intent in daily_signals)
        and any(order.side is OrderSide.BUY for order in scheduled_orders)
    )):
        raise ValueError("daily single-position signals cannot mix with inventory plans")
    final_at = (datetime.combine(final_day, time(15), ZoneInfo("Asia/Shanghai"))
                if not bars or final_day > bars[-1].session.session_date else bars[-1].ended_at)
    closed_position_cycles = 0
    # The trailing anchor belongs to the current position cycle. It begins at
    # the actual first-entry fill and thereafter follows raw minute highs.
    protection_peak: Decimal | None = None
    matched_shares: dict[int, int] = {}
    if preceding_bar is not None and (not bars or preceding_bar.ended_at >= bars[0].ended_at
            or preceding_bar.session.instrument_id != instrument):
        raise ValueError("capacity predecessor must be earlier and belong to the same security")
    first_bar_completed_volume_used = False

    def bar_capacity(index: int, *, opening_order: bool = False) -> int | None:
        nonlocal first_bar_completed_volume_used
        if capacity_mode is CapacityMode.UNLIMITED:
            return None
        if index == 0:
            if preceding_bar is not None:
                return int(Decimal(preceding_bar.volume_shares) * participation_rate)
            if opening_order:
                # The order was known before 09:30, but its opening-minute
                # capacity is observable only when that first minute completes.
                # Match at the bar-end proxy instead of treating an unavailable
                # predecessor as zero liquidity and cancelling a valid order.
                first_bar_completed_volume_used = True
                return int(Decimal(bars[index].volume_shares) * participation_rate)
            return 0
        # Known before this bar begins, including across lunch and overnight.
        # We never use the current completed bar's volume to back-fill an order
        # at that bar's opening.
        return max(0, int(Decimal(bars[index - 1].volume_shares) * participation_rate)
                   - matched_shares.get(index, 0))

    def account_after_fill(updated: PortfolioState, index: int) -> PortfolioState:
        nonlocal closed_position_cycles, protection_peak, first_entry_session
        matched_shares[index] = matched_shares.get(index, 0) + updated.fills[-1].quantity.value
        # Count actual inventory transitions, not net order quantities: splits
        # and credited shares change inventory without a buy execution.
        before = portfolio.position_quantity(instrument).value + portfolio.pending_share_delta(instrument)
        after = updated.position_quantity(instrument).value + updated.pending_share_delta(instrument)
        notify_fill = getattr(policy, "after_account_fill", None)
        if notify_fill is not None:
            latest = updated.fills[-1]
            for stale in notify_fill(latest, before=before, after=after, index=index):
                events.append(GridReplayEvent(stale, index, latest.filled_at, "cancelled",
                                              "external_position_cycle_changed"))
        if daily_position_risk is not None:
            daily_position_risk.after_fill(updated.fills[-1], before=before, after=after)
        if before > 0 and after == 0:
            closed_position_cycles += 1
            protection_peak = None
            first_entry_session = None
        elif before == 0 and after > 0:
            latest = updated.fills[-1]
            if latest.side is not OrderSide.BUY or latest.instrument_id != instrument:
                raise ValueError("position cycle must start with a buy fill")
            protection_peak = latest.price.amount
            first_entry_session = latest.filled_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
        return updated
    if any(b.session.instrument_id != instrument for b in bars):
        raise ValueError("minute replay must contain one security")
    if any(a.ended_at >= b.ended_at for a, b in zip(bars, bars[1:])):
        raise ValueError("minute bars must be strictly ordered")
    if holding_sessions is not None:
        # Market calendar, never the security's sparse bars: suspension counts.
        holding_due_session(market_sessions, first_session.session_date, holding_sessions)
        if any(bar.session.session_date not in market_sessions for bar in bars):
            raise ValueError("minute session missing from market calendar")
    rebases = {r.ex_date: r for r in price_rebases}
    if len(rebases) != len(price_rebases) or any(r.instrument_id != instrument for r in price_rebases):
        raise ValueError("rebase dates must be unique and match the replay security")
    if price_rebases and corporate_actions is None:
        raise ValueError("price rebases require pinned corporate actions")
    if corporate_actions is not None:
        session_days = seen_days
        action_ex_dates = {a.ex_date for a in corporate_actions.actions}
        if any(day not in action_ex_dates for day in rebases):
            raise ValueError("price rebase has no matching corporate action")
        for action in corporate_actions.actions:
            if action.instrument_id != instrument:
                raise ValueError("corporate action security differs from replay")
            for day in (action.record_date, action.ex_date):
                if min(session_days) <= day <= max(session_days) and day not in session_days:
                    raise ValueError("corporate action requires missing session boundary")
            if action.ex_date in session_days and action.ex_date not in rebases:
                raise ValueError("corporate action requires source price rebase")
    events = []
    equity = []
    # The counterfactual shares the exact first acquisition and costs, then
    # holds without trading. Corporate entitlements belong to each account's
    # own record-date inventory, never copied from the actively traded one.
    benchmark = portfolio
    benchmark_entered = bool(portfolio.position_quantity(instrument).value)
    observed_fill_count = len(portfolio.fills)
    if len({order.order_id for order in scheduled_orders}) != len(scheduled_orders):
        raise ValueError("scheduled order IDs must be unique")
    scheduled_done: set[str] = set()
    scheduled_active: dict[str, CellOrder] = {}
    exit_order = None
    exit_remaining = 0
    calendar_signals = {}

    def apply_session_open(session, opening):
        nonlocal portfolio, benchmark, protection, protection_peak, exit_order, exit_remaining, scheduled_orders
        if corporate_actions is None:
            return
        rebase = rebases.get(session.session_date)
        if rebase is not None:
            if daily_position_risk is not None:
                daily_position_risk.rebase(rebase.factor)
            multiplier = Decimal(1)
            for action in corporate_actions.actions:
                if action.ex_date == session.session_date:
                    if action.available_at is None or action.available_at > opening:
                        raise ValueError("corporate action terms not available before open")
                    if action.share_multiplier is not None:
                        multiplier *= action.share_multiplier
            fills = {f.fill_id: f for f in portfolio.fills}
            lots = []
            for lot in portfolio.lots:
                if lot.instrument_id != instrument:
                    lots.append(lot)
                    continue
                principal = lot.acquisition_principal
                if principal is None:
                    source_fill = fills.get(lot.opened_by_fill_id)
                    if source_fill is None:
                        # Imported inventory can receive corporate entitlements
                        # without a historical acquisition price. Keep that cost
                        # unknown; cost-based protection checks its own provenance.
                        lots.append(lot)
                        continue
                    principal = Money(source_fill.price.amount * lot.remaining_quantity.value)
                lots.append(replace(lot, acquisition_principal=Money(principal.amount * rebase.factor * multiplier)))
            portfolio = replace(portfolio, lots=tuple(lots))
            policy.rebase_prices(rebase, tick=session.price_tick)
            if protection is not None:
                protection = replace(protection, limit_price=rebase.price(protection.limit_price, "sell", session.price_tick))
                if protection_peak is not None:
                    protection_peak = rebase.price(protection_peak, "sell", session.price_tick)
            if exit_order is not None:
                adjusted_remaining = Decimal(exit_remaining) * multiplier
                if adjusted_remaining != adjusted_remaining.to_integral_value():
                    raise ValueError("exit rebase requires fractional quantity allocation")
                exit_remaining = int(adjusted_remaining)
                exit_order = replace(exit_order, quantity=exit_remaining,
                                     limit_price=rebase.price(exit_order.limit_price, "sell", session.price_tick))
            scheduled_orders = tuple(replace(o, limit_price=rebase.price(o.limit_price, o.side.value, session.price_tick))
                                     if o.session_date >= session.session_date else o for o in scheduled_orders)
        portfolio = corporate_actions.apply_before_session(
            portfolio=portfolio, instrument_id=instrument, session=session, as_of=opening,
        ).portfolio
        benchmark = corporate_actions.apply_before_session(
            portfolio=benchmark, instrument_id=instrument, session=session, as_of=opening,
        ).portfolio

    def process_closed_session(session, daily, index):
        opening = datetime.combine(session.session_date, time(9, 30), ZoneInfo("Asia/Shanghai"))
        closing = opening.replace(hour=15, minute=0)
        apply_session_open(session, opening)
        process_daily_signal(session, index, opening)
        for planned in scheduled_orders:
            if planned.session_date != session.session_date or planned.order_id in scheduled_done:
                continue
            observed = closing if planned.at == "close" else opening
            ticket = CellOrder(planned.order_id, "scheduled", planned.side.value,
                               planned.quantity or 0, planned.limit_price, index - 1, index)
            events.append(GridReplayEvent(ticket, index, observed, "skipped", "security_not_trading",
                signal_at=planned.known_at, effective_at=observed, time_quality="market_session_clock"))
            scheduled_done.add(planned.order_id)
        for order in policy.prepare_session(portfolio, session, index):
            calendar_signals[order.order_id] = opening
            reason, ambiguous = policy.signal_details(order)
            events.append(GridReplayEvent(order, index, opening, "submitted", reason,
                ambiguous_intrabar_order=ambiguous, signal_at=opening, effective_at=opening,
                time_quality="market_session_clock"))
        for order in tuple(policy.pending.values()):
            # These intents are now effective, despite the absence of a bar.
            reason = "security_not_trading"
            retry = policy.retry_failure(order, reason)
            if not retry:
                policy.reject(order)
            signal = calendar_signals.get(order.order_id)
            events.append(GridReplayEvent(order, index, opening,
                "retry_pending" if retry else "rejected", reason,
                signal_at=signal, effective_at=opening, time_quality="market_session_clock"))
        terminal = session.session_date == final_day
        # A calendar boundary expires DAY orders even though the next actual
        # minute retains the same index. Rebuilt intentions resume at that index.
        for order in policy.expire_day(None if terminal else index + 1):
            events.append(GridReplayEvent(order, index, closing, "cancelled",
                "backtest_ended" if terminal else "day_expired",
                signal_at=calendar_signals.get(order.order_id), effective_at=opening,
                time_quality="market_session_clock"))
            rebuilt = policy.pending.get(order.cell_id)
            if rebuilt is not None:
                events.append(GridReplayEvent(rebuilt, index, closing, "submitted",
                    "order_rebuilt_next_session", signal_at=calendar_signals.get(order.order_id),
                    time_quality="market_session_clock"))
            if rebuilt is not None and order.order_id in calendar_signals:
                calendar_signals[rebuilt.order_id] = calendar_signals[order.order_id]
        if not terminal:
            policy.pending = {key: replace(order, effective_bar=index)
                              for key, order in policy.pending.items()}
        apply_session_close(session, closing)
        record_equity(closing, daily.close.amount)

    def apply_session_close(session, closing):
        nonlocal portfolio, benchmark
        if corporate_actions is not None:
            portfolio = corporate_actions.apply_after_session(
                portfolio=portfolio, instrument_id=instrument, session=session, as_of=closing,
            ).portfolio
            benchmark = corporate_actions.apply_after_session(
                portfolio=benchmark, instrument_id=instrument, session=session, as_of=closing,
            ).portfolio

    def record_equity(observed_at, close):
        shares = portfolio.position_quantity(instrument).value
        receivable = portfolio.dividend_receivable(instrument).amount
        pending = portfolio.pending_share_delta(instrument)
        benchmark_value = (benchmark.cash.amount + benchmark.dividend_receivable(instrument).amount
                           + (benchmark.position_quantity(instrument).value
                              + benchmark.pending_share_delta(instrument)) * close) if benchmark_entered else None
        equity.append(GridEquityPoint(observed_at, portfolio.cash.amount, shares, close,
            portfolio.cash.amount + receivable + (shares + pending) * close,
            receivable, pending, benchmark_value))

    def process_daily_signal(session, index, opening, bar=None):
        nonlocal portfolio
        if daily_position_risk is not None:
            risk_intent = daily_position_risk.intent_for(session.session_date)
            if risk_intent is not None:
                # The pending liquidation takes precedence over an entry.
                daily_by_session[session.session_date] = risk_intent
        intent = daily_by_session.get(session.session_date)
        if intent is None:
            return
        planned, reason = intent.order_for(portfolio, instrument, minimum_shares=minimum_shares)
        if planned is None and reason == "position_already_open":
            # A policy-filtered signal is not an order or an unfilled trade.
            return
        side = planned.side.value if planned else "sell" if intent.exit else "buy"
        ticket = CellOrder(intent.intent_id, "daily_signal", side,
                           (planned.quantity or 0) if planned else 0, None, index - 1, index)
        events.append(GridReplayEvent(ticket, index, opening, "submitted", reason,
                      signal_at=intent.known_at, effective_at=opening,
                      time_quality="market_session_clock"))
        fill = None
        if bar is None or session.status is not TradingStatus.TRADING:
            reason = "security_not_trading"
        elif exit_order is not None or policy.paused or any(
            f.side is OrderSide.SELL and opening <= f.filled_at <= bar.ended_at for f in portfolio.fills[-1:]
        ):
            reason = "exit_takeover"
        elif bar.volume_shares == 0:
            reason = "no_market_trades"
        elif planned is not None:
            max_quantity = bar_capacity(index)
            inventory_bound = False
            value_bound = False
            if planned.side is OrderSide.BUY and maximum_shares is not None:
                inventory_room = max(0, maximum_shares - portfolio.position_quantity(instrument).value)
                inventory_bound = max_quantity is None or inventory_room < max_quantity
                max_quantity = inventory_room if max_quantity is None else min(max_quantity, inventory_room)
            if planned.side is OrderSide.BUY and maximum_position_cny is not None:
                sizing_price = match_bar_price(bar.prices, side=OrderSide.BUY, observation="open",
                    slippage_bps=slippage_bps, slippage_cny=slippage_cny, tick=session.price_tick)
                value_room = max(0, int(maximum_position_cny / sizing_price)
                                 - portfolio.position_quantity(instrument).value)
                value_bound = max_quantity is None or value_room < max_quantity
                max_quantity = value_room if max_quantity is None else min(max_quantity, value_room)
            updated, fill, reason = execute_scheduled(
                planned, portfolio=portfolio, prices=bar.prices, session=session,
                ended_at=opening, next_session=bar.next_session, fees=fees,
                slippage_bps=slippage_bps, slippage_cny=slippage_cny,
                max_quantity=max_quantity,
                allow_adverse_limit_volume=limit_handling is LimitHandling.ALLOW_LIMIT_VOLUME)
            if fill is not None:
                if value_bound and reason == "participation_partial_fill":
                    reason = "position_value_limit_partial_fill"
                elif inventory_bound and reason == "participation_partial_fill":
                    reason = "inventory_limit_partial_fill"
                held = updated.position_quantity(instrument).value
                if fill.side is OrderSide.BUY and maximum_shares is not None and held > maximum_shares:
                    fill, reason = None, "maximum_inventory_exceeded"
                elif (fill.side is OrderSide.BUY and maximum_position_cny is not None
                      and held * fill.price.amount > maximum_position_cny):
                    fill, reason = None, "maximum_position_value_exceeded"
                else:
                    before_external = (portfolio.position_quantity(instrument).value
                                       + portfolio.pending_share_delta(instrument))
                    portfolio = account_after_fill(updated, index)
                    after_external = (portfolio.position_quantity(instrument).value
                                      + portfolio.pending_share_delta(instrument))
                    for stale in policy.after_external_fill(
                        side=fill.side.value, before=before_external, after=after_external, index=index,
                    ):
                        events.append(GridReplayEvent(stale, index, opening, "cancelled",
                                                      "external_position_cycle_changed"))
                    ticket = replace(ticket, quantity=fill.quantity.value)
        # Ordinary indicator orders are one opening attempt. Only protection
        # and holding-period exits own persistent retries in the phase-one spec.
        status = ("partially_filled" if fill is not None and reason in {
                    "participation_partial_fill", "inventory_limit_partial_fill",
                    "position_value_limit_partial_fill"} else
                  "filled" if fill else "skipped")
        events.append(GridReplayEvent(ticket, index, opening, status, reason, fill,
                      signal_at=intent.known_at, effective_at=opening,
                      time_quality="market_session_clock"))

    def process_scheduled(at: str, index: int, bar: ReplayBar, *, side_filter=None) -> None:
        nonlocal portfolio
        local_time = bar.ended_at.astimezone(ZoneInfo("Asia/Shanghai")).time()
        for planned in scheduled_orders:
            if side_filter is not None and planned.side is not side_filter:
                continue
            if planned.at != at or planned.session_date != bar.session.session_date or planned.order_id in scheduled_done:
                continue
            ticket = scheduled_active.get(planned.order_id)
            if ticket is None:
                if local_time != (time(9, 31) if at == "open" else time(15)):
                    continue
                ticket = CellOrder(planned.order_id, "scheduled", planned.side.value,
                                   planned.quantity or 0, planned.limit_price, index - 1, index)
                scheduled_active[planned.order_id] = ticket
                events.append(GridReplayEvent(ticket, index, bar.ended_at, "submitted", "scheduled_plan"))
            sizing_details: dict[str, str | int] = {}
            opening = bar.ended_at - timedelta(minutes=1)
            daily_exit = daily_by_session.get(bar.session.session_date)
            exit_at_open = (at == "open" and local_time == time(9, 31)
                            and daily_exit is not None and daily_exit.exit)
            sold_this_bar = any(
                f.side is OrderSide.SELL and opening <= f.filled_at <= bar.ended_at
                for f in portfolio.fills[-1:]
            )
            if policy.paused or (planned.side is OrderSide.BUY and (exit_at_open or sold_this_bar)):
                fill, reason = None, "exit_takeover"
            elif bar.volume_shares == 0:
                fill, reason = None, "no_market_trades"
            else:
                updated, fill, reason = execute_scheduled(
                    planned, portfolio=portfolio, prices=bar.prices, session=bar.session,
                    ended_at=bar.ended_at, next_session=bar.next_session, fees=fees,
                    slippage_bps=slippage_bps, slippage_cny=slippage_cny,
                    sizing_details=sizing_details,
                    max_quantity=bar_capacity(index, opening_order=(at == "open")),
                )
                if fill is not None and planned.side is OrderSide.BUY and (
                    maximum_shares is not None and updated.position_quantity(instrument).value > maximum_shares
                ):
                    sizing_details.update(currentPositionQuantity=portfolio.position_quantity(instrument).value,
                                          proposedFillQuantity=fill.quantity.value,
                                          projectedPositionQuantity=updated.position_quantity(instrument).value,
                                          maximumPositionQuantity=maximum_shares)
                    fill, reason = None, "maximum_inventory_exceeded"
                elif fill is not None and planned.side is OrderSide.BUY and (
                    maximum_position_cny is not None and updated.position_quantity(instrument).value * fill.price.amount > maximum_position_cny
                ):
                    fill, reason = None, "maximum_position_value_exceeded"
                elif fill is not None and planned.side is OrderSide.SELL and (
                    updated.position_quantity(instrument).value < minimum_shares
                ):
                    fill, reason = None, "minimum_inventory_breached"
                elif fill is not None:
                    portfolio = account_after_fill(updated, index)
                    if grid_composition:
                        after_external = (portfolio.position_quantity(instrument).value
                                          + portfolio.pending_share_delta(instrument))
                        before_external = after_external + (
                            fill.quantity.value if fill.side is OrderSide.SELL else -fill.quantity.value)
                        for stale in policy.after_external_fill(
                            side=fill.side.value, before=before_external,
                            after=after_external, index=index,
                        ):
                            events.append(GridReplayEvent(stale, index, bar.ended_at,
                                "cancelled", "external_position_cycle_changed"))
            working = at == "open" and (reason == "limit_not_reached" or
                       reason == "no_market_trades" and planned.limit_price is not None)
            if fill is not None and ticket.quantity == 0:
                ticket = replace(ticket, quantity=fill.quantity.value)
            status = ("partially_filled" if fill and reason == "participation_partial_fill"
                      else "filled" if fill else "working" if working else "skipped")
            events.append(GridReplayEvent(ticket, index, bar.ended_at,
                                          status, reason, fill,
                                          sizing_details=sizing_details or None))
            if not working:
                scheduled_done.add(planned.order_id)
                scheduled_active.pop(planned.order_id, None)

    for index, bar in enumerate(bars):
        while closed_sessions and closed_sessions[0][0].session_date < bar.session.session_date:
            session, daily = closed_sessions.pop(0)
            process_closed_session(session, daily, index)
        first_in_session = index == 0 or bars[index - 1].session.session_date != bar.session.session_date
        # Independent recurring entries can start another cycle after their
        # exit completes. Legacy grid takeover remains paused.
        if ((daily_signals or resume_after_exit or grid_composition) and exit_order is not None and not exit_remaining
                and portfolio.position_quantity(instrument).value <= minimum_shares
                and not portfolio.pending_share_delta(instrument)):
            exit_order = None
            policy.paused = False
        if first_in_session and corporate_actions is not None:
            opening = datetime.combine(bar.session.session_date, time(9, 30), tzinfo=ZoneInfo("Asia/Shanghai"))
            if bar.ended_at != opening.replace(minute=31):
                raise ValueError("corporate action replay needs the session opening bar")
            apply_session_open(bar.session, opening)
        for order in policy.prepare_bar(portfolio, bar, index):
            reason, ambiguous_signal = policy.signal_details(order)
            events.append(GridReplayEvent(order, index, bar.ended_at, "submitted", reason,
                                          ambiguous_intrabar_order=ambiguous_signal))
        # Capture conditions before any fills in this bar can change cost/holdings.
        trigger, ambiguous = (protection.observe(portfolio, bar)
                              if protection is not None and exit_order is None and not policy.paused
                              and bar.session.status is TradingStatus.TRADING and bar.volume_shares > 0 else (None, False))
        if (protection is not None and protection.trailing_drawdown is not None
                and exit_order is None and not policy.paused
                and bar.session.status is TradingStatus.TRADING and bar.volume_shares > 0
                and portfolio.position_quantity(instrument).value > 0):
            if protection_peak is None:
                raise ValueError("trailing drawdown requires an actual first-entry fill")
            prior_threshold = protection_peak * (1 - protection.trailing_drawdown)
            next_peak = max(protection_peak, bar.prices.high)
            next_threshold = next_peak * (1 - protection.trailing_drawdown)
            if trigger is None and bar.prices.low <= prior_threshold:
                trigger, ambiguous = "trailing_drawdown", False
            elif trigger is None and next_peak > protection_peak and bar.prices.low <= next_threshold:
                # OHLC proves both extremes occurred, but cannot prove whether
                # the high that raised the stop preceded the low that crossed it.
                trigger, ambiguous = "trailing_drawdown", True
            protection_peak = next_peak
        # A planned holding-period exit is knowable before the opening bar.
        # Re-evaluate surviving FIFO lots, so another sell cannot leave a stale
        # quantity and a later acquisition cannot reset the old lot's clock.
        is_opening_bar = bar.ended_at.astimezone(ZoneInfo("Asia/Shanghai")).time() == time(9, 31)
        if holding_sessions is not None and is_opening_bar and exit_order is None:
            for lot in sorted(portfolio.lots, key=lambda item: (item.acquired_at, item.opened_by_fill_id.value)):
                if lot.instrument_id != instrument:
                    continue
                anchor = first_entry_session if holding_anchor == "first_entry_fill" else lot.acquired_on
                due = holding_due_session(market_sessions, anchor, holding_sessions)
                if due is None or due > bar.session.session_date:
                    continue
                available_to_exit = max(0, portfolio.position_quantity(instrument).value - minimum_shares)
                planned_quantity = min(lot.remaining_quantity.value, available_to_exit)
                if not planned_quantity:
                    continue
                order = CellOrder(f"holding:{lot.opened_by_fill_id.value}", "holding_period", "sell",
                                  planned_quantity, None, index - 1, index)
                reason = None
                if bar.session.status is not TradingStatus.TRADING:
                    reason = "security_not_trading"
                elif bar.volume_shares == 0:
                    reason = "no_market_trades"
                elif lot.sellable_on > bar.session.session_date:
                    reason = "t_plus_one_locked"
                price = match_bar_price(bar.prices, side=OrderSide.SELL, observation="open",
                                        slippage_bps=slippage_bps, slippage_cny=slippage_cny,
                                        tick=bar.session.price_tick)
                if (reason is None and limit_handling is not LimitHandling.ALLOW_LIMIT_VOLUME
                        and bar.session.lower_limit is not None and price <= bar.session.lower_limit.amount):
                    reason = "sell_at_lower_limit"
                quantity = planned_quantity
                capacity = bar_capacity(index)
                if capacity is not None:
                    quantity = min(quantity, capacity)
                if reason is None and not quantity:
                    reason = "participation_capacity_zero"
                opening = bar.ended_at - timedelta(minutes=1)
                fill = None
                if reason is None:
                    fill_price = Price(price)
                    fill = FillRecord(FillId(f"{order.order_id}:fill:{index}"), OrderId(order.order_id),
                                      instrument, OrderSide.SELL, Quantity(quantity), fill_price, opening,
                                      fees.calculate(side=OrderSide.SELL, price=fill_price,
                                                     quantity=Quantity(quantity), trade_date=bar.session.session_date))
                    try:
                        portfolio = account_after_fill(apply_sell(portfolio, fill), index)
                    except InsufficientCashError:
                        reason = "insufficient_cash_including_fees"
                        fill = None
                    else:
                        acquisition = next((event for event in events if event.fill is not None
                                            and event.fill.fill_id == lot.opened_by_fill_id), None)
                        if (quantity == lot.remaining_quantity.value and acquisition is not None
                                and acquisition.order.cell_id in policy.cells):
                            cancelled = policy.release_after_external_exit(acquisition.order.cell_id, index)
                            if cancelled is not None:
                                events.append(GridReplayEvent(cancelled, index, bar.ended_at,
                                                              "cancelled", "holding_lot_exited"))
                events.append(GridReplayEvent(order, index, opening,
                                              "retry_pending" if reason else "partially_filled"
                                              if quantity < planned_quantity else "filled",
                                              reason or "holding_period_open", fill,
                                              signal_at=datetime.combine(due, time(9, 30), ZoneInfo("Asia/Shanghai")),
                                              effective_at=opening, time_quality="market_session_clock"))
        if exit_order is not None and index >= exit_order.effective_bar and exit_remaining:
            for cancelled in policy.cancel_for_exit():
                events.append(GridReplayEvent(cancelled, index, bar.ended_at, "cancelled", "exit_takeover"))
            quantity = min(exit_remaining, portfolio.sellable_quantity(instrument, bar.session.session_date).value)
            if capacity_mode is CapacityMode.POINT_IN_TIME_VOLUME:
                quantity = min(quantity, bar_capacity(index) or 0)
            reason = None
            if bar.session.status is not TradingStatus.TRADING:
                reason = "security_not_trading"
            elif bar.volume_shares == 0:
                reason = "no_market_trades"
            elif not quantity:
                reason = ("t_plus_one_locked"
                          if portfolio.sellable_quantity(instrument, bar.session.session_date).value == 0
                          else "participation_capacity_zero")
            price = match_bar_price(bar.prices, side=OrderSide.SELL,
                                    limit=exit_order.limit_price,
                                    observation="bar" if exit_order.limit_price is not None else "open",
                                    slippage_bps=slippage_bps, slippage_cny=slippage_cny,
                                    tick=bar.session.price_tick)
            if reason is None and price is None:
                reason = "limit_not_reached"
            if (reason is None and limit_handling is not LimitHandling.ALLOW_LIMIT_VOLUME
                    and bar.session.lower_limit is not None and price <= bar.session.lower_limit.amount):
                reason = "sell_at_lower_limit"
            fill = None
            if reason is None:
                fill_price = Price(price)
                fill = FillRecord(FillId(f"{exit_order.order_id}:fill:{index}"), OrderId(exit_order.order_id),
                                  instrument, OrderSide.SELL, Quantity(quantity), fill_price, bar.ended_at,
                                  fees.calculate(side=OrderSide.SELL, price=fill_price, quantity=Quantity(quantity),
                                                 trade_date=bar.session.session_date))
                try:
                    portfolio = account_after_fill(apply_sell(portfolio, fill), index)
                except InsufficientCashError:
                    reason = "insufficient_cash_including_fees"
                    fill = None
                else:
                    exit_remaining -= quantity
                    if grid_composition:
                        after_external = (portfolio.position_quantity(instrument).value
                                          + portfolio.pending_share_delta(instrument))
                        for stale in policy.after_external_fill(
                            side="sell", before=after_external + quantity,
                            after=after_external, index=index,
                        ):
                            events.append(GridReplayEvent(stale, index, bar.ended_at,
                                "cancelled", "external_position_cycle_changed"))
            events.append(GridReplayEvent(exit_order, index, bar.ended_at,
                                          "working" if reason == "limit_not_reached" else "retry_pending" if reason else "partially_filled" if exit_remaining else "filled",
                                          reason or ("protective_exit_limit" if exit_order.limit_price is not None else "protective_exit_open"), fill))
        # Predeclared opening exits execute before any same-opening entry.
        process_scheduled("open", index, bar, side_filter=OrderSide.SELL)
        if is_opening_bar:
            process_daily_signal(bar.session, index, bar.ended_at - timedelta(minutes=1), bar)
        process_scheduled("open", index, bar, side_filter=OrderSide.BUY)
        for order in tuple(policy.pending.values()):
            if order.effective_bar > index or not policy.can_match(order, bar, index):
                continue
            refreshed = policy.refresh_order(order, portfolio, bar)
            if refreshed != order:
                events.append(GridReplayEvent(order, index, bar.ended_at, "cancelled", "due_inventory_changed"))
                if refreshed is None:
                    continue
                order = refreshed
                events.append(GridReplayEvent(order, index, bar.ended_at, "submitted", "due_inventory_resized"))
            if bar.volume_shares == 0:
                # Time still advances; do not reject or consume an otherwise
                # valid ticket because this source bar contains no trades.
                policy.retry_failure(order, "no_market_trades")
                events.append(GridReplayEvent(order, index, bar.ended_at, "working", "no_market_trades"))
                continue
            side = OrderSide(order.side)
            reason = None
            if bar.session.status is not TradingStatus.TRADING:
                reason = "security_not_trading"
            elif order.quantity <= 0:
                reason = "amount_below_minimum_order"
            elif side is OrderSide.BUY and not bar.session.is_valid_buy_quantity(order.quantity):
                reason = "invalid_buy_quantity"
            price = match_bar_price(
                (policy.matching_prices(order, bar) if hasattr(policy, "matching_prices") else bar.prices),
                side=side, limit=order.limit_price,
                slippage_bps=slippage_bps, slippage_cny=slippage_cny,
                tick=bar.session.price_tick,
            ) if reason is None else None
            if reason is None and price is None:
                continue
            held = portfolio.position_quantity(instrument).value
            remaining = policy.remaining[order.cell_id]
            if reason is None and side is OrderSide.BUY:
                if maximum_shares is not None and held + remaining > maximum_shares:
                    reason = "maximum_inventory_exceeded"
                elif (maximum_position_cny is not None
                      and (held + remaining) * price > maximum_position_cny):
                    reason = "maximum_position_value_exceeded"
            elif reason is None and side is OrderSide.SELL and held - remaining < minimum_shares:
                reason = "minimum_inventory_reached" if held >= remaining else "insufficient_position"
            if reason is None:
                if side is OrderSide.BUY and bar.session.upper_limit is not None and price >= bar.session.upper_limit.amount:
                    reason = "buy_at_upper_limit"
                elif side is OrderSide.SELL and bar.session.lower_limit is not None and price <= bar.session.lower_limit.amount:
                    reason = "sell_at_lower_limit"
            fill = None
            if reason is None:
                capacity = bar_capacity(index)
                executable = remaining if capacity is None else min(remaining, capacity)
                if executable == 0:
                    # Capacity is a matching constraint, not an order rejection
                    # or a new signal. Leave this ticket and its remainder intact.
                    events.append(GridReplayEvent(order, index, bar.ended_at,
                                                  "working", "participation_capacity_zero"))
                    continue
                quantity = Quantity(executable)
                fill_price = Price(price)
                fill = FillRecord(
                    FillId(f"{order.order_id}:fill:{index}"), OrderId(order.order_id),
                    instrument, side, quantity, fill_price, bar.ended_at,
                    fees.calculate(side=side, price=fill_price, quantity=quantity,
                                   trade_date=bar.session.session_date),
                )
                try:
                    updated = (apply_buy(portfolio, fill, sellable_on=bar.next_session)
                               if side is OrderSide.BUY else apply_sell(portfolio, fill))
                except InsufficientCashError:
                    reason = "insufficient_cash_including_fees"
                except InsufficientSellableQuantityError:
                    reason = ("t_plus_one_locked" if portfolio.position_quantity(instrument).value >= quantity.value
                              else "insufficient_position")
                else:
                    policy.on_fill(order.order_id, quantity.value, index, price=price)
                    portfolio = account_after_fill(updated, index)
            retry = reason is not None and policy.retry_failure(order, reason)
            if reason is not None:
                if not retry:
                    policy.reject(order)
                fill = None
            partial = fill is not None and fill.quantity.value < remaining
            status = ("retry_pending" if retry else "rejected" if reason else
                      "partially_filled" if partial else "filled")
            events.append(GridReplayEvent(order, index, bar.ended_at,
                status, reason or ("participation_partial_fill" if partial else "ohlc_limit_match"),
                fill))
        process_scheduled("close", index, bar)
        if not benchmark_entered:
            first_buy = next((fill for fill in portfolio.fills[observed_fill_count:]
                              if fill.side is OrderSide.BUY and fill.instrument_id == instrument), None)
            if first_buy is not None:
                benchmark = apply_buy(benchmark, first_buy, sellable_on=bar.next_session)
                benchmark_entered = True
        observed_fill_count = len(portfolio.fills)
        for expired in (policy.before_observe(portfolio, bar, index) or ()):
            events.append(GridReplayEvent(expired, index, bar.ended_at, 'cancelled', 'daily_close_replanned'))
        grid_waits_for_inventory = (grid_composition and policy.active_sides == {"sell"}
            and not getattr(policy, 'daily_grid_clocks', False)
            and portfolio.sellable_quantity(instrument, bar.session.session_date).value <= minimum_shares)
        for order in (policy.observe(bar.prices, index,
                                    minimum_quantity=bar.session.minimum_buy_quantity,
                                    quantity_increment=bar.session.buy_quantity_increment)
                      if bar.volume_shares > 0 and bar.session.status is TradingStatus.TRADING
                      and not grid_waits_for_inventory else ()):
            reason, ambiguous_signal = policy.signal_details(order)
            events.append(GridReplayEvent(order, index, bar.ended_at, "submitted", reason,
                                          ambiguous_intrabar_order=ambiguous_signal))
        if trigger and exit_order is None and portfolio.position_quantity(instrument).value > minimum_shares:
            exit_remaining = portfolio.position_quantity(instrument).value - minimum_shares
            exit_order = CellOrder(f"exit:{index}", "protection", "sell", exit_remaining,
                                  protection.limit_price, index, index + 1)
            events.append(GridReplayEvent(exit_order, index, bar.ended_at, "submitted", trigger,
                                          ambiguous_intrabar_order=ambiguous))
        last_bar = index + 1 == len(bars)
        last = last_bar and not closed_sessions
        if last_bar or bars[index + 1].session.session_date != bar.session.session_date:
            if corporate_actions is not None:
                if bar.ended_at.astimezone(ZoneInfo("Asia/Shanghai")).time() != time(15):
                    raise ValueError("corporate action replay needs the session closing bar")
                apply_session_close(bar.session, bar.ended_at)
            for ticket in scheduled_active.values():
                events.append(GridReplayEvent(ticket, index, bar.ended_at, "cancelled",
                                              "backtest_ended" if last else "day_expired"))
                scheduled_done.add(ticket.order_id)
            scheduled_active.clear()
            for order in policy.expire_day(None if last else index + 1):
                events.append(GridReplayEvent(order, index, bar.ended_at, "cancelled",
                                              "backtest_ended" if last else "day_expired"))
                rebuilt = policy.pending.get(order.cell_id)
                if rebuilt is not None:
                    events.append(GridReplayEvent(rebuilt, index, bar.ended_at, "submitted",
                                                  "order_rebuilt_next_session"))
                if rebuilt is not None and order.order_id in calendar_signals:
                    calendar_signals[rebuilt.order_id] = calendar_signals[order.order_id]
            if not last and exit_order is not None and exit_remaining:
                events.append(GridReplayEvent(exit_order, index, bar.ended_at, "cancelled", "day_expired"))
                exit_order = replace(exit_order, order_id=f"exit:{exit_order.signal_bar}:day:{index + 1}",
                                     quantity=exit_remaining, effective_bar=index + 1)
                events.append(GridReplayEvent(exit_order, index, bar.ended_at, "submitted", "exit_rebuilt_next_session"))
        if (daily_position_risk is not None
                and bar.ended_at.astimezone(ZoneInfo("Asia/Shanghai")).time() == time(15)):
            daily_position_risk.observe_close(bar.session.session_date)
        record_equity(bar.ended_at, bar.prices.close)
    for session, daily in closed_sessions:
        process_closed_session(session, daily, len(bars))
    if exit_order is not None and exit_remaining:
        events.append(GridReplayEvent(exit_order, len(bars) - 1, final_at,
                                      "cancelled", "backtest_ended_exit_unfinished"))
    # Recompute from surviving lots, not failed order quantities: another sell
    # may have consumed some or all of an overdue lot after its opening attempt.
    # Not-yet-due holdings are open positions, not unfinished exit intentions.
    unfinished_holding = 0
    if holding_sessions is not None:
        for lot in portfolio.lots:
            if lot.instrument_id != instrument:
                continue
            anchor = first_entry_session if holding_anchor == "first_entry_fill" else lot.acquired_on
            due = holding_due_session(market_sessions, anchor, holding_sessions)
            if due is None or due > final_day:
                continue
            remaining_to_exit = min(lot.remaining_quantity.value, max(
                0, portfolio.position_quantity(instrument).value - minimum_shares - unfinished_holding))
            if not remaining_to_exit:
                continue
            unfinished_holding += remaining_to_exit
            # Protection takes over outstanding exits; do not manufacture a
            # second live ticket or count the same inventory twice.
            if exit_order is None:
                attempted = next((event.order for event in reversed(events)
                                  if event.order.order_id == f"holding:{lot.opened_by_fill_id.value}"), None)
                if attempted is not None:
                    order = replace(attempted, quantity=remaining_to_exit)
                    events.append(GridReplayEvent(order, len(bars) - 1, final_at,
                                                  "cancelled", "backtest_ended_holding_exit_unfinished"))
    scheduled_by_id = {order.order_id: order for order in scheduled_orders}
    holding_cells = {event.order.cell_id for event in events if event.reason == "holding_period"}
    stamped_events = []
    def bar_start(index):
        if not bars:
            return datetime.combine(first_session.session_date, time(9, 30), ZoneInfo("Asia/Shanghai"))
        if 0 <= index < len(bars):
            return bars[index].ended_at - timedelta(minutes=1)
        last_end = bars[-1].ended_at.astimezone(ZoneInfo("Asia/Shanghai"))
        if last_end.time() == time(11, 30):
            return last_end.replace(hour=13, minute=0)
        if last_end.time() < time(15):
            return last_end
        return datetime.combine(bars[-1].next_session, time(9, 30), ZoneInfo("Asia/Shanghai"))
    for event in events:
        order = event.order
        planned = scheduled_by_id.get(order.order_id)
        if planned is not None:
            signal_at = planned.known_at
            effective_at = datetime.combine(planned.session_date, time(15) if planned.at == "close" else time(9, 30), ZoneInfo("Asia/Shanghai"))
        else:
            effective_at = bar_start(order.effective_bar)
            signal_at = (bar_start(order.signal_bar + 1)
                         if order.cell_id == "holding_period" or order.cell_id in holding_cells
                         else bars[order.signal_bar].ended_at if order.signal_bar >= 0 else bar_start(0))
        signal_at = event.signal_at or calendar_signals.get(order.order_id) or signal_at
        stamped_events.append(replace(event, signal_at=signal_at, effective_at=event.effective_at or effective_at))
    risk_unfinished = (max(0, portfolio.position_quantity(instrument).value - minimum_shares)
                       if daily_position_risk is not None and daily_position_risk.pending is not None else 0)
    capacity_assumption = (
        "unlimited_ohlc_research"
        if capacity_mode is CapacityMode.UNLIMITED
        else (
            "first_completed_minute_volume_for_opening_order_else_previous_completed_minute_volume:"
            f"{participation_rate}"
            if first_bar_completed_volume_used
            else f"previous_completed_minute_volume:{participation_rate}"
        )
    )
    result = GridReplayResult(portfolio, tuple(stamped_events),
                            capacity_assumption=capacity_assumption,
                            unfinished_exit_quantity=max(risk_unfinished, exit_remaining + policy.unfinished_exit_quantity),
                            equity=tuple(equity), unfinished_holding_exit_quantity=unfinished_holding + policy.unfinished_holding_exit_quantity,
                            corporate_action_policy=corporate_actions.policy_id if corporate_actions else None,
                            price_rebases=price_rebases,
                            benchmark_portfolio=benchmark if benchmark_entered else None,
                            closed_position_cycles=closed_position_cycles,
                            initial_equity_cny=initial_equity_cny,
                            grid_policy=getattr(policy, "policy_version", None),
                            monitoring_events=tuple(getattr(policy, "monitoring_events", ())))
    return result
