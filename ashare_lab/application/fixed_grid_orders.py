"""Independent fixed-grid order lifecycle for completed-bar replay.

Account checks and fills belong to the execution ledger. This policy never
nets different cells or treats a trigger as a fill. Bar indices enumerate
actual trading bars (no invented lunch-break bars).
"""
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Literal

from ashare_lab.domain.execution.bar_prices import BarPrices
from ashare_lab.application.minute_price_rebase import MinutePriceRebase


@dataclass(frozen=True)
class GridCell:
    cell_id: str
    buy_price: Decimal
    sell_price: Decimal
    quantity: int
    first_side: Literal["buy", "sell"] = "buy"
    order_type: Literal["grid_limit", "market", "fixed_limit"] = "grid_limit"
    buy_limit: Decimal | None = None
    sell_limit: Decimal | None = None
    amount_cny: Decimal | None = None

    def __post_init__(self) -> None:
        if (not self.buy_price.is_finite() or not self.sell_price.is_finite()
                or not 0 < self.buy_price < self.sell_price or self.quantity <= 0
                or type(self.quantity) is not int or self.first_side not in {"buy", "sell"}):
            raise ValueError("invalid grid cell")
        if self.order_type not in {"grid_limit", "market", "fixed_limit"}:
            raise ValueError("invalid grid order type")
        if self.order_type == "fixed_limit" and any(
            p is None or not p.is_finite() or p <= 0 for p in (self.buy_limit, self.sell_limit)
        ):
            raise ValueError("fixed grid limits must be positive")
        if self.amount_cny is not None and (not self.amount_cny.is_finite() or self.amount_cny <= 0):
            raise ValueError("grid amount must be positive")


@dataclass(frozen=True)
class CellOrder:
    order_id: str
    cell_id: str
    side: Literal["buy", "sell"]
    quantity: int
    limit_price: Decimal | None
    signal_bar: int
    effective_bar: int


class FixedGridOrders:
    def __init__(self, cells: list[GridCell], *, wait_for_crossing: bool = False,
                 active_sides: tuple[str, ...] = ("buy", "sell")) -> None:
        if not active_sides or len(set(active_sides)) != len(active_sides) or set(active_sides) - {"buy", "sell"}:
            raise ValueError("grid active sides must be unique buy/sell roles")
        self.active_sides = frozenset(active_sides)
        if len({c.cell_id for c in cells}) != len(cells):
            raise ValueError("grid cell IDs must be unique")
        self.cells = {c.cell_id: c for c in cells}
        self.sides = {c.cell_id: c.first_side for c in cells}
        self.armed_from = {c.cell_id: 0 for c in cells}
        self.pending: dict[str, CellOrder] = {}
        self.remaining: dict[str, int] = {}
        self.serial = 0
        self.paused = False
        self.last_observed = -1
        self.rejected: set[str] = set()
        self.wait_for_crossing = wait_for_crossing
        self.startup_reset: set[str] = set()
        self.unfinished_exit_quantity = 0
        self.unfinished_holding_exit_quantity = 0

    def rebase_prices(self, rebase: MinutePriceRebase, *, tick: Decimal = Decimal(".01")) -> None:
        """Convert prices without resetting cell state, quantities or order clocks."""
        cells = {key: replace(cell, buy_price=rebase.price(cell.buy_price, "buy", tick),
                              sell_price=rebase.price(cell.sell_price, "sell", tick),
                              buy_limit=rebase.price(cell.buy_limit, "buy", tick),
                              sell_limit=rebase.price(cell.sell_limit, "sell", tick))
                 for key, cell in self.cells.items()}
        pending = {key: replace(order, limit_price=rebase.price(order.limit_price, order.side, tick))
                   for key, order in self.pending.items()}
        self.cells, self.pending = cells, pending

    def prepare_bar(self, portfolio, bar, index: int) -> tuple[CellOrder, ...]:
        """Account-aware policies capture only state known before bar matching."""
        return ()

    def before_observe(self, portfolio, bar, index: int) -> None:
        """Refresh account state after matching, before new close-time signals."""

    def prepare_session(self, portfolio, session, index: int) -> tuple[CellOrder, ...]:
        """Calendar opening without a price observation (for suspended days)."""
        return ()

    def can_match(self, order: CellOrder, bar, index: int) -> bool:
        return True

    def refresh_order(self, order: CellOrder, portfolio, bar) -> CellOrder | None:
        return order

    def retry_failure(self, order: CellOrder, reason: str) -> bool:
        return False

    def signal_details(self, order: CellOrder) -> tuple[str, bool]:
        return "grid_cell_trigger", False

    def observe(self, bar: BarPrices, index: int, *, minimum_quantity: int = 100,
                quantity_increment: int = 100) -> tuple[CellOrder, ...]:
        if index <= self.last_observed:
            raise ValueError("bars must be observed once in increasing order")
        first = self.last_observed == -1
        self.last_observed = index
        if self.paused:
            return ()
        orders = []
        for key, cell in self.cells.items():
            if key in self.rejected or key in self.pending or index < self.armed_from[key]:
                continue
            side = self.sides[key]
            if side not in self.active_sides:
                continue
            price = cell.buy_price if side == "buy" else cell.sell_price
            if first and self.wait_for_crossing and (bar.open <= price if side == "buy" else bar.open >= price):
                self.startup_reset.add(key)
            if key in self.startup_reset:
                if bar.close > price if side == "buy" else bar.close < price:
                    self.startup_reset.remove(key)
                continue
            touched = bar.low <= price if side == "buy" else bar.high >= price
            if not touched:
                continue
            self.serial += 1
            limit = (None if cell.order_type == "market" else
                     (cell.buy_limit if side == "buy" else cell.sell_limit)
                     if cell.order_type == "fixed_limit" else price)
            quantity = cell.quantity
            if cell.amount_cny is not None:
                maximum = int(cell.amount_cny / price)
                quantity = (minimum_quantity + (maximum - minimum_quantity) // quantity_increment * quantity_increment
                            if maximum >= minimum_quantity else 0)
            order = CellOrder(f"grid:{key}:{self.serial}", key, side,
                              self.remaining.get(key, quantity), limit, index, index + 1)
            self.pending[key] = order
            self.remaining[key] = order.quantity
            orders.append(order)
        return tuple(orders)

    def on_fill(self, order_id: str, quantity: int, index: int, *, price: Decimal | None = None) -> None:
        order = next((o for o in self.pending.values() if o.order_id == order_id), None)
        if order is None:
            raise ValueError("unknown working order")
        if index < order.effective_bar or type(quantity) is not int or not 0 < quantity <= self.remaining[order.cell_id]:
            raise ValueError("invalid fill quantity or time")
        key = order.cell_id
        self.remaining[key] -= quantity
        if self.remaining[key] == 0:
            del self.pending[key]
            del self.remaining[key]
            self.sides[key] = "sell" if order.side == "buy" else "buy"
            self.armed_from[key] = index + 1

    def cancel_for_exit(self) -> tuple[CellOrder, ...]:
        """Caller invokes at the exit takeover boundary before matching grid orders."""
        self.paused = True
        cancelled = tuple(self.pending.values())
        self.pending.clear()
        return cancelled

    def reject(self, order: CellOrder) -> None:
        """End a failed intent without repeatedly emitting it on every bar."""
        if self.pending.get(order.cell_id) != order:
            raise ValueError("unknown working order")
        del self.pending[order.cell_id]
        self.remaining.pop(order.cell_id, None)
        self.rejected.add(order.cell_id)

    def release_after_external_exit(self, cell_id: str, index: int) -> CellOrder | None:
        """A lot-level scheduled exit closes this cell's sell leg as well."""
        if cell_id not in self.cells:
            raise ValueError("unknown grid cell")
        cancelled = self.pending.pop(cell_id, None)
        self.remaining.pop(cell_id, None)
        self.rejected.discard(cell_id)
        self.sides[cell_id] = "buy"
        self.armed_from[cell_id] = index + 1
        return cancelled

    def after_external_fill(self, *, side: str, before: int, after: int, index: int) -> tuple[CellOrder, ...]:
        """Rearm single-sided cells only at a real external position transition."""
        reset_side = ("buy" if side == "sell" and before > 0 and after == 0 else
                      "sell" if side == "buy" and before == 0 and after > 0 else None)
        if reset_side is None or self.active_sides != {reset_side}:
            return ()
        cancelled = tuple(self.pending.values())
        self.pending.clear()
        self.remaining.clear()
        self.paused = False
        for key in self.cells:
            self.rejected.discard(key)
            self.sides[key] = reset_side
            self.armed_from[key] = index + 1
        return cancelled

    def expire_day(self, next_bar: int | None, *, rebuild: bool = True) -> tuple[CellOrder, ...]:
        """Expire DAY tickets; carry outstanding intent/quantity into the next session.

        None is the end-of-backtest boundary: no invented liquidation/reissue.
        """
        expired = tuple(order for order in self.pending.values()
                        if next_bar is None or order.effective_bar < next_bar)
        self.pending = {key: order for key, order in self.pending.items() if order not in expired}
        if next_bar is not None and not self.paused and rebuild:
            for old in expired:
                self.serial += 1
                self.pending[old.cell_id] = CellOrder(
                    f"grid:{old.cell_id}:{self.serial}", old.cell_id, old.side,
                    self.remaining[old.cell_id], old.limit_price, old.signal_bar, next_bar,
                )
        return expired
