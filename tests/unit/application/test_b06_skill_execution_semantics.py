"""B06 deterministic execution evidence; no model, network, or real-data acceptance."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import cast

import pytest

from ashare_lab.adapters.market_data.mx_daily_history import MxDailyHistoryClient, MxDailyRow
from ashare_lab.adapters.persistence.backtest_runs import InMemoryBacktestRunStore
from ashare_lab.application.backtest_submission import BacktestRunConfig
from ashare_lab.application.result_views import calculate_result_bundle_hash
from ashare_lab.application.skill_backtest import SkillBacktestResult, run_skill_backtest
from ashare_lab.application.skill_backtest_service import (
    SkillBacktestService,
    _result_bundle,  # pyright: ignore[reportPrivateUsage] -- validate the production serializer
)
from ashare_lab.domain.execution import CapacityMode
from ashare_lab.domain.market_data import Board, TradingStatus
from ashare_lab.domain.shared import RunId
from ashare_lab.domain.signals import SignalFact
from ashare_lab.domain.strategy import (
    AllCondition,
    AnyCondition,
    BacktestConfig,
    CatalogRef,
    FirstOfExit,
    HoldingPeriodExit,
    IndicatorCondition,
    Instrument,
    PositionReturnExit,
    StrategySpec,
)
from ashare_lab.ports.provider_indicator_data import (
    ProviderIndicatorPoint,
    ProviderIndicatorSeries,
    ProviderIndicatorValue,
)
from tests.unit.application.test_skill_backtest import (
    TZ,
    _config,  # pyright: ignore[reportPrivateUsage] -- reuse existing isolated execution fixtures
    _history,  # pyright: ignore[reportPrivateUsage]
    _row,  # pyright: ignore[reportPrivateUsage]
)

SYMBOL = "000333.SZ"


def _dates(count: int) -> tuple[date, ...]:
    """A synthetic ordered weekday axis, not a verified exchange calendar."""
    result: list[date] = []
    day = date(2025, 3, 3)
    while len(result) < count:
        if day.weekday() < 5:
            result.append(day)
        day += timedelta(days=1)
    return tuple(result)


def _strategy(dates: tuple[date, ...]) -> StrategySpec:
    return StrategySpec(
        catalog=CatalogRef(catalog_id="cn_a.signals", release_version="2026.08.28"),
        instrument=Instrument(symbol=SYMBOL),
        entry=AllCondition(children=(
            IndicatorCondition(
                indicator_id="technical.rsi", definition_version="1.0.0",
                params={"period": 14}, trigger="crosses_above", value=30,
            ),
            IndicatorCondition(
                indicator_id="market.amount", definition_version="1.0.0",
                params={}, trigger="above", value=500_000_000,
            ),
        )),
        exit=FirstOfExit(children=(
            IndicatorCondition(
                indicator_id="technical.rsi", definition_version="1.0.0",
                params={"period": 14}, trigger="above", value=55,
            ),
            PositionReturnExit(trigger="stop_loss", threshold_pct=5),
            HoldingPeriodExit(sessions=10),
        )),
        backtest=BacktestConfig(start=dates[0], end=dates[-1], initial_cash_cny=1_000_000),
    )


class _Indicators:
    def __init__(self, dates: tuple[date, ...], rsi: tuple[int, ...], amount: tuple[int, ...]):
        self.dates = dates
        self.values = {"technical.rsi": rsi, "market.amount": amount}
        self.requests: list[tuple[str, str, tuple[str, ...]]] = []

    async def query_indicator_history(
        self, *, instrument_id: str, indicator_id: str, provider_indicator_name: str,
        value_names: tuple[str, ...], start: date, end: date,
    ) -> ProviderIndicatorSeries:
        assert instrument_id == SYMBOL
        self.requests.append((indicator_id, provider_indicator_name, value_names))
        return ProviderIndicatorSeries(
            provider="eastmoney_mx_finance_data", instrument_id=SYMBOL,
            indicator_id=indicator_id, requested_start=start, requested_end=end,
            points=tuple(
                ProviderIndicatorPoint(
                    session_date=day,
                    observed_at=datetime.combine(day, time(15), TZ),
                    first_available_at=datetime.combine(day, time(15), TZ),
                    values=(ProviderIndicatorValue("fixture", value_names[0], Decimal(value)),),
                )
                for day, value in zip(self.dates, self.values[indicator_id], strict=True)
            ),
            response_sha256="sha256:" + "b" * 64,
            retrieved_at=datetime(2025, 6, 1, tzinfo=UTC),
            schema_version="b06.synthetic.v1", query="deterministic fixture only",
        )


def _run(
    rows: tuple[MxDailyRow, ...], *, rsi: tuple[int, ...] | None = None,
    amount: tuple[int, ...] | None = None, strategy: StrategySpec | None = None,
    config: BacktestRunConfig | None = None,
) -> tuple[SkillBacktestResult, tuple[SignalFact | None, ...], _Indicators]:
    dates = tuple(row.session_date for row in rows)
    strategy = strategy or _strategy(dates)
    rsi = rsi or (29, 31, *([40] * (len(dates) - 2)))
    amount = amount or (600_000_000,) * len(dates)
    indicators = _Indicators(dates, rsi, amount)
    history = replace(
        _history(rows), instrument_id=SYMBOL, board=Board.MAIN,
        retrieved_at=datetime(2025, 6, 1, tzinfo=UTC),
    )
    service = SkillBacktestService(
        history=cast(MxDailyHistoryClient, object()), indicators=indicators,
        store=InMemoryBacktestRunStore(),
    )
    try:
        entries, exits, series = asyncio.run(
            service._signals(strategy, history),  # pyright: ignore[reportPrivateUsage]
        )
    finally:
        service.shutdown()
    execution = config or _config(
        commission_rate=Decimal("0"), minimum_commission_cny=Decimal("0"),
    )
    result = run_skill_backtest(
        strategy=strategy, history=history, entry_timeline=entries, exit_timeline=exits,
        config=execution,
    )
    bundle = _result_bundle(
        RunId("run:b06-exit-budget-fixture"), strategy, execution, history, series, result, None,
    )
    assert len(bundle.activities) == len(result.activities)
    assert bundle.summary.trade_count == result.metrics.trade_count
    assert all(
        activity.title == "卖出尝试已结束"
        for activity in bundle.activities
        if activity.reason in {"exit_retry_budget_exhausted", "exit_retry_disabled"}
    )
    assert bundle.audit.result_hash == calculate_result_bundle_hash(
        bundle.model_dump(mode="json", by_alias=True),
    )
    return result, entries, indicators


def test_b06_exact_all_thresholds_period_and_close_to_next_open() -> None:
    dates = _dates(9)
    result, entries, indicators = _run(
        tuple(_row(day, raw_open="10") for day in dates),
        rsi=(29, 30, 31, 29, 31, 40, 55, 56, 56),
        amount=(600_000_000, 600_000_000, 500_000_000, 600_000_000,
                600_000_000, 600_000_000, 600_000_000, 600_000_000, 600_000_000),
    )
    assert [None if fact is None else fact.triggered for fact in entries] == [
        None, False, False, False, True, False, False, False, False,
    ]
    assert sorted(indicators.requests) == [
        ("market.amount", "成交额", ("成交额",)),
        ("technical.rsi", "RSI(14)", ("RSI值",)),
    ]
    signals = [item for item in result.activities if item.kind == "signal"]
    assert [(item.side, item.occurred_at) for item in signals] == [
        ("buy", datetime.combine(dates[4], time(15), TZ)),
        ("sell", datetime.combine(dates[7], time(15), TZ)),
    ]
    fills = [item for item in result.activities if item.kind == "fill"]
    assert [(item.side, item.occurred_at) for item in fills] == [
        ("buy", datetime.combine(dates[5], time(9, 30), TZ)),
        ("sell", datetime.combine(dates[8], time(9, 30), TZ)),
    ]


def test_b06_all_and_any_preserve_distinct_three_valued_truth_tables() -> None:
    dates = _dates(6)
    rows = tuple(_row(day, raw_open="10") for day in dates)
    strategy = _strategy(dates)
    assert isinstance(strategy.entry, AllCondition)
    rsi = (29, 30, 31, 29, 31, 56)
    amount = (600_000_000, 600_000_000, 500_000_000, 400_000_000,
              500_000_001, 600_000_000)
    _, all_entries, _ = _run(rows, strategy=strategy, rsi=rsi, amount=amount)
    _, any_entries, _ = _run(rows, strategy=strategy.model_copy(update={
        "entry": AnyCondition(children=strategy.entry.children),
    }), rsi=rsi, amount=amount)
    assert [None if fact is None else fact.triggered for fact in all_entries] == [
        None, False, False, False, True, False,
    ]
    assert [None if fact is None else fact.triggered for fact in any_entries] == [
        True, True, True, False, True, True,
    ]


@pytest.mark.parametrize("first_exit", ["rsi", "stop_loss", "holding"])
def test_b06_any_first_exit_wins_without_waiting_for_other_conditions(first_exit: str) -> None:
    dates = _dates(15)
    rsi = [29, 31, *([40] * 13)]
    closes = ["10"] * 15
    if first_exit == "rsi":
        rsi[4] = 56
        closes[6] = "9.5"  # A later stop cannot replace the earlier RSI exit.
    elif first_exit == "stop_loss":
        closes[4] = "9.5"
        rsi[6] = 56
    rows = tuple(
        _row(day, raw_open="10", raw_close=close)
        for day, close in zip(dates, closes, strict=True)
    )
    result, _, _ = _run(rows, rsi=tuple(rsi))
    assert len(result.round_trips) == 1
    assert result.round_trips[0].entry_date == dates[2]
    assert result.round_trips[0].exit_date == dates[12 if first_exit == "holding" else 5]
    signal = next(
        item for item in result.activities if item.kind == "signal" and item.side == "sell"
    )
    expected_reason = {
        "rsi": "provider:eastmoney_mx_finance_data:RSI值 gt condition.value",
        "stop_loss": "stop_loss_close_confirmed",
        "holding": "holding_period_target_session_open",
    }[first_exit]
    assert signal.reason == expected_reason
    assert signal.occurred_at == datetime.combine(
        dates[12 if first_exit == "holding" else 4],
        time(9, 30) if first_exit == "holding" else time(15), TZ,
    )


def test_b06_stop_loss_is_close_confirmed_not_intraday_low() -> None:
    dates = _dates(8)
    rows = tuple(_row(day, raw_open="10") for day in dates)  # Low is 9; close is 10.
    result, _, _ = _run(rows)
    assert result.has_open_position
    assert not any(item.side == "sell" for item in result.activities)


def test_b06_holding_counts_suspended_sessions_and_retries_target_until_fill() -> None:
    dates = _dates(16)
    rows = tuple(
        _row(
            day, raw_open="10",
            status=TradingStatus.SUSPENDED if index in {4, 8, 12} else TradingStatus.TRADING,
            at_limit="down" if index == 13 else None,
        )
        for index, day in enumerate(dates)
    )
    result, _, _ = _run(rows)
    orders = [item for item in result.activities if item.kind == "order" and item.side == "sell"]
    assert [(item.occurred_at.date(), item.attempt_no) for item in orders] == [
        (dates[12], 1), (dates[13], 2), (dates[14], 3),
    ]
    assert len({item.decision_id for item in orders}) == 1
    assert result.round_trips[0].entry_date == dates[2]
    assert result.round_trips[0].exit_date == dates[14]


def test_b06_first_rsi_exit_remains_selected_while_blocked_and_stop_later_triggers() -> None:
    dates = _dates(9)
    rows = tuple(
        _row(day, raw_open="10", raw_close="9.5" if index == 4 else "10",
             status=TradingStatus.SUSPENDED if index == 4 else TradingStatus.TRADING)
        for index, day in enumerate(dates)
    )
    result, _, _ = _run(rows, rsi=(29, 31, 40, 56, 40, 40, 40, 40, 40))
    sell_signals = [
        item for item in result.activities if item.kind == "signal" and item.side == "sell"
    ]
    assert len(sell_signals) == 1
    assert "RSI值" in sell_signals[0].reason
    assert result.round_trips[0].exit_date == dates[5]


def test_b06_compound_entry_follows_frozen_single_attempt_policy() -> None:
    # SPEC 8.3 fixes composite validity at one without point-in-time state revalidation.
    dates = _dates(6)
    rows = tuple(
        _row(day, raw_open="10", at_limit="up" if index == 2 else None)
        for index, day in enumerate(dates)
    )
    strategy = _strategy(dates)
    assert isinstance(strategy.entry, AllCondition)
    compound, _, _ = _run(rows, strategy=strategy)
    bare, _, _ = _run(
        rows, strategy=strategy.model_copy(update={"entry": strategy.entry.children[0]}),
    )
    assert not compound.has_open_position
    assert bare.has_open_position
    assert [
        item.occurred_at.date() for item in compound.activities
        if item.kind == "order" and item.side == "buy"
    ] == [dates[2]]
    assert [
        item.occurred_at.date() for item in bare.activities if item.kind == "fill"
    ] == [dates[3]]


def test_b06_holding_exit_respects_attempt_cap_and_retains_unliquidated_position() -> None:
    # SPEC 8.3 permits retries only while budget remains; do not invent attempt 21.
    dates = _dates(35)
    rows = tuple(
        _row(day, raw_open="10",
             status=TradingStatus.SUSPENDED if 12 <= index < 32 else TradingStatus.TRADING)
        for index, day in enumerate(dates)
    )
    result, _, _ = _run(rows)
    orders = [item for item in result.activities if item.kind == "order" and item.side == "sell"]
    assert BacktestRunConfig().max_exit_attempts == 20
    assert len(orders) == 20
    assert (orders[0].occurred_at.date(), orders[-1].occurred_at.date()) == (dates[12], dates[31])
    assert result.round_trips == ()
    assert result.has_open_position
    assert result.open_position_notional_cny == Decimal("1000000")
    assert not any(
        item.side == "sell" and item.occurred_at.date() >= dates[32]
        for item in result.activities
    )
    terminals = [item for item in result.activities if item.reason == "exit_retry_budget_exhausted"]
    assert len(terminals) == 1
    terminal = terminals[0]
    assert terminal.kind == "unfilled" and terminal.status == "expired"
    assert terminal.attempt_no == 20
    assert terminal.order_id == terminal.parent_id == orders[-1].order_id
    assert terminal.decision_id == terminal.chain_id == orders[-1].decision_id
    assert terminal.origin_signal_id == orders[-1].origin_signal_id
    assert terminal.raw_reference_price is terminal.notional_cny is terminal.fill_id is None
    assert sum(item.reason == "security_suspended" for item in result.activities) == 20
    assert {
        item.reason for item in result.activities if item.kind == "unfilled" and item.side == "sell"
    } == {"security_suspended", "exit_retry_budget_exhausted"}


def test_b06_disabled_retry_records_one_terminal_without_another_order() -> None:
    dates = _dates(15)
    rows = tuple(
        _row(day, raw_open="10",
             status=TradingStatus.SUSPENDED if index == 12 else TradingStatus.TRADING)
        for index, day in enumerate(dates)
    )
    result, _, _ = _run(rows, config=_config(
        retry_unfilled_exits=False, commission_rate=Decimal("0"),
        minimum_commission_cny=Decimal("0"),
    ))
    orders = [item for item in result.activities if item.kind == "order" and item.side == "sell"]
    assert len(orders) == 1
    terminals = [item for item in result.activities if item.reason == "exit_retry_disabled"]
    assert len(terminals) == 1
    assert terminals[0].attempt_no == 1
    assert terminals[0].order_id == orders[0].order_id
    assert terminals[0].decision_id == orders[0].decision_id
    assert any(item.reason == "security_suspended" for item in result.activities)
    assert result.has_open_position and result.round_trips == ()


@pytest.mark.parametrize("retry", [True, False])
def test_b06_partial_exit_preserves_fill_and_explains_why_remainder_stops(retry: bool) -> None:
    dates = _dates(15)
    rows = tuple(
        replace(_row(day, raw_open="10"), volume=1000, amount=Decimal("10000"))
        if index == 11 else _row(day, raw_open="10")
        for index, day in enumerate(dates)
    )
    result, _, _ = _run(rows, config=_config(
        capacity_mode=CapacityMode.POINT_IN_TIME_VOLUME, participation_rate=Decimal("1"),
        max_exit_attempts=1, retry_unfilled_exits=retry,
        commission_rate=Decimal("0"), minimum_commission_cny=Decimal("0"),
    ))
    orders = [item for item in result.activities if item.kind == "order" and item.side == "sell"]
    assert len(orders) == 1
    partials = [
        item for item in result.activities if item.kind == "partial_fill" and item.side == "sell"
    ]
    assert len(partials) == 1
    assert partials[0].reason == "partial_liquidity_fill"
    assert partials[0].notional_cny == Decimal("10000")
    reason = "exit_retry_budget_exhausted" if retry else "exit_retry_disabled"
    terminals = [item for item in result.activities if item.reason == reason]
    assert len(terminals) == 1
    assert terminals[0].order_id == partials[0].order_id == orders[0].order_id
    assert terminals[0].decision_id == partials[0].decision_id
    assert terminals[0].attempt_no == 1
    assert terminals[0].notional_cny is terminals[0].raw_reference_price is None
    assert result.open_position_notional_cny == Decimal("990000")
    assert result.has_open_position and result.round_trips == ()


@pytest.mark.parametrize("retry", [True, False])
def test_b06_new_independent_exit_can_fill_without_a_false_terminal_failure(retry: bool) -> None:
    dates = _dates(8)
    rows = tuple(
        _row(day, raw_open="10",
             status=TradingStatus.SUSPENDED if index == 4 else TradingStatus.TRADING)
        for index, day in enumerate(dates)
    )
    result, _, _ = _run(rows, rsi=(29, 31, 40, 56, 40, 56, 40, 40), config=_config(
        max_exit_attempts=1, retry_unfilled_exits=retry,
        commission_rate=Decimal("0"), minimum_commission_cny=Decimal("0"),
    ))
    orders = [item for item in result.activities if item.kind == "order" and item.side == "sell"]
    assert [item.occurred_at.date() for item in orders] == [dates[4], dates[6]]
    assert orders[0].decision_id != orders[1].decision_id
    assert [item.attempt_no for item in orders] == [1, 1]
    terminals = [item for item in result.activities if item.reason.startswith("exit_retry_")]
    assert len(terminals) == 1
    assert terminals[0].order_id == orders[0].order_id
    second_outcomes = [
        item for item in result.activities
        if item.order_id == orders[1].order_id and item.kind != "order"
    ]
    assert len(second_outcomes) == 1 and second_outcomes[0].kind == "fill"
    assert result.round_trips[0].exit_date == dates[6]
    assert not result.has_open_position
