"""Pure evaluation of Strategy DSL v1 indicator condition trees."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, datetime, time
from decimal import Decimal
from typing import cast
from zoneinfo import ZoneInfo

from ashare_lab.domain.catalog import CatalogSnapshot
from ashare_lab.domain.events.runtime import EventRuntimeError, evaluate_event_condition_aligned
from ashare_lab.domain.financials import FinancialFactRecord
from ashare_lab.domain.market_data import DailyBar, EventEnvelope
from ashare_lab.domain.strategy import (
    AllCondition,
    AnyCondition,
    EventCondition,
    FinancialConditionV1,
    IndicatorCondition,
)
from ashare_lab.domain.strategy.models import Condition

from .comparators import Comparator, evaluate_comparator
from .indicators import (
    BollingerPoint,
    DonchianPoint,
    KdjPoint,
    NumericSeries,
    PriceField,
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
from .models import SignalEvidence, SignalFact

SHANGHAI = ZoneInfo("Asia/Shanghai")
STABLE_INDICATOR_EVALUATOR_IDS = frozenset(
    {
        "amount.average",
        "market.amount",
        "market.volume",
        "price.amplitude",
        "price.consecutive_up",
        "price.return_pct",
        "price.rolling_high",
        "price.true_range",
        "technical.adx",
        "technical.atr",
        "technical.bbi",
        "technical.bias",
        "technical.bollinger",
        "technical.cci",
        "technical.dmi",
        "technical.donchian",
        "technical.ema",
        "technical.ema_bias",
        "technical.historical_volatility",
        "technical.kdj",
        "technical.ma",
        "technical.ma_cross",
        "technical.macd",
        "technical.momentum",
        "technical.natr",
        "technical.obv",
        "technical.return_stddev",
        "technical.roc",
        "technical.rsi",
        "technical.stochastic",
        "technical.trend_regime",
        "technical.williams_r",
        "volume.price_confirmation",
        "volume.price_divergence",
        "volume.relative",
    }
)


class SignalRuntimeError(ValueError):
    """Raised when a parsed condition cannot be evaluated unambiguously."""


def validate_stable_indicator_evaluator_catalog(catalog: CatalogSnapshot) -> None:
    """Fail startup when the stable Catalog and daily evaluator drift apart."""

    stable_ids = frozenset(
        definition.id for definition in catalog.indicators if definition.status == "stable"
    )
    if stable_ids == STABLE_INDICATOR_EVALUATOR_IDS:
        return
    missing = sorted(stable_ids - STABLE_INDICATOR_EVALUATOR_IDS)
    undeclared = sorted(STABLE_INDICATOR_EVALUATOR_IDS - stable_ids)
    raise SignalRuntimeError(
        "stable indicator Catalog/evaluator mismatch: "
        f"missing_evaluators={missing}, undeclared_evaluators={undeclared}"
    )


class SignalRuntime:
    """Evaluate immutable daily bars without mutating state or seeing a suffix."""

    def evaluate(
        self,
        condition: Condition,
        bars: Sequence[DailyBar],
        events: Sequence[EventEnvelope] = (),
        financial_facts: Sequence[FinancialFactRecord] = (),
    ) -> tuple[SignalFact, ...]:
        """Return facts in session order, omitting bars still in warmup."""

        return tuple(
            fact
            for fact in self.evaluate_aligned(condition, bars, events, financial_facts)
            if fact is not None
        )

    def evaluate_aligned(
        self,
        condition: Condition,
        bars: Sequence[DailyBar],
        events: Sequence[EventEnvelope] = (),
        financial_facts: Sequence[FinancialFactRecord] = (),
    ) -> tuple[SignalFact | None, ...]:
        """Return one slot per input bar; ``None`` means insufficient history."""

        canonical_bars = tuple(bars)
        _validate_bars(canonical_bars)
        return self._evaluate_node(
            condition,
            canonical_bars,
            tuple(events),
            tuple(financial_facts),
        )

    def evaluate_latest(
        self,
        condition: Condition,
        bars: Sequence[DailyBar],
        events: Sequence[EventEnvelope] = (),
        financial_facts: Sequence[FinancialFactRecord] = (),
    ) -> SignalFact | None:
        aligned = self.evaluate_aligned(condition, bars, events, financial_facts)
        return aligned[-1] if aligned else None

    def _evaluate_node(
        self,
        condition: Condition,
        bars: tuple[DailyBar, ...],
        events: tuple[EventEnvelope, ...],
        financial_facts: tuple[FinancialFactRecord, ...],
    ) -> tuple[SignalFact | None, ...]:
        if isinstance(condition, IndicatorCondition):
            return _evaluate_indicator(condition, bars)
        if isinstance(condition, FinancialConditionV1):
            return _evaluate_financial(condition, bars, financial_facts)
        if isinstance(condition, EventCondition):
            try:
                points = evaluate_event_condition_aligned(condition, bars, events)
            except EventRuntimeError as exc:
                raise SignalRuntimeError(str(exc)) from exc
            return tuple(
                None
                if point is None
                else SignalFact(
                    instrument_id=point.instrument_id,
                    session_date=point.session_date,
                    condition_ref=(
                        f"{condition.event_code}@{condition.definition_version}:{condition.trigger}"
                    ),
                    triggered=point.triggered,
                    observed_at=point.observed_at,
                    available_at=point.available_at,
                    reason=point.reason,
                    evidence=point.evidence,
                )
                for point in points
            )
        if isinstance(condition, (AllCondition, AnyCondition)):
            child_timelines = tuple(
                self._evaluate_node(child, bars, events, financial_facts)
                for child in condition.children
            )
            return _combine(condition.type, child_timelines, bars)
        child_timeline = self._evaluate_node(condition.child, bars, events, financial_facts)
        return _combine("not", (child_timeline,), bars)


def _evaluate_financial(
    condition: FinancialConditionV1,
    bars: tuple[DailyBar, ...],
    financial_facts: tuple[FinancialFactRecord, ...],
) -> tuple[SignalFact | None, ...]:
    """Evaluate only the latest direct provider fact knowable at each close."""

    matching = tuple(
        fact
        for fact in financial_facts
        if fact.metric_id is condition.metric_id
        and (condition.period_basis is None or fact.period_basis is condition.period_basis)
        and (condition.report_type is None or fact.report_type is condition.report_type)
        and fact.statement_scope is condition.statement_scope
        and fact.unit is condition.unit
    )
    timeline: list[SignalFact | None] = []
    for bar in bars:
        as_of = max(_observed_at(bar), bar.available_at)
        available = tuple(
            fact
            for fact in matching
            if fact.instrument_id == str(bar.instrument_id)
            and fact.availability.is_available_at(as_of)
        )
        if not available:
            timeline.append(None)
            continue
        latest = max(
            available,
            key=_financial_fact_sort_key,
        )
        if latest.value is None:
            timeline.append(None)
            continue
        triggered = _evaluate_financial_comparator(
            condition.comparator,
            latest.value,
            condition.value,
        )
        result_word = "true" if triggered else "false"
        first_available_at = latest.availability.first_available_at
        assert first_available_at is not None
        timeline.append(
            SignalFact(
                instrument_id=bar.instrument_id,
                session_date=bar.session_date,
                condition_ref=(
                    f"{condition.metric_id.value}@{condition.definition_version}:"
                    f"{condition.comparator}"
                ),
                triggered=triggered,
                observed_at=_observed_at(bar),
                available_at=max(_observed_at(bar), bar.available_at, first_available_at),
                reason=(
                    f"{condition.metric_id.value}={_format_decimal(latest.value)} "
                    f"{condition.comparator} threshold={_format_decimal(condition.value)} "
                    f"=> {result_word}"
                ),
                left_value=latest.value,
                right_value=condition.value,
                evidence=(
                    SignalEvidence(
                        evidence_type="financial_fact",
                        evidence_id=(
                            f"{latest.snapshot_id}:{latest.source_dataset}:"
                            f"{latest.source_field}:{latest.revision_id}"
                        ),
                        available_at=first_available_at,
                        provider=latest.provider,
                        raw_response_sha256=latest.raw_response_sha256,
                    ),
                ),
            )
        )
    return tuple(timeline)


def _financial_fact_sort_key(
    fact: FinancialFactRecord,
) -> tuple[date, datetime, str]:
    observed_at = fact.availability.observed_at
    first_available_at = fact.availability.first_available_at
    assert observed_at is not None
    assert first_available_at is not None
    return (fact.report_period or observed_at.date(), first_available_at, fact.revision_id)


def _evaluate_financial_comparator(
    comparator: str,
    left: Decimal,
    right: Decimal,
) -> bool:
    if comparator == "gt":
        return left > right
    if comparator == "gte":
        return left >= right
    if comparator == "lt":
        return left < right
    if comparator == "lte":
        return left <= right
    if comparator == "eq":
        return left == right
    if comparator == "ne":
        return left != right
    raise SignalRuntimeError(f"unsupported financial comparator: {comparator!r}")


def _evaluate_indicator(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
) -> tuple[SignalFact | None, ...]:
    """Evaluate indicators on tradable observations and preserve input alignment.

    A zero-volume daily row represents a suspension or otherwise non-tradable
    observation in the canonical A-share feed.  It must neither advance an
    indicator window nor emit a signal.  Centralising the filter here keeps all
    stable indicator families on the same session-counting policy.
    """

    valid_pairs = tuple(
        (original_index, bar) for original_index, bar in enumerate(bars) if bar.volume.value > 0
    )
    valid_bars = tuple(bar for _, bar in valid_pairs)
    valid_timeline = _evaluate_indicator_on_valid_bars(condition, valid_bars)
    if len(valid_pairs) == len(bars):
        return valid_timeline
    aligned: list[SignalFact | None] = [None] * len(bars)
    for valid_index, (original_index, _) in enumerate(valid_pairs):
        aligned[original_index] = valid_timeline[valid_index]
    return tuple(aligned)


def _evaluate_indicator_on_valid_bars(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
) -> tuple[SignalFact | None, ...]:
    if condition.definition_version != "1.0.0":
        raise SignalRuntimeError(
            f"unsupported {condition.indicator_id} definition version "
            f"{condition.definition_version!r}"
        )
    if condition.indicator_id == "technical.ma":
        return _evaluate_ma(condition, bars)
    if condition.indicator_id == "technical.ema":
        return _evaluate_ema(condition, bars)
    if condition.indicator_id == "technical.ma_cross":
        return _evaluate_ma_cross(condition, bars)
    if condition.indicator_id == "technical.bollinger":
        return _evaluate_bollinger(condition, bars)
    if condition.indicator_id == "technical.kdj":
        return _evaluate_kdj(condition, bars)
    if condition.indicator_id == "technical.cci":
        return _evaluate_cci(condition, bars)
    if condition.indicator_id == "technical.bbi":
        return _evaluate_bbi(condition, bars)
    if condition.indicator_id == "technical.ema_bias":
        return _evaluate_ema_bias(condition, bars)
    if condition.indicator_id == "price.return_pct":
        return _evaluate_return_pct(condition, bars)
    if condition.indicator_id == "price.rolling_high":
        return _evaluate_rolling_high(condition, bars)
    if condition.indicator_id == "price.consecutive_up":
        return _evaluate_consecutive_up(condition, bars)
    if condition.indicator_id == "price.amplitude":
        return _evaluate_amplitude(condition, bars)
    if condition.indicator_id == "market.amount":
        return _evaluate_amount(condition, bars)
    if condition.indicator_id == "amount.average":
        return _evaluate_amount_average(condition, bars)
    if condition.indicator_id == "technical.macd":
        return _evaluate_macd(condition, bars)
    if condition.indicator_id == "technical.rsi":
        return _evaluate_rsi(condition, bars)
    if condition.indicator_id == "market.volume":
        return _evaluate_volume(condition, bars)
    if condition.indicator_id == "volume.relative":
        return _evaluate_relative_volume(condition, bars)
    if condition.indicator_id == "volume.price_confirmation":
        return _evaluate_volume_price_confirmation(condition, bars)
    if condition.indicator_id == "technical.obv":
        return _evaluate_obv(condition, bars)
    if condition.indicator_id == "volume.price_divergence":
        return _evaluate_obv_divergence(condition, bars)
    if condition.indicator_id == "technical.trend_regime":
        return _evaluate_trend_regime(condition, bars)
    if condition.indicator_id == "price.true_range":
        return _evaluate_true_range(condition, bars)
    if condition.indicator_id == "technical.atr":
        return _evaluate_atr(condition, bars)
    if condition.indicator_id == "technical.natr":
        return _evaluate_natr(condition, bars)
    if condition.indicator_id == "technical.adx":
        return _evaluate_adx(condition, bars)
    if condition.indicator_id == "technical.dmi":
        return _evaluate_dmi(condition, bars)
    if condition.indicator_id == "technical.bias":
        return _evaluate_bias(condition, bars)
    if condition.indicator_id == "technical.roc":
        return _evaluate_roc(condition, bars)
    if condition.indicator_id == "technical.momentum":
        return _evaluate_momentum(condition, bars)
    if condition.indicator_id == "technical.stochastic":
        return _evaluate_stochastic(condition, bars)
    if condition.indicator_id == "technical.williams_r":
        return _evaluate_williams_r(condition, bars)
    if condition.indicator_id == "technical.donchian":
        return _evaluate_donchian(condition, bars)
    if condition.indicator_id == "technical.return_stddev":
        return _evaluate_return_stddev(condition, bars)
    if condition.indicator_id == "technical.historical_volatility":
        return _evaluate_historical_volatility(condition, bars)
    raise SignalRuntimeError(f"unsupported indicator: {condition.indicator_id!r}")


def _evaluate_ma(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
) -> tuple[SignalFact | None, ...]:
    period = _int_param(condition, "period")
    price_field_raw = _str_param(condition, "price_field")
    if price_field_raw not in {"open", "high", "low", "close"}:
        raise SignalRuntimeError(f"unsupported MA price_field: {price_field_raw!r}")
    price_field = cast(PriceField, price_field_raw)
    averages = moving_average(bars, period, price_field)
    prices = tuple(getattr(bar, price_field).amount for bar in bars)
    trigger_map = {
        "price_crosses_above": Comparator.CROSSES_ABOVE,
        "price_crosses_below": Comparator.CROSSES_BELOW,
        "price_above": Comparator.GT,
        "price_below": Comparator.LT,
    }
    comparator = _resolve_trigger(condition, trigger_map, value_required=False)
    aligned: list[SignalFact | None] = [None] * len(bars)
    for index, average in enumerate(averages):
        if average is None:
            continue
        previous_price = prices[index - 1] if index > 0 else None
        previous_average = averages[index - 1] if index > 0 else None
        if comparator in {Comparator.CROSSES_ABOVE, Comparator.CROSSES_BELOW} and (
            previous_price is None or previous_average is None
        ):
            continue
        dependency_start = (
            max(0, index - period)
            if comparator.value.startswith("crosses_")
            else index - period + 1
        )
        aligned[index] = _leaf_fact(
            condition,
            bars,
            index,
            comparator,
            prices[index],
            average,
            previous_left=previous_price,
            previous_right=previous_average,
            dependency_start=dependency_start,
            value_labels=(price_field, f"ma({period})"),
        )
    return tuple(aligned)


def _evaluate_ema(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
) -> tuple[SignalFact | None, ...]:
    period = _int_param(condition, "period")
    price_field = _price_field_param(condition)
    values = exponential_moving_average(bars, period, price_field)
    return _evaluate_price_series(
        condition,
        bars,
        values,
        price_field=price_field,
        label=f"ema({period})",
        dependency_start=lambda _index, _is_cross: 0,
    )


def _evaluate_ma_cross(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
) -> tuple[SignalFact | None, ...]:
    fast_period = _int_param(condition, "fast_period")
    slow_period = _int_param(condition, "slow_period")
    price_field = _price_field_param(condition)
    points = moving_average_cross(
        bars,
        fast_period=fast_period,
        slow_period=slow_period,
        price_field=price_field,
    )
    comparator = _resolve_trigger(
        condition,
        {
            "golden_cross": Comparator.CROSSES_ABOVE,
            "death_cross": Comparator.CROSSES_BELOW,
            "fast_above_slow": Comparator.GT,
            "fast_below_slow": Comparator.LT,
        },
        value_required=False,
    )
    is_cross = comparator in {Comparator.CROSSES_ABOVE, Comparator.CROSSES_BELOW}
    aligned: list[SignalFact | None] = [None] * len(bars)
    for index, point in enumerate(points):
        if point is None:
            continue
        previous = points[index - 1] if index > 0 else None
        if is_cross and previous is None:
            continue
        aligned[index] = _leaf_fact(
            condition,
            bars,
            index,
            comparator,
            point.fast,
            point.slow,
            previous_left=previous.fast if previous is not None else None,
            previous_right=previous.slow if previous is not None else None,
            dependency_start=max(0, index - slow_period if is_cross else index - slow_period + 1),
            value_labels=(f"sma({fast_period})", f"sma({slow_period})"),
        )
    return tuple(aligned)


def _evaluate_bollinger(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
) -> tuple[SignalFact | None, ...]:
    period = _int_param(condition, "period")
    multiplier = _decimal_param(condition, "stddev_multiplier")
    price_field = _price_field_param(condition)
    points = bollinger_bands(
        bars,
        period=period,
        stddev_multiplier=multiplier,
        price_field=price_field,
    )
    trigger_map: dict[str, tuple[Comparator, str]] = {
        "price_crosses_above_upper": (Comparator.CROSSES_ABOVE, "upper"),
        "price_crosses_below_upper": (Comparator.CROSSES_BELOW, "upper"),
        "price_crosses_above_middle": (Comparator.CROSSES_ABOVE, "middle"),
        "price_crosses_below_middle": (Comparator.CROSSES_BELOW, "middle"),
        "price_crosses_above_lower": (Comparator.CROSSES_ABOVE, "lower"),
        "price_crosses_below_lower": (Comparator.CROSSES_BELOW, "lower"),
    }
    try:
        comparator, band_name = trigger_map[condition.trigger]
    except KeyError as error:
        raise SignalRuntimeError(
            f"unsupported trigger {condition.trigger!r} for {condition.indicator_id}"
        ) from error
    if condition.value is not None:
        raise SignalRuntimeError(f"trigger {condition.trigger!r} forbids a comparison value")
    prices = tuple(getattr(bar, price_field).amount for bar in bars)
    aligned: list[SignalFact | None] = [None] * len(bars)
    for index, point in enumerate(points):
        if point is None or index == 0 or points[index - 1] is None:
            continue
        previous = points[index - 1]
        assert isinstance(point, BollingerPoint)
        assert isinstance(previous, BollingerPoint)
        aligned[index] = _leaf_fact(
            condition,
            bars,
            index,
            comparator,
            prices[index],
            getattr(point, band_name),
            previous_left=prices[index - 1],
            previous_right=getattr(previous, band_name),
            dependency_start=max(0, index - period),
            value_labels=(price_field, f"bollinger_{band_name}({period})"),
        )
    return tuple(aligned)


def _evaluate_kdj(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
) -> tuple[SignalFact | None, ...]:
    period = _int_param(condition, "period")
    k_smoothing = _int_param(condition, "k_smoothing")
    d_smoothing = _int_param(condition, "d_smoothing")
    points = kdj(
        bars,
        period=period,
        k_smoothing=k_smoothing,
        d_smoothing=d_smoothing,
    )
    if condition.trigger in {"golden_cross", "death_cross"}:
        comparator = _resolve_trigger(
            condition,
            {
                "golden_cross": Comparator.CROSSES_ABOVE,
                "death_cross": Comparator.CROSSES_BELOW,
            },
            value_required=False,
        )
        return _evaluate_kdj_cross(condition, bars, points, comparator)

    comparator = _resolve_trigger(
        condition,
        {"j_above": Comparator.GT, "j_below": Comparator.LT},
        value_required=True,
    )
    threshold = _required_value(condition)
    aligned: list[SignalFact | None] = [None] * len(bars)
    for index, point in enumerate(points):
        if point is None:
            continue
        aligned[index] = _leaf_fact(
            condition,
            bars,
            index,
            comparator,
            point.j,
            threshold,
            dependency_start=0,
            value_labels=(f"kdj_j({period},{k_smoothing},{d_smoothing})", "threshold"),
        )
    return tuple(aligned)


def _evaluate_kdj_cross(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
    points: tuple[KdjPoint | None, ...],
    comparator: Comparator,
) -> tuple[SignalFact | None, ...]:
    aligned: list[SignalFact | None] = [None] * len(bars)
    for index, point in enumerate(points):
        if point is None or index == 0 or points[index - 1] is None:
            continue
        previous = points[index - 1]
        assert previous is not None
        aligned[index] = _leaf_fact(
            condition,
            bars,
            index,
            comparator,
            point.k,
            point.d,
            previous_left=previous.k,
            previous_right=previous.d,
            dependency_start=0,
            value_labels=("kdj_k", "kdj_d"),
        )
    return tuple(aligned)


def _evaluate_cci(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
) -> tuple[SignalFact | None, ...]:
    period = _int_param(condition, "period", default=14)
    constant = _decimal_param(condition, "constant")
    return _evaluate_threshold_series(
        condition,
        bars,
        cci(bars, period, constant),
        label=f"cci({period})",
        dependency_start=lambda index, is_cross: max(
            0, index - period if is_cross else index - period + 1
        ),
    )


def _evaluate_bbi(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
) -> tuple[SignalFact | None, ...]:
    periods = tuple(_int_param(condition, f"period_{index}") for index in range(1, 5))
    price_field = _price_field_param(condition)
    values = bull_and_bear_index(
        bars,
        period_1=periods[0],
        period_2=periods[1],
        period_3=periods[2],
        period_4=periods[3],
        price_field=price_field,
    )
    longest = periods[-1]
    return _evaluate_price_series(
        condition,
        bars,
        values,
        price_field=price_field,
        label=f"bbi({','.join(str(period) for period in periods)})",
        dependency_start=lambda index, is_cross: max(
            0, index - longest if is_cross else index - longest + 1
        ),
    )


def _evaluate_ema_bias(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
) -> tuple[SignalFact | None, ...]:
    period = _int_param(condition, "period")
    price_field = _price_field_param(condition)
    return _evaluate_threshold_series(
        condition,
        bars,
        ema_bias(bars, period=period, price_field=price_field),
        label=f"ema_bias({period})",
        dependency_start=lambda _index, _is_cross: 0,
    )


def _evaluate_return_pct(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
) -> tuple[SignalFact | None, ...]:
    period = _int_param(condition, "period")
    price_field = _price_field_param(condition)
    return _evaluate_threshold_series(
        condition,
        bars,
        period_return_pct(bars, period, price_field),
        label=f"return_pct({period},{price_field})",
        dependency_start=lambda index, is_cross: max(
            0, index - period - 1 if is_cross else index - period
        ),
    )


def _evaluate_rolling_high(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
) -> tuple[SignalFact | None, ...]:
    period = _int_param(condition, "period")
    price_field = _price_field_param(condition)
    thresholds = rolling_high(bars, period, price_field)
    comparator = _resolve_trigger(
        condition,
        {"new_high": Comparator.GT},
        value_required=False,
    )
    prices = tuple(getattr(bar, price_field).amount for bar in bars)
    aligned: list[SignalFact | None] = [None] * len(bars)
    for index, threshold in enumerate(thresholds):
        if threshold is None:
            continue
        aligned[index] = _leaf_fact(
            condition,
            bars,
            index,
            comparator,
            prices[index],
            threshold,
            dependency_start=index - period,
            value_labels=(price_field, f"prior_high({period})"),
        )
    return tuple(aligned)


def _evaluate_consecutive_up(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
) -> tuple[SignalFact | None, ...]:
    days = _int_param(condition, "days")
    values = consecutive_up(bars, days)
    comparator = _resolve_trigger(
        condition,
        {"at_least": Comparator.GTE},
        value_required=False,
    )
    threshold = Decimal(days)
    aligned: list[SignalFact | None] = [None] * len(bars)
    for index, value in enumerate(values):
        if value is None:
            continue
        aligned[index] = _leaf_fact(
            condition,
            bars,
            index,
            comparator,
            value,
            threshold,
            dependency_start=index - int(value) if value > 0 else index - 1,
            value_labels=("consecutive_up_days", "required_days"),
        )
    return tuple(aligned)


def _evaluate_amplitude(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
) -> tuple[SignalFact | None, ...]:
    return _evaluate_threshold_series(
        condition,
        bars,
        amplitude_pct(bars),
        label="amplitude_pct",
        dependency_start=lambda index, is_cross: max(0, index - 2 if is_cross else index - 1),
    )


def _evaluate_amount(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
) -> tuple[SignalFact | None, ...]:
    return _evaluate_threshold_series(
        condition,
        bars,
        market_amount(bars),
        label="amount",
        dependency_start=lambda index, is_cross: max(0, index - 1 if is_cross else index),
    )


def _evaluate_amount_average(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
) -> tuple[SignalFact | None, ...]:
    period = _int_param(condition, "period")
    return _evaluate_threshold_series(
        condition,
        bars,
        amount_average(bars, period),
        label=f"amount_average({period})",
        dependency_start=lambda index, is_cross: max(
            0, index - period if is_cross else index - period + 1
        ),
    )


def _evaluate_macd(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
) -> tuple[SignalFact | None, ...]:
    fast = _int_param(condition, "fast")
    slow = _int_param(condition, "slow")
    signal = _int_param(condition, "signal")
    points = macd(bars, fast=fast, slow=slow, signal=signal)
    trigger_map = {
        "golden_cross": Comparator.CROSSES_ABOVE,
        "death_cross": Comparator.CROSSES_BELOW,
        "crosses_above_zero": Comparator.CROSSES_ABOVE,
        "crosses_below_zero": Comparator.CROSSES_BELOW,
    }
    comparator = _resolve_trigger(condition, trigger_map, value_required=False)
    compare_to_zero = condition.trigger.endswith("_zero")
    aligned: list[SignalFact | None] = [None] * len(bars)
    for index, point in enumerate(points):
        if point is None or index == 0 or points[index - 1] is None:
            continue
        previous = points[index - 1]
        assert previous is not None
        right = Decimal(0) if compare_to_zero else point.signal_line
        previous_right = Decimal(0) if compare_to_zero else previous.signal_line
        aligned[index] = _leaf_fact(
            condition,
            bars,
            index,
            comparator,
            point.macd_line,
            right,
            previous_left=previous.macd_line,
            previous_right=previous_right,
            dependency_start=0,
            value_labels=("dif", "zero" if compare_to_zero else "dea"),
        )
    return tuple(aligned)


def _evaluate_rsi(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
) -> tuple[SignalFact | None, ...]:
    period = _int_param(condition, "period")
    values = rsi(bars, period)
    trigger_map = {
        "crosses_above": Comparator.CROSSES_ABOVE,
        "crosses_below": Comparator.CROSSES_BELOW,
        "above": Comparator.GT,
        "below": Comparator.LT,
    }
    comparator = _resolve_trigger(condition, trigger_map, value_required=True)
    threshold = _required_value(condition)
    aligned: list[SignalFact | None] = [None] * len(bars)
    for index, value in enumerate(values):
        if value is None:
            continue
        previous_value = values[index - 1] if index > 0 else None
        is_cross = comparator in {Comparator.CROSSES_ABOVE, Comparator.CROSSES_BELOW}
        if is_cross and previous_value is None:
            continue
        aligned[index] = _leaf_fact(
            condition,
            bars,
            index,
            comparator,
            value,
            threshold,
            previous_left=previous_value,
            previous_right=threshold if previous_value is not None else None,
            dependency_start=0,
            value_labels=(f"rsi({period})", "threshold"),
        )
    return tuple(aligned)


def _evaluate_volume(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
) -> tuple[SignalFact | None, ...]:
    period = _int_param(condition, "baseline_period")
    averages = volume_average(bars, period)
    trigger_map = {
        "gte_multiple": Comparator.GTE,
        "lte_multiple": Comparator.LTE,
    }
    comparator = _resolve_trigger(condition, trigger_map, value_required=True)
    multiple = _required_value(condition)
    if multiple <= 0:
        raise SignalRuntimeError("volume multiple must be greater than zero")
    aligned: list[SignalFact | None] = [None] * len(bars)
    for index, average in enumerate(averages):
        if average is None:
            continue
        aligned[index] = _leaf_fact(
            condition,
            bars,
            index,
            comparator,
            Decimal(bars[index].volume.value),
            average * multiple,
            dependency_start=index - period + 1,
            value_labels=("volume", f"volume_ma({period})*{_format_decimal(multiple)}"),
        )
    return tuple(aligned)


def _evaluate_relative_volume(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
) -> tuple[SignalFact | None, ...]:
    period = _int_param(condition, "baseline_period")
    values = relative_volume(bars, period)
    multiple = _required_value(condition)
    if multiple <= 0:
        raise SignalRuntimeError("relative-volume multiple must be greater than zero")
    if condition.trigger == "consecutive_gte_multiple":
        days = _int_param(condition, "consecutive_days")
        aligned: list[SignalFact | None] = [None] * len(bars)
        ready: list[tuple[int, Decimal]] = []
        for index, value in enumerate(values):
            if value is None:
                continue
            ready.append((index, value))
            if len(ready) < days:
                continue
            window = ready[-days:]
            minimum = min(item[1] for item in window)
            aligned[index] = _leaf_fact(
                condition,
                bars,
                index,
                Comparator.GTE,
                minimum,
                multiple,
                dependency_start=0,
                value_labels=(f"min_rvol({days})", "required_multiple"),
            )
        return tuple(aligned)

    comparator = _resolve_trigger(
        condition,
        {
            "gte_multiple": Comparator.GTE,
            "lte_multiple": Comparator.LTE,
        },
        value_required=True,
    )
    aligned = [None] * len(bars)
    for index, value in enumerate(values):
        if value is None:
            continue
        aligned[index] = _leaf_fact(
            condition,
            bars,
            index,
            comparator,
            value,
            multiple,
            dependency_start=0,
            value_labels=(f"rvol_prior({period})", "required_multiple"),
        )
    return tuple(aligned)


def _evaluate_volume_price_confirmation(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
) -> tuple[SignalFact | None, ...]:
    if condition.value is not None:
        raise SignalRuntimeError(f"trigger {condition.trigger!r} forbids a comparison value")
    if condition.trigger not in {"surge_up", "surge_down"}:
        raise SignalRuntimeError(
            f"unsupported trigger {condition.trigger!r} for {condition.indicator_id}"
        )
    baseline_period = _int_param(condition, "baseline_period")
    volume_multiple = _decimal_param(condition, "volume_multiple")
    return_threshold = _decimal_param(condition, "return_threshold_pct")
    if volume_multiple <= 0 or return_threshold < 0:
        raise SignalRuntimeError(
            "volume_multiple must be positive and return_threshold_pct non-negative"
        )
    points = volume_price_points(bars, baseline_period)
    aligned: list[SignalFact | None] = [None] * len(bars)
    for index, point in enumerate(points):
        if point is None:
            continue
        price_holds = (
            point.return_pct >= return_threshold
            if condition.trigger == "surge_up"
            else point.return_pct <= -return_threshold
        )
        triggered = point.relative_volume >= volume_multiple and price_holds
        direction = ">=" if condition.trigger == "surge_up" else "<="
        aligned[index] = _boolean_fact(
            condition,
            bars,
            index,
            triggered=triggered,
            dependency_start=0,
            reason=(
                f"rvol={_format_decimal(point.relative_volume)} >= "
                f"{_format_decimal(volume_multiple)} and return_pct="
                f"{_format_decimal(point.return_pct)} {direction} "
                f"{_format_decimal(return_threshold if direction == '>=' else -return_threshold)}"
            ),
            left_value=point.return_pct,
            right_value=return_threshold if direction == ">=" else -return_threshold,
        )
    return tuple(aligned)


def _evaluate_obv(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
) -> tuple[SignalFact | None, ...]:
    comparator = _resolve_trigger(
        condition,
        {"rising": Comparator.GT, "falling": Comparator.LT},
        value_required=False,
    )
    values = on_balance_volume(bars)
    aligned: list[SignalFact | None] = [None] * len(bars)
    previous_value: Decimal | None = None
    for index, value in enumerate(values):
        if value is None:
            continue
        if previous_value is not None:
            aligned[index] = _leaf_fact(
                condition,
                bars,
                index,
                comparator,
                value,
                previous_value,
                dependency_start=0,
                value_labels=("obv", "previous_valid_obv"),
            )
        previous_value = value
    return tuple(aligned)


def _evaluate_obv_divergence(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
) -> tuple[SignalFact | None, ...]:
    if condition.value is not None:
        raise SignalRuntimeError(f"trigger {condition.trigger!r} forbids a comparison value")
    if condition.trigger not in {"bearish", "bullish"}:
        raise SignalRuntimeError(
            f"unsupported trigger {condition.trigger!r} for {condition.indicator_id}"
        )
    points = confirmed_obv_divergence(
        bars,
        left_bars=_int_param(condition, "left_bars"),
        right_bars=_int_param(condition, "right_bars"),
        min_separation=_int_param(condition, "min_separation"),
        max_separation=_int_param(condition, "max_separation"),
        price_threshold_pct=_decimal_param(condition, "price_threshold_pct"),
        obv_threshold_adv=_decimal_param(condition, "obv_threshold_adv"),
        average_volume_period=_int_param(condition, "average_volume_period"),
    )
    aligned: list[SignalFact | None] = [None] * len(bars)
    for index, point in enumerate(points):
        if point is None:
            continue
        triggered = point.bearish if condition.trigger == "bearish" else point.bullish
        pivot_detail = (
            "no newly confirmed divergence"
            if point.current_pivot_index is None
            else (
                f"pivots={point.previous_pivot_index}->{point.current_pivot_index}, "
                f"price_change_pct={_format_optional_decimal(point.price_change_pct)}, "
                f"obv_change={_format_optional_decimal(point.obv_change)}, "
                f"required_obv_change={_format_optional_decimal(point.obv_threshold)}"
            )
        )
        aligned[index] = _boolean_fact(
            condition,
            bars,
            index,
            triggered=triggered,
            dependency_start=0,
            reason=f"confirmed_{condition.trigger}_obv_divergence: {pivot_detail}",
            left_value=point.obv_change,
            right_value=point.obv_threshold,
        )
    return tuple(aligned)


def _evaluate_trend_regime(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
) -> tuple[SignalFact | None, ...]:
    if condition.value is not None:
        raise SignalRuntimeError(f"trigger {condition.trigger!r} forbids a comparison value")
    expected = {"uptrend": "up", "downtrend": "down", "range": "range"}.get(condition.trigger)
    if expected is None:
        raise SignalRuntimeError(
            f"unsupported trigger {condition.trigger!r} for {condition.indicator_id}"
        )
    points = stage_trend(
        bars,
        short_period=_int_param(condition, "short_period"),
        long_period=_int_param(condition, "long_period"),
        slope_lookback=_int_param(condition, "slope_lookback"),
        adx_period=_int_param(condition, "adx_period"),
        adx_threshold=_decimal_param(condition, "adx_threshold"),
        confirmation_days=_int_param(condition, "confirmation_days"),
        stability_bars=_int_param(condition, "stability_bars"),
    )
    aligned: list[SignalFact | None] = [None] * len(bars)
    for index, point in enumerate(points):
        if point is None:
            continue
        aligned[index] = _boolean_fact(
            condition,
            bars,
            index,
            triggered=point.regime == expected,
            dependency_start=0,
            reason=(
                f"regime={point.regime}, short_ma={_format_decimal(point.short_average)}, "
                f"long_ma={_format_decimal(point.long_average)}, "
                f"adx={_format_decimal(point.adx)}, +di={_format_decimal(point.plus_di)}, "
                f"-di={_format_decimal(point.minus_di)}"
            ),
            left_value=point.adx,
        )
    return tuple(aligned)


def _evaluate_true_range(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
) -> tuple[SignalFact | None, ...]:
    return _evaluate_threshold_series(
        condition,
        bars,
        true_range(bars),
        label="true_range",
        dependency_start=lambda index, is_cross: index - 1 - int(is_cross),
    )


def _evaluate_atr(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
) -> tuple[SignalFact | None, ...]:
    period = _int_param(condition, "period")
    return _evaluate_threshold_series(
        condition,
        bars,
        average_true_range(bars, period),
        label=f"wilder_atr({period})",
        dependency_start=lambda _index, _is_cross: 0,
    )


def _evaluate_natr(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
) -> tuple[SignalFact | None, ...]:
    period = _int_param(condition, "period")
    return _evaluate_threshold_series(
        condition,
        bars,
        normalized_average_true_range(bars, period),
        label=f"natr_pct({period})",
        dependency_start=lambda _index, _is_cross: 0,
    )


def _evaluate_adx(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
) -> tuple[SignalFact | None, ...]:
    period = _int_param(condition, "period")
    values: NumericSeries = tuple(
        point.adx if point is not None else None for point in directional_movement(bars, period)
    )
    return _evaluate_threshold_series(
        condition,
        bars,
        values,
        label=f"wilder_adx({period})",
        dependency_start=lambda _index, _is_cross: 0,
    )


def _evaluate_dmi(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
) -> tuple[SignalFact | None, ...]:
    if condition.value is not None:
        raise SignalRuntimeError(f"trigger {condition.trigger!r} forbids a comparison value")
    period = _int_param(condition, "period")
    comparator = _resolve_trigger(
        condition,
        {
            "plus_crosses_above_minus": Comparator.CROSSES_ABOVE,
            "plus_crosses_below_minus": Comparator.CROSSES_BELOW,
            "plus_above_minus": Comparator.GT,
            "plus_below_minus": Comparator.LT,
        },
        value_required=False,
    )
    is_cross = comparator in {Comparator.CROSSES_ABOVE, Comparator.CROSSES_BELOW}
    points = directional_movement(bars, period)
    aligned: list[SignalFact | None] = [None] * len(bars)
    for index, point in enumerate(points):
        if point is None:
            continue
        previous = points[index - 1] if index > 0 else None
        if is_cross and previous is None:
            continue
        aligned[index] = _leaf_fact(
            condition,
            bars,
            index,
            comparator,
            point.plus_di,
            point.minus_di,
            previous_left=previous.plus_di if previous is not None else None,
            previous_right=previous.minus_di if previous is not None else None,
            dependency_start=0,
            value_labels=(f"+di({period})", f"-di({period})"),
        )
    return tuple(aligned)


def _evaluate_bias(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
) -> tuple[SignalFact | None, ...]:
    period = _int_param(condition, "period")
    price_field = _price_field_param(condition)
    return _evaluate_threshold_series(
        condition,
        bars,
        simple_bias(bars, period, price_field),
        label=f"sma_bias_pct({period},{price_field})",
        dependency_start=lambda index, is_cross: index - period + 1 - int(is_cross),
    )


def _evaluate_roc(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
) -> tuple[SignalFact | None, ...]:
    period = _int_param(condition, "period")
    price_field = _price_field_param(condition)
    return _evaluate_threshold_series(
        condition,
        bars,
        rate_of_change(bars, period, price_field),
        label=f"roc_pct({period},{price_field})",
        dependency_start=lambda index, is_cross: index - period - int(is_cross),
    )


def _evaluate_momentum(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
) -> tuple[SignalFact | None, ...]:
    period = _int_param(condition, "period")
    price_field = _price_field_param(condition)
    return _evaluate_threshold_series(
        condition,
        bars,
        momentum(bars, period, price_field),
        label=f"momentum({period},{price_field})",
        dependency_start=lambda index, is_cross: index - period - int(is_cross),
    )


def _evaluate_stochastic(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
) -> tuple[SignalFact | None, ...]:
    k_period = _int_param(condition, "k_period")
    d_period = _int_param(condition, "d_period")
    points = stochastic_oscillator(bars, k_period=k_period, d_period=d_period)
    cross_map = {
        "k_crosses_above_d": Comparator.CROSSES_ABOVE,
        "k_crosses_below_d": Comparator.CROSSES_BELOW,
    }
    if condition.trigger in cross_map:
        if condition.value is not None:
            raise SignalRuntimeError(f"trigger {condition.trigger!r} forbids a comparison value")
        comparator = cross_map[condition.trigger]
        aligned: list[SignalFact | None] = [None] * len(bars)
        for index, point in enumerate(points):
            if point is None:
                continue
            previous = points[index - 1] if index > 0 else None
            if previous is None:
                continue
            aligned[index] = _leaf_fact(
                condition,
                bars,
                index,
                comparator,
                point.k,
                point.d,
                previous_left=previous.k,
                previous_right=previous.d,
                dependency_start=index - k_period - d_period + 1,
                value_labels=(f"stoch_k({k_period})", f"stoch_d({d_period})"),
            )
        return tuple(aligned)
    threshold_comparator = {
        "k_above": Comparator.GT,
        "k_below": Comparator.LT,
    }.get(condition.trigger)
    if threshold_comparator is None:
        raise SignalRuntimeError(
            f"unsupported trigger {condition.trigger!r} for {condition.indicator_id}"
        )
    threshold = _required_value(condition)
    aligned = [None] * len(bars)
    for index, point in enumerate(points):
        if point is None:
            continue
        aligned[index] = _leaf_fact(
            condition,
            bars,
            index,
            threshold_comparator,
            point.k,
            threshold,
            dependency_start=index - k_period - d_period + 2,
            value_labels=(f"stoch_k({k_period})", "threshold"),
        )
    return tuple(aligned)


def _evaluate_williams_r(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
) -> tuple[SignalFact | None, ...]:
    period = _int_param(condition, "period")
    return _evaluate_threshold_series(
        condition,
        bars,
        williams_r(bars, period),
        label=f"williams_r({period})",
        dependency_start=lambda index, is_cross: index - period + 1 - int(is_cross),
    )


def _evaluate_donchian(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
) -> tuple[SignalFact | None, ...]:
    if condition.value is not None:
        raise SignalRuntimeError(f"trigger {condition.trigger!r} forbids a comparison value")
    period = _int_param(condition, "period")
    points = donchian_channel(bars, period)
    trigger_map: dict[str, tuple[Comparator, str]] = {
        "price_crosses_above_upper": (Comparator.CROSSES_ABOVE, "upper"),
        "price_crosses_below_lower": (Comparator.CROSSES_BELOW, "lower"),
        "price_above_upper": (Comparator.GT, "upper"),
        "price_below_lower": (Comparator.LT, "lower"),
    }
    resolved = trigger_map.get(condition.trigger)
    if resolved is None:
        raise SignalRuntimeError(
            f"unsupported trigger {condition.trigger!r} for {condition.indicator_id}"
        )
    comparator, band = resolved
    is_cross = comparator in {Comparator.CROSSES_ABOVE, Comparator.CROSSES_BELOW}
    aligned: list[SignalFact | None] = [None] * len(bars)
    for index, point in enumerate(points):
        if point is None:
            continue
        previous: DonchianPoint | None = points[index - 1] if index > 0 else None
        if is_cross and previous is None:
            continue
        band_value = point.upper if band == "upper" else point.lower
        previous_band = (
            None if previous is None else previous.upper if band == "upper" else previous.lower
        )
        aligned[index] = _leaf_fact(
            condition,
            bars,
            index,
            comparator,
            bars[index].close.amount,
            band_value,
            previous_left=bars[index - 1].close.amount if previous is not None else None,
            previous_right=previous_band,
            dependency_start=index - period - int(is_cross),
            value_labels=("close", f"donchian_{band}({period},previous_only)"),
        )
    return tuple(aligned)


def _evaluate_return_stddev(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
) -> tuple[SignalFact | None, ...]:
    period = _int_param(condition, "period")
    price_field = _price_field_param(condition)
    return _evaluate_threshold_series(
        condition,
        bars,
        return_stddev(bars, period, price_field),
        label=f"return_stddev_pct({period},{price_field},ddof=0)",
        dependency_start=lambda index, is_cross: index - period - int(is_cross),
    )


def _evaluate_historical_volatility(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
) -> tuple[SignalFact | None, ...]:
    period = _int_param(condition, "period")
    annualization_sessions = _int_param(condition, "annualization_sessions")
    price_field = _price_field_param(condition)
    return _evaluate_threshold_series(
        condition,
        bars,
        historical_volatility(
            bars,
            period=period,
            annualization_sessions=annualization_sessions,
            price_field=price_field,
        ),
        label=(
            f"historical_volatility_pct({period},{price_field},"
            f"annualization={annualization_sessions},ddof=0)"
        ),
        dependency_start=lambda index, is_cross: index - period - int(is_cross),
    )


def _evaluate_price_series(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
    values: NumericSeries,
    *,
    price_field: PriceField,
    label: str,
    dependency_start: Callable[[int, bool], int],
) -> tuple[SignalFact | None, ...]:
    comparator = _resolve_trigger(
        condition,
        {
            "price_crosses_above": Comparator.CROSSES_ABOVE,
            "price_crosses_below": Comparator.CROSSES_BELOW,
            "price_above": Comparator.GT,
            "price_below": Comparator.LT,
        },
        value_required=False,
    )
    prices = tuple(getattr(bar, price_field).amount for bar in bars)
    is_cross = comparator in {Comparator.CROSSES_ABOVE, Comparator.CROSSES_BELOW}
    aligned: list[SignalFact | None] = [None] * len(bars)
    for index, value in enumerate(values):
        if value is None:
            continue
        previous_value = values[index - 1] if index > 0 else None
        if is_cross and previous_value is None:
            continue
        aligned[index] = _leaf_fact(
            condition,
            bars,
            index,
            comparator,
            prices[index],
            value,
            previous_left=prices[index - 1] if index > 0 else None,
            previous_right=previous_value,
            dependency_start=dependency_start(index, is_cross),
            value_labels=(price_field, label),
        )
    return tuple(aligned)


def _evaluate_threshold_series(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
    values: NumericSeries,
    *,
    label: str,
    dependency_start: Callable[[int, bool], int],
) -> tuple[SignalFact | None, ...]:
    comparator = _resolve_trigger(
        condition,
        {
            "crosses_above": Comparator.CROSSES_ABOVE,
            "crosses_below": Comparator.CROSSES_BELOW,
            "above": Comparator.GT,
            "below": Comparator.LT,
        },
        value_required=True,
    )
    threshold = _required_value(condition)
    is_cross = comparator in {Comparator.CROSSES_ABOVE, Comparator.CROSSES_BELOW}
    aligned: list[SignalFact | None] = [None] * len(bars)
    for index, value in enumerate(values):
        if value is None:
            continue
        previous_value = values[index - 1] if index > 0 else None
        if is_cross and previous_value is None:
            continue
        aligned[index] = _leaf_fact(
            condition,
            bars,
            index,
            comparator,
            value,
            threshold,
            previous_left=previous_value,
            previous_right=threshold if previous_value is not None else None,
            dependency_start=dependency_start(index, is_cross),
            value_labels=(label, "threshold"),
        )
    return tuple(aligned)


def _leaf_fact(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
    index: int,
    comparator: Comparator,
    left: Decimal,
    right: Decimal,
    *,
    dependency_start: int,
    value_labels: tuple[str, str],
    previous_left: Decimal | None = None,
    previous_right: Decimal | None = None,
) -> SignalFact:
    triggered = evaluate_comparator(
        comparator,
        left,
        right,
        previous_left=previous_left,
        previous_right=previous_right,
    )
    observed_at = _observed_at(bars[index])
    dependency_availability = max(
        bar.available_at for bar in bars[max(0, dependency_start) : index + 1]
    )
    availability = max(observed_at, dependency_availability)
    result_word = "true" if triggered else "false"
    reason = (
        f"{value_labels[0]}={_format_decimal(left)} {comparator.value} "
        f"{value_labels[1]}={_format_decimal(right)} => {result_word}"
    )
    return SignalFact(
        instrument_id=bars[index].instrument_id,
        session_date=bars[index].session_date,
        condition_ref=f"{condition.indicator_id}@{condition.definition_version}:{condition.trigger}",
        triggered=triggered,
        observed_at=observed_at,
        available_at=availability,
        reason=reason,
        left_value=left,
        right_value=right,
    )


def _boolean_fact(
    condition: IndicatorCondition,
    bars: tuple[DailyBar, ...],
    index: int,
    *,
    triggered: bool,
    dependency_start: int,
    reason: str,
    left_value: Decimal | None = None,
    right_value: Decimal | None = None,
) -> SignalFact:
    observed_at = _observed_at(bars[index])
    dependency_availability = max(
        bar.available_at for bar in bars[max(0, dependency_start) : index + 1]
    )
    return SignalFact(
        instrument_id=bars[index].instrument_id,
        session_date=bars[index].session_date,
        condition_ref=f"{condition.indicator_id}@{condition.definition_version}:{condition.trigger}",
        triggered=triggered,
        observed_at=observed_at,
        available_at=max(observed_at, dependency_availability),
        reason=f"{reason} => {'true' if triggered else 'false'}",
        left_value=left_value,
        right_value=right_value,
    )


def _combine(
    operator: str,
    child_timelines: tuple[tuple[SignalFact | None, ...], ...],
    bars: tuple[DailyBar, ...],
) -> tuple[SignalFact | None, ...]:
    aligned: list[SignalFact | None] = [None] * len(bars)
    for index, bar in enumerate(bars):
        children = tuple(timeline[index] for timeline in child_timelines)
        facts = tuple(child for child in children if child is not None)
        child_states = tuple(None if child is None else child.triggered for child in children)
        if operator == "all":
            if False in child_states:
                triggered = False
            elif all(state is True for state in child_states):
                triggered = True
            else:
                continue
        elif operator == "any":
            if True in child_states:
                triggered = True
            elif all(state is False for state in child_states):
                triggered = False
            else:
                continue
        elif operator == "not":
            if child_states[0] is None:
                continue
            triggered = not facts[0].triggered
        else:
            raise SignalRuntimeError(f"unsupported boolean operator: {operator!r}")
        result_word = "true" if triggered else "false"
        child_results = ",".join(
            "unknown" if state is None else "true" if state else "false" for state in child_states
        )
        observed_at = _observed_at(bar)
        aligned[index] = SignalFact(
            instrument_id=bar.instrument_id,
            session_date=bar.session_date,
            condition_ref=operator,
            triggered=triggered,
            observed_at=observed_at,
            available_at=max(observed_at, *(fact.available_at for fact in facts)),
            reason=f"{operator}[{child_results}] => {result_word}",
            children=facts,
            evidence=_merge_evidence(facts),
        )
    return tuple(aligned)


def _merge_evidence(facts: tuple[SignalFact, ...]) -> tuple[SignalEvidence, ...]:
    merged: dict[tuple[str, str, str | None], SignalEvidence] = {}
    for fact in facts:
        for item in fact.evidence:
            key = (item.evidence_type, item.evidence_id, item.source_event_id)
            merged[key] = item
    ordered = sorted(merged, key=lambda item: (item[0], item[1], item[2] or ""))
    return tuple(merged[key] for key in ordered)


def _resolve_trigger(
    condition: IndicatorCondition,
    mapping: dict[str, Comparator],
    *,
    value_required: bool,
) -> Comparator:
    try:
        comparator = mapping[condition.trigger]
    except KeyError as error:
        raise SignalRuntimeError(
            f"unsupported trigger {condition.trigger!r} for {condition.indicator_id}"
        ) from error
    if value_required and condition.value is None:
        raise SignalRuntimeError(f"trigger {condition.trigger!r} requires a comparison value")
    if not value_required and condition.value is not None:
        raise SignalRuntimeError(f"trigger {condition.trigger!r} forbids a comparison value")
    return comparator


def _required_value(condition: IndicatorCondition) -> Decimal:
    if condition.value is None:
        raise SignalRuntimeError(f"trigger {condition.trigger!r} requires a comparison value")
    return Decimal(str(condition.value))


def _int_param(condition: IndicatorCondition, name: str, *, default: int | None = None) -> int:
    value = condition.params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SignalRuntimeError(f"parameter {name!r} must be a positive integer")
    return value


def _str_param(condition: IndicatorCondition, name: str) -> str:
    value = condition.params.get(name)
    if not isinstance(value, str) or not value:
        raise SignalRuntimeError(f"parameter {name!r} must be a non-empty string")
    return value


def _decimal_param(condition: IndicatorCondition, name: str) -> Decimal:
    value = condition.params.get(name)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SignalRuntimeError(f"parameter {name!r} must be a finite number")
    result = Decimal(str(value))
    if not result.is_finite():
        raise SignalRuntimeError(f"parameter {name!r} must be a finite number")
    return result


def _price_field_param(condition: IndicatorCondition) -> PriceField:
    raw = _str_param(condition, "price_field")
    if raw not in {"open", "high", "low", "close"}:
        raise SignalRuntimeError(f"unsupported price_field: {raw!r}")
    return cast(PriceField, raw)


def _validate_bars(bars: tuple[DailyBar, ...]) -> None:
    if not bars:
        return
    instrument_id = bars[0].instrument_id
    previous_date = None
    for bar in bars:
        if bar.instrument_id != instrument_id:
            raise SignalRuntimeError("all bars must belong to one instrument")
        if previous_date is not None and bar.session_date <= previous_date:
            raise SignalRuntimeError("bars must be strictly ordered by session_date")
        previous_date = bar.session_date


def _observed_at(bar: DailyBar) -> datetime:
    return datetime.combine(bar.session_date, time(hour=15), tzinfo=SHANGHAI)


def _format_decimal(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _format_optional_decimal(value: Decimal | None) -> str:
    return "n/a" if value is None else _format_decimal(value)
