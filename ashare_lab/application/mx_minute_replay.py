"""Join existing MX daily/session facts with canonical minute snapshots."""
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from ashare_lab.adapters.market_data.mx_daily_history import MxDailyHistory
from ashare_lab.application.minute_replay_input import PreparedMinuteReplay, prepare_minute_replay
from ashare_lab.domain.market_data import DailyBar, InstrumentSession, MinuteBar, standard_buy_quantity_rule
from ashare_lab.domain.shared import InstrumentId, Price, Quantity


def prepare_mx_minute_replay(*, history: MxDailyHistory, minutes: tuple[MinuteBar, ...],
                             start: date, end: date, market_calendar: tuple[date, ...],
                             eastmoney_whole_unit_research: bool = False,
                             external_stock_1min_research: bool = False) -> PreparedMinuteReplay:
    """Use provider-supplied raw prices, statuses and limits; never derive ST limits."""
    instrument = InstrumentId(history.instrument_id)
    minimum, increment = standard_buy_quantity_rule(history.board)
    daily = []
    sessions = []
    for row in history.rows:
        if not start <= row.session_date <= end:
            continue
        daily.append(DailyBar(
            instrument, row.session_date, Price(row.raw_open), Price(row.raw_high),
            Price(row.raw_low), Price(row.raw_close), Quantity(row.volume), row.amount,
            datetime.combine(row.session_date, time(15), tzinfo=ZoneInfo("Asia/Shanghai")),
        ))
        sessions.append(InstrumentSession(
            instrument, row.session_date, history.board, row.trading_status, Price(row.raw_preclose),
            Price(row.upper_limit) if row.upper_limit is not None else None,
            Price(row.lower_limit) if row.lower_limit is not None else None,
            minimum, increment, is_st=row.is_st,
        ))
    return prepare_minute_replay(
        instrument=instrument, start=start, end=end, minutes=minutes, daily=tuple(daily),
        sessions=tuple(sessions), market_calendar=market_calendar,
        reconciliation_profile=("external_stock_1min_research" if external_stock_1min_research else
                                "eastmoney_whole_lot_yuan_research" if eastmoney_whole_unit_research else "exact"),
    )
