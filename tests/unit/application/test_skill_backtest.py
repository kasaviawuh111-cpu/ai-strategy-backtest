from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Literal, cast
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from ashare_lab.adapters.market_data.mx_daily_history import (
    MX_BACK_ADJUSTMENT,
    MX_DAILY_HISTORY_PROVIDER,
    MxDailyHistory,
    MxDailyHistoryBeforeListingError,
    MxDailyHistoryClient,
    MxDailyHistoryFieldsMissingError,
    MxDailyRow,
    MxQueryEvidence,
)
from ashare_lab.adapters.market_data.mx_saas import MxSaasProviderUnavailableError
from ashare_lab.adapters.persistence.backtest_runs import InMemoryBacktestRunStore
from ashare_lab.application.backtest_submission import BacktestRunConfig
from ashare_lab.application.skill_backtest import run_skill_backtest
from ashare_lab.application.skill_backtest_service import (
    SkillBacktestService,
    _skill_derived_timeline,
)
from ashare_lab.domain.execution import CapacityMode, LimitHandling
from ashare_lab.domain.market_data import Board, TradingStatus
from ashare_lab.domain.shared import InstrumentId
from ashare_lab.domain.signals import SignalFact
from ashare_lab.domain.strategy import (
    BacktestConfig,
    CatalogRef,
    FirstOfExit,
    HoldingPeriodExit,
    IndicatorCondition,
    Instrument,
    StrategySpec,
)
from ashare_lab.ports.provider_indicator_data import HistoricalIndicatorData

TZ = ZoneInfo("Asia/Shanghai")
SYMBOL = "300059.SZ"
START = date(2025, 1, 2)


def test_prelisting_range_is_actionable_and_does_not_retry_or_modify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    load = AsyncMock(side_effect=MxDailyHistoryBeforeListingError(
        start=date(2020, 3, 10), listing_date=date(2021, 4, 9),
    ))
    service = SkillBacktestService(
        history=cast(MxDailyHistoryClient, SimpleNamespace(load=load)),
        indicators=cast(HistoricalIndicatorData, object()), store=InMemoryBacktestRunStore(),
    )
    monkeypatch.setattr(service.queue, "enqueue", lambda _run_id: "manual-test")
    try:
        strategy = _strategy((date(2020, 9, 6), date(2026, 9, 6)))
        created = service.submit(strategy, BacktestRunConfig(slippage_bps=Decimal("7")))
        result = service.execute(created.record.run_id)
        assert result.error_code == "skill_history_before_listing"
        assert "2021-04-09" in result.progress_label
        assert "2020-09-06" in result.progress_label
        assert "不会自动缩短区间" in result.progress_label
        assert result.strategy_json == created.record.strategy_json
        assert result.config_json == created.record.config_json
        assert load.await_count == 1
        assert result.result_json is None
    finally:
        service.shutdown()


def test_warmup_only_prelisting_keeps_requested_range(monkeypatch: pytest.MonkeyPatch) -> None:
    marker = object()
    listing = START - timedelta(days=30)
    load = AsyncMock(side_effect=[
        MxDailyHistoryBeforeListingError(start=START - timedelta(days=180), listing_date=listing),
        marker,
    ])
    service = SkillBacktestService(
        history=cast(MxDailyHistoryClient, SimpleNamespace(load=load)),
        indicators=cast(HistoricalIndicatorData, object()), store=InMemoryBacktestRunStore(),
    )
    monkeypatch.setattr(service.queue, "enqueue", lambda _run_id: "manual-test")
    try:
        import asyncio

        strategy = _strategy((START, START + timedelta(days=10)))
        config = BacktestRunConfig()
        created = service.submit(strategy, config)
        result = asyncio.run(service._load_history(created.record.run_id, strategy, config, 180))
        assert result is marker
        assert load.await_args_list[1].kwargs["start"] == listing
        assert load.await_args_list[1].kwargs["end"] == strategy.backtest.end
        assert strategy.backtest.start == START
        assert load.await_count == 2
    finally:
        service.shutdown()


@pytest.mark.parametrize(
    ("failure", "expected_code", "expected_label"),
    [
        (
            MxSaasProviderUnavailableError("private", tool="searchData", reason="read_timeout"),
            "skill_mx_read_timeout",
            "等待东方财富查数 Skill响应超时",
        ),
        (
            MxSaasProviderUnavailableError(
                "private",
                tool="selectSecurity",
                reason="connect_timeout",
            ),
            "skill_mx_connect_timeout",
            "连接东方财富选股 Skill超时",
        ),
        (
            MxSaasProviderUnavailableError(
                "private",
                tool="searchData",
                reason="http_error",
                http_status=503,
            ),
            "skill_mx_http_error",
            "东方财富查数 Skill返回服务异常（HTTP 503）",
        ),
    ],
)
def test_skill_fetch_failure_reports_exact_step_without_leaking_provider_text(
    failure: MxSaasProviderUnavailableError,
    expected_code: str,
    expected_label: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Fault-injection regression only; real-data acceptance is a separate run.
    service = SkillBacktestService(
        history=cast(MxDailyHistoryClient, SimpleNamespace(load=AsyncMock(side_effect=failure))),
        indicators=cast(HistoricalIndicatorData, object()),
        store=InMemoryBacktestRunStore(),
    )
    monkeypatch.setattr(service.queue, "enqueue", lambda _run_id: "manual-test")
    try:
        created = service.submit(
            _strategy((START, START + timedelta(days=10))),
            BacktestRunConfig(),
        )
        result = service.execute(created.record.run_id)
        assert result.error_code == expected_code
        assert expected_label in result.progress_label
        assert result.progress_percent == 10
        assert result.result_json is None
        assert "private" not in result.progress_label + caplog.text
    finally:
        service.shutdown()


@pytest.mark.parametrize("with_range", [True, False])
def test_missing_history_explains_fetch_range_without_changing_strategy_or_config(
    with_range: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = MxDailyHistoryFieldsMissingError(
        ("涨停价", "跌停价"),
        start=date(2010, 3, 9) if with_range else None,
        end=date(2012, 3, 8) if with_range else None,
    )
    service = SkillBacktestService(
        history=cast(MxDailyHistoryClient, SimpleNamespace(load=AsyncMock(side_effect=failure))),
        indicators=cast(HistoricalIndicatorData, object()),
        store=InMemoryBacktestRunStore(),
    )
    monkeypatch.setattr(service.queue, "enqueue", lambda _run_id: "manual-test")
    strategy = _strategy((date(2010, 9, 5), date(2026, 9, 5))).model_copy(
        update={
            "instrument": Instrument(symbol="600519.SH"),
        }
    )
    try:
        created = service.submit(strategy, BacktestRunConfig(slippage_bps=Decimal("0")))
        result = service.execute(created.record.run_id)
        assert result.error_code == "skill_history_fields_missing"
        expected_range = "2010-03-09 至 2012-03-08" if with_range else "本次区间"
        assert f"查询 {expected_range} 的历史数据时" in result.progress_label
        assert "东方财富未返回涨停价、跌停价" in result.progress_label
        assert "原区间和规则已保留" in result.progress_label
        assert "可修改区间或稍后重新读取" in result.progress_label
        assert result.strategy_json == created.record.strategy_json
        assert result.config_json == created.record.config_json
        assert result.result_json is None
    finally:
        service.shutdown()


def _condition(*, trigger: str) -> IndicatorCondition:
    return IndicatorCondition(
        indicator_id="technical.ma",
        definition_version="1.0.0",
        params={"period": 20, "price_field": "close"},
        trigger=trigger,
    )


def _strategy(
    dates: tuple[date, ...],
    *,
    holding_sessions: int | None = None,
) -> StrategySpec:
    exits = (
        (HoldingPeriodExit(sessions=holding_sessions),)
        if holding_sessions is not None
        else (_condition(trigger="price_crosses_below"),)
    )
    return StrategySpec(
        catalog=CatalogRef(catalog_id="cn_a.signals", release_version="2026.08.28"),
        instrument=Instrument(symbol=SYMBOL),
        entry=_condition(trigger="price_crosses_above"),
        exit=FirstOfExit(children=exits),
        backtest=BacktestConfig(
            start=dates[0],
            end=dates[-1],
            initial_cash_cny=10_000,
        ),
    )


def _row(
    session_date: date,
    *,
    raw_open: str,
    raw_close: str | None = None,
    adjusted_scale: str = "1",
    status: TradingStatus = TradingStatus.TRADING,
    at_limit: Literal["up", "down"] | None = None,
) -> MxDailyRow:
    open_value = Decimal(raw_open)
    close_value = Decimal(raw_close or raw_open)
    high = max(open_value, close_value) + Decimal("1")
    low = max(Decimal("0.01"), min(open_value, close_value) - Decimal("1"))
    scale = Decimal(adjusted_scale)
    if status is TradingStatus.SUSPENDED:
        upper = lower = None
        volume = 0
        amount = Decimal("0")
        limit_source: Literal["eastmoney_mx_finance_data", "not_applicable_suspended"] = (
            "not_applicable_suspended"
        )
    else:
        upper = open_value if at_limit == "up" else max(high + Decimal("1"), open_value)
        lower = (
            open_value
            if at_limit == "down"
            else max(Decimal("0.01"), min(low - Decimal("0.5"), open_value - Decimal("0.01")))
        )
        volume = 1_000_000
        amount = open_value * volume
        limit_source = "eastmoney_mx_finance_data"
    return MxDailyRow(
        session_date=session_date,
        raw_open=open_value,
        raw_high=high,
        raw_low=low,
        raw_close=close_value,
        raw_preclose=open_value,
        adjusted_open=open_value * scale,
        adjusted_high=high * scale,
        adjusted_low=low * scale,
        adjusted_close=close_value * scale,
        volume=volume,
        amount=amount,
        trading_status=status,
        is_st=False,
        upper_limit=upper,
        lower_limit=lower,
        limit_source=limit_source,
    )


def _history(rows: tuple[MxDailyRow, ...]) -> MxDailyHistory:
    retrieved_at = datetime(2025, 2, 1, tzinfo=UTC)
    return MxDailyHistory(
        instrument_id=SYMBOL,
        board=Board.CHINEXT,
        listing_date=date(2010, 1, 1),
        start=rows[0].session_date,
        end=rows[-1].session_date,
        retrieved_at=retrieved_at,
        provider=MX_DAILY_HISTORY_PROVIDER,
        adjustment=MX_BACK_ADJUSTMENT,
        rows=rows,
        query_evidence=(
            MxQueryEvidence(
                purpose="unit-test-fixture",
                provider=MX_DAILY_HISTORY_PROVIDER,
                query="fixture only; not live acceptance evidence",
                retrieved_at=retrieved_at,
                response_sha256=f"sha256:{'0' * 64}",
                row_count=len(rows),
            ),
        ),
    )


def _fact(session_date: date, *, triggered: bool, ref: str) -> SignalFact:
    observed_at = datetime.combine(session_date, datetime.min.time().replace(hour=15), tzinfo=TZ)
    return SignalFact(
        instrument_id=InstrumentId(SYMBOL),
        session_date=session_date,
        condition_ref=ref,
        triggered=triggered,
        observed_at=observed_at,
        available_at=observed_at,
        reason=f"{ref}:{'true' if triggered else 'false'}",
    )


@pytest.mark.parametrize("indicator_id", ["price.rolling_high", "volume.relative"])
def test_skill_derived_window_is_causal_and_preserves_source(indicator_id: str) -> None:
    # Formula-boundary fixture only; live model/data acceptance is separate.
    rows = tuple(_row(START + timedelta(days=i), raw_open="10") for i in range(20))
    suspended = _row(START + timedelta(days=20), raw_open="10", status=TradingStatus.SUSPENDED)
    breakout = replace(
        _row(START + timedelta(days=21), raw_open="11"),
        volume=1_500_000,
    )
    future = _row(START + timedelta(days=22), raw_open="999")
    condition = IndicatorCondition(
        indicator_id=indicator_id,
        definition_version="1.0.0",
        params={"period": 20, "price_field": "close"}
        if indicator_id == "price.rolling_high"
        else {"baseline_period": 20, "consecutive_days": 1},
        trigger="new_high" if indicator_id == "price.rolling_high" else "gte_multiple",
        value=None if indicator_id == "price.rolling_high" else Decimal("1.5"),
    )
    assert indicator_id in SkillBacktestService.available_indicator_ids
    assert indicator_id not in SkillBacktestService.indicator_unavailable_reasons
    timeline = _skill_derived_timeline(condition, _history((*rows, suspended, breakout)))
    assert timeline[:21] == (None,) * 21
    fact = timeline[-1]
    assert fact is not None and fact.triggered
    expected = ("11", "10") if indicator_id == "price.rolling_high" else ("1.5", "1.5")
    assert (fact.left_value, fact.right_value) == tuple(Decimal(value) for value in expected)
    assert fact.evidence[0].provider == MX_DAILY_HISTORY_PROVIDER
    assert fact.evidence[0].evidence_type == "skill_ohlcv_derived_indicator"
    extended = _skill_derived_timeline(condition, _history((*rows, suspended, breakout, future)))
    assert extended[:-1] == timeline


def test_skill_derived_ma20_cross_uses_exact_window() -> None:
    rows = tuple(_row(START + timedelta(days=i), raw_open="10") for i in range(20))
    rising = _row(START + timedelta(days=20), raw_open="11")
    falling = _row(START + timedelta(days=21), raw_open="9")
    timeline = _skill_derived_timeline(
        _condition(trigger="price_crosses_below"),
        _history((*rows, rising, falling)),
    )
    assert timeline[:19] == (None,) * 19
    assert timeline[-2] is not None and not timeline[-2].triggered
    assert timeline[-1] is not None and timeline[-1].triggered
    assert timeline[-1].left_value == Decimal("9")
    assert timeline[-1].right_value == Decimal("10")


def _config(**overrides: object) -> BacktestRunConfig:
    values: dict[str, object] = {
        "capacity_mode": CapacityMode.UNLIMITED,
        "slippage_bps": Decimal("0"),
        "commission_rate": Decimal("0.001"),
        "minimum_commission_cny": Decimal("5"),
    }
    values.update(overrides)
    return BacktestRunConfig(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(("operator", "sell_index"), [("all", 5), ("first_of", 3)])
def test_combined_exit_holding_maturity_waits_for_same_close_signal(
    operator: str, sell_index: int
) -> None:
    dates = tuple(START + timedelta(days=i) for i in range(6))
    strategy = _strategy(dates).model_copy(
        update={
            "exit": FirstOfExit.model_validate(
                {
                    "op": operator,
                    "children": [
                        HoldingPeriodExit(sessions=2),
                        _condition(trigger="price_crosses_below"),
                    ],
                }
            )
        }
    )
    result = run_skill_backtest(
        strategy=strategy,
        history=_history(tuple(_row(day, raw_open="10") for day in dates)),
        entry_timeline=tuple(
            _fact(day, triggered=i == 0, ref="entry") for i, day in enumerate(dates)
        ),
        # True before maturity must not latch forever. At maturity it is false,
        # then becomes true after maturity; ALL sells at the following open.
        exit_timeline=tuple(
            _fact(day, triggered=i in (2, 4), ref="exit") for i, day in enumerate(dates)
        ),
        config=_config(),
    )
    fills = [item for item in result.activities if item.kind == "fill"]
    assert [(item.side, item.occurred_at.date()) for item in fills] == [
        ("buy", dates[1]),
        ("sell", dates[sell_index]),
    ]


@pytest.mark.parametrize("position_rule", ["stop_loss", "trailing_drawdown"])
def test_all_exit_does_not_trigger_on_position_rule_alone(position_rule: str) -> None:
    from ashare_lab.domain.strategy import PositionReturnExit, TrailingDrawdownExit

    dates = tuple(START + timedelta(days=i) for i in range(6))
    risk_rule = (
        PositionReturnExit(trigger="stop_loss", threshold_pct=5)
        if position_rule == "stop_loss"
        else TrailingDrawdownExit(threshold_pct=5)
    )
    strategy = _strategy(dates).model_copy(
        update={
            "exit": FirstOfExit(
                op="all",
                children=(_condition(trigger="price_crosses_below"), risk_rule),
            )
        }
    )
    # Position threshold holds on day 2, but market evidence is unknown/false.
    # Only the day-4 close has both facts; no early exit is allowed.
    result = run_skill_backtest(
        strategy=strategy,
        history=_history(
            tuple(
                _row(day, raw_open="10", raw_close="9" if i >= 2 else "10")
                for i, day in enumerate(dates)
            )
        ),
        entry_timeline=tuple(
            _fact(day, triggered=i == 0, ref="entry") for i, day in enumerate(dates)
        ),
        exit_timeline=tuple(
            None if i == 2 else _fact(day, triggered=i == 4, ref="exit")
            for i, day in enumerate(dates)
        ),
        config=_config(),
    )
    sells = [item for item in result.activities if item.kind == "fill" and item.side == "sell"]
    assert [item.occurred_at.date() for item in sells] == [dates[5]]


def test_all_exit_market_timeline_combines_indicators_with_and() -> None:
    from ashare_lab.application.skill_backtest_service import _exit_condition
    from ashare_lab.domain.strategy import AllCondition

    dates = (START, START + timedelta(days=1))
    strategy = _strategy(dates).model_copy(
        update={
            "exit": FirstOfExit(
                op="all",
                children=(
                    _condition(trigger="price_crosses_above"),
                    _condition(trigger="price_crosses_below"),
                ),
            )
        }
    )
    assert isinstance(_exit_condition(strategy), AllCondition)


def test_close_signals_execute_next_open_with_configured_fees() -> None:
    dates = tuple(START + timedelta(days=offset) for offset in range(4))
    rows = tuple(
        _row(day, raw_open=open_, raw_close=close)
        for day, open_, close in zip(
            dates,
            ("10", "10", "12", "12"),
            ("10", "11", "12", "12"),
            strict=True,
        )
    )
    entries = tuple(
        _fact(day, triggered=index == 0, ref="entry") for index, day in enumerate(dates)
    )
    exits = tuple(_fact(day, triggered=index == 1, ref="exit") for index, day in enumerate(dates))

    result = run_skill_backtest(
        strategy=_strategy(dates),
        history=_history(rows),
        entry_timeline=entries,
        exit_timeline=exits,
        config=_config(),
    )

    fills = [item for item in result.activities if item.kind in {"fill", "partial_fill"}]
    assert [(item.side, item.occurred_at.date()) for item in fills] == [
        ("buy", dates[1]),
        ("sell", dates[2]),
    ]
    for side in ("buy", "sell"):
        signal = next(
            item for item in result.activities if item.kind == "signal" and item.side == side
        )
        order = next(
            item for item in result.activities if item.kind == "order" and item.side == side
        )
        fill = next(item for item in fills if item.side == side)
        assert signal.chain_id == signal.decision_id == signal.origin_signal_id == signal.id
        assert order.chain_id == order.decision_id == signal.id
        assert order.origin_signal_id == order.parent_id == signal.id
        assert order.order_id == order.id
        assert fill.chain_id == fill.decision_id == signal.id
        assert fill.origin_signal_id == signal.id
        assert fill.order_id == fill.parent_id == order.id
        assert fill.fill_id == fill.id
    assert all(item.quantity is None for item in fills)
    assert all(item.notional_cny is not None for item in fills)
    assert result.round_trips[0].entry_date == dates[1]
    assert result.round_trips[0].exit_date == dates[2]
    assert result.metrics.trade_count == 1
    assert result.has_open_position is False
    assert result.final_cash_cny == pytest.approx(Decimal("11976.02397602397602397602398"))
    assert "fees_and_slippage_from_backtest_run_config" in result.assumptions


def test_entry_validity_and_exit_retry_cover_suspension_and_price_limits() -> None:
    dates = tuple(START + timedelta(days=offset) for offset in range(7))
    rows = (
        _row(dates[0], raw_open="10"),
        _row(dates[1], raw_open="10", status=TradingStatus.SUSPENDED),
        _row(dates[2], raw_open="11", at_limit="up"),
        _row(dates[3], raw_open="10"),
        _row(dates[4], raw_open="9", at_limit="down"),
        _row(dates[5], raw_open="12"),
        _row(dates[6], raw_open="12"),
    )
    entries = tuple(
        _fact(day, triggered=index == 0, ref="entry") for index, day in enumerate(dates)
    )
    exits = tuple(None for _ in dates)

    result = run_skill_backtest(
        strategy=_strategy(dates, holding_sessions=1),
        history=_history(rows),
        entry_timeline=entries,
        exit_timeline=exits,
        config=_config(
            edge_entry_validity_sessions=3,
            max_exit_attempts=3,
            retry_unfilled_exits=True,
            limit_handling=LimitHandling.WAIT_FOR_UNLOCK,
        ),
    )

    unfilled_reasons = [item.reason for item in result.activities if item.kind == "unfilled"]
    assert "security_suspended" in unfilled_reasons
    assert "daily_buy_limit_open_unlock_unknown" in unfilled_reasons
    assert "daily_sell_limit_open_unlock_unknown" in unfilled_reasons
    fills = [item for item in result.activities if item.kind == "fill"]
    assert [(item.side, item.occurred_at.date()) for item in fills] == [
        ("buy", dates[3]),
        ("sell", dates[5]),
    ]
    assert result.round_trips[0].entry_date == dates[3]
    assert result.round_trips[0].exit_date == dates[5]
    orders = [item for item in result.activities if item.kind == "order"]
    assert [item.attempt_no for item in orders if item.side == "buy"] == [1, 2, 3]
    assert [item.attempt_no for item in orders if item.side == "sell"] == [1, 2]
    assert len({item.order_id for item in orders}) == len(orders)
    for order in orders:
        outcomes = [
            item
            for item in result.activities
            if item.kind in {"fill", "partial_fill", "unfilled"} and item.order_id == order.order_id
        ]
        assert len(outcomes) == 1
        assert outcomes[0].parent_id == order.id
        assert outcomes[0].attempt_no == order.attempt_no


def test_adjusted_scale_is_return_invariant_and_period_end_does_not_liquidate() -> None:
    dates = tuple(START + timedelta(days=offset) for offset in range(4))
    base_rows = tuple(
        _row(day, raw_open=value, raw_close=value)
        for day, value in zip(dates, ("10", "10", "11", "12"), strict=True)
    )
    scaled_rows = tuple(
        _row(day, raw_open=value, raw_close=value, adjusted_scale="100")
        for day, value in zip(dates, ("10", "10", "11", "12"), strict=True)
    )
    entries = (
        _fact(dates[0], triggered=True, ref="entry"),
        None,
        None,
        None,
    )
    exits = tuple(None for _ in dates)
    config = _config(slippage_bps=Decimal("5"))
    strategy = _strategy(dates)

    base = run_skill_backtest(
        strategy=strategy,
        history=_history(base_rows),
        entry_timeline=entries,
        exit_timeline=exits,
        config=config,
    )
    scaled = run_skill_backtest(
        strategy=strategy,
        history=_history(scaled_rows),
        entry_timeline=entries,
        exit_timeline=exits,
        config=config,
    )

    assert base.has_open_position is True
    assert base.round_trips == ()
    assert base.open_position_notional_cny == pytest.approx(
        scaled.open_position_notional_cny,
        abs=Decimal("1e-20"),
    )
    assert tuple(point.equity for point in base.equity_curve) == pytest.approx(
        tuple(point.equity for point in scaled.equity_curve),
        abs=Decimal("1e-20"),
    )
    assert base.metrics.total_return == pytest.approx(scaled.metrics.total_return)
    assert base.unknown_entry_sessions == dates[1:]
    assert base.unknown_exit_sessions == dates
    assert any("不是证券账户逐笔股份账本" in item for item in base.limitations)

    final_session_signal = run_skill_backtest(
        strategy=strategy,
        history=_history(base_rows),
        entry_timeline=tuple(
            _fact(day, triggered=index == len(dates) - 1, ref="entry")
            for index, day in enumerate(dates)
        ),
        exit_timeline=exits,
        config=config,
    )
    assert [item.kind for item in final_session_signal.activities] == ["signal"]
    pending_signal = final_session_signal.activities[0]
    assert pending_signal.origin_signal_id == pending_signal.id
    assert pending_signal.order_id is None
