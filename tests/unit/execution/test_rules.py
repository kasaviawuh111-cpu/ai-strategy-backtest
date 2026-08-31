from datetime import date
from decimal import Decimal

from ashare_lab.domain.execution import HistoricalAshareRuleBook, PriceLimitRuleInput
from ashare_lab.domain.market_data import Board, TradingStatus
from ashare_lab.domain.shared import InstrumentId, Price


def rule_input(**overrides: object) -> PriceLimitRuleInput:
    values: dict[str, object] = {
        "instrument_id": InstrumentId("300059.SZ"),
        "session_date": date(2025, 1, 2),
        "board": Board.CHINEXT,
        "status": TradingStatus.TRADING,
        "previous_close": Price(Decimal("10.03")),
        "listing_date": date(2010, 3, 19),
        "listing_session_number": 3000,
        "is_st": False,
    }
    values.update(overrides)
    return PriceLimitRuleInput(**values)  # type: ignore[arg-type]


def test_chinext_after_reform_uses_twenty_percent_and_tick_rounding() -> None:
    session = HistoricalAshareRuleBook().build_session(rule_input())

    assert session.upper_limit == Price(Decimal("12.04"))
    assert session.lower_limit == Price(Decimal("8.02"))


def test_chinext_before_reform_respects_st_five_percent() -> None:
    session = HistoricalAshareRuleBook().build_session(
        rule_input(session_date=date(2020, 8, 21), is_st=True)
    )

    assert session.upper_limit == Price(Decimal("10.53"))
    assert session.lower_limit == Price(Decimal("9.53"))


def test_star_and_registered_chinext_first_five_sessions_have_no_daily_band() -> None:
    star = HistoricalAshareRuleBook().build_session(
        rule_input(
            instrument_id=InstrumentId("688001.SH"),
            board=Board.STAR,
            listing_date=date(2025, 1, 2),
            listing_session_number=5,
        )
    )
    chinext = HistoricalAshareRuleBook().build_session(
        rule_input(listing_date=date(2025, 1, 2), listing_session_number=3)
    )

    assert star.upper_limit is star.lower_limit is None
    assert chinext.upper_limit is chinext.lower_limit is None


def test_legacy_main_board_first_session_uses_asymmetric_band() -> None:
    session = HistoricalAshareRuleBook().build_session(
        rule_input(
            instrument_id=InstrumentId("600000.SH"),
            board=Board.MAIN,
            session_date=date(2020, 1, 2),
            listing_date=date(2020, 1, 2),
            listing_session_number=1,
        )
    )

    assert session.upper_limit == Price(Decimal("14.44"))
    assert session.lower_limit == Price(Decimal("6.42"))
