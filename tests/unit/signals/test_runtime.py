from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ashare_lab.domain.signals import SignalRuntime, SignalRuntimeError
from ashare_lab.domain.signals.models import SignalFact
from ashare_lab.domain.strategy import (
    AllCondition,
    AnyCondition,
    IndicatorCondition,
    NotCondition,
)

from .conftest import make_bars


def ma_condition(trigger: str = "price_crosses_above", *, period: int = 2) -> IndicatorCondition:
    return IndicatorCondition(
        indicator_id="technical.ma",
        definition_version="1.0.0",
        params={"period": period, "price_field": "close"},
        trigger=trigger,
    )


def rsi_condition(trigger: str = "above", *, value: float = 50) -> IndicatorCondition:
    return IndicatorCondition(
        indicator_id="technical.rsi",
        definition_version="1.0.0",
        params={"period": 2},
        trigger=trigger,
        value=value,
    )


def volume_condition(trigger: str = "gte_multiple", *, value: float = 1) -> IndicatorCondition:
    return IndicatorCondition(
        indicator_id="market.volume",
        definition_version="1.0.0",
        params={"baseline_period": 2},
        trigger=trigger,
        value=value,
    )


def indicator_condition(
    indicator_id: str,
    trigger: str,
    params: dict[str, str | int | float | bool],
    *,
    value: float | None = None,
) -> IndicatorCondition:
    return IndicatorCondition(
        indicator_id=indicator_id,
        definition_version="1.0.0",
        params=params,
        trigger=trigger,
        value=value,
    )


def _fact_signature(fact: SignalFact | None) -> tuple[object, ...] | None:
    if fact is None:
        return None
    return (
        fact.condition_ref,
        fact.triggered,
        fact.reason,
        fact.left_value,
        fact.right_value,
    )


@pytest.mark.parametrize(
    "condition",
    [
        indicator_condition("technical.ma", "price_above", {"period": 5, "price_field": "close"}),
        indicator_condition("technical.ema", "price_above", {"period": 5, "price_field": "close"}),
        indicator_condition(
            "technical.ma_cross",
            "fast_above_slow",
            {"fast_period": 3, "slow_period": 6, "price_field": "close"},
        ),
        indicator_condition(
            "technical.bollinger",
            "price_crosses_above_upper",
            {"period": 5, "stddev_multiplier": 2.0, "price_field": "close"},
        ),
        indicator_condition(
            "technical.kdj",
            "j_above",
            {"period": 5, "k_smoothing": 3, "d_smoothing": 3},
            value=50,
        ),
        indicator_condition("technical.cci", "above", {"period": 5, "constant": 0.015}, value=0),
        indicator_condition(
            "technical.bbi",
            "price_above",
            {
                "period_1": 3,
                "period_2": 6,
                "period_3": 12,
                "period_4": 24,
                "price_field": "close",
            },
        ),
        indicator_condition(
            "technical.ema_bias",
            "above",
            {"period": 5, "price_field": "close"},
            value=0,
        ),
        indicator_condition(
            "price.return_pct",
            "above",
            {"period": 5, "price_field": "close"},
            value=0,
        ),
        indicator_condition(
            "price.rolling_high",
            "new_high",
            {"period": 5, "price_field": "close"},
        ),
        indicator_condition("price.consecutive_up", "at_least", {"days": 3}),
        indicator_condition("price.amplitude", "above", {}, value=0),
        indicator_condition("market.amount", "above", {}, value=0),
        indicator_condition("amount.average", "above", {"period": 5}, value=0),
        indicator_condition("technical.macd", "golden_cross", {"fast": 3, "slow": 6, "signal": 3}),
        indicator_condition("technical.rsi", "above", {"period": 5}, value=50),
        indicator_condition("market.volume", "gte_multiple", {"baseline_period": 5}, value=1),
        indicator_condition(
            "volume.relative",
            "gte_multiple",
            {"baseline_period": 5, "consecutive_days": 3},
            value=1,
        ),
        indicator_condition(
            "volume.price_confirmation",
            "surge_up",
            {"baseline_period": 5, "return_threshold_pct": 0.0, "volume_multiple": 1.0},
        ),
        indicator_condition("technical.obv", "rising", {}),
        indicator_condition(
            "volume.price_divergence",
            "bullish",
            {
                "average_volume_period": 5,
                "left_bars": 1,
                "max_separation": 20,
                "min_separation": 2,
                "obv_threshold_adv": 1.0,
                "price_threshold_pct": 1.0,
                "right_bars": 1,
            },
        ),
        indicator_condition(
            "technical.trend_regime",
            "uptrend",
            {
                "adx_period": 5,
                "adx_threshold": 1.0,
                "confirmation_days": 1,
                "long_period": 10,
                "short_period": 5,
                "slope_lookback": 3,
                "stability_bars": 20,
            },
        ),
        indicator_condition("price.true_range", "above", {}, value=0),
        indicator_condition("technical.atr", "above", {"period": 5}, value=0),
        indicator_condition("technical.natr", "above", {"period": 5}, value=0),
        indicator_condition("technical.adx", "above", {"period": 5}, value=0),
        indicator_condition("technical.dmi", "plus_above_minus", {"period": 5}),
        indicator_condition(
            "technical.bias",
            "above",
            {"period": 5, "price_field": "close"},
            value=-100,
        ),
        indicator_condition(
            "technical.roc",
            "above",
            {"period": 5, "price_field": "close"},
            value=-100,
        ),
        indicator_condition(
            "technical.momentum",
            "above",
            {"period": 5, "price_field": "close"},
            value=-1000,
        ),
        indicator_condition(
            "technical.stochastic",
            "k_above",
            {"k_period": 5, "d_period": 3},
            value=0,
        ),
        indicator_condition("technical.williams_r", "above", {"period": 5}, value=-100),
        indicator_condition("technical.donchian", "price_above_upper", {"period": 5}),
        indicator_condition(
            "technical.return_stddev",
            "above",
            {"period": 5, "price_field": "close"},
            value=0,
        ),
        indicator_condition(
            "technical.historical_volatility",
            "above",
            {"annualization_sessions": 252, "period": 5, "price_field": "close"},
            value=0,
        ),
    ],
)
def test_all_stable_indicators_skip_zero_volume_suspension_rows(
    condition: IndicatorCondition,
) -> None:
    closes = [100 + (index % 11) - (index % 4) for index in range(140)]
    volumes = [100 + (index % 7) * 10 for index in range(140)]
    base = make_bars(closes, volumes=volumes)
    suspension_index = 70
    with_suspension = make_bars(
        [*closes[:suspension_index], 9999, *closes[suspension_index:]],
        volumes=[*volumes[:suspension_index], 0, *volumes[suspension_index:]],
    )

    base_timeline = SignalRuntime().evaluate_aligned(condition, base)
    suspension_timeline = SignalRuntime().evaluate_aligned(condition, with_suspension)

    assert suspension_timeline[suspension_index] is None
    without_suspension_slot = (
        suspension_timeline[:suspension_index] + suspension_timeline[suspension_index + 1 :]
    )
    assert tuple(_fact_signature(fact) for fact in without_suspension_slot) == tuple(
        _fact_signature(fact) for fact in base_timeline
    )


@pytest.mark.parametrize(
    "condition",
    [
        indicator_condition("price.true_range", "above", {}, value=0),
        indicator_condition("technical.atr", "above", {"period": 3}, value=0),
        indicator_condition("technical.natr", "above", {"period": 3}, value=0),
        indicator_condition("technical.adx", "above", {"period": 2}, value=0),
        indicator_condition("technical.dmi", "plus_above_minus", {"period": 2}),
        indicator_condition(
            "technical.bias",
            "above",
            {"period": 3, "price_field": "close"},
            value=-100,
        ),
        indicator_condition(
            "technical.roc",
            "above",
            {"period": 3, "price_field": "close"},
            value=-100,
        ),
        indicator_condition(
            "technical.momentum",
            "above",
            {"period": 3, "price_field": "close"},
            value=-100,
        ),
        indicator_condition(
            "technical.stochastic",
            "k_above",
            {"k_period": 3, "d_period": 2},
            value=0,
        ),
        indicator_condition("technical.williams_r", "above", {"period": 3}, value=-100),
        indicator_condition("technical.donchian", "price_above_upper", {"period": 3}),
        indicator_condition(
            "technical.return_stddev",
            "above",
            {"period": 3, "price_field": "close"},
            value=0,
        ),
        indicator_condition(
            "technical.historical_volatility",
            "above",
            {"annualization_sessions": 252, "period": 3, "price_field": "close"},
            value=0,
        ),
    ],
)
def test_p1_runtime_facts_explain_left_and_right_values(condition: IndicatorCondition) -> None:
    closes = [10, 11, 13, 12, 15, 14, 16, 18, 17, 20]
    bars = make_bars(
        closes,
        highs=[value + 1 for value in closes],
        lows=[value - 1 for value in closes],
    )

    facts = SignalRuntime().evaluate(condition, bars)

    assert facts
    fact = facts[-1]
    assert fact.condition_ref.startswith(f"{condition.indicator_id}@1.0.0:")
    assert "=>" in fact.reason
    assert fact.left_value is not None
    assert fact.right_value is not None


def test_boolean_conditions_use_short_circuit_three_valued_logic_during_warmup() -> None:
    bars = make_bars([10, 11, 12])
    ready_true = indicator_condition("market.amount", "above", {}, value=0)
    ready_false = indicator_condition("market.amount", "below", {}, value=0)
    warming = indicator_condition("technical.rsi", "above", {"period": 14}, value=50)
    runtime = SignalRuntime()

    any_true = runtime.evaluate_aligned(AnyCondition(children=(ready_true, warming)), bars)
    any_unknown = runtime.evaluate_aligned(AnyCondition(children=(ready_false, warming)), bars)
    all_false = runtime.evaluate_aligned(AllCondition(children=(ready_false, warming)), bars)
    all_unknown = runtime.evaluate_aligned(AllCondition(children=(ready_true, warming)), bars)

    assert all(fact is not None and fact.triggered for fact in any_true)
    assert all(fact is None for fact in any_unknown)
    assert all(fact is not None and not fact.triggered for fact in all_false)
    assert all(fact is None for fact in all_unknown)
    first_any = any_true[0]
    first_all = all_false[0]
    assert first_any is not None and first_any.reason == "any[true,unknown] => true"
    assert first_all is not None and first_all.reason == "all[false,unknown] => false"
    assert len(first_any.children) == len(first_all.children) == 1


def test_ma_cross_uses_only_closed_prefix_and_has_explainable_fact() -> None:
    bars = make_bars([3, 1, 3])

    aligned = SignalRuntime().evaluate_aligned(ma_condition(), bars)

    assert aligned[:2] == (None, None)
    fact = aligned[2]
    assert fact is not None
    assert fact.triggered
    assert fact.left_value == Decimal("3")
    assert fact.right_value == Decimal("2")
    assert fact.observed_at == datetime(2024, 1, 4, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert fact.available_at == bars[2].available_at
    assert fact.reason == "close=3 crosses_above ma(2)=2 => true"
    assert fact.children == ()

    with pytest.raises(FrozenInstanceError):
        fact.triggered = False  # type: ignore[misc]


def test_level_and_volume_boundaries_respect_strict_and_inclusive_operators() -> None:
    bars = make_bars([1, 2], volumes=[100, 300])

    ma_fact = SignalRuntime().evaluate_latest(ma_condition("price_above"), bars)
    volume_fact = SignalRuntime().evaluate_latest(volume_condition(value=1.5), bars)

    assert ma_fact is not None and ma_fact.triggered
    assert volume_fact is not None and volume_fact.triggered
    assert volume_fact.left_value == volume_fact.right_value == Decimal("300.0")


def test_dual_moving_average_level_relation_does_not_require_a_cross() -> None:
    bars = make_bars([1, 2, 3, 4])
    condition = indicator_condition(
        "technical.ma_cross",
        "fast_above_slow",
        {"fast_period": 2, "slow_period": 3, "price_field": "close"},
    )

    aligned = SignalRuntime().evaluate_aligned(condition, bars)

    assert aligned[:2] == (None, None)
    assert aligned[2] is not None and aligned[2].triggered
    assert aligned[3] is not None and aligned[3].triggered


def test_macd_and_rsi_crosses_require_two_ready_observations() -> None:
    macd_condition = IndicatorCondition(
        indicator_id="technical.macd",
        definition_version="1.0.0",
        params={"fast": 2, "slow": 3, "signal": 2},
        trigger="golden_cross",
    )
    bars = make_bars([1, 2, 3, 4, 5])

    macd_aligned = SignalRuntime().evaluate_aligned(macd_condition, bars)
    rsi_aligned = SignalRuntime().evaluate_aligned(
        rsi_condition("crosses_above", value=50), bars[:4]
    )

    assert macd_aligned[:4] == (None, None, None, None)
    assert macd_aligned[4] is not None
    assert rsi_aligned[:3] == (None, None, None)
    assert rsi_aligned[3] is not None


def test_all_any_and_not_wait_for_every_child_warmup() -> None:
    bars = make_bars([1, 2, 3], volumes=[100, 200, 300])
    ma = ma_condition("price_above")
    not_low_volume = NotCondition(child=volume_condition("lte_multiple", value=1))
    all_condition = AllCondition(children=(ma, not_low_volume))
    any_condition = AnyCondition(children=(NotCondition(child=ma), not_low_volume))

    all_facts = SignalRuntime().evaluate_aligned(all_condition, bars)
    any_fact = SignalRuntime().evaluate_latest(any_condition, bars)

    assert all_facts[0] is None
    assert all_facts[2] is not None and all_facts[2].triggered
    assert all_facts[2].reason == "all[true,true] => true"
    assert len(all_facts[2].children) == 2
    assert any_fact is not None and any_fact.triggered
    assert any_fact.reason == "any[false,true] => true"


def test_late_dependency_delays_signal_availability() -> None:
    bars = make_bars([1, 2, 3], first_available_delay_days=5)

    fact = SignalRuntime().evaluate_latest(ma_condition("price_above", period=3), bars)

    assert fact is not None
    assert fact.observed_at.date() == date(2024, 1, 4)
    assert fact.available_at == bars[0].available_at


def test_rejects_unsorted_or_mixed_instrument_bars() -> None:
    bars = make_bars([1, 2, 3])
    with pytest.raises(SignalRuntimeError, match="strictly ordered"):
        SignalRuntime().evaluate(ma_condition(), (bars[1], bars[0]))

    other = make_bars([3], symbol="600000.SH")[0]
    with pytest.raises(SignalRuntimeError, match="one instrument"):
        SignalRuntime().evaluate(ma_condition(), (bars[0], other))


def test_rejects_unknown_indicator_version_or_trigger() -> None:
    bars = make_bars([1, 2, 3])
    wrong_version = ma_condition().model_copy(update={"definition_version": "2.0.0"})
    wrong_trigger = ma_condition().model_copy(update={"trigger": "surprise"})

    with pytest.raises(SignalRuntimeError, match="definition version"):
        SignalRuntime().evaluate(wrong_version, bars)
    with pytest.raises(SignalRuntimeError, match="unsupported trigger"):
        SignalRuntime().evaluate(wrong_trigger, bars)


@settings(max_examples=40, deadline=None)
@given(st.lists(st.integers(min_value=1, max_value=1_000), min_size=6, max_size=30))
def test_prefix_invariance(prices: list[int]) -> None:
    """Adding future bars must never revise an already-emitted signal fact."""

    bars = make_bars(prices, volumes=[100 + index for index in range(len(prices))])
    condition = AllCondition(
        children=(
            ma_condition("price_above", period=3),
            NotCondition(child=rsi_condition("below", value=40)),
        )
    )
    runtime = SignalRuntime()
    full = runtime.evaluate_aligned(condition, bars)

    for prefix_length in range(1, len(bars) + 1):
        assert runtime.evaluate_aligned(condition, bars[:prefix_length]) == full[:prefix_length]


@pytest.mark.parametrize(
    ("condition", "first_fact_index"),
    [
        (
            indicator_condition(
                "technical.ema",
                "price_crosses_above",
                {"period": 3, "price_field": "close"},
            ),
            3,
        ),
        (
            indicator_condition(
                "technical.ma_cross",
                "golden_cross",
                {"fast_period": 2, "slow_period": 3, "price_field": "close"},
            ),
            3,
        ),
        (
            indicator_condition(
                "technical.bollinger",
                "price_crosses_above_middle",
                {"period": 3, "stddev_multiplier": 2.0, "price_field": "close"},
            ),
            3,
        ),
        (
            indicator_condition(
                "technical.kdj",
                "golden_cross",
                {"period": 3, "k_smoothing": 3, "d_smoothing": 3},
            ),
            3,
        ),
        (
            indicator_condition(
                "technical.cci",
                "crosses_above",
                {"period": 3, "constant": 0.015},
                value=0,
            ),
            3,
        ),
        (
            indicator_condition(
                "technical.bbi",
                "price_crosses_above",
                {
                    "period_1": 1,
                    "period_2": 2,
                    "period_3": 3,
                    "period_4": 4,
                    "price_field": "close",
                },
            ),
            4,
        ),
        (
            indicator_condition(
                "technical.ema_bias",
                "crosses_above",
                {"period": 3, "price_field": "close"},
                value=0,
            ),
            3,
        ),
    ],
)
def test_new_cross_indicators_wait_for_two_ready_points(
    condition: IndicatorCondition,
    first_fact_index: int,
) -> None:
    bars = make_bars([5, 4, 3, 2, 3, 4, 5])

    aligned = SignalRuntime().evaluate_aligned(condition, bars)

    assert aligned[:first_fact_index] == (None,) * first_fact_index
    assert aligned[first_fact_index] is not None


def test_cci_runtime_defaults_to_fourteen_and_cross_needs_fifteen_bars() -> None:
    condition = indicator_condition(
        "technical.cci",
        "crosses_above",
        {"constant": 0.015},
        value=0,
    )

    aligned = SignalRuntime().evaluate_aligned(condition, make_bars(range(1, 16)))

    assert aligned[:14] == (None,) * 14
    assert aligned[14] is not None
    assert "cci(14)" in aligned[14].reason


@pytest.mark.parametrize(
    ("condition", "prices"),
    [
        (
            indicator_condition(
                "technical.ema",
                "price_crosses_above",
                {"period": 3, "price_field": "close"},
            ),
            [5, 4, 3, 5, 6],
        ),
        (
            indicator_condition(
                "technical.ma_cross",
                "golden_cross",
                {"fast_period": 2, "slow_period": 3, "price_field": "close"},
            ),
            [3, 2, 1, 2, 3, 4],
        ),
        (
            indicator_condition(
                "technical.bollinger",
                "price_crosses_above_middle",
                {"period": 3, "stddev_multiplier": 2, "price_field": "close"},
            ),
            [5, 4, 3, 5, 6],
        ),
        (
            indicator_condition(
                "technical.kdj",
                "golden_cross",
                {"period": 3, "k_smoothing": 3, "d_smoothing": 3},
            ),
            [3, 2, 1, 2, 3, 4],
        ),
        (
            indicator_condition(
                "technical.cci",
                "crosses_above",
                {"period": 3, "constant": 0.015},
                value=0,
            ),
            [3, 2, 1, 2, 3, 4],
        ),
        (
            indicator_condition(
                "technical.bbi",
                "price_crosses_above",
                {
                    "period_1": 1,
                    "period_2": 2,
                    "period_3": 3,
                    "period_4": 4,
                    "price_field": "close",
                },
            ),
            [5, 4, 3, 2, 3, 4, 5],
        ),
        (
            indicator_condition(
                "technical.ema_bias",
                "crosses_above",
                {"period": 3, "price_field": "close"},
                value=0,
            ),
            [5, 4, 3, 5, 6],
        ),
    ],
)
def test_new_cross_triggers_fire_once_after_lines_separate(
    condition: IndicatorCondition,
    prices: list[int],
) -> None:
    facts = SignalRuntime().evaluate(condition, make_bars(prices))

    assert sum(fact.triggered for fact in facts) == 1
    triggered_index = next(index for index, fact in enumerate(facts) if fact.triggered)
    assert all(not fact.triggered for fact in facts[triggered_index + 1 :])


NEW_PREFIX_CONDITIONS = (
    indicator_condition(
        "technical.ema",
        "price_above",
        {"period": 3, "price_field": "close"},
    ),
    indicator_condition(
        "technical.ma_cross",
        "golden_cross",
        {"fast_period": 2, "slow_period": 3, "price_field": "close"},
    ),
    indicator_condition(
        "technical.bollinger",
        "price_crosses_below_lower",
        {"period": 3, "stddev_multiplier": 2, "price_field": "close"},
    ),
    indicator_condition(
        "technical.kdj",
        "j_above",
        {"period": 3, "k_smoothing": 3, "d_smoothing": 3},
        value=80,
    ),
    indicator_condition(
        "technical.cci",
        "above",
        {"period": 3, "constant": 0.015},
        value=100,
    ),
    indicator_condition(
        "technical.bbi",
        "price_below",
        {
            "period_1": 1,
            "period_2": 2,
            "period_3": 3,
            "period_4": 4,
            "price_field": "close",
        },
    ),
    indicator_condition(
        "technical.ema_bias",
        "below",
        {"period": 3, "price_field": "close"},
        value=-5,
    ),
)


@settings(max_examples=25, deadline=None)
@pytest.mark.parametrize("condition", NEW_PREFIX_CONDITIONS)
@given(st.lists(st.integers(min_value=1, max_value=1_000), min_size=6, max_size=20))
def test_every_new_indicator_is_prefix_invariant(
    condition: IndicatorCondition,
    prices: list[int],
) -> None:
    bars = make_bars(prices)
    runtime = SignalRuntime()
    full = runtime.evaluate_aligned(condition, bars)

    for prefix_length in range(1, len(bars) + 1):
        assert runtime.evaluate_aligned(condition, bars[:prefix_length]) == full[:prefix_length]


@pytest.mark.parametrize("condition", NEW_PREFIX_CONDITIONS)
def test_appending_a_future_extreme_never_rewrites_new_indicator_history(
    condition: IndicatorCondition,
) -> None:
    base = make_bars([10, 9, 8, 9, 10, 11, 12, 11])
    extended = make_bars([10, 9, 8, 9, 10, 11, 12, 11, 1_000_000])
    runtime = SignalRuntime()

    assert runtime.evaluate_aligned(condition, extended)[: len(base)] == runtime.evaluate_aligned(
        condition, base
    )


def test_recursive_new_indicator_keeps_late_source_availability() -> None:
    bars = make_bars([5, 4, 3, 5], first_available_delay_days=7)
    condition = indicator_condition(
        "technical.ema",
        "price_crosses_above",
        {"period": 3, "price_field": "close"},
    )

    fact = SignalRuntime().evaluate_latest(condition, bars)

    assert fact is not None
    assert fact.available_at == bars[0].available_at


def test_return_pct_cross_uses_percentage_points_and_two_ready_values() -> None:
    condition = indicator_condition(
        "price.return_pct",
        "crosses_above",
        {"period": 1, "price_field": "close"},
        value=5,
    )

    aligned = SignalRuntime().evaluate_aligned(condition, make_bars([100, 104, 110]))

    assert aligned[:2] == (None, None)
    fact = aligned[2]
    assert fact is not None and fact.triggered
    assert fact.left_value == pytest.approx(Decimal(150) / Decimal(26))
    assert fact.right_value == Decimal(5)


def test_rolling_high_excludes_today_and_equality_is_not_new_high() -> None:
    condition = indicator_condition(
        "price.rolling_high",
        "new_high",
        {"period": 2, "price_field": "close"},
    )

    aligned = SignalRuntime().evaluate_aligned(condition, make_bars([10, 12, 12, 13]))

    assert aligned[:2] == (None, None)
    assert aligned[2] is not None and not aligned[2].triggered
    assert aligned[2].left_value == aligned[2].right_value == Decimal(12)
    assert aligned[3] is not None and aligned[3].triggered


def test_consecutive_up_at_least_days_and_flat_resets() -> None:
    condition = indicator_condition(
        "price.consecutive_up",
        "at_least",
        {"days": 2},
    )

    aligned = SignalRuntime().evaluate_aligned(condition, make_bars([1, 2, 3, 3, 4, 5]))

    assert aligned[:2] == (None, None)
    assert aligned[2] is not None and aligned[2].triggered
    assert aligned[3] is not None and not aligned[3].triggered
    assert aligned[3].left_value == Decimal(0)
    assert aligned[5] is not None and aligned[5].triggered


def test_amplitude_cross_uses_previous_close() -> None:
    condition = indicator_condition(
        "price.amplitude",
        "crosses_below",
        {},
        value=20,
    )
    bars = make_bars(
        [10, 11, 12],
        highs=[10, 12, 12],
        lows=[10, 9, 11],
    )

    aligned = SignalRuntime().evaluate_aligned(condition, bars)

    assert aligned[:2] == (None, None)
    fact = aligned[2]
    assert fact is not None and fact.triggered
    assert fact.left_value == pytest.approx(Decimal(100) / Decimal(11))
    assert fact.right_value == Decimal(20)


def test_raw_amount_and_amount_average_read_daily_bar_turnover() -> None:
    raw_condition = indicator_condition("market.amount", "crosses_above", {}, value=250)
    average_condition = indicator_condition(
        "amount.average",
        "crosses_above",
        {"period": 2},
        value=250,
    )
    bars = make_bars([1, 1, 1], volumes=[1_000, 1_000, 1_000], amounts=[100, 200, 400])

    raw = SignalRuntime().evaluate_aligned(raw_condition, bars)
    average = SignalRuntime().evaluate_aligned(average_condition, bars)

    assert raw[2] is not None and raw[2].triggered
    assert raw[2].left_value == Decimal(400)
    assert average[:2] == (None, None)
    assert average[2] is not None and average[2].triggered
    assert average[2].left_value == Decimal(300)


def test_consecutive_up_exact_streak_keeps_earliest_source_availability() -> None:
    condition = indicator_condition(
        "price.consecutive_up",
        "at_least",
        {"days": 2},
    )
    bars = make_bars([1, 2, 3, 4], first_available_delay_days=7)

    fact = SignalRuntime().evaluate_latest(condition, bars)

    assert fact is not None and fact.left_value == Decimal(3)
    assert fact.available_at == bars[0].available_at


SECOND_BATCH_PREFIX_CONDITIONS = (
    indicator_condition(
        "price.return_pct",
        "above",
        {"period": 3, "price_field": "close"},
        value=5,
    ),
    indicator_condition(
        "price.rolling_high",
        "new_high",
        {"period": 3, "price_field": "close"},
    ),
    indicator_condition(
        "price.consecutive_up",
        "at_least",
        {"days": 3},
    ),
    indicator_condition("price.amplitude", "above", {}, value=5),
    indicator_condition("market.amount", "crosses_above", {}, value=50_000),
    indicator_condition(
        "amount.average",
        "above",
        {"period": 3},
        value=50_000,
    ),
)


@settings(max_examples=25, deadline=None)
@pytest.mark.parametrize("condition", SECOND_BATCH_PREFIX_CONDITIONS)
@given(st.lists(st.integers(min_value=2, max_value=1_000), min_size=6, max_size=20))
def test_every_second_batch_indicator_is_prefix_invariant(
    condition: IndicatorCondition,
    prices: list[int],
) -> None:
    bars = make_bars(
        prices,
        highs=[price + 1 for price in prices],
        lows=[price - 1 for price in prices],
        amounts=[price * 1_000 for price in prices],
    )
    runtime = SignalRuntime()
    full = runtime.evaluate_aligned(condition, bars)

    for prefix_length in range(1, len(bars) + 1):
        assert runtime.evaluate_aligned(condition, bars[:prefix_length]) == full[:prefix_length]


@pytest.mark.parametrize("condition", SECOND_BATCH_PREFIX_CONDITIONS)
def test_appending_future_extreme_never_rewrites_second_batch_history(
    condition: IndicatorCondition,
) -> None:
    base_prices = [10, 9, 8, 9, 10, 11, 12, 11]
    extended_prices = [*base_prices, 1_000_000]
    base = make_bars(
        base_prices,
        highs=[price + 1 for price in base_prices],
        lows=[price - 1 for price in base_prices],
        amounts=[price * 1_000 for price in base_prices],
    )
    extended = make_bars(
        extended_prices,
        highs=[price + 1 for price in extended_prices],
        lows=[price - 1 for price in extended_prices],
        amounts=[price * 1_000 for price in extended_prices],
    )
    runtime = SignalRuntime()

    assert runtime.evaluate_aligned(condition, extended)[: len(base)] == runtime.evaluate_aligned(
        condition, base
    )


def _relative_volume_condition(
    trigger: str = "gte_multiple",
    *,
    value: float = 1.5,
    days: int = 3,
) -> IndicatorCondition:
    return indicator_condition(
        "volume.relative",
        trigger,
        {"baseline_period": 2, "consecutive_days": days},
        value=value,
    )


def _divergence_condition(trigger: str = "bearish") -> IndicatorCondition:
    return indicator_condition(
        "volume.price_divergence",
        trigger,
        {
            "left_bars": 1,
            "right_bars": 1,
            "min_separation": 2,
            "max_separation": 10,
            "price_threshold_pct": 1,
            "obv_threshold_adv": 1,
            "average_volume_period": 2,
        },
    )


def _trend_condition(trigger: str = "uptrend") -> IndicatorCondition:
    return indicator_condition(
        "technical.trend_regime",
        trigger,
        {
            "short_period": 2,
            "long_period": 3,
            "slope_lookback": 1,
            "adx_period": 2,
            "adx_threshold": 25,
            "confirmation_days": 2,
            "stability_bars": 6,
        },
    )


def test_relative_volume_uses_prior_valid_sessions_and_suspension_is_none() -> None:
    bars = make_bars([10, 10, 10, 10], volumes=[100, 0, 200, 300])

    aligned = SignalRuntime().evaluate_aligned(_relative_volume_condition(value=2), bars)

    assert aligned[:3] == (None, None, None)
    assert aligned[3] is not None and aligned[3].triggered
    assert aligned[3].left_value == aligned[3].right_value == Decimal(2)
    assert "rvol_prior(2)" in aligned[3].reason


def test_consecutive_volume_counts_valid_sessions_and_requires_every_day() -> None:
    bars = make_bars(
        [10, 10, 10, 10, 10, 10],
        volumes=[100, 100, 150, 0, 180, 220],
    )
    condition = _relative_volume_condition(
        "consecutive_gte_multiple",
        value=1.2,
        days=3,
    )

    aligned = SignalRuntime().evaluate_aligned(condition, bars)

    assert aligned[3] is None
    assert aligned[5] is not None and aligned[5].triggered


def test_volume_price_confirmation_requires_both_volume_and_price() -> None:
    bars = make_bars([10, 10, 10, "10.6"], volumes=[100, 100, 100, 160])
    condition = indicator_condition(
        "volume.price_confirmation",
        "surge_up",
        {"baseline_period": 2, "volume_multiple": 1.5, "return_threshold_pct": 5},
    )

    fact = SignalRuntime().evaluate_latest(condition, bars)

    assert fact is not None and fact.triggered
    assert fact.left_value == Decimal(6)
    assert "rvol=1.6" in fact.reason


def test_obv_rising_ignores_zero_volume_session() -> None:
    bars = make_bars([10, 9, 11], volumes=[100, 0, 200])
    condition = indicator_condition("technical.obv", "rising", {})

    aligned = SignalRuntime().evaluate_aligned(condition, bars)

    assert aligned[1] is None
    assert aligned[2] is not None and aligned[2].triggered


def test_divergence_is_emitted_on_confirmation_day_not_pivot_or_terminal_high() -> None:
    bars = make_bars(
        [8, 10, 9, 11, 8, 12],
        volumes=[100, 100, 1_000, 100, 1_000, 100],
    )

    aligned = SignalRuntime().evaluate_aligned(_divergence_condition(), bars)

    assert aligned[3] is None
    assert aligned[4] is not None and aligned[4].triggered
    assert aligned[5] is not None and not aligned[5].triggered


def test_stage_trend_is_close_confirmed_and_suspension_has_no_fact() -> None:
    prices = list(range(1, 16))
    volumes = [100] * len(prices)
    volumes[8] = 0
    bars = make_bars(
        prices,
        highs=[price + 1 for price in prices],
        lows=prices,
        volumes=volumes,
    )

    aligned = SignalRuntime().evaluate_aligned(_trend_condition(), bars)

    assert aligned[8] is None
    assert aligned[-1] is not None and aligned[-1].triggered
    assert aligned[-1].observed_at.hour == 15


THIRD_BATCH_PREFIX_CONDITIONS = (
    _relative_volume_condition(),
    indicator_condition(
        "volume.price_confirmation",
        "surge_up",
        {"baseline_period": 2, "volume_multiple": 1.5, "return_threshold_pct": 5},
    ),
    indicator_condition("technical.obv", "rising", {}),
    _divergence_condition(),
    _trend_condition(),
)


@pytest.mark.parametrize("condition", THIRD_BATCH_PREFIX_CONDITIONS)
def test_volume_and_trend_indicators_are_prefix_invariant(condition: IndicatorCondition) -> None:
    prices = [8, 10, 9, 11, 8, 12, 10, 13, 11, 14, 12, 15]
    volumes = [100, 100, 1_000, 100, 1_000, 100, 200, 400, 150, 500, 200, 600]
    bars = make_bars(
        prices,
        highs=[price + 1 for price in prices],
        lows=prices,
        volumes=volumes,
    )
    runtime = SignalRuntime()
    full = runtime.evaluate_aligned(condition, bars)

    for prefix_length in range(1, len(bars) + 1):
        assert runtime.evaluate_aligned(condition, bars[:prefix_length]) == full[:prefix_length]
