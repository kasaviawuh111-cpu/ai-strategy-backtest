"""Adapter contract checks, not real-minute-data or public-flow acceptance."""
import json
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from ashare_lab.application.backtest_submission import BacktestRunConfig
from ashare_lab.application.daily_signal_execution import execute_hybrid_signals
from ashare_lab.application.daily_signal_execution import prepare_daily_signal_intents
from ashare_lab.application.daily_signal_execution import DailyPositionRiskObserver
from ashare_lab.application.minute_replay_input import PreparedMinuteReplay, MinuteReplayDataError
from ashare_lab.domain.execution import CapacityMode, LimitHandling
from ashare_lab.domain.signals import SignalFact
from ashare_lab.domain.strategy import StrategySpec, HybridExecutionPolicy
from tests.unit.application.test_minute_grid_replay import SEC, FEES, bar


@pytest.mark.parametrize("retry", ["none", "next_session", "unfinished"])
@pytest.mark.parametrize("trigger,close", [("take_profit", "11"), ("stop_loss", "9"), ("trailing_drawdown", "9")])
def test_daily_position_exit_uses_actual_entry_and_next_open(trigger, close, retry):
    from ashare_lab.application.fixed_grid_orders import FixedGridOrders
    from ashare_lab.application.minute_grid_replay import replay_grid
    from ashare_lab.domain.market_data import DailyBar
    from ashare_lab.domain.portfolio import PortfolioState
    from ashare_lab.domain.shared import Money, Price, Quantity
    from ashare_lab.domain.strategy import PositionReturnExit, TrailingDrawdownExit
    from tests.unit.application.test_minute_grid_replay import daily_intent
    close_at = datetime(2026, 9, 9, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    daily = DailyBar(SEC, date(2026, 9, 9), Price(Decimal(10)),
        Price(Decimal(11)), Price(Decimal(9)), Price(Decimal(close)),
        Quantity(10000), Decimal(100000), close_at)
    sessions = tuple(date(2026, 9, day) for day in (8, 9, 10, 11))
    observer = DailyPositionRiskObserver(
        rules=(TrailingDrawdownExit(threshold_pct=5) if trigger == "trailing_drawdown"
               else PositionReturnExit(trigger=trigger, threshold_pct=5),),
        bars=(daily,), market_sessions=sessions)
    minutes = [bar(9, 31, 10), replace(bar(9, 32, close), ended_at=close_at),
               bar(10, 31, "10.5" if retry == "none" else "8")]
    if retry == "next_session":
        minutes.append(bar(11, 31, "10.5"))
    result = replay_grid(FixedGridOrders([]), minutes,
        PortfolioState(Money(Decimal(10000))), FEES,
        slippage_bps=Decimal(0), daily_signals=(daily_intent(8, 9, enter=True),),
        market_sessions=sessions, daily_position_risk=observer)
    expected = [("buy", date(2026, 9, 9), Decimal(10))]
    if retry != "unfinished":
        expected.append(("sell", date(2026, 9, 11 if retry == "next_session" else 10), Decimal("10.5")))
        assert observer.anchor is None and observer.pending is None
        assert result.unfinished_exit_quantity == 0
    else:
        assert observer.pending is not None
        assert result.unfinished_exit_quantity == result.portfolio.position_quantity(SEC).value > 0
    assert [(fill.side.value, fill.filled_at.date(), fill.price.amount)
            for fill in result.portfolio.fills] == expected


def test_daily_position_observer_preserves_first_fill_through_adds_partial_sales_and_rebase():
    from types import SimpleNamespace
    from ashare_lab.domain.orders import OrderSide
    from ashare_lab.domain.shared import Price
    observer = DailyPositionRiskObserver(rules=(), bars=(), market_sessions=())
    first = SimpleNamespace(side=OrderSide.BUY, price=Price(Decimal(10)))
    observer.after_fill(first, before=0, after=100)
    observer.after_fill(SimpleNamespace(side=OrderSide.BUY, price=Price(Decimal(12))),
                        before=100, after=200)
    observer.after_fill(SimpleNamespace(side=OrderSide.SELL), before=200, after=100)
    assert observer.anchor is first and observer.anchor_price == Decimal(10)
    observer.peak = Decimal(14)
    observer.rebase(Decimal("0.5"))
    assert observer.anchor_price == Decimal(5) and observer.peak == Decimal(7)
    observer.after_fill(SimpleNamespace(side=OrderSide.SELL), before=100, after=0)
    second = SimpleNamespace(side=OrderSide.BUY, price=Price(Decimal(8)))
    observer.after_fill(second, before=0, after=100)
    assert observer.anchor is second and observer.peak == Decimal(8)


def inputs():
    payload = json.loads((Path(__file__).resolve().parents[3] /
        "contracts/examples/strategy.macd-volume.daily.v1.json").read_text())
    payload["exit"] = {"children": [{"type": "minute_protection_exit", "take_profit_pct": 5, "stop_loss_pct": 3}]}
    payload["execution"] = HybridExecutionPolicy().model_dump()
    payload["backtest"] = {"start": "2026-09-08", "end": "2026-09-10", "initial_cash_cny": 10000}
    strategy = StrategySpec.model_validate(payload)
    days = tuple(date(2026, 9, d) for d in (8, 9, 10, 11))
    at = datetime(2026, 9, 8, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    entry = SignalFact(SEC, days[0], "fixture:confirmed-entry", True, at, at, "fixture signal")
    signals = SimpleNamespace(history=SimpleNamespace(instrument_id=SEC.value,
        rows=tuple(SimpleNamespace(session_date=day) for day in days[:3])),
        entry_timeline=(entry, None, None), exit_timeline=(None, None, None))
    minutes = PreparedMinuteReplay(tuple([bar(8, 31, 10), bar(9, 31, 10), bar(9, 32, Decimal("10.6")),
        bar(9, 33, 11), bar(10, 31, 11)]), (), days, ())
    config = BacktestRunConfig(slippage_bps=Decimal(0), capacity_mode=CapacityMode.UNLIMITED,
        limit_handling=LimitHandling.STRICT_NO_FILL_AT_LIMIT, run_robustness=False)
    return strategy, signals, minutes, config


def execute(strategy, signals, minutes, config):
    return execute_hybrid_signals(strategy=strategy, signal_input=signals, prepared=minutes,
                                  config=config, exchange=FEES.policy.exchange)


@pytest.mark.parametrize("leg", ["entry", "exit"])
def test_independent_daily_leg_preserves_next_session_and_does_not_invent_other_leg(leg):
    strategy, signals, minutes, config = inputs()
    fact = signals.entry_timeline[0]
    signals.entry_timeline = (fact if leg == "entry" else None, None, None)
    signals.exit_timeline = (fact if leg == "exit" else None, None, None)
    intents = prepare_daily_signal_intents(
        strategy=strategy, signal_input=signals, market_sessions=minutes.market_sessions,
        cash_fraction=config.allocation_ratio,
    )
    assert len(intents) == 1
    assert intents[0].enter is (leg == "entry")
    assert intents[0].exit is (leg == "exit")
    assert intents[0].session_date == date(2026, 9, 9)
    assert intents[0].known_at == fact.available_at
    intents[0].validate_calendar(minutes.market_sessions)


def test_accumulating_hybrid_projects_state_episodes_before_trading_range():
    strategy, signals, minutes, config = inputs()
    strategy = strategy.model_copy(update={
        "execution": HybridExecutionPolicy(position_policy="accumulate_on_new_entry_signal"),
    })
    # Use a definite state predicate regardless of the example's composite shape.
    from ashare_lab.domain.strategy import IndicatorCondition
    strategy = strategy.model_copy(update={"entry": IndicatorCondition(
        indicator_id="provider.numeric", definition_version="1.0.0", trigger="above", value=0)})
    seed = signals.entry_timeline[0]
    signals.entry_timeline = tuple(replace(seed, session_date=row.session_date,
        observed_at=datetime.combine(row.session_date, datetime.min.time().replace(hour=15), ZoneInfo("Asia/Shanghai")),
        available_at=datetime.combine(row.session_date, datetime.min.time().replace(hour=15), ZoneInfo("Asia/Shanghai")))
        for row in signals.history.rows)
    intents = prepare_daily_signal_intents(strategy=strategy, signal_input=signals,
        market_sessions=minutes.market_sessions, cash_fraction=config.allocation_ratio)
    assert len(intents) == 1
    assert intents[0].position_policy == "accumulate_on_new_entry_signal"
    later = strategy.model_copy(update={"backtest": strategy.backtest.model_copy(update={"start": date(2026, 9, 9)})})
    assert prepare_daily_signal_intents(strategy=later, signal_input=signals,
        market_sessions=minutes.market_sessions, cash_fraction=config.allocation_ratio) == ()


def test_daily_facts_drive_actual_entry_then_minute_protection():
    strategy, signals, minutes, config = inputs()
    result = execute(strategy, signals, minutes, config)
    assert [(f.side.value, f.filled_at.date()) for f in result.portfolio.fills] == [
        ("buy", date(2026, 9, 9)), ("sell", date(2026, 9, 10))]
    assert result.closed_position_cycles == 1
    assert result.portfolio.position_quantity(SEC).value == 0
    assert result.capacity_assumption == "unlimited_ohlc_research"


@pytest.mark.parametrize("invalid_field", ["instrument", "session", "observed", "available"])
def test_suppressed_state_repeat_cannot_hide_invalid_raw_evidence(invalid_field):
    from ashare_lab.domain.shared import InstrumentId
    from ashare_lab.domain.strategy import IndicatorCondition

    strategy, signals, minutes, config = inputs()
    strategy = strategy.model_copy(update={
        "execution": HybridExecutionPolicy(position_policy="accumulate_on_new_entry_signal"),
        "entry": IndicatorCondition(indicator_id="provider.numeric",
                                    definition_version="1.0.0", trigger="above", value=0),
    })
    closing = datetime(2026, 9, 9, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    repeated = replace(signals.entry_timeline[0], session_date=closing.date(),
                       observed_at=closing, available_at=closing)
    if invalid_field == "instrument":
        repeated = replace(repeated, instrument_id=InstrumentId("600519.SH"))
    elif invalid_field == "session":
        repeated = replace(repeated, session_date=date(2026, 9, 8))
    elif invalid_field == "observed":
        repeated = replace(repeated, observed_at=closing.replace(hour=14))
    else:
        repeated = replace(repeated, available_at=closing.replace(day=10, hour=9, minute=30))
    signals.entry_timeline = (signals.entry_timeline[0], repeated, None)
    reason = "not_available_before_open" if invalid_field == "available" else "identity_or_clock"
    with pytest.raises(MinuteReplayDataError, match=reason):
        prepare_daily_signal_intents(strategy=strategy, signal_input=signals,
            market_sessions=minutes.market_sessions, cash_fraction=config.allocation_ratio)


def test_hybrid_connects_default_capacity_limit_and_robustness_settings_to_base_run():
    strategy, signals, minutes, config = inputs()
    point_in_time = execute(strategy, signals, minutes,
        replace(config, capacity_mode=CapacityMode.POINT_IN_TIME_VOLUME))
    assert point_in_time.capacity_assumption == "previous_completed_minute_volume:0.05"
    assert point_in_time.portfolio.fills[0].quantity.value == 500
    assert execute(strategy, signals, minutes,
        replace(config, limit_handling=LimitHandling.WAIT_FOR_UNLOCK)).portfolio.fills
    # The service owns the additional stress scenario; the base executor must
    # still accept the persisted flag and run the exact base assumptions.
    assert execute(strategy, signals, minutes,
        replace(config, run_robustness=True)).portfolio.fills


def test_hybrid_facts_require_aligned_identity_and_preopen_knowledge():
    strategy, signals, minutes, config = inputs()
    original = signals.entry_timeline[0]
    for invalid, reason in (
        (replace(original, session_date=date(2026, 9, 9)), "identity_or_clock"),
        (replace(original, available_at=datetime(2026, 9, 9, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai"))), "not_available_before_open"),
    ):
        signals.entry_timeline = (invalid, None, None)
        with pytest.raises(MinuteReplayDataError, match=reason):
            execute(strategy, signals, minutes, config)


def test_hybrid_reports_odd_lot_partial_entry_without_repeating_order():
    from ashare_lab.application.minute_result import minute_result_bundle
    strategy, signals, minutes, config = inputs()
    minutes = replace(minutes, bars=(replace(minutes.bars[0], volume_shares=1140), *minutes.bars[1:]))
    result = execute(strategy, signals, minutes,
        replace(config, capacity_mode=CapacityMode.POINT_IN_TIME_VOLUME))
    buys = [fill for fill in result.portfolio.fills if fill.side.value == "buy"]
    assert len(buys) == 1 and buys[0].quantity.value == 57
    entry = next(event for event in result.events if event.fill == buys[0])
    assert entry.status == "partially_filled"
    report = minute_result_bundle(run_id="partial-entry", result=result,
        initial_cash=Decimal(10000), snapshot_id="fixture:minute")
    assert any(item.quantity == 57 and item.status == "partially_filled" for item in report.activities)


def test_minute_hybrid_robustness_is_attached_and_rehashes_bundle():
    from ashare_lab.application.minute_result import minute_result_bundle, attach_minute_robustness
    strategy, signals, minutes, config = inputs()
    base_result = execute(strategy, signals, minutes, config)
    stress_bps = Decimal(10)
    stressed_result = execute(strategy, signals, minutes,
        replace(config, slippage_bps=stress_bps))
    base = minute_result_bundle(run_id="base", result=base_result,
        initial_cash=Decimal(10000), snapshot_id="fixture:minute")
    stressed = minute_result_bundle(run_id="stress", result=stressed_result,
        initial_cash=Decimal(10000), snapshot_id="fixture:minute")
    original_hash = base.audit.result_hash
    attached = attach_minute_robustness(base=base, stressed=stressed,
        config=config, stress_slippage_bps=stress_bps)
    assert attached.robustness is not None
    assert attached.robustness.scenarios[0].id == "higher_slippage"
    assert attached.audit.result_hash != original_hash
