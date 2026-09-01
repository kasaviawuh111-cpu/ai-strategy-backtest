"""Versioned fallback rules for historical A-share daily sessions.

Production snapshots should carry exchange/vendor reference limits per security
and date.  This rule book is a deterministic fallback for research data that
does not contain those fields; its derived provenance must be recorded in the
run manifest.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from ashare_lab.domain.market_data import (
    Board,
    InstrumentSession,
    TradingStatus,
    standard_buy_quantity_rule,
)
from ashare_lab.domain.shared import DomainValidationError, InstrumentId, Price


@dataclass(frozen=True, slots=True)
class PriceLimitRuleInput:
    instrument_id: InstrumentId
    session_date: date
    board: Board
    status: TradingStatus
    previous_close: Price
    listing_date: date
    listing_session_number: int
    is_st: bool = False

    def __post_init__(self) -> None:
        if self.listing_date > self.session_date:
            raise DomainValidationError("listing_date cannot be after session_date")
        if self.listing_session_number < 1:
            raise DomainValidationError("listing_session_number must be at least one")


class HistoricalAshareRuleBook:
    """Derive daily limits and buy declaration rules for release ``v2``."""

    version = "cn_a.daily_market_rules.fallback.v2"
    _CHINEXT_REFORM = date(2020, 8, 24)
    _MAIN_REGISTRATION_FIRST_LISTINGS = date(2023, 4, 10)

    def build_session(self, item: PriceLimitRuleInput) -> InstrumentSession:
        upper_ratio, lower_ratio = self._ratios(item)
        tick = Decimal("0.001") if item.board is Board.STOCK_ETF else Decimal("0.01")
        if upper_ratio is None or lower_ratio is None:
            upper_limit = None
            lower_limit = None
        else:
            upper_limit = Price(
                (item.previous_close.amount * (Decimal("1") + upper_ratio)).quantize(
                    tick, rounding=ROUND_HALF_UP
                ),
                item.previous_close.currency,
            )
            lower_limit = Price(
                (item.previous_close.amount * (Decimal("1") - lower_ratio)).quantize(
                    tick, rounding=ROUND_HALF_UP
                ),
                item.previous_close.currency,
            )

        minimum_buy_quantity, buy_quantity_increment = standard_buy_quantity_rule(item.board)
        return InstrumentSession(
            instrument_id=item.instrument_id,
            session_date=item.session_date,
            board=item.board,
            status=item.status,
            previous_close=item.previous_close,
            upper_limit=upper_limit,
            lower_limit=lower_limit,
            minimum_buy_quantity=minimum_buy_quantity,
            buy_quantity_increment=buy_quantity_increment,
            price_tick=tick,
            is_st=item.is_st,
        )

    def _ratios(self, item: PriceLimitRuleInput) -> tuple[Decimal | None, Decimal | None]:
        if item.board is Board.STOCK_ETF:
            if item.is_st:
                raise DomainValidationError("stock_etf cannot use stock ST rules")
            return Decimal("0.10"), Decimal("0.10")

        if item.board is Board.STAR:
            if item.listing_session_number <= 5:
                return None, None
            return Decimal("0.20"), Decimal("0.20")

        if item.board is Board.CHINEXT:
            if item.listing_date >= self._CHINEXT_REFORM and item.listing_session_number <= 5:
                return None, None
            if item.session_date >= self._CHINEXT_REFORM:
                return Decimal("0.20"), Decimal("0.20")
            ratio = Decimal("0.05") if item.is_st else Decimal("0.10")
            return ratio, ratio

        if item.board is Board.BSE:
            if item.listing_session_number == 1:
                return None, None
            return Decimal("0.30"), Decimal("0.30")

        if (
            item.listing_date >= self._MAIN_REGISTRATION_FIRST_LISTINGS
            and item.listing_session_number <= 5
        ):
            return None, None
        if (
            item.listing_date < self._MAIN_REGISTRATION_FIRST_LISTINGS
            and item.listing_session_number == 1
        ):
            return Decimal("0.44"), Decimal("0.36")
        ratio = Decimal("0.05") if item.is_st else Decimal("0.10")
        return ratio, ratio
