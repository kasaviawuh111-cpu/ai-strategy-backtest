"""Price-triggered grid; Eastmoney customer-service evidence, 2026-09-13.

The broker's tick stream is unavailable. One completed bar can establish one
trigger: an opening gap or one unambiguous threshold touch. Two-sided touches
are not sequenced speculatively. Sleep is monitoring state, not a failed order.
"""
from datetime import time
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from zoneinfo import ZoneInfo

from ashare_lab.application.fixed_grid_orders import CellOrder, FixedGridOrders
from ashare_lab.domain.orders import OrderSide
from ashare_lab.domain.shared import Price, Quantity

D = Decimal


class TriggerGridOrders(FixedGridOrders):
    policy_version = "eastmoney-trigger-grid.research.v1"

    def __init__(self, parameters, fees, *, active_sides=("buy", "sell")):
        super().__init__([], active_sides=active_sides)
        self.parameters = parameters
        self.anchor = parameters.resolved_anchor
        self.fees = fees
        self.sleeping = {}
        self.monitoring_events = []
        self.account = None
        self.current_bar = None
        self.order_prices = {}
        self.startup_reset = set()

    def before_observe(self, portfolio, bar, index):
        self.account, self.current_bar = portfolio, bar

    def prepare_bar(self, portfolio, bar, index):
        self.before_observe(portfolio, bar, index)
        if index == 0 and self.parameters.initial_shares:
            # An explicit model/user build plan is an ordinary DAY order,
            # not free opening inventory and not a grid-price trigger. The
            # common matcher retains its remainder under minute capacity.
            key = "initial-build"
            order = CellOrder("grid-initial-build", key, "buy", self.parameters.initial_shares,
                              None, index - 1, index)
            self.pending[key] = order
            self.remaining[key] = order.quantity
            self.order_prices[key] = bar.prices.open
            return (order,)
        return ()

    def _state(self, side, reason, index):
        # Product decision: sleep lasts for this entire run. Account recovery
        # does not automatically wake either side or issue a catch-up order.
        if reason is None or side in self.sleeping:
            return
        self.sleeping[side] = reason
        self.monitoring_events.append(dict(
            bar=index, at=self.current_bar.ended_at.isoformat(), side=side,
            state="sleeping", reason=reason,
            anchor=str(self.anchor)))

    def _threshold(self, side):
        mode, spacing = self.parameters.spacing_for(side)
        sign = -1 if side == "buy" else 1
        if mode == "percent":
            value = self.anchor / (1 + spacing / 100) if side == "buy" else self.anchor * (1 + spacing / 100)
        else:
            # anchor_percent follows the moving base in this policy only.
            gap = spacing if mode == "cny" else self.anchor * spacing / 100
            value = self.anchor + sign * gap
        return value.quantize(D(".01"), rounding=ROUND_FLOOR if side == "buy" else ROUND_CEILING)

    def _quantity(self, price, minimum, increment):
        if self.parameters.sizing_mode == "shares":
            return self.parameters.order_shares
        maximum = int(self.parameters.order_amount_cny / price) if price > 0 else 0
        return minimum + (maximum - minimum) // increment * increment if maximum >= minimum else 0

    def _limit(self, side, trigger):
        p = self.parameters
        if p.price_mode == "next_open":
            return None
        if p.price_mode == "fixed_limit":
            return p.buy_limit if side == "buy" else p.sell_limit
        return trigger + p.limit_offset_cny * (1 if side == "buy" else -1)

    def _cost(self, price, quantity):
        # Includes configured friction; the next-bar ledger still rechecks
        # actual price, fees and availability rather than guaranteeing a fill.
        price = (price * (1 + self.parameters.slippage_bps / 10000)
                 + self.parameters.slippage_cny).quantize(D(".01"), rounding=ROUND_CEILING)
        return price * quantity + self.fees.calculate(
            side=OrderSide.BUY, price=Price(price), quantity=Quantity(quantity),
            trade_date=self.current_bar.session.session_date).total.amount

    def _unavailable(self, side, trigger, quantity):
        p, account, session = self.parameters, self.account, self.current_bar.session
        limit = self._limit(side, trigger)
        if not p.lower_price <= trigger <= p.upper_price or (limit is not None and not p.lower_price <= limit <= p.upper_price):
            return "grid_price_outside_range"
        if quantity <= 0:
            return "amount_below_minimum_order"
        if side == "buy" and not session.is_valid_buy_quantity(quantity):
            return "invalid_buy_quantity"
        held = account.position_quantity(session.instrument_id).value
        pending = [o for o in self.pending.values() if o.side == side]
        reserved_quantity = sum(self.remaining[o.cell_id] for o in pending)
        if side == "sell":
            if held - reserved_quantity - quantity < p.min_shares:
                return "insufficient_position" if held < reserved_quantity + quantity else "minimum_inventory_reached"
            if account.sellable_quantity(session.instrument_id, session.session_date).value - reserved_quantity < quantity:
                return "t_plus_one_locked"
        else:
            price = limit or trigger
            if held + reserved_quantity + quantity > p.max_shares:
                return "maximum_inventory_exceeded"
            if p.max_position_cny is not None and (held + reserved_quantity + quantity) * price > p.max_position_cny:
                return "maximum_position_value_exceeded"
            reserved_cash = sum((self._cost(o.limit_price or self.order_prices[o.cell_id], self.remaining[o.cell_id])
                                 for o in pending), D(0))
            if self._cost(price, quantity) + reserved_cash > account.cash.amount:
                return "insufficient_cash_including_fees"
        return None

    def observe(self, bar, index, *, minimum_quantity=100, quantity_increment=100):
        if index <= self.last_observed:
            raise ValueError("bars must be observed once in increasing order")
        first = self.last_observed < 0
        self.last_observed = index
        if self.paused:
            return ()
        if self.current_bar is None:
            raise ValueError("trigger grid requires current account and session")
        # End-labelled 14:57 includes the final continuous-auction minute;
        # 14:58-15:00 belong to the closing auction, never a fresh trigger.
        clock = self.current_bar.ended_at.astimezone(ZoneInfo("Asia/Shanghai")).time()
        if not (time(9, 30) < clock <= time(11, 30) or time(13) < clock <= time(14, 57)):
            return ()
        thresholds = {s: self._threshold(s) for s in ("buy", "sell") if s in self.active_sides}
        for side, level in thresholds.items():
            if first and self.parameters.startup_mode == "wait_for_crossing" and (
                    bar.open <= level if side == "buy" else bar.open >= level):
                self.startup_reset.add(side)
            if side in self.startup_reset and (bar.close > level if side == "buy" else bar.close < level):
                self.startup_reset.remove(side)
                # A crossing cannot be established inside this reset bar.
                thresholds[side] = None
        candidates = []
        for side, level in thresholds.items():
            if level is None or side in self.sleeping or side in self.startup_reset:
                continue
            gap = bar.open <= level if side == "buy" else bar.open >= level
            touched = bar.low <= level if side == "buy" else bar.high >= level
            if touched:
                candidates.append((side, level, gap))
        opening = [c for c in candidates if c[2]]
        if len(candidates) > 1 and not opening:
            self.monitoring_events.append(dict(bar=index, at=self.current_bar.ended_at.isoformat(),
                state="ambiguous", reason="grid_two_sided_bar_order_unknown", anchor=str(self.anchor)))
            return ()
        if not candidates:
            return ()
        side, trigger, _ = (opening or candidates)[0]
        quantity = self._quantity(trigger, minimum_quantity, quantity_increment)
        reason = self._unavailable(side, trigger, quantity)
        self._state(side, reason, index)
        if reason:
            return ()
        self.serial += 1
        key = f"trigger:{self.serial}"
        order = CellOrder(f"grid:{key}", key, side, quantity, self._limit(side, trigger), index, index + 1)
        self.pending[key], self.remaining[key] = order, quantity
        self.order_prices[key] = trigger
        previous = self.anchor
        self.anchor = trigger
        self.monitoring_events.append(dict(bar=index, at=self.current_bar.ended_at.isoformat(),
            state="triggered", side=side, orderId=order.order_id,
            previousAnchor=str(previous), anchor=str(trigger)))
        return (order,)

    def signal_details(self, order):
        return ("scheduled_plan" if order.cell_id == "initial-build"
                else "grid_price_trigger_anchor_updated"), False

    def retry_failure(self, order, reason):
        if reason in {"buy_at_upper_limit", "sell_at_lower_limit", "security_not_trading", "no_market_trades"}:
            # A temporary matching restriction does not revoke an accepted
            # DAY ticket. Keep it separate from fresh trigger monitoring.
            return True
        if reason in {"insufficient_cash_including_fees", "insufficient_position", "t_plus_one_locked",
                      "minimum_inventory_reached", "maximum_inventory_exceeded", "maximum_position_value_exceeded"}:
            self._state(order.side, reason, self.last_observed + 1)
        return False

    def on_fill(self, order_id, quantity, index, *, price=None):
        order = next(o for o in self.pending.values() if o.order_id == order_id)
        if index < order.effective_bar or not 0 < quantity <= self.remaining[order.cell_id]:
            raise ValueError("invalid fill quantity or time")
        self.remaining[order.cell_id] -= quantity
        if not self.remaining[order.cell_id]:
            self.pending.pop(order.cell_id)
            self.remaining.pop(order.cell_id)
            self.order_prices.pop(order.cell_id)
        # Neither partial nor complete fills move the trigger reference.

    def reject(self, order):
        self.pending.pop(order.cell_id)
        self.remaining.pop(order.cell_id, None)
        self.order_prices.pop(order.cell_id, None)

    def expire_day(self, next_bar, *, rebuild=True):
        expired = tuple(self.pending.values())
        self.pending.clear()
        self.remaining.clear()
        self.order_prices.clear()
        return expired

    def rebase_prices(self, rebase, *, tick=D(".01")):
        # Grid-specific corporate-action continuation is not established by
        # the supplied customer-service record. Reuse research price rebasing,
        # explicitly disclosed by the common result rather than call it live.
        from ashare_lab.application.grid_strategy import _rebase_grid_parameters
        self.parameters = _rebase_grid_parameters(self.parameters, rebase)
        self.anchor = rebase.price(self.anchor, "buy", tick)
        from dataclasses import replace
        self.pending = {k: replace(o, limit_price=rebase.price(o.limit_price, o.side, tick))
                        for k, o in self.pending.items()}
        self.order_prices = {k: rebase.price(v, "buy", tick) for k, v in self.order_prices.items()}
