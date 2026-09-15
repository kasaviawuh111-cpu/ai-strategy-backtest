from datetime import datetime
from decimal import Decimal as D
from zoneinfo import ZoneInfo

from ashare_lab.application.minute_grid_replay import GridEquityPoint, GridReplayResult
from ashare_lab.application.minute_result import minute_result_bundle
from ashare_lab.domain.portfolio import PortfolioState
from ashare_lab.domain.shared import Money


def test_price_adjustment_note_excludes_warmup_and_unrelated_plans():
    from datetime import date
    from types import SimpleNamespace as NS
    from ashare_lab.application.minute_result import _price_adjustment_note

    rule = NS(target_price=D(20), limit_price=None, activation_price=None, gap=None, gap_unit='percent')
    strategy = NS(trading_plan=NS(kind='conditional', parameters=NS(rules=[rule])),
                  backtest=NS(start=date(2026, 1, 1), end=date(2026, 9, 11)))
    result = NS(price_rebases=(NS(ex_date=date(2025, 4, 16), factor=D('.99')),))
    assert _price_adjustment_note(result, strategy) is None
    result.price_rebases += (NS(ex_date=date(2026, 4, 21), factor=D('.995')),)
    note = _price_adjustment_note(result, strategy)
    assert '卡片保留原始设置' in note
    assert '不保证以触发价成交' in note
    rule.target_price = None
    assert _price_adjustment_note(result, strategy) is None
    strategy.trading_plan.kind = 'grid'
    assert _price_adjustment_note(result, strategy) == note
    strategy.trading_plan.kind = 'scheduled'
    assert _price_adjustment_note(result, strategy) is None
    assert _price_adjustment_note(result, None) is None


def test_intraday_recovery_does_not_erase_maximum_drawdown():
    points = tuple(GridEquityPoint(
        datetime(2026, 9, 11, 10, minute, tzinfo=ZoneInfo('Asia/Shanghai')),
        D(value), 0, D(10), D(value)) for minute, value in enumerate((100, 80, 100)))
    result = GridReplayResult(PortfolioState(Money(D(100))), (), equity=points,
                             initial_equity_cny=D(100))
    report = minute_result_bundle(run_id='intraday-recovery', result=result,
                                 initial_cash=D(100), snapshot_id='test:intraday')
    assert report.summary.max_drawdown == -0.2
    assert report.summary.total_return == 0
    assert report.series[-1].drawdown == 0
    assert len(report.series) == 4
    assert report.series[0].date.hour == 9 and report.series[0].date.minute == 30
    assert [point.date for point in report.series[1:]] == [point.observed_at for point in points]
