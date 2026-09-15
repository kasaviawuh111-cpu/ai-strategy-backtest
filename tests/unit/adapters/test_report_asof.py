from datetime import date
from decimal import Decimal

import pytest

from ashare_lab.adapters.market_data.report_asof import (
    DisclosedReportValue, project_disclosed_reports,
)


def report(period, notice, value, update=None):
    return DisclosedReportValue(date.fromisoformat(period), date.fromisoformat(notice),
                                Decimal(value), date.fromisoformat(update) if update else None)


def test_report_end_does_not_make_unpublished_profit_available():
    reports = [report('2025-06-30', '2025-08-16', '12.9953'),
               report('2025-09-30', '2025-10-25', '14.2504')]
    sessions = [date.fromisoformat(d) for d in ['2025-09-30', '2025-10-24', '2025-10-27']]
    values, evidence = project_disclosed_reports(reports, sessions)
    assert values == (Decimal('12.9953'), Decimal('12.9953'), Decimal('14.2504'))
    assert evidence[-1]['noticeDate'] == '2025-10-25'


def test_date_only_release_is_not_used_on_announcement_day_and_revision_is_retained():
    reports = [report('2024-12-31', '2025-03-14', '11'),
               report('2025-03-31', '2025-04-25', '12', '2026-04-25')]
    values, evidence = project_disclosed_reports(reports, [date(2025, 4, 25), date(2025, 4, 28)])
    assert values == (Decimal('11'), Decimal('12'))
    assert evidence[-1]['revisionRisk'] is True
    assert evidence[-1]['pitVerified'] is False


def test_missing_initial_disclosure_cannot_be_backfilled():
    with pytest.raises(ValueError, match='no disclosed report'):
        project_disclosed_reports([report('2025-09-30', '2025-10-25', '14')], [date(2025, 9, 30)])


def test_conflicting_versions_are_not_selected_arbitrarily():
    with pytest.raises(ValueError, match='ambiguous'):
        project_disclosed_reports([report('2025-06-30', '2025-08-16', '12'),
                                   report('2025-06-30', '2025-08-16', '13')], [date(2025, 9, 12)])


def test_old_report_published_later_does_not_replace_newer_period():
    values, _ = project_disclosed_reports([
        report('2025-03-31', '2025-08-20', '10'),
        report('2025-06-30', '2025-08-16', '12'),
    ], [date(2025, 8, 21)])
    assert values == (Decimal('12'),)
