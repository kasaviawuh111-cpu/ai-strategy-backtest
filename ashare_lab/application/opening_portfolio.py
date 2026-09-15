"""Create sourced opening inventory without fabricating a first-day BUY."""

from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from ashare_lab.application.minute_grid_plan import MinuteGridCapabilityError
from ashare_lab.domain.portfolio import PortfolioState, PositionLot
from ashare_lab.domain.shared import FillId, Money, Quantity


def opening_portfolio(params, prepared, *, first_price: Decimal):
    if not params.opening_shares:
        return PortfolioState(Money(params.initial_cash_cny)), None
    first_bar = prepared.bars[0]
    return initialize_opening_portfolio(
        params, first_price=first_price, first_day=first_bar.session.session_date,
        instrument_id=first_bar.session.instrument_id, market_sessions=prepared.market_sessions,
    )


def initialize_opening_portfolio(params, *, first_price: Decimal, first_day: date,
                                 instrument_id, market_sessions: tuple[date, ...]):
    """Shared cash/inventory seed for daily and intraday execution adapters."""
    if not params.opening_shares:
        return PortfolioState(Money(params.initial_cash_cny)), None
    previous = next((day for day in reversed(market_sessions) if day < first_day), None)
    if previous is None:
        raise MinuteGridCapabilityError("opening_holding_prior_session_missing")
    mark = first_price * params.opening_shares
    if params.initial_capital_scope == "total_equity":
        if mark > params.initial_cash_cny:
            raise MinuteGridCapabilityError("opening_holding_exceeds_initial_equity")
        cash, equity = params.initial_cash_cny - mark, params.initial_cash_cny
    else:
        cash, equity = params.initial_cash_cny, params.initial_cash_cny + mark
    acquired_at = datetime.combine(previous, time(15), ZoneInfo("Asia/Shanghai"))
    lot = PositionLot(
        FillId("opening-import:declared-position"),
        instrument_id,
        acquired_at,
        previous,
        first_day,
        Quantity(params.opening_shares),
        Money(mark),
        None,
    )
    return PortfolioState(Money(cash), lots=(lot,)), equity
