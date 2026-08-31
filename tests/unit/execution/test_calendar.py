from datetime import date, datetime

import pytest

from ashare_lab.domain.execution import NoNextSessionError, TradingCalendar
from ashare_lab.domain.shared import DomainValidationError


def calendar() -> TradingCalendar:
    return TradingCalendar(
        version="sse-szse.sessions.2025.v1",
        sessions=(
            date(2025, 1, 24),
            date(2025, 1, 27),
            date(2025, 2, 5),
            date(2025, 2, 6),
        ),
    )


def test_next_session_uses_explicit_holiday_gap_not_weekday_arithmetic() -> None:
    trading_calendar = calendar()

    assert trading_calendar.next_session(date(2025, 1, 27)) == date(2025, 2, 5)
    assert trading_calendar.next_session(date(2025, 2, 1)) == date(2025, 2, 5)
    assert not trading_calendar.is_session(date(2025, 2, 3))


def test_next_session_after_snapshot_end_raises_instead_of_guessing() -> None:
    with pytest.raises(NoNextSessionError, match="has no session after"):
        calendar().next_session(date(2025, 2, 6))


@pytest.mark.parametrize(
    "sessions",
    [
        (date(2025, 1, 2), date(2025, 1, 2)),
        (date(2025, 1, 3), date(2025, 1, 2)),
    ],
)
def test_calendar_rejects_duplicate_or_unordered_sessions(
    sessions: tuple[date, ...],
) -> None:
    with pytest.raises(DomainValidationError, match="strictly increasing"):
        TradingCalendar(version="bad.v1", sessions=sessions)


def test_calendar_rejects_datetime_subclass_as_session_date() -> None:
    with pytest.raises(DomainValidationError, match="every session must be a date"):
        TradingCalendar(
            version="bad.v1",
            sessions=(datetime(2025, 1, 2),),  # type: ignore[arg-type]
        )
