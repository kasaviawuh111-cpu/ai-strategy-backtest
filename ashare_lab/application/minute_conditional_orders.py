"""Sequential minute conditions sharing the grid replay's matching and ledger."""

from decimal import Decimal
from dataclasses import replace
from datetime import time
from zoneinfo import ZoneInfo

from ashare_lab.adapters.strategies.vnpy_conditions import trailing_price, price_reached
from ashare_lab.application.fixed_grid_orders import FixedGridOrders, CellOrder
from ashare_lab.application.minute_grid_plan import MinuteGridCapabilityError
from ashare_lab.application.trading_schedule import holding_due_session


class MinuteConditionalOrders(FixedGridOrders):
    def __init__(self, rules, repeat_cycles=1, *, market_sessions=(), minimum_shares=0,
                 recurring_exits=False, external_entries=False):
        super().__init__([])
        if recurring_exits and any(rule.side != "sell" for rule in rules):
            raise MinuteGridCapabilityError("recurring_schedule_requires_exit_rules")
        self.recurring_exits = recurring_exits
        self.external_entries = external_entries
        if any(rule.kind == "holding_period" for rule in rules) and not market_sessions:
            raise MinuteGridCapabilityError("holding_period_market_calendar_missing")
        self.market_sessions = market_sessions
        self.minimum_shares = minimum_shares
        self.rules = list(rules)
        self.repeat_cycles = repeat_cycles
        self.stage = self.cycles = self.active_from = 0
        self.selected = None
        self.context = None
        self.extremes = {}
        self.last_fill_price = None
        self.first_observation_price = None
        self.fill_notional = Decimal(0)
        self.fill_quantity = 0
        self.holding_target_remaining = 0
        self.details = {}
        self.blocked_session = None
        self.stage_end = None
        if self.rules and all(
            self.rules[index].kind == "relative_price"
            and self.rules[index].reference_mode == "previous_fill"
            for index in self._stage_indices()
        ):
            raise MinuteGridCapabilityError("conditional_initial_reference_missing")

    def prepare_bar(self, portfolio, bar, index):
        self.context = (portfolio, bar.session.instrument_id)
        self.current_session = bar.session.session_date
        if bar.ended_at.astimezone(ZoneInfo("Asia/Shanghai")).time() != time(9, 31):
            return ()
        return self.prepare_session(portfolio, bar.session, index)

    def prepare_session(self, portfolio, session, index):
        self.context = (portfolio, session.instrument_id)
        self.current_session = session.session_date
        if self.pending or self.paused or index < self.active_from or self.stage >= len(self.rules):
            return ()
        for number in self._stage_indices():
            rule = self.rules[number]
            if rule.kind != "holding_period":
                continue
            due_quantity = 0
            for lot in portfolio.lots:
                if lot.instrument_id != session.instrument_id:
                    continue
                due = holding_due_session(self.market_sessions, lot.acquired_on, rule.sessions)
                if due is not None and due <= self.current_session:
                    due_quantity += lot.remaining_quantity.value
            self.holding_target_remaining = max(
                0, portfolio.position_quantity(session.instrument_id).value - self.minimum_shares
            )
            quantity = min(due_quantity, self.holding_target_remaining
                           if rule.sizing_mode == "all_position" else rule.quantity - self.fill_quantity)
            if quantity <= 0:
                continue
            self.serial += 1
            if self.stage_end is None:
                self.stage_end = self._stage_indices().stop
            key = f"condition:{self.cycles}:{number}"
            order = CellOrder(
                f"{key}:{self.serial}", key, "sell", quantity, rule.limit_price, index - 1, index
            )
            self.pending[key] = order
            self.remaining[key] = quantity
            self.selected = number
            self.details[key] = ("holding_period", False)
            return (order,)
        return ()

    def can_match(self, order, bar, index):
        return self.blocked_session != bar.session.session_date

    def refresh_order(self, order, portfolio, bar):
        rule = self.rules[self.selected]
        if rule.kind != "holding_period" and not self.recurring_exits:
            return order
        self.holding_target_remaining = max(
            0, portfolio.position_quantity(bar.session.instrument_id).value - self.minimum_shares
        )
        due_quantity = self.holding_target_remaining
        if rule.kind == "holding_period":
            due_quantity = 0
            for lot in portfolio.lots:
                if lot.instrument_id != bar.session.instrument_id:
                    continue
                due = holding_due_session(self.market_sessions, lot.acquired_on, rule.sessions)
                if due is not None and due <= bar.session.session_date:
                    due_quantity += lot.remaining_quantity.value
        quantity = min(self.remaining[order.cell_id], due_quantity)
        if rule.sizing_mode == "all_position":
            quantity = min(quantity, self.holding_target_remaining)
        if quantity == order.quantity:
            return order
        del self.pending[order.cell_id]
        del self.remaining[order.cell_id]
        if quantity == 0:
            if self.recurring_exits:
                # Another sell leg already consumed this inventory. Cancel the
                # stale ticket without inventing a fill or permanently pausing
                # protection for a later calendar/indicator entry.
                self.stage = 0
                self.stage_end = self.selected = None
                self.fill_notional, self.fill_quantity = Decimal(0), 0
                self.extremes.clear()
                self.active_from = max(order.effective_bar, self.last_observed + 1)
            return None
        self.serial += 1
        updated = replace(order, order_id=f"{order.cell_id}:{self.serial}", quantity=quantity)
        self.pending[order.cell_id] = updated
        self.remaining[order.cell_id] = quantity
        return updated

    def _stage_indices(self):
        if self.stage_end is not None and self.selected is not None:
            return range(self.selected, self.selected + 1)
        stop = self.stage + 1
        group = self.rules[self.stage].group
        while group is not None and stop < len(self.rules) and self.rules[stop].group == group:
            stop += 1
        return range(self.stage, stop)

    def observe(self, bar, index, *, minimum_quantity=100, quantity_increment=100):
        if index <= self.last_observed:
            raise ValueError("bars must be observed once in increasing order")
        self.last_observed = index
        if self.first_observation_price is None:
            self.first_observation_price = bar.open
        if self.paused or self.pending or index < self.active_from or self.stage >= len(self.rules):
            return ()
        portfolio, instrument = self.context
        lots = [lot for lot in portfolio.lots if lot.instrument_id == instrument]
        shares = sum(lot.remaining_quantity.value for lot in lots)
        cost = None
        needs_cost = any(
            self.rules[number].kind in {"take_profit", "stop_loss"}
            for number in self._stage_indices()
        )
        if shares and needs_cost:
            if any(lot.acquisition_principal is None for lot in lots):
                raise MinuteGridCapabilityError("conditional_cost_provenance_missing")
            economic_shares = shares + portfolio.pending_share_delta(instrument)
            cost = (sum(lot.acquisition_principal.amount for lot in lots)
                    + portfolio.pending_share_principal(instrument)) / economic_shares
        candidates = []
        for number in self._stage_indices():
            rule = self.rules[number]
            if rule.kind == "holding_period":
                continue
            direction, target = rule.direction, rule.target_price
            reference = None
            if rule.kind in {"take_profit", "stop_loss"}:
                if cost is None:
                    continue
                reference = cost
                direction = "up" if rule.kind == "take_profit" else "down"
            elif rule.kind == "relative_price":
                reference = (
                    self.first_observation_price
                    if rule.reference_mode == "first_observation"
                    else self.last_fill_price
                )
                if reference is None:
                    continue
            elif rule.kind in {"rebound", "pullback"}:
                upward = rule.kind == "rebound"
                direction = "up" if upward else "down"
                reference = self.extremes.get(number)
                if reference is None:
                    if rule.activation_price is not None:
                        active = (
                            bar.low <= rule.activation_price
                            if upward
                            else bar.high >= rule.activation_price
                        )
                        if active:
                            self.extremes[number] = bar.low if upward else bar.high
                        # Intrabar activation cannot use this bar's unknown path.
                        continue
                    reference = bar.open
                self.extremes[number] = (
                    min(reference, bar.low) if upward else max(reference, bar.high)
                )
            if reference is not None:
                target = trailing_price(
                    reference, rule.gap, unit=rule.gap_unit, direction=direction
                )
            if target is None or target <= 0:
                continue
            inclusive = rule.kind != "price" or rule.price_comparison == "inclusive"
            touched = price_reached(bar.high if direction == "up" else bar.low, target,
                                    direction, inclusive=inclusive)
            if touched:
                at_open = price_reached(bar.open, target, direction, inclusive=inclusive)
                candidates.append((number, target, at_open))
        if not candidates:
            return ()
        ambiguous = False
        protections = [
            c for c in candidates if self.rules[c[0]].kind in {"take_profit", "stop_loss"}
        ]
        if len(protections) >= 2 and len({self.rules[c[0]].kind for c in protections}) == 2:
            opened = [c for c in protections if c[2]]
            chosen = (
                opened[0]
                if opened
                else next(c for c in protections if self.rules[c[0]].kind == "stop_loss")
            )
            ambiguous = not opened
        else:
            chosen = candidates[0]
        number, target, _ = chosen
        self.stage_end = self._stage_indices().stop
        rule = self.rules[number]
        quantity = rule.quantity
        if rule.sizing_mode == "all_position":
            quantity = max(0, shares - self.minimum_shares)
            if quantity == 0:
                return ()
        if rule.sizing_mode == "amount":
            maximum = int(rule.amount_cny / target)
            quantity = (
                minimum_quantity
                + (maximum - minimum_quantity) // quantity_increment * quantity_increment
                if maximum >= minimum_quantity
                else 0
            )
        self.serial += 1
        key = f"condition:{self.cycles}:{number}"
        order = CellOrder(
            f"{key}:{self.serial}", key, rule.side, quantity, rule.limit_price, index, index + 1
        )
        self.pending[key] = order
        self.remaining[key] = quantity
        self.selected = number
        self.details[key] = (rule.kind, ambiguous)
        return (order,)

    def signal_details(self, order):
        return self.details[order.cell_id]

    def retry_failure(self, order, reason):
        retry = (self.recurring_exits or self.external_entries or self.rules[self.selected].kind in {
            "take_profit",
            "stop_loss",
            "holding_period",
        }) and reason in {
            "t_plus_one_locked",
            "sell_at_lower_limit",
            "security_not_trading",
            "insufficient_cash_including_fees",
            "no_market_trades",
        }
        if retry and (
            self.rules[self.selected].kind == "holding_period" or reason == "t_plus_one_locked"
        ):
            # T+1 cannot unlock on a later minute of the same session. Keep
            # the exit ticket, but do not emit an impossible retry each bar.
            self.blocked_session = self.current_session
        return retry

    def on_fill(self, order_id, quantity, index, *, price=None):
        order = next(o for o in self.pending.values() if o.order_id == order_id)
        if (
            price is None
            or index < order.effective_bar
            or not 0 < quantity <= self.remaining[order.cell_id]
        ):
            raise ValueError("invalid conditional fill")
        self.remaining[order.cell_id] -= quantity
        self.fill_notional += price * quantity
        self.fill_quantity += quantity
        self.holding_target_remaining = max(0, self.holding_target_remaining - quantity)
        if not self.remaining[order.cell_id]:
            del self.pending[order.cell_id]
            del self.remaining[order.cell_id]
            if (
                self.rules[self.selected].kind == "holding_period"
                and (self.holding_target_remaining > 0
                     if self.rules[self.selected].sizing_mode == "all_position"
                     else self.fill_quantity < self.rules[self.selected].quantity)
            ):
                self.active_from = index + 1
                return
            self.last_fill_price = self.fill_notional / self.fill_quantity
            self.fill_notional, self.fill_quantity = Decimal(0), 0
            self.stage = self.stage_end
            self.stage_end = self.selected = None
            self.extremes.clear()
            self.active_from = index + 1
            if self.stage == len(self.rules):
                self.cycles += 1
                if self.recurring_exits or self.cycles < self.repeat_cycles:
                    self.stage = 0

    def reject(self, order):
        super().reject(order)
        self.paused = True

    def expire_day(self, next_bar):
        protection = self.selected is not None and (self.recurring_exits or self.external_entries or self.rules[self.selected].kind in {
            "take_profit",
            "stop_loss",
            "holding_period",
        })
        if protection and next_bar is None:
            if self.rules[self.selected].kind == "holding_period":
                self.unfinished_holding_exit_quantity = sum(self.remaining.values())
            else:
                self.unfinished_exit_quantity = sum(self.remaining.values())
        expired = super().expire_day(next_bar, rebuild=protection)
        if expired and not protection:
            self.paused = True
        return expired

    def rebase_prices(self, rebase, *, tick=Decimal(".01")):
        super().rebase_prices(rebase, tick=tick)
        self.rules = [
            rule.model_copy(
                update={
                    "target_price": rebase.price(rule.target_price, rule.side, tick),
                    "limit_price": rebase.price(rule.limit_price, rule.side, tick),
                    "activation_price": rebase.price(rule.activation_price, rule.side, tick),
                    "gap": rebase.price(rule.gap, rule.side, tick)
                    if rule.gap_unit == "cny"
                    else rule.gap,
                }
            )
            for rule in self.rules
        ]
        self.extremes = {key: value * rebase.factor for key, value in self.extremes.items()}
        if self.last_fill_price is not None:
            self.last_fill_price *= rebase.factor
        if self.first_observation_price is not None:
            self.first_observation_price *= rebase.factor


def execute_minute_conditions(
    params, *, prepared, exchange, corporate_actions=None, price_rebases=(), execution_config=None,
    daily_signals=(), protection=None, holding_sessions=None, holding_anchor="each_entry_fill", daily_position_risk=None,
    scheduled_orders=(),
):
    from ashare_lab.application.minute_replay_input import replay_start
    from ashare_lab.application.opening_portfolio import opening_portfolio
    from ashare_lab.application.minute_grid_replay import replay_grid
    from ashare_lab.application.scheduled_execution import ScheduledOrder
    from ashare_lab.domain.execution.fees import FeeCalculator, FeePolicy
    from ashare_lab.domain.shared import Money

    daily_entry = any(intent.enter for intent in daily_signals)
    if daily_entry and any(rule.side == "buy" for rule in params.rules):
        raise ValueError("daily entry adapter requires a separate sell-only condition leg")
    first_day, first_price, first_at = replay_start(prepared)
    initial = (
        (
            ScheduledOrder(
                "condition-initial-build", first_day, first_at, quantity=params.initial_shares
            ),
        )
        if params.initial_shares
        else ()
    )
    portfolio, initial_equity = opening_portfolio(params, prepared, first_price=first_price)
    policy = MinuteConditionalOrders(
            params.rules, params.repeat_cycles, market_sessions=prepared.market_sessions,
            minimum_shares=params.min_shares,
            recurring_exits=daily_entry,
            external_entries=any(order.side.value == "buy" for order in scheduled_orders)
                and all(rule.side == "sell" for rule in params.rules),
        )
    if params.observation == 'daily_close':
        from ashare_lab.application.independent_order_routing import IndependentOrderRouting
        dates = {bar.session.session_date for bar in prepared.bars}
        for clock in (time(9, 31), time(15)):
            if dates != {bar.session.session_date for bar in prepared.bars
                         if bar.ended_at.astimezone(ZoneInfo('Asia/Shanghai')).time() == clock}:
                raise MinuteGridCapabilityError('composed_daily_open_or_close_missing')
        leg = 'exit' if all(rule.side == 'sell' for rule in params.rules) else 'entry'
        policy.daily_limit_intraday = True
        policy = IndependentOrderRouting(
            entry=policy if leg == 'entry' else FixedGridOrders([]),
            exit=policy if leg == 'exit' else FixedGridOrders([]),
            observations={leg: 'daily_close'}, minimum_shares=params.min_shares)
    return replay_grid(
        policy,
        list(prepared.bars),
        portfolio,
        FeeCalculator(
            FeePolicy(exchange, params.commission_rate, Money(params.minimum_commission_cny))
        ),
        slippage_bps=params.slippage_bps,
        slippage_cny=params.slippage_cny,
        scheduled_orders=(*initial, *scheduled_orders),
        daily_signals=daily_signals,
        daily_position_risk=daily_position_risk,
        protection=protection,
        holding_sessions=holding_sessions, holding_anchor=holding_anchor,
        resume_after_exit=protection is not None and params.repeat_cycles > 1,
        market_sessions=prepared.market_sessions,
        minimum_shares=params.min_shares,
        maximum_shares=params.max_shares,
        maximum_position_cny=params.max_position_cny,
        corporate_actions=corporate_actions,
        price_rebases=price_rebases,
        nontrading_closes=getattr(prepared, "nontrading_closes", ()),
        initial_equity_cny=initial_equity,
        **(dict(capacity_mode=execution_config.capacity_mode,
                participation_rate=execution_config.participation_rate)
           if execution_config is not None else {}),
    )
