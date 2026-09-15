"""Daily target-position grid intents on the shared minute execution ledger."""
from datetime import time
from decimal import Decimal
from zoneinfo import ZoneInfo

from ashare_lab.application.fixed_grid_orders import FixedGridOrders, CellOrder
from ashare_lab.application.grid_strategy import (
    grid_order, advance_grid_fill, _rebase_grid_parameters, rebase_price_order,
)
from ashare_lab.domain.strategy.price_plans import GridSpecificationError


class DailyGridOrders(FixedGridOrders):
    # Daily limit tickets may match any later minute of their execution day.
    daily_limit_intraday = True

    def __init__(self, parameters, *, first_open, side):
        super().__init__([], active_sides=(side,))
        if parameters.anchor_mode == 'latest_price' and parameters.anchor_price is None:
            raise GridSpecificationError('行情最新价尚未取得，请先加载最新行情基准价')
        if parameters.anchor_mode == 'first_open':
            if not parameters.lower_price <= first_open <= parameters.upper_price:
                raise GridSpecificationError('起始开盘价不在网格范围内')
            parameters = parameters.model_copy(update={'anchor_price': first_open})
        # Do not apply the minute-grid gate: daily grids already support the
        # actual-last-fill anchor and must retain that semantics in a pair.
        self.parameters = parameters.resolve_geometry()
        self.side = side
        self.startup_distance = self.parameters.distance(first_open)
        self.baseline = None if parameters.startup_mode == 'wait_for_crossing' else Decimal(0)
        self.units = Decimal(0)
        self.reference = first_open if self.baseline is None else self.parameters.resolved_anchor
        self.source = None
        self.intents = {}

    def before_observe(self, portfolio, bar, index):
        self.source = bar
        if bar.ended_at.astimezone(ZoneInfo('Asia/Shanghai')).time() != time(15):
            return ()
        # Matching has already consumed the last minute. Re-plan remaining
        # demand from actual fills at this close, like the original daily loop.
        expired = tuple(self.pending.values())
        self.pending.clear()
        self.remaining.clear()
        self.intents.clear()
        return expired

    def observe(self, bar, index, **kwargs):
        if index <= self.last_observed:
            raise ValueError('bars must be observed once in increasing order')
        self.last_observed = index
        if self.paused or self.source is None:
            return ()
        intent = grid_order(self.parameters, price=bar.close,
            session=self.source.session.session_date, board=self.source.session.board,
            filled_units=self.units, baseline_units=self.baseline or Decimal(0),
            startup_distance=self.startup_distance if self.baseline is None else None,
            reference_price=self.reference)
        if intent is None or intent.side != self.side:
            return ()
        self.serial += 1
        key = f'daily-grid:{self.serial}'
        order = CellOrder(key, key, self.side, intent.quantity, intent.limit_price, index, index + 1)
        self.pending[key] = order
        self.remaining[key] = order.quantity
        self.intents[key] = intent
        return (order,)

    def on_fill(self, order_id, quantity, index, *, price=None):
        order = self.pending.get(order_id)
        if order is None or index < order.effective_bar or not 0 < quantity <= self.remaining[order_id]:
            raise ValueError('invalid daily grid fill')
        if price is None:
            raise ValueError('actual fill price is required')
        self.parameters, self.baseline, self.units, self.reference = advance_grid_fill(
            self.parameters, self.intents[order_id], quantity=quantity, price=price,
            filled_units=self.units, reference_price=self.reference)
        self.remaining[order_id] -= quantity
        if not self.remaining[order_id]:
            self.pending.pop(order_id)
            self.remaining.pop(order_id)
            self.intents.pop(order_id)

    def after_external_fill(self, *, side, before, after, index):
        reset = (self.side == 'buy' and side == 'sell' and before > 0 and after == 0
                 or self.side == 'sell' and side == 'buy' and before == 0 and after > 0)
        if not reset:
            return ()
        cancelled = tuple(self.pending.values())
        self.pending.clear()
        self.remaining.clear()
        self.intents.clear()
        self.units = Decimal(0)
        self.baseline = None if self.parameters.startup_mode == 'wait_for_crossing' else Decimal(0)
        self.reference = self.parameters.resolved_anchor
        return cancelled

    def expire_day(self, next_bar, *, rebuild=True):
        if next_bar is not None:
            return ()  # close-time re-planning has already expired today's tickets
        expired = tuple(self.pending.values())
        self.pending.clear()
        self.remaining.clear()
        self.intents.clear()
        return expired

    def signal_details(self, order):
        return 'daily_grid_close_target', False

    def rebase_prices(self, rebase, **kwargs):
        super().rebase_prices(rebase, **kwargs)
        self.parameters = _rebase_grid_parameters(self.parameters, rebase)
        self.reference *= rebase.factor
        self.intents = {key: rebase_price_order(intent, rebase) for key, intent in self.intents.items()}
