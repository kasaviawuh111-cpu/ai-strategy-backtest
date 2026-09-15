"""Phase-one OHLC price matching, independent of cash/settlement eligibility.

The caller must only pass bars for which the order was already effective at
the start. A touched price is not proof of inventory, cash or queue access.
"""
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from ashare_lab.domain.execution.matching import DailyBarMatchingModel
from ashare_lab.domain.orders import OrderSide
from ashare_lab.domain.shared import Price


@dataclass(frozen=True)
class BarPrices:
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    def __post_init__(self) -> None:
        values = (self.open, self.high, self.low, self.close)
        if not all(v.is_finite() and v > 0 for v in values):
            raise ValueError("OHLC must be finite and positive")
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError("inconsistent OHLC")


def match_bar_price(bar: BarPrices, *, side: OrderSide,
                    limit: Decimal | None = None,
                    observation: Literal["bar", "open", "close"] = "bar",
                    slippage_bps: Decimal = Decimal(5),
                    slippage_cny: Decimal = Decimal(0),
                    tick: Decimal = Decimal("0.01")) -> Decimal | None:
    """Return a price bounded by both the bar and any order limit.

    `open`/`close` are scheduled attempts; `bar` allows an already-working
    limit order to use H/L, with price improvement at the opening observation.
    """
    if observation not in {"bar", "open", "close"}:
        raise ValueError("unknown observation")
    if side not in {OrderSide.BUY, OrderSide.SELL}:
        raise ValueError("unknown side")
    if (not slippage_bps.is_finite() or not 0 <= slippage_bps <= 1000
            or not slippage_cny.is_finite() or slippage_cny < 0
            or not tick.is_finite() or tick <= 0):
        raise ValueError("invalid execution friction")
    if limit is not None and (not limit.is_finite() or limit <= 0):
        raise ValueError("invalid limit price")
    base = bar.close if observation == "close" else bar.open
    buy = side == OrderSide.BUY
    if limit is not None:
        marketable = base <= limit if buy else base >= limit
        if not marketable:
            touched = bar.low <= limit if buy else bar.high >= limit
            if observation != "bar" or not touched:
                return None
            base = limit
    price = DailyBarMatchingModel.price_with_slippage(
        Price(base), side=side, slippage_bps=slippage_bps,
        slippage_cny=slippage_cny, tick=tick,
        price_floor=bar.low, price_ceiling=bar.high,
    ).amount
    if limit is not None:
        price = min(price, limit) if buy else max(price, limit)
    return price


def protective_exit(bar: BarPrices, *, take_profit: Decimal | None,
                    stop_loss: Decimal | None) -> tuple[str | None, bool]:
    """Choose an already-armed long-position exit; report ambiguous H/L order."""
    if take_profit is not None and bar.open >= take_profit:
        return "take_profit", False
    if stop_loss is not None and bar.open <= stop_loss:
        return "stop_loss", False
    profit = take_profit is not None and bar.high >= take_profit
    loss = stop_loss is not None and bar.low <= stop_loss
    if loss:
        return "stop_loss", profit
    return ("take_profit", False) if profit else (None, False)
