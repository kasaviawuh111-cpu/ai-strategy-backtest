from datetime import date
from decimal import Decimal as D
import pytest

from ashare_lab.application.daily_scheduled_plan import execute_daily_schedule
from ashare_lab.application.corporate_action_timeline import TimelineCorporateActionApplier
from ashare_lab.application.minute_price_rebase import MinutePriceRebase
from ashare_lab.application.minute_replay_input import MinuteReplayDataError
from ashare_lab.domain.execution.fees import AshareExchange
from ashare_lab.domain.market_data import CorporateActionKind
from ashare_lab.domain.strategy.price_plans import ScheduledParameters
from tests.unit.application.test_skill_backtest import _row, _history
from tests.unit.portfolio.test_corporate_actions import _action, _clock, INSTRUMENT


def test_sourced_schedule_hash_covers_prior_liquidity_but_not_future():
    from dataclasses import replace
    from types import SimpleNamespace
    from ashare_lab.application.backtest_submission import BacktestRunConfig
    from ashare_lab.application.daily_scheduled_plan import execute_sourced_daily_schedule
    days = tuple(date(2025, 1, n) for n in (2, 3, 6))
    strategy = SimpleNamespace(backtest=SimpleNamespace(start=days[1], end=days[1]),
        trading_plan=SimpleNamespace(parameters=ScheduledParameters(frequency="once", sizing_mode="shares",
            quantity=100, slippage_bps=0)))
    calendar = (days, dict(provider="test", sourceSha256="test", sessions=list(days)), b"test-calendar")
    def run(volume, future):
        rows = (replace(_row(days[0], raw_open="10"), volume=volume),
                _row(days[1], raw_open="10"), _row(days[2], raw_open=future))
        return execute_sourced_daily_schedule(strategy, _history(rows), calendar_input=calendar,
                                             config=BacktestRunConfig())
    first, second, future = run(1140, "10"), run(1160, "10"), run(1140, "20")
    assert first[0].portfolio.fills[0].quantity.value == 57
    assert second[0].portfolio.fills[0].quantity.value == 58
    assert first[1] != second[1]
    assert first[1] == future[1]
    assert len(first[3]["dailyControls"]["rows"]) == 2


@pytest.mark.parametrize("prior_volume,expected", [(1140, 57), (0, 0), (None, 0)])
def test_daily_schedule_capacity_uses_prior_session_and_expires_remainder(prior_volume, expected):
    from dataclasses import replace
    from ashare_lab.application.backtest_submission import BacktestRunConfig
    days = tuple(date(2025, 1, n) for n in (2, 3, 6))
    rows = [_row(days[1], raw_open="10")]
    if prior_volume is not None:
        rows.insert(0, replace(_row(days[0], raw_open="10"), volume=prior_volume))
    result = execute_daily_schedule(ScheduledParameters(frequency="once", sizing_mode="shares",
        quantity=100, slippage_bps=0), history=_history(tuple(rows)), market_sessions=days,
        exchange=AshareExchange.SHENZHEN, start=days[1], end=days[1], config=BacktestRunConfig())
    assert sum(fill.quantity.value for fill in result.portfolio.fills) == expected
    assert result.capacity_assumption == "previous_completed_session_volume:0.05"
    if expected:
        assert result.events[-2].status == "partially_filled"
        assert result.events[-1].status == "cancelled"
        assert result.events[-1].observed_at.hour == 15
        assert result.events[-1].order.quantity == 43
        assert result.events[-1].sizing_details == {
            "requestedQuantity": 100, "filledQuantity": 57, "cancelledQuantity": 43}
        from ashare_lab.application.minute_result import minute_result_bundle
        report = minute_result_bundle(run_id="partial-schedule", result=result,
            initial_cash=D(1000000), snapshot_id="fixture")
        expired = next(item for item in report.activities if item.kind == "expired")
        assert expired.quantity == 43
        assert expired.reason == "当日委托有效期结束，未成交余量已撤销；已成交部分保留。"
        assert report.summary.execution_note is None
    else:
        assert result.events[-1].reason == "previous_session_liquidity_unavailable"


@pytest.mark.parametrize("at,limit,expected_price,hour,quality", [
    ("open", D("10.5"), D("10"), 9, "daily_bar_open_proxy"),
    ("open", D("9.5"), D("9.5"), 15, "daily_bar_available_at_proxy"),
    ("close", D("10.5"), D("10"), 15, "daily_bar_available_at_proxy"),
])
def test_scheduled_limit_fill_time_distinguishes_activation_and_observation(at, limit, expected_price, hour, quality):
    from ashare_lab.application.minute_result import minute_result_bundle
    day = date(2025, 1, 2)
    result = execute_daily_schedule(ScheduledParameters(frequency="once", sizing_mode="shares",
        quantity=100, at=at, limit_price=limit, slippage_bps=0),
        history=_history((_row(day, raw_open="10"),)), market_sessions=(day, date(2025, 1, 3)),
        exchange=AshareExchange.SHENZHEN, start=day, end=day)
    fill = result.portfolio.fills[0]
    assert fill.price.amount == expected_price
    assert fill.filled_at.hour == hour
    event = result.events[-1]
    assert event.observed_at == fill.filled_at
    assert event.effective_at.hour == (9 if at == "open" else 15)
    assert event.time_quality == quality
    report = minute_result_bundle(run_id="limit-clock", result=result,
        initial_cash=D(1000000), snapshot_id="fixture")
    activity = next(item for item in report.activities if item.kind == "fill")
    assert activity.occurred_at == fill.filled_at
    assert activity.time_quality == quality


@pytest.mark.parametrize("at,limit", [("open", D("8")), ("close", D("9.5"))])
def test_unhit_scheduled_limit_expires_at_close_not_rejected_at_open(at, limit):
    from ashare_lab.application.minute_result import minute_result_bundle
    day = date(2025, 1, 2)
    result = execute_daily_schedule(ScheduledParameters(frequency="once", sizing_mode="shares",
        quantity=100, at=at, limit_price=limit),
        history=_history((_row(day, raw_open="10"),)), market_sessions=(day, date(2025, 1, 3)),
        exchange=AshareExchange.SHENZHEN, start=day, end=day)
    assert not result.portfolio.fills
    event = result.events[-1]
    assert event.observed_at.hour == 15
    assert event.status == "cancelled"
    assert event.reason == "limit_not_reached"
    report = minute_result_bundle(run_id="limit-expiry", result=result,
        initial_cash=D(1000000), snapshot_id="fixture")
    assert report.activities[-1].kind == "expired"
    assert report.activities[-1].status == "cancelled"


@pytest.mark.parametrize("limit,expected_hour,filled", [(D("9.5"), 9, True),
    (D("10.5"), 15, True), (D("12"), 15, False)])
def test_scheduled_sell_limit_uses_same_clock_and_expiry_rules(limit, expected_hour, filled):
    days = tuple(date(2025, 1, n) for n in (2, 3, 6))
    result = execute_daily_schedule(ScheduledParameters(frequency="weekly", day=5,
        sizing_mode="shares", quantity=100, side="sell", initial_shares=100,
        limit_price=limit, slippage_bps=0),
        history=_history(tuple(_row(day, raw_open="10") for day in days[:2])),
        market_sessions=days, exchange=AshareExchange.SHENZHEN, start=days[0], end=days[1])
    event = result.events[-1]
    assert bool(event.fill) is filled
    assert event.observed_at.hour == expected_hour
    assert event.effective_at.hour == 9
    if filled:
        assert event.fill.filled_at == event.observed_at
    else:
        assert event.status == "cancelled"


def test_skipped_small_budget_is_not_reported_as_expired_order():
    from ashare_lab.application.minute_result import minute_result_bundle
    day = date(2025, 1, 2)
    history = _history((_row(day, raw_open="10"),))
    result = execute_daily_schedule(ScheduledParameters(frequency="once", budget_cny=10),
        history=history, market_sessions=(day, date(2025, 1, 3)), exchange=AshareExchange.SHENZHEN, start=day, end=day)
    report = minute_result_bundle(run_id="small-budget", result=result,
                                 initial_cash=D(1000000), snapshot_id="fixture")
    skipped = [item for item in report.activities if item.outcome_reason == "budget_below_minimum_order"]
    assert len(skipped) == 1 and skipped[0].status == "rejected"
    assert not result.portfolio.fills
    details = skipped[0].execution_details
    assert details["budgetCny"] == "10"
    assert details["minimumOrderQuantity"] == 100
    assert D(details["minimumOrderCostCny"]) > 1000  # Fees cannot be ignored.
    assert "单次预算不足" in skipped[0].reason
    assert "本次没有成交" in report.summary.execution_note
    assert "1次定投" in report.summary.execution_note


@pytest.mark.parametrize("at", ["open", "close"])
def test_schedule_inventory_rejection_explains_actual_quantities_without_spending_cash(at):
    from ashare_lab.application.minute_result import minute_result_bundle
    day = date(2025, 1, 2)
    result = execute_daily_schedule(ScheduledParameters(frequency="once", at=at,
        budget_cny=20000, max_shares=1000, slippage_bps=0),
        history=_history((_row(day, raw_open="10"),)),
        market_sessions=(day, date(2025, 1, 3)), exchange=AshareExchange.SHENZHEN,
        start=day, end=day)
    assert not result.portfolio.fills and result.portfolio.cash.amount == 1000000
    event = result.events[-1]
    assert event.reason == "maximum_inventory_exceeded"
    assert event.sizing_details["currentPositionQuantity"] == 0
    assert event.sizing_details["projectedPositionQuantity"] > 1000
    report = minute_result_bundle(run_id="inventory-cap", result=result,
                                 initial_cash=D(1000000), snapshot_id="fixture")
    rejected = next(a for a in report.activities if a.outcome_reason == event.reason)
    assert "当前持仓0股" in rejected.reason and "持仓上限1000股" in rejected.reason
    assert "不自动缩量" in rejected.reason


@pytest.mark.parametrize("budget,filled", [(1000, False), (10000, True)])
def test_monthly_stock_budget_is_distinct_from_million_yuan_account(budget, filled):
    day, next_day = date(2025, 10, 9), date(2025, 10, 10)
    history = _history((_row(day, raw_open="26.52"),))
    result = execute_daily_schedule(ScheduledParameters(frequency="monthly", budget_cny=budget),
        history=history, market_sessions=(day, next_day), exchange=AshareExchange.SHENZHEN,
        start=date(2025, 10, 1), end=day)
    assert bool(result.portfolio.fills) is filled
    if filled:
        assert result.portfolio.fills[0].quantity.value == 300
        assert D(1000000) - result.portfolio.cash.amount <= budget
    else:
        assert result.portfolio.cash.amount == 1000000
        skipped = result.events[-1]
        assert skipped.reason == "budget_below_minimum_order"
        assert D(skipped.sizing_details["minimumOrderCostCny"]) > budget


def test_daily_dividend_reuses_entitlement_receivable_and_settlement_ledger():
    days = tuple(date(2025, 1, day) for day in (2, 3, 6, 7))
    history = _history(tuple(_row(day, raw_open="10" if day == days[0] else "9.5") for day in days))
    action = _action(CorporateActionKind.CASH_DIVIDEND, cash_per_share=D(".5"), pay_date=days[2])
    rebase = MinutePriceRebase(INSTRUMENT, days[1], D(".95"), _clock(days[1], 9), "a" * 64)
    result = execute_daily_schedule(ScheduledParameters(frequency="once", sizing_mode="shares", quantity=100, slippage_bps=0),
        history=history, market_sessions=days, exchange=AshareExchange.SHENZHEN, start=days[0], end=days[2],
        corporate_actions=TimelineCorporateActionApplier((action,)), price_rebases=(rebase,))
    assert result.equity[1].dividend_receivable == 50
    assert result.equity[2].dividend_receivable == 0
    assert result.equity[2].cash == result.equity[1].cash + 50
    assert result.portfolio.lots[0].acquisition_principal.amount == 950
    assert len({point.equity for point in result.equity}) == 1
    assert all(point.equity == point.benchmark_equity for point in result.equity)
    assert result.corporate_action_policy is not None


@pytest.mark.parametrize("actions", [None, TimelineCorporateActionApplier(())])
def test_daily_price_factor_cannot_silently_run_without_its_action(actions):
    days = tuple(date(2025, 1, day) for day in (2, 3, 6))
    history = _history(tuple(_row(day, raw_open="10") for day in days))
    rebase = MinutePriceRebase(INSTRUMENT, days[1], D(".95"), _clock(days[1], 9), "a" * 64)
    with pytest.raises(MinuteReplayDataError, match="corporate_action_(source_missing|rebase_event_missing)"):
        execute_daily_schedule(ScheduledParameters(frequency="once"), history=history, market_sessions=days,
            exchange=AshareExchange.SHENZHEN, start=days[0], end=days[1],
            corporate_actions=actions, price_rebases=(rebase,))


def test_daily_ex_date_requires_source_factor_not_inferred_gap():
    days = tuple(date(2025, 1, day) for day in (2, 3, 6))
    history = _history(tuple(_row(day, raw_open="10") for day in days))
    action = _action(CorporateActionKind.CASH_DIVIDEND, cash_per_share=D(".5"), pay_date=days[2])
    with pytest.raises(MinuteReplayDataError, match="price_rebase_missing"):
        execute_daily_schedule(ScheduledParameters(frequency="once"), history=history, market_sessions=days,
            exchange=AshareExchange.SHENZHEN, start=days[0], end=days[1],
            corporate_actions=TimelineCorporateActionApplier((action,)))
