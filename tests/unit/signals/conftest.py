from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from ashare_lab.domain.market_data import DailyBar
from ashare_lab.domain.shared import InstrumentId, Price, Quantity

SHANGHAI = ZoneInfo("Asia/Shanghai")


def make_bars(
    closes: Sequence[int | str | Decimal],
    *,
    opens: Sequence[int | str | Decimal] | None = None,
    highs: Sequence[int | str | Decimal] | None = None,
    lows: Sequence[int | str | Decimal] | None = None,
    volumes: Sequence[int] | None = None,
    amounts: Sequence[int | str | Decimal] | None = None,
    symbol: str = "300059.SZ",
    first_available_delay_days: int = 0,
) -> tuple[DailyBar, ...]:
    opens = closes if opens is None else opens
    highs = closes if highs is None else highs
    lows = closes if lows is None else lows
    if volumes is None:
        volumes = [100] * len(closes)
    if amounts is None:
        amounts = [
            _decimal(close) * Decimal(volume) for close, volume in zip(closes, volumes, strict=True)
        ]
    assert len(closes) == len(opens) == len(highs) == len(lows) == len(volumes) == len(amounts)
    start = date(2024, 1, 2)
    bars: list[DailyBar] = []
    for index, (raw_open, raw_high, raw_low, raw_close, volume, raw_amount) in enumerate(
        zip(opens, highs, lows, closes, volumes, amounts, strict=True)
    ):
        session_date = start + timedelta(days=index)
        open_amount = _decimal(raw_open)
        high_amount = _decimal(raw_high)
        low_amount = _decimal(raw_low)
        close_amount = _decimal(raw_close)
        turnover = _decimal(raw_amount)
        available_date = session_date
        if index == 0:
            available_date += timedelta(days=first_available_delay_days)
        available_at = datetime.combine(
            available_date,
            time(hour=15, minute=1),
            tzinfo=SHANGHAI,
        )
        bars.append(
            DailyBar(
                instrument_id=InstrumentId(symbol),
                session_date=session_date,
                open=Price(open_amount),
                high=Price(high_amount),
                low=Price(low_amount),
                close=Price(close_amount),
                volume=Quantity(volume),
                turnover=turnover,
                available_at=available_at,
            )
        )
    return tuple(bars)


def _decimal(value: int | str | Decimal) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))
