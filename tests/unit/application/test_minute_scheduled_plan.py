from datetime import date, datetime
from decimal import Decimal as D
from zoneinfo import ZoneInfo

import pytest

from ashare_lab.application.minute_replay_input import PreparedMinuteReplay
from ashare_lab.application.minute_scheduled_plan import execute_minute_schedule
from ashare_lab.domain.execution.fees import AshareExchange
from ashare_lab.domain.strategy.price_plans import ScheduledParameters, ConditionRule
from tests.unit.application.test_minute_grid_replay import bar, SEC


def test_calendar_buy_with_minute_protection_respects_t1_and_resumes_next_schedule():
    from ashare_lab.application.minute_grid_replay import Protection
    days = tuple(date(2026, 9, day) for day in (9, 10, 11, 14, 15, 16, 17))
    prepared = PreparedMinuteReplay((bar(9, 31, 10), bar(9, 32, D('10.6')),
        bar(9, 33, D('10.6')), bar(10, 31, 11), bar(10, 32, 11),
        bar(16, 31, 10), bar(16, 32, D('10.6')), bar(17, 31, 11)), (), days)
    result = execute_minute_schedule(ScheduledParameters(frequency='weekly', day=3,
        sizing_mode='shares', quantity=100, slippage_bps=0), prepared=prepared,
        exchange=AshareExchange.SHENZHEN, start=days[0], end=days[-1],
        protection=Protection(take_profit=D('.05')))
    assert [(f.side.value, f.filled_at.day, f.quantity.value) for f in result.portfolio.fills] == [
        ('buy', 9, 100), ('sell', 10, 100), ('buy', 16, 100), ('sell', 17, 100)]
    assert any(event.reason == 't_plus_one_locked' for event in result.events)
    assert result.portfolio.position_quantity(SEC).value == 0


def test_daily_buy_and_weekly_sell_share_inventory_and_exit_priority():
    from tests.unit.application.test_minute_grid_replay import daily_intent
    days = tuple(date(2026, 9, day) for day in (8, 9, 10, 11, 14, 15, 16, 17))
    prepared = PreparedMinuteReplay(tuple(bar(day.day, 31, 10) for day in days[1:]), (), days)
    result = execute_minute_schedule(
        ScheduledParameters(frequency="weekly", day=4, side="sell", sizing_mode="shares",
                            quantity=900, initial_cash_cny=10000, slippage_bps=0),
        prepared=prepared, exchange=AshareExchange.SHENZHEN, start=days[1], end=days[-1],
        daily_signals=(daily_intent(8, 9, enter=True), daily_intent(9, 10, enter=True),
                       daily_intent(10, 11, enter=True)))
    assert [(f.side.value, f.filled_at.day) for f in result.portfolio.fills] == [
        ("buy", 9), ("sell", 10), ("buy", 11), ("sell", 17)]
    assert any(e.order.order_id == "daily:9" and e.reason == "exit_takeover"
               for e in result.events)


@pytest.mark.parametrize("minimum,expected_sells", [(0, [100, 100]), (100, [100])])
def test_calendar_adapter_combines_periodic_entry_with_close_confirmed_exit(minimum, expected_sells):
    from tests.unit.application.test_minute_grid_replay import daily_intent
    days = tuple(date(2026, 9, day) for day in (9, 10, 11, 14, 15, 16, 17, 18))
    prepared = PreparedMinuteReplay(
        tuple(bar(day.day, 31, 10) for day in days), (), days)
    result = execute_minute_schedule(
        ScheduledParameters(frequency="weekly", day=3, sizing_mode="shares", quantity=100,
                            min_shares=minimum),
        prepared=prepared, exchange=AshareExchange.SHENZHEN, start=days[0], end=days[-1],
        daily_signals=(daily_intent(9, 10, exit=True), daily_intent(16, 17, exit=True)),
    )
    assert [(f.quantity.value, f.filled_at.day) for f in result.portfolio.fills
            if f.side.value == "buy"] == [(100, 9), (100, 16)]
    sells = [f for f in result.portfolio.fills if f.side.value == "sell"]
    assert [f.quantity.value for f in sells] == expected_sells
    assert [f.filled_at.day for f in sells] == ([10, 17] if minimum == 0 else [17])
    assert result.portfolio.position_quantity(SEC).value == minimum


def test_first_open_without_predecessor_uses_first_completed_minute_capacity():
    from ashare_lab.application.backtest_submission import BacktestRunConfig
    from ashare_lab.domain.execution import CapacityMode
    from dataclasses import replace
    days = (date(2026, 9, 9), date(2026, 9, 10))
    prepared = PreparedMinuteReplay((bar(9, 31, 10),), (), days,
        preceding_bar=replace(bar(8, 59, 10), volume_shares=10000))
    params = ScheduledParameters(frequency="once", sizing_mode="shares", quantity=100)
    config = BacktestRunConfig(capacity_mode=CapacityMode.POINT_IN_TIME_VOLUME)
    result = execute_minute_schedule(params, prepared=prepared, exchange=AshareExchange.SHENZHEN,
        start=days[0], end=days[0], execution_config=config)
    assert result.portfolio.position_quantity(SEC).value == 100
    no_prior = execute_minute_schedule(params, prepared=replace(prepared, preceding_bar=None),
        exchange=AshareExchange.SHENZHEN, start=days[0], end=days[0], execution_config=config)
    assert no_prior.portfolio.position_quantity(SEC).value == 100
    assert no_prior.capacity_assumption == (
        "first_completed_minute_volume_for_opening_order_else_previous_completed_minute_volume:0.05"
    )


@pytest.mark.parametrize("start_day,expected", [(1, [1, 2]), (2, [2])])
def test_start_buy_deduplicates_first_monthly_schedule(start_day, expected):
    from ashare_lab.application.minute_scheduled_plan import schedule_orders
    sessions = (date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3))
    params = ScheduledParameters(frequency="monthly", day=2, sizing_mode="shares",
                                 quantity=100, buy_on_start=True)
    orders = schedule_orders(params, market_sessions=sessions,
                             start=date(2026, 9, start_day), end=sessions[-1])
    assert [o.session_date.day for o in orders] == expected
    assert all(o.quantity == 100 for o in orders)


def test_weekly_schedule_with_recurring_price_exit_keeps_future_investments():
    days = tuple(date(2026, 9, day) for day in (9, 10, 11, 14, 15, 16, 17, 18))
    prepared = PreparedMinuteReplay(
        (bar(9, 31, 10), bar(9, 32, 11), bar(9, 33, 11), bar(10, 31, 11),
         bar(16, 31, 10), bar(16, 32, 11), bar(16, 33, 11), bar(17, 31, 11)), (), days)
    result = execute_minute_schedule(
        ScheduledParameters(frequency="weekly", day=3, sizing_mode="shares", quantity=100),
        prepared=prepared, exchange=AshareExchange.SHENZHEN, start=days[0], end=days[-2],
        exit_rules=(ConditionRule(kind="price", side="sell", target_price=11,
                                  sizing_mode="all_position"),))
    assert [(fill.side.value, fill.quantity.value) for fill in result.portfolio.fills] == [
        ("buy", 100), ("sell", 100), ("buy", 100), ("sell", 100)]
    assert result.portfolio.position_quantity(SEC).value == 0


def test_weekly_hold_exit_sells_each_batch_at_its_own_due_session():
    days = tuple(date(2026, 9, day) for day in (7, 8, 9, 10, 11, 14, 15, 16, 17))
    prepared = PreparedMinuteReplay(tuple(bar(day.day, 31, 10) for day in days[:-1]), (), days)
    result = execute_minute_schedule(
        ScheduledParameters(frequency="weekly", day=1, sizing_mode="shares", quantity=100),
        prepared=prepared, exchange=AshareExchange.SHENZHEN, start=days[0], end=days[-2],
        exit_rules=(ConditionRule(kind="holding_period", side="sell", sessions=2,
                                  sizing_mode="all_position"),))
    fills = [(fill.side.value, fill.trading_date, fill.quantity.value) for fill in result.portfolio.fills]
    assert fills == [("buy", days[0], 100), ("sell", days[2], 100),
                     ("buy", days[5], 100), ("sell", days[7], 100)]


@pytest.mark.parametrize("requested,expected,reason", [
    (100, 57, "participation_partial_fill"),
    (99, 0, "invalid_buy_quantity"),
])
def test_lot_validation_applies_to_order_not_partial_fill(requested, expected, reason):
    from ashare_lab.application.scheduled_execution import ScheduledOrder, execute_scheduled
    from ashare_lab.domain.portfolio import PortfolioState
    from ashare_lab.domain.shared import Money
    from tests.unit.application.test_minute_grid_replay import FEES
    current = bar(9, 31, 10)
    opening = datetime(2026, 9, 9, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    portfolio, fill, outcome = execute_scheduled(
        ScheduledOrder("partial", current.session.session_date, opening, quantity=requested),
        portfolio=PortfolioState(Money(D(10000))), prices=current.prices,
        session=current.session, ended_at=opening, next_session=current.next_session,
        fees=FEES, slippage_bps=D(0), slippage_cny=D(0), max_quantity=57)
    assert outcome == reason
    assert portfolio.position_quantity(SEC).value == expected
    assert (fill.quantity.value if fill else 0) == expected


def test_weekly_budget_buys_whole_lots_including_fees_and_does_not_repeat():
    prepared = PreparedMinuteReplay(
        (bar(9, 31, 10), bar(9, 32, 10), bar(10, 31, 10)), (),
        (date(2026, 9, 9), date(2026, 9, 10)))
    result = execute_minute_schedule(
        ScheduledParameters(frequency="weekly", day=3, budget_cny=D(2000)),
        prepared=prepared, exchange=AshareExchange.SHENZHEN,
        start=date(2026, 9, 9), end=date(2026, 9, 10))
    assert len(result.portfolio.fills) == 1
    assert result.portfolio.position_quantity(SEC).value == 100
    assert D(0) < D(1000000) - result.portfolio.cash.amount <= D(2000)


def test_scheduled_sell_cannot_consume_minimum_inventory():
    prepared = PreparedMinuteReplay(
        (bar(9, 31, 10), bar(10, 31, 10)), (),
        (date(2026, 9, 9), date(2026, 9, 10)))
    result = execute_minute_schedule(
        ScheduledParameters(frequency="weekly", day=4, side="sell", sizing_mode="shares",
                            quantity=100, initial_shares=100, min_shares=100),
        prepared=prepared, exchange=AshareExchange.SHENZHEN,
        start=date(2026, 9, 9), end=date(2026, 9, 10))
    assert len(result.portfolio.fills) == 1
    assert result.portfolio.position_quantity(SEC).value == 100
    assert any(e.reason == "minimum_inventory_breached" for e in result.events)
