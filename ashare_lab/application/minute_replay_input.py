"""Join canonical minute data to explicit session rules and daily controls.

This gate belongs to data preparation, never natural-language parsing.
No security status, market calendar or daily reference is fabricated here.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from ashare_lab.application.minute_grid_replay import ReplayBar
from ashare_lab.domain.execution.bar_prices import BarPrices
from ashare_lab.domain.market_data import DailyBar, InstrumentSession, MinuteBar, PriceBasis, TradingStatus
from ashare_lab.domain.shared import InstrumentId


class MinuteReplayDataError(ValueError):
    """Recognized strategy needs missing or consistent source data."""


@dataclass(frozen=True)
class MinuteReconciliation:
    session_date: date
    volume_delta_shares: int
    turnover_delta_cny: Decimal
    profile: str
    approximate: bool


@dataclass(frozen=True)
class PreparedMinuteReplay:
    bars: tuple[ReplayBar, ...]
    reconciliation: tuple[MinuteReconciliation, ...]
    market_sessions: tuple[date, ...] = ()
    nontrading_closes: tuple[tuple[InstrumentSession, DailyBar], ...] = ()
    preceding_bar: ReplayBar | None = None


def prepare_minute_replay(*, instrument: InstrumentId, start: date, end: date,
                          minutes: tuple[MinuteBar, ...], daily: tuple[DailyBar, ...],
                          sessions: tuple[InstrumentSession, ...],
                          market_calendar: tuple[date, ...],
                          reconciliation_profile: Literal["exact", "eastmoney_whole_lot_yuan_research", "external_stock_1min_research"] = "exact",
                          ) -> PreparedMinuteReplay:
    if reconciliation_profile not in {"exact", "eastmoney_whole_lot_yuan_research", "external_stock_1min_research"}:
        raise MinuteReplayDataError("unknown_reconciliation_profile")
    if start > end or any(a >= b for a, b in zip(market_calendar, market_calendar[1:])):
        raise MinuteReplayDataError("invalid_market_calendar")
    days = [day for day in market_calendar if start <= day <= end]
    if not days or not market_calendar or market_calendar[-1] <= days[-1]:
        raise MinuteReplayDataError("market_calendar_missing_next_settlement_session")
    session_map = {s.session_date: s for s in sessions}
    daily_map = {b.session_date: b for b in daily}
    if len(session_map) != len(sessions) or len(daily_map) != len(daily):
        raise MinuteReplayDataError("duplicate_daily_or_session_rows")
    if any(item.instrument_id != instrument for item in (*minutes, *daily, *sessions)):
        raise MinuteReplayDataError("market_data_instrument_mismatch")
    if any(a.bar_end_at >= b.bar_end_at for a, b in zip(minutes, minutes[1:])):
        raise MinuteReplayDataError("minute_data_not_strictly_ordered")
    shanghai = ZoneInfo("Asia/Shanghai")
    by_day = {}
    for bar in minutes:
        day = bar.bar_end_at.astimezone(shanghai).date()
        if start <= day <= end:
            if day not in days:
                raise MinuteReplayDataError("minute_data_outside_market_calendar")
            by_day.setdefault(day, []).append(bar)
    result = []
    reconciliation = []
    nontrading_closes = []
    for day in days:
        session = session_map.get(day)
        if session is None:
            raise MinuteReplayDataError(f"security_session_missing:{day}")
        bars = by_day.get(day, [])
        reference = daily_map.get(day)
        if reference is None or reference.price_basis is not PriceBasis.UNADJUSTED:
            raise MinuteReplayDataError(f"raw_daily_control_missing:{day}")
        if session.status is not TradingStatus.TRADING:
            suspended_placeholders = (
                reconciliation_profile == "external_stock_1min_research"
                and bars and not reference.volume.value and not reference.turnover
                and all(not bar.volume.value and not bar.turnover
                        and bar.open == bar.high == bar.low == bar.close == reference.close
                        for bar in bars)
            )
            if bars and not suspended_placeholders:
                raise MinuteReplayDataError(f"minute_data_on_nontrading_session:{day}")
            if reference.volume.value or reference.turnover:
                raise MinuteReplayDataError(f"nontrading_daily_volume_conflict:{day}")
            nontrading_closes.append((session, reference))
            continue
        expected = [datetime.combine(day, time(hour, 30 if hour == 9 else 0), tzinfo=shanghai)
                    + timedelta(minutes=i) for hour in (9, 13) for i in range(120)]
        if [bar.bar_start_at.astimezone(shanghai) for bar in bars] != expected:
            raise MinuteReplayDataError(f"incomplete_minute_session:{day}")
        # No fabricated release latency: replay currently consumes completed
        # exchange bars. A feed with delayed availability needs explicit routing.
        if any(bar.available_at != bar.bar_end_at for bar in bars):
            raise MinuteReplayDataError("minute_release_latency_requires_clock_mapping")
        controls = (bars[0].open, max((b.high for b in bars), key=lambda p: p.amount),
                    min((b.low for b in bars), key=lambda p: p.amount), bars[-1].close)
        if controls != (reference.open, reference.high, reference.low, reference.close):
            raise MinuteReplayDataError(f"daily_minute_ohlc_mismatch:{day}")
        volume_delta = sum(b.volume.value for b in bars) - reference.volume.value
        amount_delta = sum(b.turnover for b in bars) - reference.turnover
        volume_tolerance = Decimal(0)
        amount_tolerance = Decimal(0)
        if reconciliation_profile == 'external_stock_1min_research':
            # Explicit cross-source research envelope: one lot / yuan per
            # original record including auction, capped at 1bp of the day.
            # This is a chosen tolerance, not a claim about vendor rounding.
            volume_tolerance = min(Decimal(100 * (len(bars) + 1)), Decimal(reference.volume.value) / 10000)
            amount_tolerance = min(Decimal(len(bars) + 1), reference.turnover / 10000)
        if reconciliation_profile == "eastmoney_whole_lot_yuan_research" and (volume_delta or amount_delta):
            if any(b.volume.value % 100 or b.turnover != b.turnover.to_integral_value() for b in bars):
                raise MinuteReplayDataError("minute_precision_does_not_match_research_profile")
            # Explicit research envelope, not a claim about vendor rounding.
            # One source unit per row (plus auction), capped at 1bp of daily.
            volume_tolerance = min(Decimal(100 * (len(bars) + 1)), Decimal(reference.volume.value) / 10000)
            amount_tolerance = min(Decimal(len(bars) + 1), reference.turnover / 10000)
        if abs(volume_delta) > volume_tolerance:
            raise MinuteReplayDataError(f"daily_minute_volume_mismatch:{day}")
        if abs(amount_delta) > amount_tolerance:
            raise MinuteReplayDataError(f"daily_minute_turnover_mismatch:{day}")
        reconciliation.append(MinuteReconciliation(day, volume_delta, amount_delta,
                                                   reconciliation_profile, bool(volume_delta or amount_delta)))
        next_session = market_calendar[market_calendar.index(day) + 1]
        for bar in bars:
            result.append(ReplayBar(bar.bar_end_at, BarPrices(bar.open.amount, bar.high.amount,
                                                            bar.low.amount, bar.close.amount),
                                    session, next_session, bar.volume.value))
    return PreparedMinuteReplay(tuple(result), tuple(reconciliation), market_calendar, tuple(nontrading_closes))


def replay_start(prepared):
    """First observed session and source opening reference, not an invented bar."""
    candidates = [(bar.session.session_date, bar.prices.open,
                   bar.ended_at - timedelta(minutes=1)) for bar in prepared.bars[:1]]
    candidates.extend((session.session_date, daily.open.amount,
                       datetime.combine(session.session_date, time(9, 30), ZoneInfo("Asia/Shanghai")))
                      for session, daily in getattr(prepared, "nontrading_closes", ()))
    if not candidates:
        raise MinuteReplayDataError("minute_execution_data_missing")
    return min(candidates, key=lambda item: item[0])
