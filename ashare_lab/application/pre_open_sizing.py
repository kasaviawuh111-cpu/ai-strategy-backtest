"""Point-in-time-safe sizing for A-share pre-open BUY orders."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

from ashare_lab.domain.market_data import InstrumentSession
from ashare_lab.domain.orders import OrderSide
from ashare_lab.domain.portfolio import FeeBreakdown
from ashare_lab.domain.shared import Money, Price, Quantity

PRE_OPEN_BUY_SIZING_UPPER_LIMIT_UNAVAILABLE = "pre_open_buy_sizing_upper_limit_unavailable"


class FeeQuoteProvider(Protocol):
    def calculate(
        self,
        *,
        side: OrderSide,
        price: Price,
        quantity: Quantity,
        trade_date: date,
    ) -> FeeBreakdown: ...


@dataclass(frozen=True, slots=True)
class PreOpenBuySizing:
    """A quantity and price bound knowable before the opening print."""

    affordability_price: Price
    quantity: Quantity


def size_pre_open_buy(
    *,
    cash: Money,
    session: InstrumentSession,
    allocation_ratio: Decimal,
    fee_calculator: FeeQuoteProvider,
    trading_date: date,
) -> PreOpenBuySizing | None:
    """Size against the legal worst-case BUY price known before the open.

    The daily matcher caps adverse BUY slippage at the session's exchange upper
    limit.  That upper limit is therefore the maximum legally executable price
    for the session.  Quoting both notional and fees at that price reserves
    enough cash for every allowed slippage outcome without reading the future
    opening print.  Sessions without a price band (for example, some IPO
    sessions) cannot be sized safely from daily data and fail closed.
    """

    affordability_price = session.upper_limit
    if affordability_price is None:
        return None

    budget = cash.amount * allocation_ratio
    shares = session.floor_buy_quantity(int(budget / affordability_price.amount))
    while shares > 0:
        quantity = Quantity(shares)
        fees = fee_calculator.calculate(
            side=OrderSide.BUY,
            price=affordability_price,
            quantity=quantity,
            trade_date=trading_date,
        )
        if affordability_price.amount * Decimal(shares) + fees.total.amount <= budget:
            return PreOpenBuySizing(
                affordability_price=affordability_price,
                quantity=quantity,
            )
        shares = session.previous_buy_quantity(shares)
    return PreOpenBuySizing(
        affordability_price=affordability_price,
        quantity=Quantity.zero(),
    )
