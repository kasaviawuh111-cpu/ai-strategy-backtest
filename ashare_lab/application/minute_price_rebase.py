"""Source-owned, point-in-time price-domain conversion at an ex-date boundary."""
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
import re
from zoneinfo import ZoneInfo
from dataclasses import replace

from ashare_lab.domain.shared import InstrumentId, require_aware


def dynamic_front_adjusted_bars(bars, *, price_rebases, as_of):
    """Rebase a raw historical prefix to its decision-date price domain.

    Caller supplies a complete, verified event/factor source. This function
    transforms it; an empty sequence does not certify no corporate actions.
    Volume and turnover remain provider observations, not adjusted share counts.
    """
    from ashare_lab.domain.market_data import PriceBasis
    from ashare_lab.domain.shared import Price
    require_aware(as_of, "signal as_of")
    if not bars:
        return ()
    instrument = bars[0].instrument_id
    if any(bar.instrument_id != instrument or bar.price_basis is not PriceBasis.UNADJUSTED
           or bar.available_at > as_of for bar in bars):
        raise ValueError("dynamic signal input requires available raw bars for one instrument")
    if any(a.session_date >= b.session_date for a, b in zip(bars, bars[1:])):
        raise ValueError("dynamic signal dates must be strictly ordered")
    if len({item.ex_date for item in price_rebases}) != len(price_rebases) or any(
            item.instrument_id != instrument for item in price_rebases):
        raise ValueError("dynamic signal factors must be unique and match the instrument")
    decision_day = as_of.astimezone(ZoneInfo("Asia/Shanghai")).date()
    active = [item for item in price_rebases if item.ex_date <= decision_day and item.available_at <= as_of]
    result = []
    for bar in bars:
        factor = Decimal(1)
        for item in active:
            if bar.session_date < item.ex_date:
                factor *= item.factor
        # Indicator inputs retain decimal precision; order price tick rounding
        # belongs only to the execution/limit-price conversion.
        result.append(replace(bar, open=Price(bar.open.amount * factor), high=Price(bar.high.amount * factor),
            low=Price(bar.low.amount * factor), close=Price(bar.close.amount * factor),
            price_basis=PriceBasis.DYNAMIC_FRONT_ADJUSTED))
    return tuple(result)


@dataclass(frozen=True)
class MinutePriceRebase:
    instrument_id: InstrumentId
    ex_date: date
    factor: Decimal
    available_at: datetime
    source_sha256: str

    def __post_init__(self):
        require_aware(self.available_at, "price rebase available_at")
        if not self.factor.is_finite() or self.factor <= 0:
            raise ValueError("price rebase factor must be positive")
        if self.available_at > datetime.combine(self.ex_date, time(9, 30), tzinfo=ZoneInfo("Asia/Shanghai")):
            raise ValueError("price rebase was not known before the ex-date open")
        if not re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", self.source_sha256):
            raise ValueError("price rebase requires a source hash")

    def price(self, value: Decimal | None, side: str, tick: Decimal = Decimal(".01")) -> Decimal | None:
        if value is None:
            return None
        rounding = ROUND_FLOOR if side == "buy" else ROUND_CEILING
        result = (value * self.factor / tick).to_integral_value(rounding=rounding) * tick
        if result <= 0:
            raise ValueError("rebased price falls below one tick")
        return result
