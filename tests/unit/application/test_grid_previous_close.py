from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace as NS

import pytest

from ashare_lab.application.grid_previous_close import bind_previous_close
from ashare_lab.application.minute_grid_plan import _resolve_parameters
from ashare_lab.domain.strategy.price_plans import GridParameters, GridSpecificationError


def params(**updates):
    return GridParameters(**dict(anchor_mode="previous_close", lower_price=1,
        upper_price=100, anchor_update="last_trigger", **updates))


def bind(p, start=date(2026, 9, 14), rows=None, symbol="300059.SZ"):
    return bind_previous_close(p, start=start, end=date(2026, 9, 16), symbol=symbol,
        sessions=[date(2026, 9, d) for d in (10, 11, 14, 15, 16)],
        history=NS(instrument_id="300059.SZ", rows=rows if rows is not None else [
            NS(session_date=date(2026, 9, 11), raw_close=D("18.31")),
            NS(session_date=date(2026, 9, 14), raw_close=D("30"))]))


@pytest.mark.parametrize("start", [date(2026, 9, 12), date(2026, 9, 14)])
def test_weekend_and_monday_use_friday_not_future_close_or_first_open(start):
    p = bind(params(anchor_price=99), start=start)
    assert p.anchor_price == D("18.31")
    assert p.anchor_quote_time_label == "2026-09-11"
    assert _resolve_parameters(p, D(85)).resolved_anchor == D("18.31")


def test_date_edit_rebinds_instead_of_reusing_saved_price():
    p = bind(params(anchor_price=99), start=date(2026, 9, 15))
    assert p.anchor_price == 30 and p.anchor_quote_time_label == "2026-09-14"


def test_holiday_gap_uses_calendar_not_calendar_day_subtraction():
    p = bind_previous_close(params(), start=date(2026, 10, 1), end=date(2026, 10, 9),
        symbol="300059.SZ", sessions=[date(2026, 9, 30), date(2026, 10, 9)],
        history=NS(instrument_id="300059.SZ", rows=[NS(session_date=date(2026, 9, 30), raw_close=D(20))]))
    assert p.anchor_quote_time_label == "2026-09-30"


def test_missing_exact_previous_session_never_uses_older_close():
    with pytest.raises(GridSpecificationError, match="缺少"):
        bind(params(), rows=[NS(session_date=date(2026, 9, 10), raw_close=D(18))])


def test_wrong_symbol_rejected():
    with pytest.raises(GridSpecificationError):
        bind(params(), symbol="000001.SZ")


def test_manual_price_unchanged():
    p = GridParameters(anchor_mode="manual", anchor_price=85, lower_price=1, upper_price=100)
    assert bind(p, rows=[]) is p
