"""Project disclosed report values onto real trading sessions, never period ends.

This is a research replay of the retrieved report versions, not an assertion
that later-restated reports are an immutable point-in-time archive.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import re


@dataclass(frozen=True)
class DisclosedReportValue:
    period: date
    notice_date: date
    value: Decimal
    update_date: date | None = None


def report_period(value: str) -> date:
    """Decode the source's fiscal-quarter labels; never treat them as availability."""
    match = re.fullmatch(r"(\d{4})(一季报|中报|半年报|三季报|年报)", value)
    if not match:
        raise ValueError("unrecognized fiscal report label")
    month, day = {"一季报": (3, 31), "中报": (6, 30), "半年报": (6, 30),
                  "三季报": (9, 30), "年报": (12, 31)}[match[2]]
    return date(int(match[1]), month, day)


def project_disclosed_reports(
    reports: Sequence[DisclosedReportValue], sessions: Sequence[date],
) -> tuple[tuple[Decimal, ...], tuple[Mapping[str, object], ...]]:
    """Date-only announcements become usable strictly after publication day.

    Callers supply actual market sessions. No synthetic weekday calendar,
    backfill from a future report, or interpolation is permitted.
    """
    if not sessions or list(sessions) != sorted(set(sessions)):
        raise ValueError("sessions must be nonempty, unique and ascending")
    by_period: dict[date, DisclosedReportValue] = {}
    for report in reports:
        if not report.value.is_finite() or report.notice_date < report.period:
            raise ValueError("invalid report value or announcement date")
        previous = by_period.get(report.period)
        if previous is not None and previous != report:
            raise ValueError("ambiguous report version")
        by_period[report.period] = report
    values: list[Decimal] = []
    evidence: list[Mapping[str, object]] = []
    for session in sessions:
        available = [r for r in by_period.values() if r.notice_date < session]
        if not available:
            raise ValueError("no disclosed report available at session start")
        # A late announcement of an older period must not roll the series back.
        report = max(available, key=lambda r: r.period)
        values.append(report.value)
        evidence.append({
            "sessionDate": session.isoformat(),
            "reportPeriod": report.period.isoformat(),
            "noticeDate": report.notice_date.isoformat(),
            "updateDate": report.update_date.isoformat() if report.update_date else None,
            "revisionRisk": bool(report.update_date and report.update_date > report.notice_date),
            "availabilityPolicy": "announcement_date_next_trading_session.v1",
            "pitVerified": False,
        })
    return tuple(values), tuple(evidence)
