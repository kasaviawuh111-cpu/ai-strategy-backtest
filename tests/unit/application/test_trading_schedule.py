from datetime import date
from decimal import Decimal

from ashare_lab.application.trading_schedule import holding_due_session, investment_schedule


def test_monthly_holiday_roll_includes_range_first_session_without_catchup():
    sessions = [date(2024, 12, 31), date(2025, 1, 2), date(2025, 1, 3), date(2025, 2, 5)]
    from decimal import Decimal
    kwargs = dict(sessions=sessions, end=date(2025, 2, 5), frequency='monthly', day=1,
                  budget=Decimal(100))
    assert investment_schedule(start=date(2025, 1, 2), **kwargs) == {
        date(2025, 1, 2): Decimal(100), date(2025, 2, 5): Decimal(100)}
    assert investment_schedule(start=date(2025, 1, 3), **kwargs) == {
        date(2025, 2, 5): Decimal(100)}


def test_weekly_holiday_roll_includes_start_but_not_previous_fill():
    from decimal import Decimal
    kwargs = dict(sessions=[date(2025, 1, 3), date(2025, 1, 7), date(2025, 1, 8), date(2025, 1, 13)],
                  end=date(2025, 1, 13), frequency='weekly', day=1, budget=Decimal(100))
    assert date(2025, 1, 7) in investment_schedule(start=date(2025, 1, 7), **kwargs)
    assert date(2025, 1, 8) not in investment_schedule(start=date(2025, 1, 8), **kwargs)


def test_holding_counts_market_sessions_not_calendar_days():
    sessions = [date(2026, 9, 11), date(2026, 9, 14), date(2026, 9, 15)]
    assert holding_due_session(sessions, sessions[0], 2) == sessions[2]
    assert holding_due_session(sessions, sessions[1], 2) is None


def test_month_end_and_holiday_roll_forward():
    assert investment_schedule(sessions=[date(2026, 3, 2)], start=date(2026, 2, 1),
        end=date(2026, 3, 2), frequency="monthly", day=31, budget=Decimal(100)) == {
            date(2026, 3, 2): Decimal(100)}


def test_multiple_plans_roll_to_same_session_merge_budget():
    assert investment_schedule(sessions=[date(2026, 10, 12)], start=date(2026, 10, 1),
        end=date(2026, 10, 12), frequency="weekly", day=1, budget=Decimal(100)) == {
            date(2026, 10, 12): Decimal(200)}
