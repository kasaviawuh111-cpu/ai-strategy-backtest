"""Known-before-open orders and budget sizing using the shared account ledger."""
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from ashare_lab.application.trading_schedule import investment_schedule

from ashare_lab.domain.execution.bar_prices import BarPrices, match_bar_price
from ashare_lab.domain.execution.fees import FeeCalculator
from ashare_lab.domain.market_data import InstrumentSession, TradingStatus
from ashare_lab.domain.orders import OrderSide
from ashare_lab.domain.portfolio import PortfolioState, FillRecord, apply_buy, apply_sell, InsufficientCashError, InsufficientSellableQuantityError
from ashare_lab.domain.shared import FillId, OrderId, Price, Quantity, require_aware


@dataclass(frozen=True)
class ScheduledOrder:
    order_id: str
    session_date: date
    known_at: datetime
    side: OrderSide = OrderSide.BUY
    at: Literal["open", "close"] = "open"
    quantity: int | None = None
    budget: Decimal | None = None
    limit_price: Decimal | None = None

    def __post_init__(self) -> None:
        require_aware(self.known_at, "known_at")
        opening = datetime.combine(self.session_date, time(9, 30), tzinfo=ZoneInfo("Asia/Shanghai"))
        if self.known_at > opening or not self.order_id or self.at not in {"open", "close"}:
            raise ValueError("scheduled order must be known before session open")
        if not isinstance(self.side, OrderSide) or (self.quantity is None) == (self.budget is None):
            raise ValueError("scheduled order requires side and exactly one size")
        if self.quantity is not None and (type(self.quantity) is not int or self.quantity <= 0):
            raise ValueError("invalid scheduled quantity")
        if self.budget is not None and (self.side is not OrderSide.BUY or not self.budget.is_finite() or self.budget <= 0):
            raise ValueError("invalid buy budget")
        if self.limit_price is not None and (not self.limit_price.is_finite() or self.limit_price <= 0):
            raise ValueError("invalid scheduled limit")


def investment_orders(*, sessions: tuple[date, ...], start: date, end: date,
                      frequency: Literal["weekly", "monthly"], day: int, budget: Decimal,
                      known_at: datetime, at: Literal["open", "close"] = "open") -> tuple[ScheduledOrder, ...]:
    """Convert merged calendar budgets into single, non-catch-up tickets."""
    planned = investment_schedule(sessions=sessions, start=start, end=end,
                                  frequency=frequency, day=day, budget=budget)
    return tuple(ScheduledOrder(f"investment:{target.isoformat()}", target, known_at,
                                budget=amount, at=at) for target, amount in planned.items())


def execute_scheduled(order: ScheduledOrder, *, portfolio: PortfolioState,
                      prices: BarPrices, session: InstrumentSession, ended_at: datetime,
                      next_session: date, fees: FeeCalculator, slippage_bps: Decimal,
                      slippage_cny: Decimal, max_quantity: int | None = None,
                      allow_adverse_limit_volume: bool = False,
                      fill_id_suffix: str = "",
                      sizing_details: dict[str, str | int] | None = None) -> tuple[PortfolioState, FillRecord | None, str]:
    """Caller enforces activation. An unhit limit remains working until DAY end."""
    if session.session_date != order.session_date:
        raise ValueError("scheduled order session mismatch")
    if session.status is not TradingStatus.TRADING:
        return portfolio, None, "security_not_trading"
    mode = "close" if order.at == "close" else "bar" if order.limit_price is not None else "open"
    value = match_bar_price(prices, side=order.side, observation=mode, limit=order.limit_price,
                            slippage_bps=slippage_bps, slippage_cny=slippage_cny, tick=session.price_tick)
    if value is None:
        return portfolio, None, "limit_not_reached"
    if (not allow_adverse_limit_volume and order.side is OrderSide.BUY
            and session.upper_limit is not None and value >= session.upper_limit.amount):
        return portfolio, None, "buy_at_upper_limit"
    if (not allow_adverse_limit_volume and order.side is OrderSide.SELL
            and session.lower_limit is not None and value <= session.lower_limit.amount):
        return portfolio, None, "sell_at_lower_limit"
    price = Price(value)
    size = order.quantity
    if order.budget is not None:
        if sizing_details is not None:
            minimum = session.minimum_buy_quantity
            minimum_fees = fees.calculate(side=order.side, price=price, quantity=Quantity(minimum),
                                          trade_date=session.session_date)
            sizing_details.update(
                budgetCny=str(order.budget), availableCashCny=str(portfolio.cash.amount),
                minimumOrderQuantity=minimum, sizingPrice=str(value),
                minimumOrderCostCny=str(value * minimum + minimum_fees.total.amount),
            )
        size = session.floor_buy_quantity(int(order.budget / value))
        while size:
            fee = fees.calculate(side=order.side, price=price, quantity=Quantity(size), trade_date=session.session_date)
            if value * size + fee.total.amount <= order.budget:
                break
            size = session.previous_buy_quantity(size)
        if not size:
            return portfolio, None, "budget_below_minimum_order"
    # Lot rules constrain the submitted order, not each execution fragment.
    # A valid 100-share order may, for example, receive a 57-share fill.
    if order.side is OrderSide.BUY and not session.is_valid_buy_quantity(size):
        return portfolio, None, "invalid_buy_quantity"
    requested_size = size
    if sizing_details is not None:
        sizing_details["requestedQuantity"] = requested_size
    if max_quantity is not None:
        if max_quantity < 0:
            raise ValueError("maximum execution quantity cannot be negative")
        size = min(size, max_quantity)
        if not size:
            return portfolio, None, "participation_capacity_zero"
    quantity = Quantity(size)
    if sizing_details is not None:
        sizing_details["capacityLimitedQuantity"] = size
    fill = FillRecord(FillId(order.order_id + ":fill" + fill_id_suffix), OrderId(order.order_id), session.instrument_id,
                      order.side, quantity, price, ended_at,
                      fees.calculate(side=order.side, price=price, quantity=quantity, trade_date=session.session_date))
    try:
        updated = (apply_buy(portfolio, fill, sellable_on=next_session)
                   if order.side is OrderSide.BUY else apply_sell(portfolio, fill))
    except InsufficientCashError:
        return portfolio, None, "insufficient_cash_including_fees"
    except InsufficientSellableQuantityError:
        return portfolio, None, ("t_plus_one_locked" if portfolio.position_quantity(session.instrument_id).value >= size
                                 else "insufficient_position")
    return updated, fill, ("participation_partial_fill" if size < requested_size else "scheduled_" + order.at)
