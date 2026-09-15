"""Namespace two existing trigger policies without merging their state machines."""
from dataclasses import replace
from datetime import time
from zoneinfo import ZoneInfo
from ashare_lab.domain.execution.bar_prices import BarPrices


class IndependentOrderRouting:
    def __init__(self, *, entry, exit, minimum_shares=0, observations=None):
        self.legs = {"entry": entry, "exit": exit}
        self.active_sides = frozenset(("buy", "sell"))
        self.account_context = None
        self.minimum_shares = minimum_shares
        self.last_exit_fill_bar = None
        self.observations = observations or {}

    @property
    def cells(self):
        return {f"{leg}:{key}": replace(cell, cell_id=f"{leg}:{key}")
                for leg, policy in self.legs.items() for key, cell in policy.cells.items()}

    @property
    def paused(self):
        return all(policy.paused for policy in self.legs.values())

    @paused.setter
    def paused(self, value):
        for policy in self.legs.values():
            policy.paused = value

    @property
    def last_observed(self):
        return max(policy.last_observed for policy in self.legs.values())

    @property
    def unfinished_exit_quantity(self):
        return sum(policy.unfinished_exit_quantity for policy in self.legs.values())

    @property
    def unfinished_holding_exit_quantity(self):
        return sum(policy.unfinished_holding_exit_quantity for policy in self.legs.values())

    def _collect(self, method, *args, **kwargs):
        return tuple(self._wrap(leg, order) for leg in ("exit", "entry")
                     for order in getattr(self.legs[leg], method)(*args, **kwargs))

    def prepare_bar(self, portfolio, bar, index):
        self.account_context = (portfolio, bar)
        return self._collect("prepare_bar", portfolio, bar, index)

    def prepare_session(self, portfolio, session, index):
        return self._collect("prepare_session", portfolio, session, index)

    def before_observe(self, portfolio, bar, index):
        self.account_context = (portfolio, bar)
        return tuple(self._wrap(leg, order) for leg, policy in self.legs.items()
                     for order in (policy.before_observe(portfolio, bar, index) or ()))

    def expire_day(self, next_bar):
        return self._collect("expire_day", next_bar)

    def cancel_for_exit(self):
        return self._collect("cancel_for_exit")

    def after_external_fill(self, **kwargs):
        return self._collect("after_external_fill", **kwargs)

    def after_account_fill(self, fill, *, before, after, index):
        # The filled leg consumes its own ticket via on_fill. Notify only the
        # opposite owner, so a fill never cancels itself before accounting.
        if fill.side.value == "sell":
            self.last_exit_fill_bar = index
        leg = "exit" if fill.side.value == "buy" else "entry"
        return tuple(self._wrap(leg, order) for order in self.legs[leg].after_external_fill(
            side=fill.side.value, before=before, after=after, index=index))

    def release_after_external_exit(self, cell_id, index):
        leg, key = cell_id.split(":", 1)
        order = self.legs[leg].release_after_external_exit(key, index)
        return None if order is None else self._wrap(leg, order)

    def rebase_prices(self, rebase, **kwargs):
        for policy in self.legs.values():
            policy.rebase_prices(rebase, **kwargs)

    @staticmethod
    def _wrap(leg, order):
        return replace(order, cell_id=f"{leg}:{order.cell_id}", order_id=f"{leg}:{order.order_id}")

    def _unwrap(self, order):
        leg, order_id = order.order_id.split(":", 1)
        cell_leg, cell_id = order.cell_id.split(":", 1)
        if leg != cell_leg or leg not in self.legs:
            raise ValueError("order leg identity mismatch")
        return self.legs[leg], replace(order, order_id=order_id, cell_id=cell_id)

    @property
    def pending(self):
        # Exit intents precede entries under the shared ledger's exit priority.
        return {f"{leg}:{key}": self._wrap(leg, order) for leg in ("exit", "entry")
                for key, order in self.legs[leg].pending.items()}

    @pending.setter
    def pending(self, orders):
        # Calendar-only sessions can advance ticket activation without a bar.
        # Decode the updated tickets back to their original policy owners.
        grouped = {leg: {} for leg in self.legs}
        for key, order in orders.items():
            if key != order.cell_id:
                raise ValueError('pending order key does not match its cell')
            _, raw = self._unwrap(order)
            grouped[order.cell_id.split(':', 1)[0]][raw.cell_id] = raw
        for leg, policy in self.legs.items():
            policy.pending = grouped[leg]

    @property
    def remaining(self):
        return {f"{leg}:{key}": quantity for leg, policy in self.legs.items()
                for key, quantity in policy.remaining.items()}

    def observe(self, bar, index, **kwargs):
        orders = []
        for leg in ("exit", "entry"):
            policy = self.legs[leg]
            observed = bar
            if self.observations.get(leg) == 'daily_close':
                if not self.account_context:
                    raise ValueError('daily close observation requires a timestamped source bar')
                source = self.account_context[1]
                if source.ended_at.astimezone(ZoneInfo('Asia/Shanghai')).time() != time(15):
                    continue
                observed = BarPrices(bar.close, bar.close, bar.close, bar.close)
            if leg == "exit" and policy.active_sides == {"sell"} and self.account_context:
                portfolio, source = self.account_context
                available = (portfolio.position_quantity(source.session.instrument_id).value
                    if self.observations.get(leg) == 'daily_close' else
                    portfolio.sellable_quantity(source.session.instrument_id, source.session.session_date).value)
                if available <= self.minimum_shares:
                    continue
            orders.extend(self._wrap(leg, order) for order in policy.observe(observed, index, **kwargs))
        return tuple(orders)

    def on_fill(self, order_id, quantity, index, *, price=None):
        leg, raw_id = order_id.split(":", 1)
        self.legs[leg].on_fill(raw_id, quantity, index, price=price)

    def reject(self, order):
        policy, raw = self._unwrap(order)
        policy.reject(raw)

    def refresh_order(self, order, portfolio, bar):
        policy, raw = self._unwrap(order)
        updated = policy.refresh_order(raw, portfolio, bar)
        return None if updated is None else self._wrap(order.order_id.split(":", 1)[0], updated)

    def signal_details(self, order):
        policy, raw = self._unwrap(order)
        return policy.signal_details(raw)

    def retry_failure(self, order, reason):
        policy, raw = self._unwrap(order)
        return policy.retry_failure(raw, reason)

    def can_match(self, order, bar, index):
        policy, raw = self._unwrap(order)
        if self.observations.get(order.order_id.split(':', 1)[0]) == 'daily_close':
            if (not (raw.limit_price is not None and getattr(policy, 'daily_limit_intraday', False))
                    and bar.ended_at.astimezone(ZoneInfo('Asia/Shanghai')).time() != time(9, 31)):
                return False
        if raw.side == "buy" and self.last_exit_fill_bar == index:
            return False
        return policy.pending.get(raw.cell_id) == raw and policy.can_match(raw, bar, index)

    def matching_prices(self, order, bar):
        if self.observations.get(order.order_id.split(':', 1)[0]) == 'daily_close':
            policy, raw = self._unwrap(order)
            if raw.limit_price is not None and getattr(policy, 'daily_limit_intraday', False):
                return bar.prices
            price = bar.prices.open
            return BarPrices(price, price, price, price)
        return bar.prices
