"""Phase-one planned execution dates, using the supplied market calendar only."""
from bisect import bisect_left
from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal, Sequence


def holding_due_session(sessions: Sequence[date], bought: date, count: int) -> date | None:
    """D0 is the actual fill session; suspensions do not remove market sessions."""
    if type(count) is not int or count < 1:
        raise ValueError("holding period must be positive")
    if any(a >= b for a, b in zip(sessions, sessions[1:])):
        raise ValueError("market calendar must be unique and ordered")
    index = bisect_left(sessions, bought)
    if index == len(sessions) or sessions[index] != bought:
        raise ValueError("fill date is not in market calendar")
    due = index + count
    return sessions[due] if due < len(sessions) else None


def investment_schedule(*, sessions: Sequence[date], start: date, end: date,
                        frequency: Literal["weekly", "monthly"], day: int,
                        budget: Decimal) -> dict[date, Decimal]:
    """Roll planned dates forward, merging budgets; no execution or catch-up logic."""
    if start > end or budget <= 0:
        raise ValueError("invalid investment period or budget")
    if frequency not in {"weekly", "monthly"} or not 1 <= day <= (7 if frequency == "weekly" else 31):
        raise ValueError("invalid recurrence")
    if any(a >= b for a, b in zip(sessions, sessions[1:])):
        raise ValueError("market calendar must be unique and ordered")
    result: dict[date, Decimal] = {}
    current = start
    while current <= end:
        planned = (current.isoweekday() == day if frequency == "weekly" else
                   current.day == min(day, monthrange(current.year, current.month)[1]))
        if planned:
            index = bisect_left(sessions, current)
            if index < len(sessions) and sessions[index] <= end:
                target = sessions[index]
                result[target] = result.get(target, Decimal(0)) + budget
        current += timedelta(days=1)
    return result
