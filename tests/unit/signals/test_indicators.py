from __future__ import annotations

from decimal import Decimal

import pytest

from ashare_lab.domain.signals import (
    amount_average,
    amplitude_pct,
    average_true_range,
    bollinger_bands,
    bull_and_bear_index,
    cci,
    confirmed_obv_divergence,
    consecutive_up,
    directional_movement,
    donchian_channel,
    ema_bias,
    exponential_moving_average,
    historical_volatility,
    kdj,
    macd,
    market_amount,
    momentum,
    moving_average,
    moving_average_cross,
    normalized_average_true_range,
    on_balance_volume,
    period_return_pct,
    previous_volume_average,
    rate_of_change,
    relative_volume,
    return_stddev,
    rolling_high,
    rsi,
    simple_bias,
    stage_trend,
    stochastic_oscillator,
    true_range,
    volume_average,
    volume_price_points,
    williams_r,
)

from .conftest import make_bars


def test_ma_and_volume_golden_values_and_warmup() -> None:
    bars = make_bars([1, 2, 3, 4, 5], volumes=[10, 20, 30, 40, 50])

    assert moving_average(bars, 3) == (
        None,
        None,
        Decimal("2"),
        Decimal("3"),
        Decimal("4"),
    )
    assert volume_average(bars, 2) == (
        None,
        Decimal("15"),
        Decimal("25"),
        Decimal("35"),
        Decimal("45"),
    )


def test_previous_volume_baseline_excludes_today_and_skips_suspensions() -> None:
    bars = make_bars([10, 10, 10, 10], volumes=[100, 0, 200, 300])

    assert previous_volume_average(bars, 2) == (
        None,
        None,
        None,
        Decimal(150),
    )
    assert relative_volume(bars, 2) == (
        None,
        None,
        None,
        Decimal(2),
    )


def test_volume_price_confirmation_uses_previous_valid_close() -> None:
    bars = make_bars([10, 10, 10, 11], volumes=[100, 0, 100, 200])

    points = volume_price_points(bars, baseline_period=2)

    assert points[:3] == (None, None, None)
    assert points[3] is not None
    assert points[3].relative_volume == Decimal(2)
    assert points[3].return_pct == Decimal(10)


def test_obv_is_aligned_and_zero_volume_day_is_not_actionable() -> None:
    bars = make_bars([10, 10, 11, 9], volumes=[100, 0, 200, 50])

    assert on_balance_volume(bars) == (
        Decimal(100),
        None,
        Decimal(300),
        Decimal(250),
    )


def test_confirmed_bearish_obv_divergence_emits_on_right_confirmation_day() -> None:
    bars = make_bars(
        [8, 10, 9, 11, 8, 12],
        volumes=[100, 100, 1_000, 100, 1_000, 100],
    )

    points = confirmed_obv_divergence(
        bars,
        left_bars=1,
        right_bars=1,
        min_separation=2,
        max_separation=10,
        price_threshold_pct=Decimal(1),
        obv_threshold_adv=Decimal(1),
        average_volume_period=2,
    )

    assert points[3] is None  # the second high is not confirmed yet
    assert points[4] is not None and points[4].bearish
    assert points[4].current_pivot_index == 3
    assert points[5] is not None and not points[5].bearish  # terminal high has no right bar


def test_directional_movement_and_stage_trend_golden_direction() -> None:
    prices = list(range(1, 132))
    bars = make_bars(
        prices,
        highs=[price + 1 for price in prices],
        lows=prices,
        volumes=[100] * len(prices),
    )

    dmi = directional_movement(bars, period=14)
    assert dmi[27] is not None
    assert dmi[27].adx == Decimal(100)
    assert dmi[27].plus_di > dmi[27].minus_di

    regimes = stage_trend(bars)
    assert regimes[119] is not None and regimes[119].regime == "range"
    assert regimes[120] is not None and regimes[120].regime == "up"


def test_true_range_atr_and_natr_use_wilder_golden_values() -> None:
    bars = make_bars(
        [10, 12, 11, 14],
        highs=[10, 13, 12, 15],
        lows=[10, 11, 10, 13],
    )

    assert true_range(bars) == (None, Decimal(3), Decimal(2), Decimal(4))
    assert average_true_range(bars, period=2) == (
        None,
        None,
        Decimal("2.5"),
        Decimal("3.25"),
    )
    natr = normalized_average_true_range(bars, period=2)
    assert natr[:2] == (None, None)
    assert natr[2] == pytest.approx(Decimal(250) / Decimal(11))
    assert natr[3] == pytest.approx(Decimal(325) / Decimal(14))


def test_dmi_and_adx_use_wilder_seed_and_recurrence_golden_values() -> None:
    bars = make_bars(
        [9, 11, 10, 13, 12],
        highs=[10, 12, 11, 14, 13],
        lows=[8, 9, 8, 10, 9],
    )

    points = directional_movement(bars, period=2)

    assert points[:3] == (None, None, None)
    first = points[3]
    second = points[4]
    assert first is not None and second is not None
    assert first.plus_di == pytest.approx(Decimal(400) / Decimal(7))
    assert first.minus_di == pytest.approx(Decimal(50) / Decimal(7))
    assert first.adx == pytest.approx(Decimal(500) / Decimal(9))
    assert second.plus_di == pytest.approx(Decimal(80) / Decimal(3))
    assert second.minus_di == pytest.approx(Decimal(50) / Decimal(3))
    assert second.adx == pytest.approx(Decimal(4600) / Decimal(117))


def test_bias_roc_and_momentum_golden_values() -> None:
    bars = make_bars([100, 110, 90])

    assert simple_bias(bars, period=3) == (None, None, Decimal(-10))
    assert rate_of_change(bars, period=2) == (None, None, Decimal(-10))
    assert momentum(bars, period=2) == (None, None, Decimal(-10))


def test_stochastic_and_williams_r_have_fixed_scales_and_flat_rules() -> None:
    bars = make_bars(
        [2, 3, 4, 2],
        highs=[3, 4, 5, 4],
        lows=[1, 1, 1, 1],
    )
    points = stochastic_oscillator(bars, k_period=3, d_period=2)

    assert points[:3] == (None, None, None)
    assert points[3] is not None
    assert points[3].k == Decimal(25)
    assert points[3].d == Decimal(50)
    assert williams_r(bars, period=3) == (
        None,
        None,
        Decimal(-25),
        Decimal(-75),
    )

    flat = make_bars([5, 5, 5, 5])
    flat_stochastic = stochastic_oscillator(flat, k_period=3, d_period=2)[3]
    assert flat_stochastic is not None
    assert flat_stochastic.k == flat_stochastic.d == Decimal(50)
    assert williams_r(flat, period=3)[2] == Decimal(-50)


def test_donchian_excludes_current_bar_and_return_volatility_golden_values() -> None:
    channel = donchian_channel(
        make_bars([1, 2, 3], highs=[2, 3, 4], lows=[1, 1, 2]),
        period=2,
    )
    assert channel[:2] == (None, None)
    assert channel[2] is not None
    assert channel[2].upper == Decimal(3)
    assert channel[2].middle == Decimal(2)
    assert channel[2].lower == Decimal(1)

    bars = make_bars([100, 110, 99])
    assert return_stddev(bars, period=2) == (None, None, Decimal(10))
    volatility = historical_volatility(bars, period=2, annualization_sessions=252)
    assert volatility[:2] == (None, None)
    assert volatility[2] == pytest.approx(Decimal("159.2774266833688873591169176241955"))


@pytest.mark.parametrize(
    "calculator",
    [
        lambda bars: true_range(bars),
        lambda bars: average_true_range(bars, period=5),
        lambda bars: normalized_average_true_range(bars, period=5),
        lambda bars: directional_movement(bars, period=5),
        lambda bars: simple_bias(bars, period=5),
        lambda bars: rate_of_change(bars, period=5),
        lambda bars: momentum(bars, period=5),
        lambda bars: stochastic_oscillator(bars, k_period=5, d_period=3),
        lambda bars: williams_r(bars, period=5),
        lambda bars: donchian_channel(bars, period=5),
        lambda bars: return_stddev(bars, period=5),
        lambda bars: historical_volatility(bars, period=5),
    ],
)
def test_p1_indicator_prefixes_never_change_when_future_bars_arrive(calculator: object) -> None:
    bars = make_bars(
        [100 + (index % 7) - (index % 3) for index in range(40)],
        highs=[102 + (index % 7) - (index % 3) for index in range(40)],
        lows=[98 + (index % 7) - (index % 3) for index in range(40)],
    )
    calculate = calculator
    assert callable(calculate)
    full = calculate(bars)
    for prefix_length in range(1, len(bars) + 1):
        assert calculate(bars[:prefix_length]) == full[:prefix_length]


def test_p1_flat_market_outputs_are_finite_and_never_nan() -> None:
    bars = make_bars([10] * 40)
    series = (
        true_range(bars),
        average_true_range(bars, period=5),
        normalized_average_true_range(bars, period=5),
        simple_bias(bars, period=5),
        rate_of_change(bars, period=5),
        momentum(bars, period=5),
        williams_r(bars, period=5),
        return_stddev(bars, period=5),
        historical_volatility(bars, period=5),
    )
    for values in series:
        assert all(value is None or value.is_finite() for value in values)

    dmi = directional_movement(bars, period=5)
    assert dmi[9] is not None
    assert dmi[9].adx == dmi[9].plus_di == dmi[9].minus_di == Decimal(0)

    stochastic = stochastic_oscillator(bars, k_period=5, d_period=3)
    assert stochastic[6] is not None
    assert stochastic[6].k == stochastic[6].d == Decimal(50)

    donchian = donchian_channel(bars, period=5)
    assert donchian[5] is not None
    assert donchian[5].upper == donchian[5].middle == donchian[5].lower == Decimal(10)


def test_p1_runtime_functions_reject_nonpositive_window_parameters() -> None:
    bars = make_bars([10, 11, 12])

    with pytest.raises(ValueError):
        average_true_range(bars, period=0)
    with pytest.raises(ValueError):
        normalized_average_true_range(bars, period=0)
    with pytest.raises(ValueError):
        directional_movement(bars, period=0)
    with pytest.raises(ValueError):
        simple_bias(bars, period=0)
    with pytest.raises(ValueError):
        rate_of_change(bars, period=0)
    with pytest.raises(ValueError):
        momentum(bars, period=0)
    with pytest.raises(ValueError):
        stochastic_oscillator(bars, k_period=0, d_period=3)
    with pytest.raises(ValueError):
        stochastic_oscillator(bars, k_period=3, d_period=0)
    with pytest.raises(ValueError):
        williams_r(bars, period=0)
    with pytest.raises(ValueError):
        donchian_channel(bars, period=0)
    with pytest.raises(ValueError):
        return_stddev(bars, period=0)
    with pytest.raises(ValueError):
        historical_volatility(bars, period=0)
    with pytest.raises(ValueError):
        historical_volatility(bars, period=2, annualization_sessions=0)


def test_macd_first_value_seeded_golden_values_and_warmup() -> None:
    bars = make_bars([1, 2, 3, 4, 5, 6])

    values = macd(bars, fast=2, slow=3, signal=2)

    assert values[:3] == (None, None, None)
    first_point = values[3]
    assert first_point is not None
    assert first_point.macd_line == pytest.approx(Decimal(85) / Decimal(216))
    assert first_point.signal_line == pytest.approx(Decimal(37) / Decimal(108))
    assert first_point.histogram == pytest.approx(Decimal(11) / Decimal(108))


def test_wilder_rsi_golden_values_and_flat_market_rule() -> None:
    bars = make_bars([1, 2, 3, 2, 2])

    assert rsi(bars, period=2) == (
        None,
        None,
        Decimal("100"),
        Decimal("50"),
        Decimal("50"),
    )
    assert rsi(make_bars([5, 5, 5]), period=2) == (None, None, Decimal("50"))


def test_ema_is_first_value_seeded_but_hidden_until_period() -> None:
    values = exponential_moving_average(make_bars([1, 2, 3, 4]), period=3)

    assert values == (
        None,
        None,
        Decimal("2.25"),
        Decimal("3.125"),
    )


def test_fast_and_slow_simple_moving_averages_have_exact_golden_values() -> None:
    values = moving_average_cross(
        make_bars([1, 2, 3, 4]),
        fast_period=2,
        slow_period=3,
    )

    assert values[:2] == (None, None)
    assert values[2] is not None
    assert values[2].fast == Decimal("2.5")
    assert values[2].slow == Decimal("2")
    assert values[3] is not None
    assert values[3].fast == Decimal("3.5")
    assert values[3].slow == Decimal("3")


def test_bollinger_uses_population_standard_deviation_and_flat_window() -> None:
    values = bollinger_bands(
        make_bars([1, 2, 3]),
        period=3,
        stddev_multiplier=Decimal(2),
    )
    point = values[2]

    assert values[:2] == (None, None)
    assert point is not None
    expected_width = (Decimal(2) / Decimal(3)).sqrt() * Decimal(2)
    assert point.middle == Decimal(2)
    assert point.upper == pytest.approx(Decimal(2) + expected_width)
    assert point.lower == pytest.approx(Decimal(2) - expected_width)

    flat = bollinger_bands(
        make_bars([5, 5, 5]),
        period=3,
        stddev_multiplier=Decimal(2),
    )[2]
    assert flat is not None
    assert flat.middle == flat.upper == flat.lower == Decimal(5)


def test_kdj_golden_values_and_zero_range_is_neutral() -> None:
    bars = make_bars(
        [1, 2, 3, 1],
        highs=[3, 3, 3, 3],
        lows=[1, 1, 1, 1],
    )
    values = kdj(bars, period=3, k_smoothing=3, d_smoothing=3)

    assert values[:2] == (None, None)
    first = values[2]
    second = values[3]
    assert first is not None and second is not None
    assert first.k == pytest.approx(Decimal(200) / Decimal(3))
    assert first.d == pytest.approx(Decimal(500) / Decimal(9))
    assert first.j == pytest.approx(Decimal(800) / Decimal(9))
    assert second.k == pytest.approx(Decimal(400) / Decimal(9))
    assert second.d == pytest.approx(Decimal(1400) / Decimal(27))
    assert second.j == pytest.approx(Decimal(800) / Decimal(27))

    flat = kdj(make_bars([5, 5, 5]), period=3)[2]
    assert flat is not None
    assert flat.k == flat.d == flat.j == Decimal(50)


def test_cci_typical_price_mad_golden_value_and_flat_window() -> None:
    values = cci(make_bars([1, 2, 3]), period=3, constant=Decimal("0.015"))

    assert values == (None, None, Decimal(100))
    assert cci(make_bars([5, 5, 5]), period=3)[2] == Decimal(0)


def test_cci_default_period_is_fourteen() -> None:
    values = cci(make_bars(range(1, 16)))

    assert values[:13] == (None,) * 13
    assert values[13] is not None
    assert values[14] is not None


def test_bbi_and_ema_bias_golden_values_and_warmup() -> None:
    bars = make_bars([1, 2, 3, 4])

    assert bull_and_bear_index(
        bars,
        period_1=1,
        period_2=2,
        period_3=3,
        period_4=4,
    ) == (None, None, None, Decimal("3.25"))
    bias = ema_bias(bars, period=3)
    assert bias[:2] == (None, None)
    assert bias[2] == pytest.approx(Decimal(100) / Decimal(3))
    assert bias[3] == Decimal(28)


def test_period_return_pct_uses_n_prior_periods_and_percentage_points() -> None:
    values = period_return_pct(make_bars([100, 110, 90, 99]), period=2)

    assert values == (None, None, Decimal("-10.0"), Decimal("-10.0"))
    assert period_return_pct(make_bars([100, 105]), period=1)[1] == Decimal(5)


def test_rolling_high_excludes_current_bar_and_preserves_equal_boundary() -> None:
    values = rolling_high(make_bars([10, 12, 12, 13]), period=2)

    assert values == (None, None, Decimal(12), Decimal(12))


def test_consecutive_up_requires_close_increase_and_flat_resets_streak() -> None:
    values = consecutive_up(make_bars([1, 2, 3, 3, 4, 5, 6]), days=2)

    assert values == (
        None,
        None,
        Decimal(2),
        Decimal(0),
        Decimal(1),
        Decimal(2),
        Decimal(3),
    )


def test_amplitude_uses_previous_close_and_percentage_points() -> None:
    bars = make_bars(
        [10, 11, 12],
        highs=[10, 12, 15],
        lows=[10, 9, 12],
    )

    values = amplitude_pct(bars)
    assert values[:2] == (None, Decimal(30))
    assert values[2] == pytest.approx(Decimal(300) / Decimal(11))


def test_market_amount_and_inclusive_average_use_turnover_not_price_times_volume() -> None:
    bars = make_bars([1, 1, 1], amounts=[100, 300, 500])

    assert market_amount(bars) == (Decimal(100), Decimal(300), Decimal(500))
    assert amount_average(bars, period=2) == (
        None,
        Decimal(200),
        Decimal(400),
    )


def test_new_indicator_parameter_relations_fail_closed() -> None:
    bars = make_bars([1, 2, 3, 4])

    with pytest.raises(ValueError, match="fast period"):
        moving_average_cross(bars, fast_period=3, slow_period=3)
    with pytest.raises(ValueError, match="stddev_multiplier"):
        bollinger_bands(bars, period=3, stddev_multiplier=Decimal(0))
    with pytest.raises(ValueError, match="CCI constant"):
        cci(bars, period=3, constant=Decimal(0))
    with pytest.raises(ValueError, match="strictly increasing"):
        bull_and_bear_index(
            bars,
            period_1=1,
            period_2=3,
            period_3=2,
            period_4=4,
        )


@pytest.mark.parametrize("period", [0, -1, True])
def test_period_must_be_positive_integer(period: int) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        moving_average(make_bars([1, 2]), period)
