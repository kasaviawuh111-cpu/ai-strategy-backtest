"""Pure, prefix-stable daily indicator implementations.

All arithmetic uses ``Decimal``.  Missing warmup values are represented by
``None`` so callers cannot accidentally coerce them into actionable zeroes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Literal

from ashare_lab.domain.market_data import DailyBar

PriceField = Literal["open", "high", "low", "close"]
NumericSeries = tuple[Decimal | None, ...]
TrendRegime = Literal["up", "down", "range"]


@dataclass(frozen=True, slots=True)
class MacdPoint:
    macd_line: Decimal
    signal_line: Decimal
    histogram: Decimal


@dataclass(frozen=True, slots=True)
class MovingAverageCrossPoint:
    fast: Decimal
    slow: Decimal


@dataclass(frozen=True, slots=True)
class BollingerPoint:
    middle: Decimal
    upper: Decimal
    lower: Decimal


@dataclass(frozen=True, slots=True)
class KdjPoint:
    k: Decimal
    d: Decimal
    j: Decimal


@dataclass(frozen=True, slots=True)
class VolumePricePoint:
    relative_volume: Decimal
    return_pct: Decimal


@dataclass(frozen=True, slots=True)
class PivotDivergencePoint:
    bearish: bool
    bullish: bool
    current_pivot_index: int | None = None
    previous_pivot_index: int | None = None
    price_change_pct: Decimal | None = None
    obv_change: Decimal | None = None
    obv_threshold: Decimal | None = None


@dataclass(frozen=True, slots=True)
class DirectionalMovementPoint:
    adx: Decimal
    plus_di: Decimal
    minus_di: Decimal


@dataclass(frozen=True, slots=True)
class StochasticPoint:
    k: Decimal
    d: Decimal


@dataclass(frozen=True, slots=True)
class DonchianPoint:
    upper: Decimal
    middle: Decimal
    lower: Decimal


@dataclass(frozen=True, slots=True)
class TrendRegimePoint:
    regime: TrendRegime
    short_average: Decimal
    long_average: Decimal
    adx: Decimal
    plus_di: Decimal
    minus_di: Decimal


def moving_average(
    bars: Sequence[DailyBar],
    period: int,
    price_field: PriceField = "close",
) -> NumericSeries:
    """Simple moving average, first available after exactly ``period`` bars."""

    _require_positive_period(period)
    values = tuple(getattr(bar, price_field).amount for bar in bars)
    return _simple_moving_average(values, period)


def volume_average(bars: Sequence[DailyBar], period: int) -> NumericSeries:
    """Legacy inclusive volume average over the current and prior bars.

    This function intentionally preserves the original ``market.volume@1.0.0``
    definition. New relative-volume signals must use
    :func:`previous_volume_average`, whose baseline excludes the observation
    being evaluated.
    """

    _require_positive_period(period)
    values = tuple(Decimal(bar.volume.value) for bar in bars)
    return _simple_moving_average(values, period)


def previous_volume_average(bars: Sequence[DailyBar], period: int) -> NumericSeries:
    """Mean of the previous N positive-volume sessions, excluding today.

    Zero-volume bars represent a suspension or otherwise non-tradable daily
    observation. They neither enter the baseline nor produce an observation.
    The returned series stays aligned to the original bars.
    """

    _require_positive_period(period)
    history: list[Decimal] = []
    result: list[Decimal | None] = [None] * len(bars)
    with localcontext() as context:
        context.prec = 34
        for index, bar in enumerate(bars):
            volume = Decimal(bar.volume.value)
            if volume <= 0:
                continue
            if len(history) >= period:
                result[index] = sum(history[-period:], Decimal(0)) / Decimal(period)
            history.append(volume)
    return tuple(result)


def relative_volume(bars: Sequence[DailyBar], period: int = 20) -> NumericSeries:
    """Current volume divided by the previous N valid-session mean."""

    baselines = previous_volume_average(bars, period)
    result: list[Decimal | None] = [None] * len(bars)
    with localcontext() as context:
        context.prec = 34
        for index, baseline in enumerate(baselines):
            if baseline is None or baseline <= 0 or bars[index].volume.value <= 0:
                continue
            result[index] = Decimal(bars[index].volume.value) / baseline
    return tuple(result)


def volume_price_points(
    bars: Sequence[DailyBar],
    baseline_period: int = 20,
) -> tuple[VolumePricePoint | None, ...]:
    """RVOL and close return versus the previous valid trading session."""

    relative = relative_volume(bars, baseline_period)
    result: list[VolumePricePoint | None] = [None] * len(bars)
    previous_valid_index: int | None = None
    with localcontext() as context:
        context.prec = 34
        for index, bar in enumerate(bars):
            if bar.volume.value <= 0:
                continue
            ratio = relative[index]
            if ratio is not None and previous_valid_index is not None:
                prior_close = bars[previous_valid_index].close.amount
                result[index] = VolumePricePoint(
                    relative_volume=ratio,
                    return_pct=(bar.close.amount / prior_close - Decimal(1)) * Decimal(100),
                )
            previous_valid_index = index
    return tuple(result)


def on_balance_volume(bars: Sequence[DailyBar]) -> NumericSeries:
    """Standard OBV aligned to bars and silent on zero-volume sessions.

    The first valid observation is seeded with its own volume, matching the
    common TA-Lib convention. A suspension leaves the accumulator unchanged but
    emits ``None`` so it cannot trigger a trading signal.
    """

    result: list[Decimal | None] = [None] * len(bars)
    accumulator: Decimal | None = None
    previous_close: Decimal | None = None
    for index, bar in enumerate(bars):
        if bar.volume.value <= 0:
            continue
        volume = Decimal(bar.volume.value)
        if accumulator is None or previous_close is None:
            accumulator = volume
        elif bar.close.amount > previous_close:
            accumulator += volume
        elif bar.close.amount < previous_close:
            accumulator -= volume
        result[index] = accumulator
        previous_close = bar.close.amount
    return tuple(result)


def confirmed_obv_divergence(
    bars: Sequence[DailyBar],
    *,
    left_bars: int = 3,
    right_bars: int = 3,
    min_separation: int = 5,
    max_separation: int = 60,
    price_threshold_pct: Decimal = Decimal(2),
    obv_threshold_adv: Decimal = Decimal(1),
    average_volume_period: int = 20,
) -> tuple[PivotDivergencePoint | None, ...]:
    """Confirmed price/OBV pivot divergence without back-filling the pivot.

    A pivot at valid-session position ``p`` is only examined at ``p +
    right_bars``. The true observation is therefore emitted on that later
    confirmation session. Only positive-volume sessions participate.
    """

    for period in (left_bars, right_bars, min_separation, max_separation):
        _require_positive_period(period)
    _require_positive_period(average_volume_period)
    if min_separation > max_separation:
        raise ValueError("minimum pivot separation cannot exceed maximum separation")
    for name, value in (
        ("price_threshold_pct", price_threshold_pct),
        ("obv_threshold_adv", obv_threshold_adv),
    ):
        if not value.is_finite() or value < 0:
            raise ValueError(f"{name} must be a finite non-negative Decimal")

    valid_indices = [index for index, bar in enumerate(bars) if bar.volume.value > 0]
    valid_bars = [bars[index] for index in valid_indices]
    valid_obv = [value for value in on_balance_volume(valid_bars) if value is not None]
    result: list[PivotDivergencePoint | None] = [None] * len(bars)
    high_pivots: list[int] = []
    low_pivots: list[int] = []
    first_ready_position = max(average_volume_period, left_bars + min_separation) + right_bars

    for current_position, current_index in enumerate(valid_indices):
        if current_position < left_bars + right_bars:
            continue
        pivot_position = current_position - right_bars
        pivot_kind = _confirmed_pivot_kind(
            valid_bars,
            pivot_position,
            left_bars=left_bars,
            right_bars=right_bars,
        )
        point = PivotDivergencePoint(bearish=False, bullish=False)
        if pivot_kind == "high":
            previous = _latest_separated_pivot(
                high_pivots,
                pivot_position,
                min_separation=min_separation,
                max_separation=max_separation,
            )
            if previous is not None:
                point = _divergence_point(
                    valid_bars,
                    valid_obv,
                    previous,
                    pivot_position,
                    average_volume_period=average_volume_period,
                    price_threshold_pct=price_threshold_pct,
                    obv_threshold_adv=obv_threshold_adv,
                    bearish=True,
                    valid_indices=valid_indices,
                )
            high_pivots.append(pivot_position)
        elif pivot_kind == "low":
            previous = _latest_separated_pivot(
                low_pivots,
                pivot_position,
                min_separation=min_separation,
                max_separation=max_separation,
            )
            if previous is not None:
                point = _divergence_point(
                    valid_bars,
                    valid_obv,
                    previous,
                    pivot_position,
                    average_volume_period=average_volume_period,
                    price_threshold_pct=price_threshold_pct,
                    obv_threshold_adv=obv_threshold_adv,
                    bearish=False,
                    valid_indices=valid_indices,
                )
            low_pivots.append(pivot_position)
        if current_position >= first_ready_position:
            result[current_index] = point
    return tuple(result)


def directional_movement(
    bars: Sequence[DailyBar],
    period: int = 14,
) -> tuple[DirectionalMovementPoint | None, ...]:
    """Wilder ADX, +DI and -DI on positive-volume trading sessions."""

    _require_positive_period(period)
    valid_indices = [index for index, bar in enumerate(bars) if bar.volume.value > 0]
    valid_bars = [bars[index] for index in valid_indices]
    aligned: list[DirectionalMovementPoint | None] = [None] * len(bars)
    if len(valid_bars) <= period:
        return tuple(aligned)

    true_ranges: list[Decimal] = []
    plus_dm: list[Decimal] = []
    minus_dm: list[Decimal] = []
    for position in range(1, len(valid_bars)):
        current = valid_bars[position]
        previous = valid_bars[position - 1]
        up_move = current.high.amount - previous.high.amount
        down_move = previous.low.amount - current.low.amount
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else Decimal(0))
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else Decimal(0))
        true_ranges.append(
            max(
                current.high.amount - current.low.amount,
                abs(current.high.amount - previous.close.amount),
                abs(current.low.amount - previous.close.amount),
            )
        )

    with localcontext() as context:
        context.prec = 34
        smoothed_tr = sum(true_ranges[:period], Decimal(0))
        smoothed_plus = sum(plus_dm[:period], Decimal(0))
        smoothed_minus = sum(minus_dm[:period], Decimal(0))
        dx_values: list[Decimal] = []
        di_values: dict[int, tuple[Decimal, Decimal]] = {}
        for position in range(period, len(valid_bars)):
            if position > period:
                source_index = position - 1
                smoothed_tr = (
                    smoothed_tr - smoothed_tr / Decimal(period) + true_ranges[source_index]
                )
                smoothed_plus = (
                    smoothed_plus - smoothed_plus / Decimal(period) + plus_dm[source_index]
                )
                smoothed_minus = (
                    smoothed_minus - smoothed_minus / Decimal(period) + minus_dm[source_index]
                )
            plus_di = Decimal(0) if smoothed_tr == 0 else Decimal(100) * smoothed_plus / smoothed_tr
            minus_di = (
                Decimal(0) if smoothed_tr == 0 else Decimal(100) * smoothed_minus / smoothed_tr
            )
            denominator = plus_di + minus_di
            dx = (
                Decimal(0)
                if denominator == 0
                else Decimal(100) * abs(plus_di - minus_di) / denominator
            )
            dx_values.append(dx)
            di_values[position] = (plus_di, minus_di)

        if len(dx_values) < period:
            return tuple(aligned)
        adx = sum(dx_values[:period], Decimal(0)) / Decimal(period)
        first_adx_position = period * 2 - 1
        for position in range(first_adx_position, len(valid_bars)):
            if position > first_adx_position:
                dx = dx_values[position - period]
                adx = (adx * Decimal(period - 1) + dx) / Decimal(period)
            plus_di, minus_di = di_values[position]
            aligned[valid_indices[position]] = DirectionalMovementPoint(
                adx=adx,
                plus_di=plus_di,
                minus_di=minus_di,
            )
    return tuple(aligned)


def true_range(bars: Sequence[DailyBar]) -> NumericSeries:
    """Daily true range using only the previous observed close.

    The first observation is intentionally unknown: without a prior close the
    gap components of true range cannot be reconstructed.  The runtime removes
    zero-volume observations before calling this function, so ``previous``
    always means the previous effective signal session.
    """

    result: list[Decimal | None] = [None] * len(bars)
    for index in range(1, len(bars)):
        current = bars[index]
        previous_close = bars[index - 1].close.amount
        result[index] = max(
            current.high.amount - current.low.amount,
            abs(current.high.amount - previous_close),
            abs(current.low.amount - previous_close),
        )
    return tuple(result)


def average_true_range(bars: Sequence[DailyBar], period: int = 14) -> NumericSeries:
    """Wilder ATR with ``alpha=1/period`` and an arithmetic seed.

    The seed at position ``period`` is the mean of true ranges 1..period.
    Subsequent observations use ``(prior*(period-1)+TR)/period``.  This is
    deliberately not pandas ``ewm(span=period)``.
    """

    _require_positive_period(period)
    ranges = true_range(bars)
    result: list[Decimal | None] = [None] * len(bars)
    if len(bars) <= period:
        return tuple(result)
    with localcontext() as context:
        context.prec = 34
        seed_values = tuple(value for value in ranges[1 : period + 1] if value is not None)
        if len(seed_values) != period:
            return tuple(result)
        previous = sum(seed_values, Decimal(0)) / Decimal(period)
        result[period] = previous
        for index in range(period + 1, len(bars)):
            current = ranges[index]
            if current is None:
                continue
            previous = (previous * Decimal(period - 1) + current) / Decimal(period)
            result[index] = previous
    return tuple(result)


def normalized_average_true_range(
    bars: Sequence[DailyBar],
    period: int = 14,
) -> NumericSeries:
    """NATR in percentage points: ``100 * WilderATR / close``."""

    averages = average_true_range(bars, period)
    result: list[Decimal | None] = [None] * len(bars)
    with localcontext() as context:
        context.prec = 34
        for index, average in enumerate(averages):
            close = bars[index].close.amount
            if average is not None and close > 0:
                result[index] = Decimal(100) * average / close
    return tuple(result)


def simple_bias(
    bars: Sequence[DailyBar],
    period: int = 20,
    price_field: PriceField = "close",
) -> NumericSeries:
    """SMA BIAS in percentage points: ``100 * (price / SMA - 1)``."""

    averages = moving_average(bars, period, price_field)
    result: list[Decimal | None] = [None] * len(bars)
    with localcontext() as context:
        context.prec = 34
        for index, average in enumerate(averages):
            if average is None or average == 0:
                continue
            price = getattr(bars[index], price_field).amount
            result[index] = Decimal(100) * (price / average - Decimal(1))
    return tuple(result)


def rate_of_change(
    bars: Sequence[DailyBar],
    period: int = 12,
    price_field: PriceField = "close",
) -> NumericSeries:
    """Price ROC in percentage points versus exactly ``period`` prior bars."""

    return period_return_pct(bars, period, price_field)


def momentum(
    bars: Sequence[DailyBar],
    period: int = 10,
    price_field: PriceField = "close",
) -> NumericSeries:
    """Absolute price momentum ``price_t - price_(t-period)``."""

    _require_positive_period(period)
    prices = tuple(getattr(bar, price_field).amount for bar in bars)
    result: list[Decimal | None] = [None] * len(bars)
    for index in range(period, len(bars)):
        result[index] = prices[index] - prices[index - period]
    return tuple(result)


def stochastic_oscillator(
    bars: Sequence[DailyBar],
    *,
    k_period: int = 14,
    d_period: int = 3,
) -> tuple[StochasticPoint | None, ...]:
    """Fast stochastic %K and its inclusive ``d_period`` SMA.

    ``%K=100*(close-LL)/(HH-LL)`` over ``k_period`` observations. A flat
    range is neutral 50. Both K and D must be available before a point is
    emitted, avoiding an implicit partial-warmup signal.
    """

    _require_positive_period(k_period)
    _require_positive_period(d_period)
    raw_k: list[Decimal | None] = [None] * len(bars)
    with localcontext() as context:
        context.prec = 34
        for index in range(k_period - 1, len(bars)):
            window = bars[index - k_period + 1 : index + 1]
            highest = max(bar.high.amount for bar in window)
            lowest = min(bar.low.amount for bar in window)
            spread = highest - lowest
            raw_k[index] = (
                Decimal(50)
                if spread == 0
                else Decimal(100) * (bars[index].close.amount - lowest) / spread
            )
        result: list[StochasticPoint | None] = [None] * len(bars)
        first_ready = k_period + d_period - 2
        for index in range(first_ready, len(bars)):
            window = raw_k[index - d_period + 1 : index + 1]
            if any(value is None for value in window):
                continue
            ready = tuple(value for value in window if value is not None)
            current_k = raw_k[index]
            assert current_k is not None
            result[index] = StochasticPoint(
                k=current_k,
                d=sum(ready, Decimal(0)) / Decimal(d_period),
            )
    return tuple(result)


def williams_r(bars: Sequence[DailyBar], period: int = 14) -> NumericSeries:
    """Williams %R on the conventional ``[-100, 0]`` scale.

    A flat high-low window is neutral ``-50``.
    """

    _require_positive_period(period)
    result: list[Decimal | None] = [None] * len(bars)
    with localcontext() as context:
        context.prec = 34
        for index in range(period - 1, len(bars)):
            window = bars[index - period + 1 : index + 1]
            highest = max(bar.high.amount for bar in window)
            lowest = min(bar.low.amount for bar in window)
            spread = highest - lowest
            result[index] = (
                Decimal(-50)
                if spread == 0
                else Decimal(-100) * (highest - bars[index].close.amount) / spread
            )
    return tuple(result)


def donchian_channel(
    bars: Sequence[DailyBar],
    period: int = 20,
) -> tuple[DonchianPoint | None, ...]:
    """Previous-N-session Donchian channel, excluding the current bar.

    Excluding the current high/low makes a strict close breakout observable;
    including it would make ``close > upper`` impossible for valid OHLC data.
    """

    _require_positive_period(period)
    result: list[DonchianPoint | None] = [None] * len(bars)
    with localcontext() as context:
        context.prec = 34
        for index in range(period, len(bars)):
            window = bars[index - period : index]
            upper = max(bar.high.amount for bar in window)
            lower = min(bar.low.amount for bar in window)
            result[index] = DonchianPoint(
                upper=upper,
                middle=(upper + lower) / Decimal(2),
                lower=lower,
            )
    return tuple(result)


def return_stddev(
    bars: Sequence[DailyBar],
    period: int = 20,
    price_field: PriceField = "close",
) -> NumericSeries:
    """Population standard deviation of simple daily returns, in percent."""

    _require_positive_period(period)
    prices = tuple(getattr(bar, price_field).amount for bar in bars)
    returns = tuple(
        Decimal(100) * (prices[index] / prices[index - 1] - Decimal(1))
        for index in range(1, len(prices))
    )
    result: list[Decimal | None] = [None] * len(bars)
    with localcontext() as context:
        context.prec = 34
        for index in range(period, len(bars)):
            window = returns[index - period : index]
            mean = sum(window, Decimal(0)) / Decimal(period)
            variance = sum((value - mean) ** 2 for value in window) / Decimal(period)
            result[index] = variance.sqrt()
    return tuple(result)


def historical_volatility(
    bars: Sequence[DailyBar],
    *,
    period: int = 20,
    annualization_sessions: int = 252,
    price_field: PriceField = "close",
) -> NumericSeries:
    """Annualized population standard deviation of log returns, in percent."""

    _require_positive_period(period)
    _require_positive_period(annualization_sessions)
    prices = tuple(getattr(bar, price_field).amount for bar in bars)
    log_returns = tuple((prices[index] / prices[index - 1]).ln() for index in range(1, len(prices)))
    result: list[Decimal | None] = [None] * len(bars)
    with localcontext() as context:
        context.prec = 34
        scale = Decimal(annualization_sessions).sqrt() * Decimal(100)
        for index in range(period, len(bars)):
            window = log_returns[index - period : index]
            mean = sum(window, Decimal(0)) / Decimal(period)
            variance = sum((value - mean) ** 2 for value in window) / Decimal(period)
            result[index] = variance.sqrt() * scale
    return tuple(result)


def stage_trend(
    bars: Sequence[DailyBar],
    *,
    short_period: int = 20,
    long_period: int = 60,
    slope_lookback: int = 5,
    adx_period: int = 14,
    adx_threshold: Decimal = Decimal(25),
    confirmation_days: int = 2,
    stability_bars: int = 120,
) -> tuple[TrendRegimePoint | None, ...]:
    """Three-state trend regime from SMA structure, slope and DMI/ADX.

    Up/down regimes require ``confirmation_days`` consecutive valid sessions;
    every other ready observation is ``range``. The default 120-session
    stability gate is deliberately conservative for daily backtests.
    """

    for period in (
        short_period,
        long_period,
        slope_lookback,
        adx_period,
        confirmation_days,
        stability_bars,
    ):
        _require_positive_period(period)
    if short_period >= long_period:
        raise ValueError("short trend period must be less than long period")
    if not adx_threshold.is_finite() or adx_threshold < 0:
        raise ValueError("ADX threshold must be a finite non-negative Decimal")

    valid_indices = [index for index, bar in enumerate(bars) if bar.volume.value > 0]
    valid_bars = [bars[index] for index in valid_indices]
    short_values = moving_average(valid_bars, short_period)
    long_values = moving_average(valid_bars, long_period)
    dmi_values = directional_movement(valid_bars, adx_period)
    aligned: list[TrendRegimePoint | None] = [None] * len(bars)
    raw_regimes: list[TrendRegime] = []

    for position, original_index in enumerate(valid_indices):
        short_average = short_values[position]
        long_average = long_values[position]
        dmi = dmi_values[position]
        if (
            position < stability_bars - 1
            or position < slope_lookback
            or short_average is None
            or long_average is None
            or dmi is None
            or short_values[position - slope_lookback] is None
        ):
            raw_regimes.append("range")
            continue
        prior_short = short_values[position - slope_lookback]
        assert prior_short is not None
        close = valid_bars[position].close.amount
        if (
            close > short_average > long_average
            and short_average > prior_short
            and dmi.adx >= adx_threshold
            and dmi.plus_di > dmi.minus_di
        ):
            raw: TrendRegime = "up"
        elif (
            close < short_average < long_average
            and short_average < prior_short
            and dmi.adx >= adx_threshold
            and dmi.minus_di > dmi.plus_di
        ):
            raw = "down"
        else:
            raw = "range"
        raw_regimes.append(raw)
        confirmed: TrendRegime = "range"
        if (
            raw in {"up", "down"}
            and len(raw_regimes) >= confirmation_days
            and all(item == raw for item in raw_regimes[-confirmation_days:])
        ):
            confirmed = raw
        aligned[original_index] = TrendRegimePoint(
            regime=confirmed,
            short_average=short_average,
            long_average=long_average,
            adx=dmi.adx,
            plus_di=dmi.plus_di,
            minus_di=dmi.minus_di,
        )
    return tuple(aligned)


def period_return_pct(
    bars: Sequence[DailyBar],
    period: int,
    price_field: PriceField = "close",
) -> NumericSeries:
    """N-period percentage return, where ``5`` means five percent.

    The observation at index ``t`` compares the current price with ``t-period``
    and therefore needs ``period + 1`` bars. Canonical prices are strictly
    positive, so the denominator cannot be zero.
    """

    _require_positive_period(period)
    prices = tuple(getattr(bar, price_field).amount for bar in bars)
    result: list[Decimal | None] = [None] * len(prices)
    with localcontext() as context:
        context.prec = 34
        for index in range(period, len(prices)):
            result[index] = (prices[index] / prices[index - period] - Decimal(1)) * Decimal(100)
    return tuple(result)


def rolling_high(
    bars: Sequence[DailyBar],
    period: int,
    price_field: PriceField = "close",
) -> NumericSeries:
    """Highest price over the previous N complete bars, excluding today.

    A current price is a new high only when it is strictly greater than the
    returned threshold. Equality is deliberately not a new high.
    """

    _require_positive_period(period)
    prices = tuple(getattr(bar, price_field).amount for bar in bars)
    result: list[Decimal | None] = [None] * len(prices)
    for index in range(period, len(prices)):
        result[index] = max(prices[index - period : index])
    return tuple(result)


def consecutive_up(bars: Sequence[DailyBar], days: int) -> NumericSeries:
    """Consecutive close-to-close rises, withheld until ``days`` transitions.

    Equal closes and declines both reset the streak to zero. A configured
    ``days`` condition needs ``days + 1`` closes before it is evaluable.
    """

    _require_positive_period(days)
    result: list[Decimal | None] = [None] * len(bars)
    streak = 0
    for index in range(1, len(bars)):
        if bars[index].close.amount > bars[index - 1].close.amount:
            streak += 1
        else:
            streak = 0
        if index >= days:
            result[index] = Decimal(streak)
    return tuple(result)


def amplitude_pct(bars: Sequence[DailyBar]) -> NumericSeries:
    """Daily amplitude ``100 * (high - low) / previous_close``.

    The first bar has no prior close and is therefore unavailable. Percentage
    values use percentage points: ``5`` means five percent.
    """

    result: list[Decimal | None] = [None] * len(bars)
    with localcontext() as context:
        context.prec = 34
        for index in range(1, len(bars)):
            result[index] = (
                Decimal(100)
                * (bars[index].high.amount - bars[index].low.amount)
                / bars[index - 1].close.amount
            )
    return tuple(result)


def market_amount(bars: Sequence[DailyBar]) -> NumericSeries:
    """Raw daily transaction amount from canonical ``DailyBar.turnover``."""

    return tuple(bar.turnover for bar in bars)


def amount_average(bars: Sequence[DailyBar], period: int) -> NumericSeries:
    """Inclusive N-day simple average of canonical daily transaction amount."""

    _require_positive_period(period)
    return _simple_moving_average(tuple(bar.turnover for bar in bars), period)


def exponential_moving_average(
    bars: Sequence[DailyBar],
    period: int,
    price_field: PriceField = "close",
) -> NumericSeries:
    """First-value-seeded EMA, withheld until ``period`` bars are present.

    The recursion matches ``ewm(adjust=False)`` and common domestic charts, but
    its unstable leading values are not exposed as actionable observations.
    """

    _require_positive_period(period)
    values = tuple(getattr(bar, price_field).amount for bar in bars)
    result: list[Decimal | None] = [None] * len(values)
    if len(values) < period or not values:
        return tuple(result)

    recursive = _ema_from_first(values, period)
    for index in range(period - 1, len(values)):
        result[index] = recursive[index]
    return tuple(result)


def moving_average_cross(
    bars: Sequence[DailyBar],
    *,
    fast_period: int,
    slow_period: int,
    price_field: PriceField = "close",
) -> tuple[MovingAverageCrossPoint | None, ...]:
    """Two inclusive simple moving averages aligned to the same daily bars."""

    _require_positive_period(fast_period)
    _require_positive_period(slow_period)
    if fast_period >= slow_period:
        raise ValueError("moving-average fast period must be less than slow period")
    fast_values = moving_average(bars, fast_period, price_field)
    slow_values = moving_average(bars, slow_period, price_field)
    return tuple(
        None
        if fast_value is None or slow_value is None
        else MovingAverageCrossPoint(fast=fast_value, slow=slow_value)
        for fast_value, slow_value in zip(fast_values, slow_values, strict=True)
    )


def bollinger_bands(
    bars: Sequence[DailyBar],
    *,
    period: int = 20,
    stddev_multiplier: Decimal = Decimal(2),
    price_field: PriceField = "close",
) -> tuple[BollingerPoint | None, ...]:
    """Bollinger bands using the population standard deviation (``ddof=0``)."""

    _require_positive_period(period)
    if not stddev_multiplier.is_finite() or stddev_multiplier <= 0:
        raise ValueError("Bollinger stddev_multiplier must be a positive Decimal")
    values = tuple(getattr(bar, price_field).amount for bar in bars)
    result: list[BollingerPoint | None] = [None] * len(values)
    if len(values) < period:
        return tuple(result)

    with localcontext() as context:
        context.prec = 34
        for index in range(period - 1, len(values)):
            window = values[index - period + 1 : index + 1]
            middle = sum(window, Decimal(0)) / Decimal(period)
            variance = sum((value - middle) ** 2 for value in window) / Decimal(period)
            width = variance.sqrt() * stddev_multiplier
            result[index] = BollingerPoint(
                middle=middle,
                upper=middle + width,
                lower=middle - width,
            )
    return tuple(result)


def kdj(
    bars: Sequence[DailyBar],
    *,
    period: int = 9,
    k_smoothing: int = 3,
    d_smoothing: int = 3,
) -> tuple[KdjPoint | None, ...]:
    """Chinese KDJ with RSV and recursive K/D lines seeded at neutral 50.

    A zero high-low range yields neutral RSV 50. This makes one-price windows
    deterministic without carrying a value from outside the declared window.
    """

    _require_positive_period(period)
    _require_positive_period(k_smoothing)
    _require_positive_period(d_smoothing)
    result: list[KdjPoint | None] = [None] * len(bars)
    if len(bars) < period:
        return tuple(result)

    with localcontext() as context:
        context.prec = 34
        previous_k = Decimal(50)
        previous_d = Decimal(50)
        for index in range(period - 1, len(bars)):
            window = bars[index - period + 1 : index + 1]
            lowest = min(bar.low.amount for bar in window)
            highest = max(bar.high.amount for bar in window)
            spread = highest - lowest
            rsv = (
                Decimal(50)
                if spread == 0
                else (bars[index].close.amount - lowest) / spread * Decimal(100)
            )
            current_k = (previous_k * Decimal(k_smoothing - 1) + rsv) / Decimal(k_smoothing)
            current_d = (previous_d * Decimal(d_smoothing - 1) + current_k) / Decimal(d_smoothing)
            result[index] = KdjPoint(
                k=current_k,
                d=current_d,
                j=Decimal(3) * current_k - Decimal(2) * current_d,
            )
            previous_k = current_k
            previous_d = current_d
    return tuple(result)


def cci(
    bars: Sequence[DailyBar],
    period: int = 14,
    constant: Decimal = Decimal("0.015"),
) -> NumericSeries:
    """Commodity Channel Index from typical price and population MAD.

    A flat typical-price window has zero mean absolute deviation and is defined
    as neutral CCI 0.
    """

    _require_positive_period(period)
    if not constant.is_finite() or constant <= 0:
        raise ValueError("CCI constant must be a positive Decimal")
    typical = tuple(
        (bar.high.amount + bar.low.amount + bar.close.amount) / Decimal(3) for bar in bars
    )
    result: list[Decimal | None] = [None] * len(typical)
    if len(typical) < period:
        return tuple(result)

    with localcontext() as context:
        context.prec = 34
        for index in range(period - 1, len(typical)):
            window = typical[index - period + 1 : index + 1]
            mean = sum(window, Decimal(0)) / Decimal(period)
            mean_deviation = sum(abs(value - mean) for value in window) / Decimal(period)
            result[index] = (
                Decimal(0)
                if mean_deviation == 0
                else (typical[index] - mean) / (constant * mean_deviation)
            )
    return tuple(result)


def bull_and_bear_index(
    bars: Sequence[DailyBar],
    *,
    period_1: int = 3,
    period_2: int = 6,
    period_3: int = 12,
    period_4: int = 24,
    price_field: PriceField = "close",
) -> NumericSeries:
    """BBI as the arithmetic mean of four strictly increasing simple MAs."""

    periods = (period_1, period_2, period_3, period_4)
    for period in periods:
        _require_positive_period(period)
    if not period_1 < period_2 < period_3 < period_4:
        raise ValueError("BBI periods must be strictly increasing")
    averages = tuple(moving_average(bars, period, price_field) for period in periods)
    result: list[Decimal | None] = [None] * len(bars)
    with localcontext() as context:
        context.prec = 34
        for index in range(len(bars)):
            points = tuple(series[index] for series in averages)
            if any(point is None for point in points):
                continue
            ready = tuple(point for point in points if point is not None)
            result[index] = sum(ready, Decimal(0)) / Decimal(4)
    return tuple(result)


def ema_bias(
    bars: Sequence[DailyBar],
    *,
    period: int = 28,
    price_field: PriceField = "close",
) -> NumericSeries:
    """Percentage deviation from EMA: ``(price / EMA - 1) * 100``."""

    averages = exponential_moving_average(bars, period, price_field)
    prices = tuple(getattr(bar, price_field).amount for bar in bars)
    result: list[Decimal | None] = [None] * len(bars)
    with localcontext() as context:
        context.prec = 34
        for index, average in enumerate(averages):
            if average is None:
                continue
            # Canonical prices are strictly positive; retain a fail-closed
            # guard so this pure function is safe if the domain ever widens.
            if average == 0:
                continue
            result[index] = (prices[index] / average - Decimal(1)) * Decimal(100)
    return tuple(result)


def macd(
    bars: Sequence[DailyBar],
    *,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[MacdPoint | None, ...]:
    """MACD using first-value-seeded EMAs and the A-share ``2 * DIF`` histogram.

    First-value seeding matches ``ewm(adjust=False)`` and common domestic chart
    implementations.  Values are deliberately withheld until
    ``slow + signal - 1`` bars have accumulated; a crossover needs one further
    point.  This separates mathematical recursion from the stability warmup.
    """

    _require_positive_period(fast)
    _require_positive_period(slow)
    _require_positive_period(signal)
    if fast >= slow:
        raise ValueError("MACD fast period must be less than slow period")

    closes = tuple(bar.close.amount for bar in bars)
    fast_ema = _ema_from_first(closes, fast)
    slow_ema = _ema_from_first(closes, slow)
    dif: list[Decimal] = []
    for fast_value, slow_value in zip(fast_ema, slow_ema, strict=True):
        dif.append(fast_value - slow_value)

    signal_values = _ema_from_first(dif, signal)
    first_ready_index = slow + signal - 2

    result: list[MacdPoint | None] = [None] * len(bars)
    for index in range(first_ready_index, len(bars)):
        dif_value = dif[index]
        signal_value = signal_values[index]
        result[index] = MacdPoint(
            macd_line=dif_value,
            signal_line=signal_value,
            histogram=Decimal(2) * (dif_value - signal_value),
        )
    return tuple(result)


def rsi(bars: Sequence[DailyBar], period: int = 14) -> NumericSeries:
    """Wilder RSI, first available after ``period + 1`` closing prices.

    A completely flat seed/window is defined as neutral RSI 50.  No-loss and
    no-gain windows are defined as 100 and 0 respectively.
    """

    _require_positive_period(period)
    closes = tuple(bar.close.amount for bar in bars)
    result: list[Decimal | None] = [None] * len(closes)
    if len(closes) <= period:
        return tuple(result)

    with localcontext() as context:
        context.prec = 34
        changes = tuple(closes[index] - closes[index - 1] for index in range(1, len(closes)))
        gains = tuple(max(change, Decimal(0)) for change in changes)
        losses = tuple(max(-change, Decimal(0)) for change in changes)
        average_gain = sum(gains[:period], Decimal(0)) / Decimal(period)
        average_loss = sum(losses[:period], Decimal(0)) / Decimal(period)
        result[period] = _rsi_value(average_gain, average_loss)

        for change_index in range(period, len(changes)):
            average_gain = (average_gain * Decimal(period - 1) + gains[change_index]) / Decimal(
                period
            )
            average_loss = (average_loss * Decimal(period - 1) + losses[change_index]) / Decimal(
                period
            )
            result[change_index + 1] = _rsi_value(average_gain, average_loss)

    return tuple(result)


def _confirmed_pivot_kind(
    bars: Sequence[DailyBar],
    pivot_position: int,
    *,
    left_bars: int,
    right_bars: int,
) -> Literal["high", "low"] | None:
    if pivot_position < left_bars or pivot_position + right_bars >= len(bars):
        return None
    window = bars[pivot_position - left_bars : pivot_position + right_bars + 1]
    pivot_offset = left_bars
    pivot_high = bars[pivot_position].high.amount
    pivot_low = bars[pivot_position].low.amount
    if all(
        pivot_high > bar.high.amount for offset, bar in enumerate(window) if offset != pivot_offset
    ):
        return "high"
    if all(
        pivot_low < bar.low.amount for offset, bar in enumerate(window) if offset != pivot_offset
    ):
        return "low"
    return None


def _latest_separated_pivot(
    pivots: Sequence[int],
    current: int,
    *,
    min_separation: int,
    max_separation: int,
) -> int | None:
    return next(
        (
            pivot
            for pivot in reversed(pivots)
            if min_separation <= current - pivot <= max_separation
        ),
        None,
    )


def _divergence_point(
    bars: Sequence[DailyBar],
    obv_values: Sequence[Decimal],
    previous: int,
    current: int,
    *,
    average_volume_period: int,
    price_threshold_pct: Decimal,
    obv_threshold_adv: Decimal,
    bearish: bool,
    valid_indices: Sequence[int],
) -> PivotDivergencePoint:
    prior_price = bars[previous].high.amount if bearish else bars[previous].low.amount
    current_price = bars[current].high.amount if bearish else bars[current].low.amount
    with localcontext() as context:
        context.prec = 34
        if bearish:
            price_change = (current_price / prior_price - Decimal(1)) * Decimal(100)
            obv_change = obv_values[previous] - obv_values[current]
        else:
            price_change = (prior_price / current_price - Decimal(1)) * Decimal(100)
            obv_change = obv_values[current] - obv_values[previous]
        baseline = sum(
            (Decimal(bar.volume.value) for bar in bars[current - average_volume_period : current]),
            Decimal(0),
        ) / Decimal(average_volume_period)
        threshold = baseline * obv_threshold_adv
        triggered = price_change >= price_threshold_pct and obv_change >= threshold
    return PivotDivergencePoint(
        bearish=bearish and triggered,
        bullish=not bearish and triggered,
        current_pivot_index=valid_indices[current],
        previous_pivot_index=valid_indices[previous],
        price_change_pct=price_change,
        obv_change=obv_change,
        obv_threshold=threshold,
    )


def _simple_moving_average(values: Sequence[Decimal], period: int) -> NumericSeries:
    result: list[Decimal | None] = [None] * len(values)
    if len(values) < period:
        return tuple(result)

    with localcontext() as context:
        context.prec = 34
        running_sum = sum(values[:period], Decimal(0))
        result[period - 1] = running_sum / Decimal(period)
        for index in range(period, len(values)):
            running_sum += values[index] - values[index - period]
            result[index] = running_sum / Decimal(period)
    return tuple(result)


def _ema_from_first(values: Sequence[Decimal], period: int) -> tuple[Decimal, ...]:
    if not values:
        return ()
    with localcontext() as context:
        context.prec = 34
        alpha = Decimal(2) / Decimal(period + 1)
        previous = values[0]
        result = [previous]
        for value in values[1:]:
            previous = previous + alpha * (value - previous)
            result.append(previous)
    return tuple(result)


def _rsi_value(average_gain: Decimal, average_loss: Decimal) -> Decimal:
    if average_gain == 0 and average_loss == 0:
        return Decimal(50)
    if average_loss == 0:
        return Decimal(100)
    if average_gain == 0:
        return Decimal(0)
    relative_strength = average_gain / average_loss
    return Decimal(100) - Decimal(100) / (Decimal(1) + relative_strength)


def _require_positive_period(period: int) -> None:
    if isinstance(period, bool) or period <= 0:
        raise ValueError("indicator period must be a positive integer")
