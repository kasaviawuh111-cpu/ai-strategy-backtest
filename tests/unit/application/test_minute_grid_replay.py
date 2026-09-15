from datetime import date, datetime
from dataclasses import replace
from decimal import Decimal as D
from zoneinfo import ZoneInfo

import pytest

from ashare_lab.application.fixed_grid_orders import FixedGridOrders, GridCell
from ashare_lab.application.minute_grid_replay import Protection, ReplayBar, replay_grid
from ashare_lab.application.scheduled_execution import ScheduledOrder, investment_orders
from ashare_lab.domain.execution.bar_prices import BarPrices
from ashare_lab.domain.execution.fees import AshareExchange, FeeCalculator, FeePolicy
from ashare_lab.domain.market_data import Board, InstrumentSession, TradingStatus
from ashare_lab.domain.portfolio import PortfolioState
from ashare_lab.domain.shared import InstrumentId, Money, Price

SEC = InstrumentId("300059.SZ")
FEES = FeeCalculator(FeePolicy(AshareExchange.SHENZHEN, D("0.00025"), Money(D(5))))


def daily_intent(signal_day, target_day, *, enter=False, exit=False):
    from ashare_lab.application.daily_signal_execution import DailySignalIntent
    return DailySignalIntent(f"daily:{signal_day}", SEC,
        datetime(2026, 9, signal_day, 15, tzinfo=ZoneInfo("Asia/Shanghai")),
        date(2026, 9, target_day), enter=enter, exit=exit)


def bar(day, minute, price):
    session = InstrumentSession(SEC, date(2026, 9, day), Board.CHINEXT,
                                TradingStatus.TRADING, Price(D(10)),
                                Price(D(12)), Price(D(8)), 100, 100)
    return ReplayBar(datetime(2026, 9, day, 9, minute, tzinfo=ZoneInfo("Asia/Shanghai")),
                     BarPrices(D(price), D(price), D(price), D(price)), session,
                     date(2026, 9, day + 1), 10000)


@pytest.mark.parametrize("kind", ["grid", "conditional"])
@pytest.mark.parametrize("side", ["buy", "sell"])
def test_minute_orders_retain_partial_remainder_and_wait_for_capacity(kind, side):
    from ashare_lab.application.minute_conditional_orders import MinuteConditionalOrders
    from ashare_lab.domain.execution import CapacityMode
    from ashare_lab.domain.portfolio import PositionLot
    from ashare_lab.domain.shared import FillId, Quantity
    from ashare_lab.domain.strategy.price_plans import ConditionRule

    price = D(9 if side == "buy" else 11)
    bars = [replace(bar(9, 31 + i, price), volume_shares=volume)
            for i, volume in enumerate((1140, 0, 1140, 10000))]
    # The second bar must trade, but its tiny volume must not replace the
    # already-known 1,140-share capacity proxy used to match at that bar.
    bars[1] = replace(bars[1], volume_shares=1)
    lots = () if side == "buy" else (PositionLot(
        FillId("opening"), SEC, bar(8, 31, 10).ended_at, date(2026, 9, 8),
        date(2026, 9, 9), Quantity(200), Money(D(2000)), None),)
    policy = (FixedGridOrders([GridCell("a", D(9), D(11), 100, first_side=side)])
              if kind == "grid" else MinuteConditionalOrders([
                  ConditionRule(kind="price", side=side, direction="down" if side == "buy" else "up",
                                target_price=price, quantity=100, limit_price=price)]))
    result = replay_grid(policy, bars, PortfolioState(Money(D(1000000)), lots=lots), FEES,
        slippage_bps=D(0), capacity_mode=CapacityMode.POINT_IN_TIME_VOLUME,
        minimum_shares=100 if side == "sell" else 0, maximum_shares=100 if side == "buy" else 200,
        initial_equity_cny=D(1000000) + (D(2000) if side == "sell" else D(0)))
    fills = [event for event in result.events if event.fill]
    assert [(e.bar_index, e.fill.quantity.value, e.status) for e in fills] == [
        (1, 57, "partially_filled"), (3, 43, "filled")]
    assert any(e.bar_index == 2 and e.reason == "participation_capacity_zero"
               and e.status == "working" for e in result.events)
    assert result.portfolio.position_quantity(SEC).value == 100
    assert len({f.fill_id for f in result.portfolio.fills}) == 2
    assert not policy.rejected
    from ashare_lab.application.minute_result import minute_result_bundle
    report = minute_result_bundle(run_id="partial", result=result,
                                  initial_cash=D(1000000), snapshot_id="fixture:minute")
    assert [e.kind for e in report.activities if e.fill_id] == ["partial_fill", "fill"]
    assert next(e.reason for e in report.activities if e.kind == "partial_fill") == (
        "达到本次成交量参与率上限，委托仅部分成交。")


def test_multiple_grid_tickets_share_one_minute_capacity():
    from ashare_lab.domain.execution import CapacityMode
    bars = [replace(bar(9, 31 + i, 9), volume_shares=volume)
            for i, volume in enumerate((1140, 1, 1140, 5000, 5000))]
    result = replay_grid(FixedGridOrders([GridCell(key, D(9), D(11), 100) for key in ("a", "b")]),
        bars, PortfolioState(Money(D(1000000))), FEES, slippage_bps=D(0),
        capacity_mode=CapacityMode.POINT_IN_TIME_VOLUME, maximum_shares=200)
    assert [(e.bar_index, e.order.cell_id, e.fill.quantity.value) for e in result.events if e.fill] == [
        (1, "a", 57), (3, "a", 43), (3, "b", 14), (4, "b", 86)]
    assert result.portfolio.position_quantity(SEC).value == 200


def test_minute_scheduled_orders_honor_shared_capacity_including_first_bar():
    from ashare_lab.domain.execution import CapacityMode
    bars = [replace(bar(9, 31, 10), volume_shares=1140), bar(10, 31, 10)]
    orders = tuple(ScheduledOrder(f"build:{day}", date(2026, 9, day),
        bar(day - 1, 31, 10).ended_at, quantity=100) for day in (9, 10))
    result = replay_grid(FixedGridOrders([]), bars, PortfolioState(Money(D(1000000))), FEES,
        scheduled_orders=orders, capacity_mode=CapacityMode.POINT_IN_TIME_VOLUME)
    assert [(e.bar_index, e.fill.quantity.value, e.status) for e in result.events if e.fill] == [
        (0, 57, "partially_filled"), (1, 57, "partially_filled")]
    assert result.capacity_assumption == (
        "first_completed_minute_volume_for_opening_order_else_previous_completed_minute_volume:0.05"
    )


def test_first_open_without_predecessor_still_does_not_fill_a_zero_volume_bar():
    from ashare_lab.domain.execution import CapacityMode
    first = replace(bar(9, 31, 10), volume_shares=0)
    order = ScheduledOrder("build", date(2026, 9, 9), bar(8, 31, 10).ended_at, quantity=100)
    result = replay_grid(FixedGridOrders([]), [first], PortfolioState(Money(D(1000000))), FEES,
        scheduled_orders=(order,), capacity_mode=CapacityMode.POINT_IN_TIME_VOLUME)
    assert not result.portfolio.fills
    assert any(e.bar_index == 0 and e.reason == "no_market_trades" for e in result.events)


@pytest.mark.parametrize("anchor,expected", [
    ("each_entry_fill", [(11, 100), (14, 100)]),
    ("first_entry_fill", [(11, 100), (11, 100)]),
])
def test_holding_clock_distinguishes_each_purchase_from_first_cycle_fill(anchor, expected):
    from ashare_lab.application.scheduled_execution import ScheduledOrder
    from ashare_lab.domain.orders import OrderSide
    days = tuple(date(2026, 9, d) for d in (9, 10, 11, 14, 15))
    known = datetime(2026, 9, 8, tzinfo=ZoneInfo("Asia/Shanghai"))
    result = replay_grid(FixedGridOrders([]), [bar(day.day, 31, 10) for day in days],
        PortfolioState(Money(D(10000))), FEES, slippage_bps=D(0),
        scheduled_orders=tuple(ScheduledOrder(f"buy:{day.day}", day, known, quantity=100)
                               for day in days[:2]),
        holding_sessions=2, holding_anchor=anchor, market_sessions=days)
    assert [(f.filled_at.day, f.quantity.value) for f in result.portfolio.fills
            if f.side is OrderSide.SELL] == expected


@pytest.mark.parametrize("minimum", [0, 100])
def test_first_fill_holding_exit_preserves_floor_without_unfinished_ticket(minimum):
    from ashare_lab.application.scheduled_execution import ScheduledOrder
    days = tuple(date(2026, 9, d) for d in (9, 10, 11, 14))
    known = datetime(2026, 9, 8, tzinfo=ZoneInfo("Asia/Shanghai"))
    result = replay_grid(FixedGridOrders([]), [bar(day.day, 31, 10) for day in days],
        PortfolioState(Money(D(10000))), FEES, slippage_bps=D(0),
        scheduled_orders=(ScheduledOrder("first", days[0], known, quantity=200),),
        holding_sessions=2, holding_anchor="first_entry_fill", market_sessions=days,
        minimum_shares=minimum)
    assert result.portfolio.position_quantity(SEC).value == minimum
    assert result.unfinished_exit_quantity == 0
    assert not any(e.reason == "backtest_ended_holding_exit_unfinished" for e in result.events)
    sells = [f for f in result.portfolio.fills if f.side.value == "sell"]
    assert [(f.filled_at.day, f.quantity.value) for f in sells] == [(11, 200 - minimum)]


def test_daily_budget_entry_is_sized_within_position_value_before_fill():
    result = replay_grid(FixedGridOrders([]), [bar(9, 31, 10)],
        PortfolioState(Money(D(10000))), FEES, slippage_bps=D(0),
        daily_signals=(daily_intent(8, 9, enter=True),),
        market_sessions=tuple(date(2026, 9, d) for d in (8, 9, 10)),
        maximum_position_cny=D(2500))
    # Submitted orders satisfy lot sizing; partial executions may be odd lots.
    assert result.portfolio.position_quantity(SEC).value == 250
    assert result.portfolio.fills[0].price.amount * result.portfolio.fills[0].quantity.value <= D(2500)
    assert any(event.reason == "position_value_limit_partial_fill" for event in result.events)


def test_first_fill_holding_clock_restarts_only_after_flat():
    from ashare_lab.application.scheduled_execution import ScheduledOrder
    days = tuple(date(2026, 9, d) for d in (9, 10, 11, 14, 15, 16))
    known = datetime(2026, 9, 8, tzinfo=ZoneInfo("Asia/Shanghai"))
    result = replay_grid(FixedGridOrders([]), [bar(day.day, 31, 10) for day in days],
        PortfolioState(Money(D(10000))), FEES, slippage_bps=D(0),
        scheduled_orders=tuple(ScheduledOrder(f"buy:{day.day}", day, known, quantity=100)
                               for day in (days[0], days[3])),
        holding_sessions=2, holding_anchor="first_entry_fill", market_sessions=days)
    assert [(f.side.value, f.filled_at.day) for f in result.portfolio.fills] == [
        ("buy", 9), ("sell", 11), ("buy", 14), ("sell", 16)]
    assert result.closed_position_cycles == 2


def test_daily_entry_minute_protection_and_later_cycle_share_one_ledger():
    # Entry-bar H/L is not allowed to protect newly acquired inventory.
    opening = replace(bar(9, 31, 10), prices=BarPrices(D(10), D(11), D(9), D(10)))
    bars = [opening, bar(9, 32, D("10.6")), bar(9, 33, D("10.5")),
            bar(10, 31, 11), bar(10, 32, 11), bar(11, 31, 10)]
    result = replay_grid(FixedGridOrders([]), bars, PortfolioState(Money(D(10000))), FEES,
        slippage_bps=D(0), protection=Protection(take_profit=D(".05"), stop_loss=D(".03")),
        daily_signals=(daily_intent(8, 9, enter=True), daily_intent(9, 10, enter=True),
                       daily_intent(10, 11, enter=True)),
        market_sessions=tuple(date(2026, 9, d) for d in (8, 9, 10, 11)))
    fills = result.portfolio.fills
    assert [(f.side.value, f.filled_at.date()) for f in fills] == [
        ("buy", date(2026, 9, 9)), ("sell", date(2026, 9, 10)), ("buy", date(2026, 9, 11))]
    assert fills[0].filled_at.hour == 9 and fills[0].filled_at.minute == 30
    triggers = [e for e in result.events if e.order.cell_id == "protection" and e.status == "submitted"]
    assert triggers[0].bar_index == 1
    assert any(e.reason == "t_plus_one_locked" for e in result.events)
    assert any(e.order.order_id == "daily:9" and e.reason == "exit_takeover" for e in result.events)
    assert result.closed_position_cycles == 1
    assert result.unfinished_exit_quantity == 0
    entry = next(e for e in result.events if e.order.order_id == "daily:8" and e.fill)
    assert entry.signal_at == daily_intent(8, 9, enter=True).confirmed_at
    assert entry.effective_at == fills[0].filled_at


def test_minute_trailing_drawdown_distinguishes_known_peak_from_same_bar_ambiguity():
    opening = bar(9, 31, 10)
    ambiguous_bar = replace(bar(9, 32, 11), prices=BarPrices(D(11), D(12), D("10.7"), D(11)))
    ambiguous = replay_grid(
        FixedGridOrders([]), [opening, ambiguous_bar, bar(10, 31, 10)],
        PortfolioState(Money(D(10000))), FEES, slippage_bps=D(0),
        protection=Protection(trailing_drawdown=D(".10")),
        daily_signals=(daily_intent(8, 9, enter=True),),
        market_sessions=tuple(date(2026, 9, d) for d in (8, 9, 10)),
    )
    submitted = next(e for e in ambiguous.events
                     if e.order.cell_id == "protection" and e.status == "submitted")
    assert submitted.reason == "trailing_drawdown"
    assert submitted.ambiguous_intrabar_order is True

    known_peak = replay_grid(
        FixedGridOrders([]),
        [opening, replace(bar(9, 32, 12), prices=BarPrices(D(11), D(12), D(11), D(12))),
         replace(bar(9, 33, 11), prices=BarPrices(D(11), D("11.2"), D("10.7"), D(11))),
         bar(10, 31, 10)],
        PortfolioState(Money(D(10000))), FEES, slippage_bps=D(0),
        protection=Protection(trailing_drawdown=D(".10")),
        daily_signals=(daily_intent(8, 9, enter=True),),
        market_sessions=tuple(date(2026, 9, d) for d in (8, 9, 10)),
    )
    submitted = next(e for e in known_peak.events
                     if e.order.cell_id == "protection" and e.status == "submitted")
    assert submitted.bar_index == 2
    assert submitted.ambiguous_intrabar_order is False


def test_daily_signals_do_not_pyramid_and_exit_uses_actual_inventory():
    result = replay_grid(FixedGridOrders([]), [bar(9, 31, 10), bar(10, 31, 11), bar(11, 31, 11)],
        PortfolioState(Money(D(10000))), FEES, slippage_bps=D(0),
        daily_signals=(daily_intent(8, 9, enter=True), daily_intent(9, 10, enter=True),
                       daily_intent(10, 11, enter=True, exit=True)),
        market_sessions=tuple(date(2026, 9, d) for d in (8, 9, 10, 11)))
    assert len(result.portfolio.fills) == 2
    assert result.portfolio.fills[0].quantity == result.portfolio.fills[1].quantity
    assert result.portfolio.position_quantity(SEC).value == 0
    assert not any(e.order.order_id == "daily:9" for e in result.events)


def test_new_entry_policy_accumulates_and_exit_sells_actual_total():
    intents = tuple(replace(daily_intent(day, day + 1, enter=True, exit=day == 10),
        cash_fraction=D("0.5"), position_policy="accumulate_on_new_entry_signal")
        for day in (8, 9, 10))
    result = replay_grid(FixedGridOrders([]), [bar(day, 31, 10) for day in (9, 10, 11)],
        PortfolioState(Money(D(10000))), FEES, slippage_bps=D(0),
        daily_signals=intents, market_sessions=tuple(date(2026, 9, d) for d in (8, 9, 10, 11)))
    fills = result.portfolio.fills
    assert [fill.side.value for fill in fills] == ["buy", "buy", "sell"]
    assert fills[2].quantity.value == fills[0].quantity.value + fills[1].quantity.value
    assert result.portfolio.position_quantity(SEC).value == 0


def test_scheduled_entries_accumulate_daily_exit_wins_and_future_schedule_resumes():
    known = datetime(2026, 9, 7, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    orders = tuple(ScheduledOrder(f"schedule:{day}", date(2026, 9, day), known,
                                  quantity=100) for day in (8, 9, 10, 11))
    result = replay_grid(
        FixedGridOrders([]), [bar(day, 31, 10) for day in (8, 9, 10, 11)],
        PortfolioState(Money(D(10000))), FEES, slippage_bps=D(0),
        scheduled_orders=orders, daily_signals=(daily_intent(9, 10, exit=True),),
        market_sessions=tuple(date(2026, 9, d) for d in (8, 9, 10, 11)),
        maximum_shares=1000,
    )
    assert [(f.side.value, f.quantity.value, f.filled_at.day) for f in result.portfolio.fills] == [
        ("buy", 100, 8), ("buy", 100, 9), ("sell", 200, 10), ("buy", 100, 11)]
    assert result.portfolio.position_quantity(SEC).value == 100
    assert any(e.order.order_id == "schedule:10" and e.status == "skipped"
               and e.reason == "exit_takeover" for e in result.events)


def test_daily_signal_does_not_roll_over_security_suspension():
    from ashare_lab.domain.market_data import DailyBar
    from ashare_lab.domain.shared import Quantity
    suspended = replace(bar(9, 31, 10).session, status=TradingStatus.SUSPENDED)
    close = datetime(2026, 9, 9, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    daily = DailyBar(SEC, suspended.session_date, Price(D(10)), Price(D(10)), Price(D(10)), Price(D(10)),
                     Quantity(0), D(0), close)
    result = replay_grid(FixedGridOrders([]), [bar(10, 31, 10)], PortfolioState(Money(D(10000))), FEES,
        daily_signals=(daily_intent(8, 9, enter=True),),
        market_sessions=tuple(date(2026, 9, d) for d in (8, 9, 10)), nontrading_closes=((suspended, daily),))
    assert not result.portfolio.fills
    assert any(e.order.order_id == "daily:8" and e.reason == "security_not_trading" for e in result.events)


def test_ordinary_daily_exit_failure_does_not_become_minute_protection_retry():
    result = replay_grid(FixedGridOrders([]),
        [bar(9, 31, 10), bar(10, 31, 8), bar(10, 32, 10), bar(11, 31, 10)],
        PortfolioState(Money(D(10000))), FEES, slippage_bps=D(0),
        daily_signals=(daily_intent(8, 9, enter=True), daily_intent(9, 10, exit=True)),
        market_sessions=tuple(date(2026, 9, d) for d in (8, 9, 10, 11)))
    assert len(result.portfolio.fills) == 1
    assert any(event.reason == "sell_at_lower_limit" and event.status == "skipped"
               for event in result.events)
    assert result.unfinished_exit_quantity == 0


def test_daily_signal_rejects_skipped_market_day_or_unsourced_activation():
    import pytest
    from ashare_lab.application.daily_signal_execution import DailySignalIntent
    with pytest.raises(ValueError, match="next market session"):
        daily_intent(8, 10, enter=True).validate_calendar(tuple(date(2026, 9, d) for d in (8, 9, 10)))
    with pytest.raises(ValueError, match="after close"):
        DailySignalIntent("early", SEC, datetime(2026, 9, 8, 14, tzinfo=ZoneInfo("Asia/Shanghai")),
                          date(2026, 9, 9), enter=True)
    with pytest.raises(ValueError, match="sourced session"):
        replay_grid(FixedGridOrders([]), [bar(10, 31, 10)], PortfolioState(Money(D(10000))), FEES,
                    daily_signals=(daily_intent(8, 9, enter=True),),
                    market_sessions=tuple(date(2026, 9, d) for d in (8, 9, 10)))


def test_daily_signal_checks_evidence_clock_identity_and_opening_bar():
    import pytest
    intent = daily_intent(8, 9, enter=True)
    opening = datetime(2026, 9, 9, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    with pytest.raises(ValueError, match="evidence must be known"):
        replace(intent, available_at=opening)
    # A dependency released after signal close but before next opening is not
    # falsely stamped as known at yesterday's close.
    late = replace(intent, available_at=opening.replace(minute=0))
    result = replay_grid(FixedGridOrders([]), [bar(9, 31, 10)], PortfolioState(Money(D(10000))), FEES,
                        daily_signals=(late,), market_sessions=(date(2026, 9, 8), date(2026, 9, 9)))
    assert all(e.signal_at == late.available_at for e in result.events)
    for signal, bars, message in (
        (replace(intent, instrument_id=InstrumentId("600519.SH")), [bar(9, 31, 10)], "instrument differs"),
        (intent, [bar(9, 32, 10)], "opening minute"),
    ):
        with pytest.raises(ValueError, match=message):
            replay_grid(FixedGridOrders([]), bars, PortfolioState(Money(D(10000))), FEES,
                        daily_signals=(signal,), market_sessions=(date(2026, 9, 8), date(2026, 9, 9)))


def test_grid_hits_then_fills_next_bar_and_sells_next_session():
    bars = [bar(9, 31, 9), bar(9, 32, 9), bar(9, 33, 11), bar(10, 31, 11)]
    result = replay_grid(FixedGridOrders([GridCell("a", D(9), D(11), 100)]),
                         bars, PortfolioState(Money(D(1000000))), FEES)
    fills = [e for e in result.events if e.status == "filled"]
    assert [e.bar_index for e in fills] == [1, 3]
    assert result.portfolio.position_quantity(SEC).value == 0
    assert result.portfolio.cash.amount == D("1000189.43")
    assert len(result.portfolio.ledger_entries) == 2
    from ashare_lab.application.minute_result import minute_result_bundle
    report = minute_result_bundle(run_id="cash-reconciliation", result=result,
                                  initial_cash=D(1000000), snapshot_id="fixture:minute")
    activities = [item for item in report.activities if item.kind == "fill"]
    details = [item.model_dump(mode="json", by_alias=True)["executionDetails"]
               for item in activities]
    assert len(details) == 2
    assert D(str(details[0]["cashDeltaCny"])) < 0
    assert D(str(details[1]["cashDeltaCny"])) > 0
    assert D(str(details[0]["stampTaxCny"])) == 0
    assert D(str(details[1]["stampTaxCny"])) > 0
    assert D(1000000) + sum(D(str(item["cashDeltaCny"])) for item in details) == result.portfolio.cash.amount
    for activity, detail, fill in zip(activities, details, result.portfolio.fills, strict=True):
        assert D(str(activity.notional_cny)) == fill.gross_amount.amount
        assert D(str(detail["totalFeesCny"])) == fill.fees.total.amount


def test_suspended_tail_adds_daily_valuation_without_bars_or_fills():
    from ashare_lab.domain.market_data import DailyBar
    from ashare_lab.domain.shared import Quantity
    bars = [bar(9, 31, 9), bar(9, 32, 9)]
    result = replay_grid(FixedGridOrders([GridCell("a", D(9), D(11), 100)]),
                         bars, PortfolioState(Money(D(1000000))), FEES)
    closes = []
    for day in (10, 11):
        session = replace(bar(day, 31, 9).session, status=TradingStatus.SUSPENDED)
        closing = datetime(2026, 9, day, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
        daily = DailyBar(SEC, session.session_date, Price(D(9)), Price(D(9)), Price(D(9)), Price(D(9)),
                         Quantity(0), D(0), closing)
        closes.append((session, daily))
    def replay_with_closed_sessions(items):
        return replay_grid(FixedGridOrders([GridCell("a", D(9), D(11), 100)]),
                           bars, PortfolioState(Money(D(1000000))), FEES,
                           nontrading_closes=tuple(items))
    extended = replay_with_closed_sessions(closes)
    assert extended.events == result.events
    assert extended.portfolio == result.portfolio
    assert extended.equity[-1].observed_at == closes[-1][1].available_at
    assert all(point.equity == result.equity[-1].equity and point.shares == 100 for point in extended.equity[-2:])
    assert len(extended.equity) == len(result.equity) + 2
    import pytest
    session, daily = closes[0]
    for invalid in (
        [closes[0], closes[0]],
        [(session, replace(daily, instrument_id=InstrumentId("600519.SH")))],
        [(session, replace(daily, session_date=date(2026, 9, 11)))],
        [(replace(session, session_date=date(2026, 9, 9)),
          replace(daily, session_date=date(2026, 9, 9)))],
    ):
        with pytest.raises(ValueError, match="nontrading session"):
            replay_with_closed_sessions(invalid)


def test_t_plus_one_not_misreported_as_cash_failure():
    bars = [bar(9, 31, 9), bar(9, 32, 9), bar(9, 33, 11), bar(9, 34, 11)]
    result = replay_grid(FixedGridOrders([GridCell("a", D(9), D(11), 100)]),
                         bars, PortfolioState(Money(D(1000000))), FEES)
    failures = [e for e in result.events if e.status == "rejected"]
    assert [e.reason for e in failures] == ["t_plus_one_locked"]
    assert result.portfolio.position_quantity(SEC).value == 100
    assert len(result.portfolio.fills) == 1


def test_cash_start_builds_inventory_floor_but_cannot_sell_it_next_day():
    result = replay_grid(FixedGridOrders([GridCell("a", D(9), D(11), 100)]),
        [bar(9, 31, 9), bar(9, 32, 9), bar(9, 33, 11), bar(10, 31, 11)],
        PortfolioState(Money(D(1000000))), FEES, minimum_shares=100)
    assert len(result.portfolio.fills) == 1
    assert result.portfolio.position_quantity(SEC).value == 100
    assert any(event.reason == "minimum_inventory_reached" for event in result.events)


def test_zero_volume_neither_triggers_nor_fills_and_preserves_pending_limit():
    bars = [replace(bar(9, 31, 9), volume_shares=0), bar(9, 32, 9),
            replace(bar(9, 33, 9), volume_shares=0), bar(9, 34, 9)]
    result = replay_grid(FixedGridOrders([GridCell("a", D(9), D(11), 100)]),
                         bars, PortfolioState(Money(D(1000000))), FEES)
    assert [(e.bar_index, e.status) for e in result.events] == [
        (1, "submitted"), (2, "working"), (3, "filled")]
    assert result.events[1].reason == "no_market_trades"
    assert result.events[1].order.order_id == result.events[2].order.order_id
    assert len(result.portfolio.fills) == 1


def test_zero_volume_open_skips_market_schedule_but_limit_waits():
    known = datetime(2026, 9, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    bars = [replace(bar(9, 31, 10), volume_shares=0), bar(9, 32, 10)]
    orders = (ScheduledOrder("market", date(2026, 9, 9), known, quantity=100),
              ScheduledOrder("limit", date(2026, 9, 9), known, quantity=100, limit_price=D(10)))
    result = replay_grid(FixedGridOrders([]), bars, PortfolioState(Money(D(1000000))), FEES,
                         scheduled_orders=orders)
    assert [f.order_id.value for f in result.portfolio.fills] == ["limit"]
    assert any(e.order.order_id == "market" and e.status == "skipped"
               and e.reason == "no_market_trades" for e in result.events)


def test_insufficient_cash_does_not_repeat_rejected_grid_each_minute():
    result = replay_grid(FixedGridOrders([GridCell("a", D(9), D(11), 100)]),
                         [bar(9, 31, 9), bar(9, 32, 9), bar(9, 33, 9)],
                         PortfolioState(Money(D(900))), FEES)
    assert [e.reason for e in result.events if e.status == "rejected"] == ["insufficient_cash_including_fees"]
    assert not result.portfolio.fills
    assert len([e for e in result.events if e.status == "submitted"]) == 1


def test_protection_uses_prebar_inventory_and_retries_t1_then_takes_over():
    bars = [bar(9, 31, 9), bar(9, 32, 9), bar(9, 33, 10),
            bar(9, 34, 10), bar(10, 31, 10)]
    result = replay_grid(FixedGridOrders([GridCell("a", D(9), D(10), 100)]),
                         bars, PortfolioState(Money(D(1000000))), FEES,
                         protection=Protection(take_profit=D("0.05")))
    exits = [e for e in result.events if e.order.cell_id == "protection"]
    assert [(e.bar_index, e.status) for e in exits] == [(2, "submitted"), (3, "retry_pending"),
                                                      (3, "cancelled"), (3, "submitted"), (4, "filled")]
    assert exits[1].reason == "t_plus_one_locked"
    assert any(e.reason == "exit_takeover" for e in result.events)
    assert result.portfolio.position_quantity(SEC).value == 0
    assert result.unfinished_exit_quantity == 0


def test_both_protection_levels_use_old_cost_and_do_not_fake_end_liquidation():
    from dataclasses import replace
    bars = [bar(9, 31, 9), bar(9, 32, 9),
            replace(bar(9, 33, 9), prices=BarPrices(D(9), D(10), D(8), D(9)))]
    result = replay_grid(FixedGridOrders([GridCell("a", D(9), D(11), 100)]),
                         bars, PortfolioState(Money(D(1000000))), FEES,
                         protection=Protection(take_profit=D("0.05"), stop_loss=D("0.05")))
    exits = [e for e in result.events if e.order.cell_id == "protection"]
    assert exits[0].reason == "stop_loss" and exits[0].ambiguous_intrabar_order
    assert exits[-1].reason == "backtest_ended_exit_unfinished"
    assert result.unfinished_exit_quantity == 100
    assert len(result.portfolio.fills) == 1


def test_holding_period_tracks_each_fill_not_last_addition():
    bars = [bar(9, 31, 9), bar(9, 32, 9), bar(10, 31, 8), bar(10, 32, 8), bar(11, 31, 9)]
    result = replay_grid(FixedGridOrders([GridCell("a", D(9), D(11), 100),
                                          GridCell("b", D(8), D(11), 100)]),
                         bars, PortfolioState(Money(D(1000000))), FEES,
                         holding_sessions=2, market_sessions=tuple(date(2026, 9, d) for d in [9, 10, 11, 14]))
    exits = [e for e in result.events if e.reason == "holding_period_open"]
    assert len(exits) == 1 and exits[0].fill.quantity.value == 100
    assert exits[0].bar_index == 4
    assert result.portfolio.position_quantity(SEC).value == 100
    assert result.portfolio.lots[0].acquired_on == date(2026, 9, 10)


def test_holding_exit_retries_next_open_not_next_minute_after_suspension():
    from dataclasses import replace
    suspended = bar(10, 31, 9)
    suspended = replace(suspended, session=replace(suspended.session, status=TradingStatus.SUSPENDED))
    result = replay_grid(FixedGridOrders([GridCell("a", D(9), D(11), 100)]),
                         [bar(9, 31, 9), bar(9, 32, 9), suspended,
                          replace(bar(10, 32, 9), session=suspended.session), bar(11, 31, 9)],
                         PortfolioState(Money(D(1000000))), FEES,
                         holding_sessions=1, market_sessions=tuple(date(2026, 9, d) for d in [9, 10, 11]))
    exits = [e for e in result.events if e.order.cell_id == "holding_period"]
    assert [(e.bar_index, e.status) for e in exits] == [(2, "retry_pending"), (4, "filled")]
    assert exits[0].order.order_id == exits[1].order.order_id


def test_holding_partial_fills_share_bar_capacity_and_retry_only_next_open():
    from ashare_lab.application.scheduled_execution import execute_scheduled
    from ashare_lab.domain.execution import CapacityMode
    portfolio = PortfolioState(Money(D(1000000)))
    acquired = bar(9, 31, 10)
    for order_id in ("first", "second"):
        portfolio, fill, _ = execute_scheduled(
            ScheduledOrder(order_id, acquired.session.session_date, acquired.ended_at.replace(minute=30), quantity=100),
            portfolio=portfolio, prices=acquired.prices, session=acquired.session,
            ended_at=acquired.ended_at.replace(minute=30), next_session=acquired.next_session,
            fees=FEES, slippage_bps=D(0), slippage_cny=D(0))
        assert fill is not None
    result = replay_grid(FixedGridOrders([]),
        [replace(acquired, volume_shares=1140), bar(10, 31, 10),
         replace(bar(10, 32, 10), volume_shares=2000), bar(11, 31, 10), bar(14, 31, 10)],
        portfolio, FEES, slippage_bps=D(0), holding_sessions=1,
        market_sessions=tuple(date(2026, 9, d) for d in (9, 10, 11, 14, 15)),
        capacity_mode=CapacityMode.POINT_IN_TIME_VOLUME, initial_equity_cny=D(1000000))
    exits = [event for event in result.events if event.order.cell_id == "holding_period" and event.fill]
    assert [(event.bar_index, event.fill.quantity.value, event.status) for event in exits] == [
        (1, 57, "partially_filled"), (3, 43, "filled"),
        (3, 57, "partially_filled"), (4, 43, "filled")]
    assert all(event.fill.filled_at.hour == 9 and event.fill.filled_at.minute == 30 for event in exits)
    assert exits[0].order.order_id == exits[1].order.order_id
    assert exits[2].order.order_id == exits[3].order.order_id
    assert result.portfolio.position_quantity(SEC).value == 0
    assert result.unfinished_holding_exit_quantity == 0


def test_protective_limit_retains_price_across_day_expiry_and_never_forces_market_fill():
    result = replay_grid(FixedGridOrders([GridCell("a", D(9), D(12), 100)]),
                         [bar(9, 31, 9), bar(9, 32, 9), bar(9, 33, 10),
                          bar(10, 31, 9), bar(10, 32, 10), bar(11, 31, 11)],
                         PortfolioState(Money(D(1000000))), FEES,
                         protection=Protection(take_profit=D("0.05"), limit_price=D(11)))
    exits = [e for e in result.events if e.order.cell_id == "protection"]
    assert all(e.order.limit_price == D(11) for e in exits)
    assert [e.bar_index for e in exits if e.status == "working"] == [3, 4]
    filled = [e for e in exits if e.status == "filled"]
    assert len(filled) == 1 and filled[0].bar_index == 5
    assert filled[0].fill.price.amount == D(11)
    assert filled[0].order.order_id != exits[0].order.order_id
    assert filled[0].order.signal_bar == exits[0].order.signal_bar == 2


def test_overdue_inventory_is_reported_without_forced_liquidation_or_counting_new_lots():
    from ashare_lab.application.minute_result import minute_result_bundle
    result = replay_grid(FixedGridOrders([GridCell("a", D(9), D(12), 100),
                                         GridCell("b", D(8), D(12), 100)]),
                         [bar(9, 31, 9), bar(9, 32, 9), bar(10, 31, 8), bar(10, 32, 8)],
                         PortfolioState(Money(D(1000000))), FEES,
                         holding_sessions=1, market_sessions=(date(2026, 9, 9), date(2026, 9, 10)))
    assert result.portfolio.position_quantity(SEC).value == 200
    assert result.unfinished_holding_exit_quantity == 100
    assert result.unfinished_exit_quantity == 0
    assert len(result.portfolio.fills) == 2  # only the actual buys
    ended = [e for e in result.events if e.reason == "backtest_ended_holding_exit_unfinished"]
    assert len(ended) == 1 and ended[0].order.quantity == 100
    bundle = minute_result_bundle(run_id="holding-end", result=result,
                                  initial_cash=D(1000000), snapshot_id="fixture:minute")
    assert any("100股已到持有期限" in warning for warning in bundle.summary.warnings)


def test_later_grid_sell_removes_unfinished_holding_exit():
    result = replay_grid(FixedGridOrders([GridCell("a", D(9), D(10), 100)]),
                         [bar(9, 31, 9), bar(9, 32, 9), bar(10, 31, 8),
                          bar(10, 32, 10), bar(10, 33, 10)],
                         PortfolioState(Money(D(1000000))), FEES,
                         holding_sessions=1, market_sessions=(date(2026, 9, 9), date(2026, 9, 10)))
    assert any(e.reason == "sell_at_lower_limit" for e in result.events)
    assert result.portfolio.position_quantity(SEC).value == 0
    assert result.unfinished_holding_exit_quantity == 0
    assert not any(e.reason == "backtest_ended_holding_exit_unfinished" for e in result.events)


def test_investment_uses_budget_with_fees_and_skips_without_catchup():
    known = datetime(2026, 9, 1, tzinfo=ZoneInfo("Asia/Shanghai"))
    sessions = (date(2026, 9, 9), date(2026, 9, 16), date(2026, 9, 17))
    orders = investment_orders(sessions=sessions, start=sessions[0], end=sessions[-1],
                               frequency="weekly", day=3, budget=D(1000), known_at=known)
    result = replay_grid(FixedGridOrders([]), [bar(9, 31, 9), bar(16, 31, 9), bar(17, 31, 9)],
                         PortfolioState(Money(D(1500))), FEES, scheduled_orders=orders)
    assert len(result.portfolio.fills) == 1
    assert result.portfolio.fills[0].quantity.value == 100
    assert [e.reason for e in result.events if e.status == "skipped"] == ["insufficient_cash_including_fees"]
    assert not any(e.bar_index == 2 for e in result.events)


def test_close_limit_uses_close_not_intrabar_high():
    from dataclasses import replace
    from ashare_lab.domain.orders import OrderSide
    known = datetime(2026, 9, 1, tzinfo=ZoneInfo("Asia/Shanghai"))
    close = replace(bar(10, 31, 9), ended_at=datetime(2026, 9, 10, 15, tzinfo=ZoneInfo("Asia/Shanghai")),
                    prices=BarPrices(D(9), D(11), D(9), D(9)))
    orders = (ScheduledOrder("buy", date(2026, 9, 9), known, quantity=100),
              ScheduledOrder("sell", date(2026, 9, 10), known, side=OrderSide.SELL,
                             at="close", quantity=100, limit_price=D(10)))
    result = replay_grid(FixedGridOrders([]), [bar(9, 31, 9), bar(10, 31, 9), close],
                         PortfolioState(Money(D(1000000))), FEES, scheduled_orders=orders)
    assert result.portfolio.position_quantity(SEC).value == 100
    assert len(result.portfolio.fills) == 1
    assert result.events[-1].reason == "limit_not_reached"


def test_minute_report_preserves_fill_times_and_only_marks_actual_fills():
    from ashare_lab.application.minute_result import minute_result_bundle
    result = replay_grid(FixedGridOrders([GridCell("a", D(9), D(11), 100)]),
                         [bar(9, 31, 9), bar(9, 32, 9), bar(9, 33, 11), bar(9, 34, 11)],
                         PortfolioState(Money(D(1000000))), FEES)
    bundle = minute_result_bundle(run_id="minute-test", result=result,
                                  initial_cash=D(1000000), snapshot_id="fixture:minute")
    filled = [event for event in bundle.activities if event.kind == "fill"]
    assert len(filled) == 1 and filled[0].occurred_at.minute == 32
    ledger_fill = result.portfolio.fills[0]
    assert filled[0].notional_cny == float(ledger_fill.gross_amount.amount)
    assert filled[0].fill_id == str(ledger_fill.fill_id)
    details = filled[0].model_dump(mode="json", by_alias=True)["executionDetails"]
    assert D(str(details["totalFeesCny"])) == ledger_fill.fees.total.amount
    assert D(str(details["cashDeltaCny"])) == -ledger_fill.gross_amount.amount - ledger_fill.fees.total.amount
    assert sum(D(str(details[key])) for key in (
        "commissionCny", "stampTaxCny", "transferFeeCny", "otherFeesCny",
    )) == ledger_fill.fees.total.amount
    assert filled[0].time_quality == "minute_bar_end_proxy"
    assert bundle.audit.open_position_shares == 100
    assert bundle.audit.result_hash.startswith("sha256:")
    assert bundle.summary.benchmark_comparison_status == "comparable"
    assert bundle.summary.trade_count == 0
    rejected = [event for event in bundle.activities if event.outcome_reason == "t_plus_one_locked"]
    assert len(rejected) == 1 and rejected[0].status == "rejected"
    assert bundle.series[-1].equity == float(result.equity[-1].equity / D(10000))


def test_grid_inventory_and_position_value_limits_are_executed():
    for constraints, reason in [({"maximum_shares": 50}, "maximum_inventory_exceeded"),
                                 ({"maximum_position_cny": D(800)}, "maximum_position_value_exceeded")]:
        result = replay_grid(FixedGridOrders([GridCell("a", D(9), D(11), 100)]),
                             [bar(9, 31, 9), bar(9, 32, 9)], PortfolioState(Money(D(1000000))), FEES,
                             **constraints)
        assert not result.portfolio.fills
        assert [e.reason for e in result.events if e.status == "rejected"] == [reason]


def test_minute_report_pins_complete_input_manifest_only_with_runtime_evidence():
    import json
    from hashlib import sha256
    from ashare_lab.application.minute_result import minute_result_bundle
    from ashare_lab.domain.strategy import StrategySpec, canonical_hash
    strategy = StrategySpec.model_validate(dict(
        catalog=dict(catalog_id="cn_a.signals", release_version="2026.09.01"),
        instrument=dict(symbol="300059.SZ"),
        trading_plan=dict(kind="grid", parameters=dict(anchor_price="10", lower_price="8",
            upper_price="12", spacing="1", observation="minute_bar")),
        execution=dict(position_policy="bounded_inventory"),
        backtest=dict(start="2026-09-09", end="2026-09-09", initial_cash_cny=1000000)))
    result = replay_grid(FixedGridOrders([GridCell("a", D(9), D(11), 100)]),
        [bar(9, 31, 9), bar(9, 32, 9)], PortfolioState(Money(D(1000000))), FEES)
    source = dict(minute=dict(snapshotId="fixture:minute"), calendar=dict(sourceSha256="a" * 64),
                  corporateActions=dict(status="not_connected"))
    args = dict(run_id="evidence-test", result=result, initial_cash=D(1000000),
                snapshot_id="fixture:minute", source_evidence=source, strategy=strategy)
    assert minute_result_bundle(**args).summary.run_evidence is None
    runtime = dict(catalog_hash="sha256:" + "b" * 64, code_revision="test-worktree", engine_version="test-engine")
    bundle = minute_result_bundle(**args, runtime_evidence=runtime)
    evidence = bundle.summary.run_evidence
    assert evidence is not None
    ledger = json.loads(bundle.audit.price_plan_ledger)
    inputs = {key: ledger[key] for key in ("snapshotId", "sourceEvidence")}
    digest = sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert evidence.data_snapshot_checksum == "sha256:" + digest
    assert evidence.strategy_hash == canonical_hash(strategy.model_dump(mode="json"))
    assert evidence.code_revision == "test-worktree"
    changed = dict(source, calendar=dict(sourceSha256="c" * 64))
    other = minute_result_bundle(**{**args, "source_evidence": changed}, runtime_evidence=runtime)
    assert other.summary.run_evidence.data_snapshot_checksum != evidence.data_snapshot_checksum
    assert any("公司行动数据尚未接入" in warning for warning in bundle.summary.warnings)


def test_compiler_keeps_yuan_percent_and_order_price_separate():
    from ashare_lab.application.minute_grid_plan import compile_minute_grid
    from ashare_lab.domain.strategy.price_plans import GridParameters
    params = GridParameters(anchor_price=D(100), lower_price=D(80), upper_price=D(120),
                            spacing_mode="anchor_percent", buy_spacing=D(3), sell_spacing=D(1),
                            price_mode="fixed_limit", buy_limit=D(95), sell_limit=D(105))
    plan = compile_minute_grid(params, first_open=D(100))
    first = next(c for c in plan.cells if c.cell_id == "buy:1")
    assert (first.buy_price, first.sell_price) == (D(97), D(98))
    assert (first.buy_limit, first.sell_limit) == (D(95), D(105))
    yuan = compile_minute_grid(params.model_copy(update={"spacing_mode": "cny"}), first_open=D(100))
    assert yuan.cells[0].buy_price == D(97)
    different = compile_minute_grid(params.model_copy(update={"anchor_price": D(90)}), first_open=D(90))
    assert different.cells[0].buy_price == D("87.30")


def test_compiled_plan_initial_build_flows_through_actual_cash_and_t1_ledger():
    from ashare_lab.application.minute_grid_plan import execute_minute_grid
    from ashare_lab.application.minute_replay_input import PreparedMinuteReplay
    from ashare_lab.domain.strategy.price_plans import GridParameters
    params = GridParameters(anchor_mode="first_open", lower_price=D(8), upper_price=D(12),
                            spacing=D(1), initial_shares=100, min_shares=100,
                            initial_cash_cny=D(1000000), max_shares=100,
                            startup_mode="wait_for_crossing", price_mode="grid_limit")
    prepared = PreparedMinuteReplay((bar(9, 31, 10), bar(9, 32, 9), bar(9, 33, 9)), ())
    result = execute_minute_grid(params, prepared=prepared, exchange=AshareExchange.SHENZHEN)
    assert len(result.portfolio.fills) == 1
    assert result.portfolio.fills[0].order_id.value == "grid-initial-build"
    assert result.portfolio.position_quantity(SEC).value == 100
    assert result.portfolio.sellable_quantity(SEC, date(2026, 9, 9)).value == 0
    assert result.portfolio.cash.amount == D("998994.99")
    assert any(event.reason == "maximum_inventory_exceeded" for event in result.events)
