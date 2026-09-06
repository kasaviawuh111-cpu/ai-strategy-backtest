"""Provider query recipes for the executable A-share indicator catalog.

The recipes describe which already-calculated values must be requested from a
data provider and how a strategy condition compares those values.  They do not
contain an OHLCV indicator formula and are independent of any security symbol.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from ashare_lab.domain.strategy import IndicatorCondition
from ashare_lab.domain.strategy.models import JsonScalar

from .comparators import Comparator


class ProviderCatalogError(ValueError):
    """A condition cannot be represented by the provider value catalog."""


class RightOperandSource(StrEnum):
    FIELD = "field"
    PREVIOUS_FIELD = "previous_field"
    CONDITION_VALUE = "condition_value"
    PARAMETER = "parameter"
    CONSTANT = "constant"


@dataclass(frozen=True, slots=True)
class ProviderRightOperand:
    source: RightOperandSource
    field_name: str | None = None
    parameter_name: str | None = None
    constant: Decimal | None = None
    multiplier: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        supplied = sum(
            value is not None for value in (self.field_name, self.parameter_name, self.constant)
        )
        expected = (
            1
            if self.source
            in {
                RightOperandSource.FIELD,
                RightOperandSource.PREVIOUS_FIELD,
                RightOperandSource.PARAMETER,
                RightOperandSource.CONSTANT,
            }
            else 0
        )
        if supplied != expected:
            raise ProviderCatalogError("provider right operand shape is invalid")
        if (
            self.source
            in {
                RightOperandSource.FIELD,
                RightOperandSource.PREVIOUS_FIELD,
            }
            and not self.field_name
        ):
            raise ProviderCatalogError("provider right field cannot be blank")
        if self.source is RightOperandSource.PARAMETER and not self.parameter_name:
            raise ProviderCatalogError("provider right parameter cannot be blank")
        if self.source is RightOperandSource.CONSTANT and self.constant is None:
            raise ProviderCatalogError("provider right constant is required")
        if not self.multiplier.is_finite():
            raise ProviderCatalogError("provider right multiplier must be finite")


@dataclass(frozen=True, slots=True)
class ProviderComparisonBinding:
    left_field: str
    comparator: Comparator
    right: ProviderRightOperand

    def __post_init__(self) -> None:
        if not self.left_field.strip():
            raise ProviderCatalogError("provider left field cannot be blank")


@dataclass(frozen=True, slots=True)
class ProviderIndicatorBinding:
    indicator_id: str
    trigger: str
    provider_indicator_name: str
    value_names: tuple[str, ...]
    comparisons: tuple[ProviderComparisonBinding, ...]
    consecutive_sessions: int = 1

    def __post_init__(self) -> None:
        if not self.indicator_id or not self.trigger or not self.provider_indicator_name:
            raise ProviderCatalogError("provider binding identity cannot be blank")
        if not self.value_names or len(self.value_names) != len(set(self.value_names)):
            raise ProviderCatalogError("provider value names must be unique and non-empty")
        if any(not item.strip() for item in self.value_names) or not self.comparisons:
            raise ProviderCatalogError("provider binding values and comparisons are required")
        if self.consecutive_sessions < 1:
            raise ProviderCatalogError("provider consecutive session count must be positive")

    @property
    def required_fields(self) -> tuple[str, ...]:
        fields: list[str] = []
        for comparison in self.comparisons:
            fields.append(comparison.left_field)
            if comparison.right.source in {
                RightOperandSource.FIELD,
                RightOperandSource.PREVIOUS_FIELD,
            }:
                assert comparison.right.field_name is not None
                fields.append(comparison.right.field_name)
        return tuple(dict.fromkeys(fields))


type ProviderBindingBuilder = Callable[[IndicatorCondition], ProviderIndicatorBinding]


def _number(value: object, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ProviderCatalogError(f"provider parameter {name!r} must be numeric")
    try:
        result = Decimal(str(value))
    except Exception as exc:  # pragma: no cover - Pydantic normally catches this first.
        raise ProviderCatalogError(f"provider parameter {name!r} must be numeric") from exc
    if not result.is_finite():
        raise ProviderCatalogError(f"provider parameter {name!r} must be finite")
    return result


def _param(condition: IndicatorCondition, name: str) -> JsonScalar:
    try:
        return condition.params[name]
    except KeyError as exc:
        raise ProviderCatalogError(f"provider condition is missing parameter {name!r}") from exc


def _int_param(condition: IndicatorCondition, name: str) -> int:
    value = _param(condition, name)
    if isinstance(value, bool) or int(value) != value:
        raise ProviderCatalogError(f"provider parameter {name!r} must be an integer")
    return int(value)


def _format_number(value: object) -> str:
    numeric = _number(value, "display")
    return format(numeric.normalize(), "f")


def _price_field(condition: IndicatorCondition) -> str:
    value = str(_param(condition, "price_field"))
    try:
        return {
            "open": "开盘价",
            "high": "最高价",
            "low": "最低价",
            "close": "收盘价",
        }[value]
    except KeyError as exc:
        raise ProviderCatalogError(f"unsupported provider price field {value!r}") from exc


def _condition_value() -> ProviderRightOperand:
    return ProviderRightOperand(RightOperandSource.CONDITION_VALUE)


def _constant(value: str) -> ProviderRightOperand:
    return ProviderRightOperand(
        RightOperandSource.CONSTANT,
        constant=Decimal(value),
    )


def _field(name: str, *, multiplier: Decimal = Decimal("1")) -> ProviderRightOperand:
    return ProviderRightOperand(
        RightOperandSource.FIELD,
        field_name=name,
        multiplier=multiplier,
    )


def _previous_field(name: str) -> ProviderRightOperand:
    return ProviderRightOperand(RightOperandSource.PREVIOUS_FIELD, field_name=name)


def _parameter(name: str, *, multiplier: Decimal = Decimal("1")) -> ProviderRightOperand:
    return ProviderRightOperand(
        RightOperandSource.PARAMETER,
        parameter_name=name,
        multiplier=multiplier,
    )


def _binding(
    condition: IndicatorCondition,
    *,
    provider_name: str,
    values: tuple[str, ...],
    comparisons: tuple[ProviderComparisonBinding, ...],
    consecutive_sessions: int = 1,
) -> ProviderIndicatorBinding:
    return ProviderIndicatorBinding(
        indicator_id=condition.indicator_id,
        trigger=condition.trigger,
        provider_indicator_name=provider_name,
        value_names=values,
        comparisons=comparisons,
        consecutive_sessions=consecutive_sessions,
    )


_THRESHOLD_COMPARATORS = {
    "crosses_above": Comparator.CROSSES_ABOVE,
    "crosses_below": Comparator.CROSSES_BELOW,
    "above": Comparator.GT,
    "below": Comparator.LT,
    "at_least": Comparator.GTE,
    "at_most": Comparator.LTE,
}


def _threshold(
    condition: IndicatorCondition,
    *,
    provider_name: str,
    field_name: str,
) -> ProviderIndicatorBinding:
    try:
        comparator = _THRESHOLD_COMPARATORS[condition.trigger]
    except KeyError as exc:
        raise ProviderCatalogError(
            f"unsupported provider trigger {condition.indicator_id}:{condition.trigger}"
        ) from exc
    return _binding(
        condition,
        provider_name=provider_name,
        values=(field_name,),
        comparisons=(ProviderComparisonBinding(field_name, comparator, _condition_value()),),
    )


def _price_line(
    condition: IndicatorCondition,
    *,
    provider_name: str,
    line_field: str,
) -> ProviderIndicatorBinding:
    price = _price_field(condition)
    comparators = {
        "price_crosses_above": Comparator.CROSSES_ABOVE,
        "price_crosses_below": Comparator.CROSSES_BELOW,
        "price_above": Comparator.GT,
        "price_below": Comparator.LT,
    }
    try:
        comparator = comparators[condition.trigger]
    except KeyError as exc:
        raise ProviderCatalogError(
            f"unsupported provider trigger {condition.indicator_id}:{condition.trigger}"
        ) from exc
    return _binding(
        condition,
        provider_name=provider_name,
        values=(price, line_field),
        comparisons=(ProviderComparisonBinding(price, comparator, _field(line_field)),),
    )


def _two_lines(
    condition: IndicatorCondition,
    *,
    provider_name: str,
    values: tuple[str, ...],
    left: str,
    right: str,
    trigger_map: dict[str, Comparator],
) -> ProviderIndicatorBinding:
    try:
        comparator = trigger_map[condition.trigger]
    except KeyError as exc:
        raise ProviderCatalogError(
            f"unsupported provider trigger {condition.indicator_id}:{condition.trigger}"
        ) from exc
    return _binding(
        condition,
        provider_name=provider_name,
        values=values,
        comparisons=(ProviderComparisonBinding(left, comparator, _field(right)),),
    )


def _scalar_builder(
    provider_label: str,
    field_label: str,
    *,
    period_param: str | None = "period",
) -> ProviderBindingBuilder:
    def build(condition: IndicatorCondition) -> ProviderIndicatorBinding:
        suffix = f"({_int_param(condition, period_param)})" if period_param else ""
        return _threshold(
            condition,
            provider_name=provider_label + suffix,
            field_name=field_label,
        )

    return build


def _macd(condition: IndicatorCondition) -> ProviderIndicatorBinding:
    fast = _int_param(condition, "fast")
    slow = _int_param(condition, "slow")
    signal = _int_param(condition, "signal")
    name = f"MACD({fast},{slow},{signal})"
    values = ("DIF值", "DEA值")
    if condition.trigger in {"golden_cross", "death_cross"}:
        comparator = (
            Comparator.CROSSES_ABOVE
            if condition.trigger == "golden_cross"
            else Comparator.CROSSES_BELOW
        )
        comparison = ProviderComparisonBinding("DIF值", comparator, _field("DEA值"))
    elif condition.trigger in {"crosses_above_zero", "crosses_below_zero"}:
        comparator = (
            Comparator.CROSSES_ABOVE
            if condition.trigger == "crosses_above_zero"
            else Comparator.CROSSES_BELOW
        )
        comparison = ProviderComparisonBinding("DIF值", comparator, _constant("0"))
    else:
        raise ProviderCatalogError(f"unsupported provider MACD trigger {condition.trigger!r}")
    return _binding(
        condition,
        provider_name=name,
        values=values,
        comparisons=(comparison,),
    )


def _moving_average(condition: IndicatorCondition) -> ProviderIndicatorBinding:
    period = _int_param(condition, "period")
    return _price_line(
        condition,
        provider_name=f"{period}日移动平均线",
        line_field=f"{period}日MA简单移动平均",
    )


def _ema(condition: IndicatorCondition) -> ProviderIndicatorBinding:
    period = _int_param(condition, "period")
    return _price_line(
        condition,
        provider_name=f"EMA({period})",
        line_field=f"EMA{period}值",
    )


def _ma_cross(condition: IndicatorCondition) -> ProviderIndicatorBinding:
    fast = _int_param(condition, "fast_period")
    slow = _int_param(condition, "slow_period")
    fast_field = f"{fast}日MA简单移动平均"
    slow_field = f"{slow}日MA简单移动平均"
    return _two_lines(
        condition,
        provider_name=f"{fast}日与{slow}日移动平均线",
        values=(fast_field, slow_field),
        left=fast_field,
        right=slow_field,
        trigger_map={
            "golden_cross": Comparator.CROSSES_ABOVE,
            "death_cross": Comparator.CROSSES_BELOW,
            "fast_above_slow": Comparator.GT,
            "fast_below_slow": Comparator.LT,
        },
    )


def _bollinger(condition: IndicatorCondition) -> ProviderIndicatorBinding:
    period = _int_param(condition, "period")
    multiplier = _format_number(_param(condition, "stddev_multiplier"))
    price = _price_field(condition)
    bands = {
        "price_crosses_above_upper": (Comparator.CROSSES_ABOVE, "上轨值"),
        "price_crosses_below_upper": (Comparator.CROSSES_BELOW, "上轨值"),
        "price_crosses_above_middle": (Comparator.CROSSES_ABOVE, "中轨值"),
        "price_crosses_below_middle": (Comparator.CROSSES_BELOW, "中轨值"),
        "price_crosses_above_lower": (Comparator.CROSSES_ABOVE, "下轨值"),
        "price_crosses_below_lower": (Comparator.CROSSES_BELOW, "下轨值"),
    }
    try:
        comparator, band = bands[condition.trigger]
    except KeyError as exc:
        raise ProviderCatalogError(
            f"unsupported provider BOLL trigger {condition.trigger!r}"
        ) from exc
    return _binding(
        condition,
        provider_name=f"BOLL({period},{multiplier})",
        values=(price, "上轨值", "中轨值", "下轨值"),
        comparisons=(ProviderComparisonBinding(price, comparator, _field(band)),),
    )


def _kdj(condition: IndicatorCondition) -> ProviderIndicatorBinding:
    period = _int_param(condition, "period")
    k = _int_param(condition, "k_smoothing")
    d = _int_param(condition, "d_smoothing")
    name = f"KDJ({period},{k},{d})"
    if condition.trigger in {"golden_cross", "death_cross"}:
        comparator = (
            Comparator.CROSSES_ABOVE
            if condition.trigger == "golden_cross"
            else Comparator.CROSSES_BELOW
        )
        comparison = ProviderComparisonBinding("K值", comparator, _field("D值"))
    elif condition.trigger in {"j_above", "j_below"}:
        comparison = ProviderComparisonBinding(
            "J值",
            Comparator.GT if condition.trigger == "j_above" else Comparator.LT,
            _condition_value(),
        )
    else:
        raise ProviderCatalogError(f"unsupported provider KDJ trigger {condition.trigger!r}")
    return _binding(
        condition,
        provider_name=name,
        values=("K值", "D值", "J值"),
        comparisons=(comparison,),
    )


def _bbi(condition: IndicatorCondition) -> ProviderIndicatorBinding:
    periods = tuple(_int_param(condition, f"period_{index}") for index in range(1, 5))
    return _price_line(
        condition,
        provider_name=f"BBI({','.join(str(item) for item in periods)})",
        line_field="BBI值",
    )


def _close(condition: IndicatorCondition) -> ProviderIndicatorBinding:
    return _threshold(condition, provider_name="收盘价", field_name="收盘价")


def _return_pct(condition: IndicatorCondition) -> ProviderIndicatorBinding:
    period = _int_param(condition, "period")
    return _threshold(
        condition,
        provider_name=f"{period}日涨跌幅",
        field_name=f"{period}日涨跌幅",
    )


def _rolling_high(condition: IndicatorCondition) -> ProviderIndicatorBinding:
    if condition.trigger != "new_high":
        raise ProviderCatalogError(
            f"unsupported provider rolling-high trigger {condition.trigger!r}"
        )
    _int_param(condition, "period")
    field = "近期创阶段新高"
    return _binding(
        condition,
        provider_name=field,
        values=(field,),
        comparisons=(ProviderComparisonBinding(field, Comparator.GTE, _constant("1")),),
    )


def _consecutive_up(condition: IndicatorCondition) -> ProviderIndicatorBinding:
    if condition.trigger != "at_least":
        raise ProviderCatalogError(
            f"unsupported provider consecutive-up trigger {condition.trigger!r}"
        )
    field = "连续上涨天数"
    return _binding(
        condition,
        provider_name=field,
        values=(field,),
        comparisons=(ProviderComparisonBinding(field, Comparator.GTE, _parameter("days")),),
    )


def _volume(condition: IndicatorCondition) -> ProviderIndicatorBinding:
    period = _int_param(condition, "baseline_period")
    comparators = {
        "gte_multiple": Comparator.GTE,
        "lte_multiple": Comparator.LTE,
    }
    try:
        comparator = comparators[condition.trigger]
    except KeyError as exc:
        raise ProviderCatalogError(
            f"unsupported provider volume trigger {condition.trigger!r}"
        ) from exc
    average = f"{period}日平均成交量"
    return _binding(
        condition,
        provider_name=f"成交量与{average}",
        values=("成交量", average),
        comparisons=(
            ProviderComparisonBinding(
                "成交量",
                comparator,
                ProviderRightOperand(
                    RightOperandSource.FIELD,
                    field_name=average,
                    multiplier=_number(condition.value, "value"),
                ),
            ),
        ),
    )


def _relative_volume(condition: IndicatorCondition) -> ProviderIndicatorBinding:
    _int_param(condition, "baseline_period")
    comparators = {
        "gt_multiple": Comparator.GT,
        "gte_multiple": Comparator.GTE,
        "lte_multiple": Comparator.LTE,
        "consecutive_gte_multiple": Comparator.GTE,
    }
    try:
        comparator = comparators[condition.trigger]
    except KeyError as exc:
        raise ProviderCatalogError(
            f"unsupported provider relative-volume trigger {condition.trigger!r}"
        ) from exc
    consecutive = (
        _int_param(condition, "consecutive_days")
        if condition.trigger == "consecutive_gte_multiple"
        else 1
    )
    return _binding(
        condition,
        provider_name="量比",
        values=("量比",),
        comparisons=(ProviderComparisonBinding("量比", comparator, _condition_value()),),
        consecutive_sessions=consecutive,
    )


def _volume_price_confirmation(condition: IndicatorCondition) -> ProviderIndicatorBinding:
    if condition.trigger not in {"surge_up", "surge_down"}:
        raise ProviderCatalogError(
            f"unsupported provider volume-price trigger {condition.trigger!r}"
        )
    period = _int_param(condition, "baseline_period")
    return_comparator = Comparator.GTE if condition.trigger == "surge_up" else Comparator.LTE
    return_multiplier = Decimal("1") if condition.trigger == "surge_up" else Decimal("-1")
    return _binding(
        condition,
        provider_name=f"{period}日相对成交量与当日涨跌幅",
        values=("相对成交量", "当日涨跌幅"),
        comparisons=(
            ProviderComparisonBinding("相对成交量", Comparator.GTE, _parameter("volume_multiple")),
            ProviderComparisonBinding(
                "当日涨跌幅",
                return_comparator,
                _parameter("return_threshold_pct", multiplier=return_multiplier),
            ),
        ),
    )


def _obv(condition: IndicatorCondition) -> ProviderIndicatorBinding:
    comparators = {"rising": Comparator.GT, "falling": Comparator.LT}
    try:
        comparator = comparators[condition.trigger]
    except KeyError as exc:
        raise ProviderCatalogError(
            f"unsupported provider OBV trigger {condition.trigger!r}"
        ) from exc
    return _binding(
        condition,
        provider_name="OBV",
        values=("OBV值",),
        comparisons=(ProviderComparisonBinding("OBV值", comparator, _previous_field("OBV值")),),
    )


def _direct_flag(
    condition: IndicatorCondition,
    *,
    provider_name: str,
    trigger_fields: dict[str, str],
) -> ProviderIndicatorBinding:
    try:
        field = trigger_fields[condition.trigger]
    except KeyError as exc:
        raise ProviderCatalogError(
            f"unsupported provider trigger {condition.indicator_id}:{condition.trigger}"
        ) from exc
    return _binding(
        condition,
        provider_name=provider_name,
        values=tuple(dict.fromkeys(trigger_fields.values())),
        comparisons=(ProviderComparisonBinding(field, Comparator.GTE, _constant("1")),),
    )


def _divergence(condition: IndicatorCondition) -> ProviderIndicatorBinding:
    params = ",".join(f"{name}={value}" for name, value in condition.params.items())
    return _direct_flag(
        condition,
        provider_name=f"量价背离({params})",
        trigger_fields={"bearish": "看跌量价背离标记", "bullish": "看涨量价背离标记"},
    )


def _trend_regime(condition: IndicatorCondition) -> ProviderIndicatorBinding:
    params = ",".join(f"{name}={value}" for name, value in condition.params.items())
    return _direct_flag(
        condition,
        provider_name=f"阶段趋势({params})",
        trigger_fields={
            "uptrend": "上涨趋势标记",
            "downtrend": "下跌趋势标记",
            "range": "震荡区间标记",
        },
    )


def _dmi(condition: IndicatorCondition) -> ProviderIndicatorBinding:
    period = _int_param(condition, "period")
    return _two_lines(
        condition,
        provider_name=f"DMI({period})",
        values=("+DI值", "-DI值"),
        left="+DI值",
        right="-DI值",
        trigger_map={
            "plus_crosses_above_minus": Comparator.CROSSES_ABOVE,
            "plus_crosses_below_minus": Comparator.CROSSES_BELOW,
            "plus_above_minus": Comparator.GT,
            "plus_below_minus": Comparator.LT,
        },
    )


def _stochastic(condition: IndicatorCondition) -> ProviderIndicatorBinding:
    k_period = _int_param(condition, "k_period")
    d_period = _int_param(condition, "d_period")
    name = f"Stochastic({k_period},{d_period})"
    if condition.trigger in {"k_crosses_above_d", "k_crosses_below_d"}:
        comparator = (
            Comparator.CROSSES_ABOVE
            if condition.trigger == "k_crosses_above_d"
            else Comparator.CROSSES_BELOW
        )
        comparison = ProviderComparisonBinding("K值", comparator, _field("D值"))
    elif condition.trigger in {"k_above", "k_below"}:
        comparison = ProviderComparisonBinding(
            "K值",
            Comparator.GT if condition.trigger == "k_above" else Comparator.LT,
            _condition_value(),
        )
    else:
        raise ProviderCatalogError(f"unsupported provider Stochastic trigger {condition.trigger!r}")
    return _binding(
        condition,
        provider_name=name,
        values=("K值", "D值"),
        comparisons=(comparison,),
    )


def _donchian(condition: IndicatorCondition) -> ProviderIndicatorBinding:
    period = _int_param(condition, "period")
    mapping = {
        "price_crosses_above_upper": (Comparator.CROSSES_ABOVE, "上轨值"),
        "price_crosses_below_lower": (Comparator.CROSSES_BELOW, "下轨值"),
        "price_above_upper": (Comparator.GT, "上轨值"),
        "price_below_lower": (Comparator.LT, "下轨值"),
    }
    try:
        comparator, band = mapping[condition.trigger]
    except KeyError as exc:
        raise ProviderCatalogError(
            f"unsupported provider Donchian trigger {condition.trigger!r}"
        ) from exc
    return _binding(
        condition,
        provider_name=f"唐奇安通道({period})",
        values=("收盘价", "上轨值", "下轨值"),
        comparisons=(ProviderComparisonBinding("收盘价", comparator, _field(band)),),
    )


def _amount_average(condition: IndicatorCondition) -> ProviderIndicatorBinding:
    period = _int_param(condition, "period")
    return _threshold(
        condition,
        provider_name=f"{period}日平均成交额",
        field_name=f"{period}日平均成交额",
    )


def _ema_bias(condition: IndicatorCondition) -> ProviderIndicatorBinding:
    period = _int_param(condition, "period")
    return _threshold(
        condition,
        provider_name=f"EMA{period}乖离率",
        field_name="EMA乖离率",
    )


def _bias_like(provider_label: str, field_label: str) -> ProviderBindingBuilder:
    def build(condition: IndicatorCondition) -> ProviderIndicatorBinding:
        period = _int_param(condition, "period")
        return _threshold(
            condition,
            provider_name=f"{provider_label}({period})",
            field_name=field_label,
        )

    return build


PROVIDER_INDICATOR_BUILDERS: dict[str, ProviderBindingBuilder] = {
    "technical.macd": _macd,
    "technical.ma": _moving_average,
    "technical.rsi": _scalar_builder("RSI", "RSI值"),
    "market.volume": _volume,
    "technical.ema": _ema,
    "technical.ma_cross": _ma_cross,
    "technical.bollinger": _bollinger,
    "technical.kdj": _kdj,
    "technical.cci": _scalar_builder("CCI", "CCI值"),
    "technical.bbi": _bbi,
    "technical.ema_bias": _ema_bias,
    "price.close": _close,
    "price.return_pct": _return_pct,
    "price.rolling_high": _rolling_high,
    "price.consecutive_up": _consecutive_up,
    "price.amplitude": _scalar_builder("振幅", "振幅", period_param=None),
    "market.amount": _scalar_builder("成交额", "成交额", period_param=None),
    "market.turnover_rate": _scalar_builder("换手率", "换手率", period_param=None),
    "amount.average": _amount_average,
    "volume.relative": _relative_volume,
    "volume.price_confirmation": _volume_price_confirmation,
    "technical.obv": _obv,
    "volume.price_divergence": _divergence,
    "technical.trend_regime": _trend_regime,
    "price.true_range": _scalar_builder("真实波幅TR", "TR值", period_param=None),
    "technical.atr": _scalar_builder("ATR", "ATR值"),
    "technical.natr": _scalar_builder("NATR", "NATR值"),
    "technical.adx": _scalar_builder("ADX", "ADX值"),
    "technical.dmi": _dmi,
    "technical.bias": _bias_like("BIAS", "BIAS值"),
    "technical.roc": _bias_like("ROC", "ROC值"),
    "technical.momentum": _bias_like("MOM", "MOM值"),
    "technical.stochastic": _stochastic,
    "technical.williams_r": _scalar_builder("Williams %R", "%R值"),
    "technical.donchian": _donchian,
    "technical.return_stddev": _scalar_builder("收益率标准差", "收益率标准差"),
    "technical.historical_volatility": _scalar_builder("历史波动率", "历史波动率"),
}


def provider_binding_for_condition(condition: IndicatorCondition) -> ProviderIndicatorBinding:
    """Build a symbol-independent provider request and comparison recipe."""

    if condition.definition_version != "1.0.0":
        raise ProviderCatalogError(
            f"unsupported provider definition version {condition.definition_version!r}"
        )
    try:
        builder = PROVIDER_INDICATOR_BUILDERS[condition.indicator_id]
    except KeyError as exc:
        raise ProviderCatalogError(
            f"provider indicator is not registered: {condition.indicator_id}"
        ) from exc
    return builder(condition)
