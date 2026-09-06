"""Strict MX field contracts for the locally executable indicator subset."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

ACTIVE_SKILL_INDICATORS = frozenset(
    {
        "technical.ma",
        "technical.ma_cross",
        "technical.rsi",
        "price.close",
        "price.amplitude",
        "market.amount",
        "market.turnover_rate",
        "price.consecutive_up",
    }
)

UNSUPPORTED_SKILL_INDICATOR_REASONS: Mapping[str, str] = {
    "technical.macd": "真实列和默认周期已找到，但后复权参数尚未通过验证。",
    "market.volume": "真实结果缺少请求周期的平均成交量列。",
    "technical.ema": "真实结果未返回 EMA 指标列。",
    "technical.bollinger": "真实三轨参数可识别，但返回的复权参数不是后复权。",
    "technical.kdj": "真实 KDJ 周期可识别，但返回的复权参数不是后复权。",
    "technical.cci": "真实 CCI 周期可识别，但返回的复权参数不是后复权。",
    "technical.bbi": "真实 BBI 周期可识别，但返回的复权参数不是后复权。",
    "technical.ema_bias": "真实结果误识别为 DMA，未返回 EMA 乖离率。",
    "price.return_pct": "真实查询未返回区间涨跌幅序列。",
    "price.rolling_high": "真实结果未证明使用了请求的滚动窗口。",
    "amount.average": "真实查询未返回请求周期的平均成交额序列。",
    "volume.relative": "真实量比结果未证明使用了请求的基准周期。",
    "volume.price_confirmation": "真实量价结果未证明使用了请求的基准周期。",
    "technical.obv": "真实 OBV 列已找到，但返回的复权参数不是后复权。",
    "volume.price_divergence": "真实查询未返回可验证的量价背离标记列。",
    "technical.trend_regime": "真实结果是同行业阶段表现，不是策略趋势状态。",
    "price.true_range": "真实结果未证明是请求口径的独立 TR 序列。",
    "technical.atr": "真实结果未证明是请求口径的平均真实波幅。",
    "technical.natr": "真实结果未返回可验证的归一化 ATR。",
    "technical.adx": "真实结果未返回独立 ADX 列。",
    "technical.dmi": "真实结果缺少可分别验证的正负 DI 列。",
    "technical.bias": "真实结果使用了不同于请求的周期参数。",
    "technical.roc": "真实 ROC 周期可识别，但返回的复权参数不是后复权。",
    "technical.momentum": "真实结果使用了不同于请求的动量周期。",
    "technical.stochastic": "真实结果是 KDJ，不是请求口径的随机指标。",
    "technical.williams_r": "真实结果的周期和数值符号口径均不匹配。",
    "technical.donchian": "真实结果误返回 PE/PB 估值通道，不是唐奇安通道。",
    "technical.return_stddev": "真实结果是逐日涨跌幅，不是滚动收益标准差。",
    "technical.historical_volatility": "真实结果不是可逐日回放的滚动年化波动率。",
}


class UnsupportedSkillIndicatorError(ValueError):
    """The local MX profile has no verified field contract for an indicator."""

    def __init__(self, indicator_id: str, reason: str) -> None:
        self.indicator_id = indicator_id
        self.reason = reason
        super().__init__(f"{indicator_id}: {reason}")


@dataclass(frozen=True, slots=True)
class _FieldRule:
    requested_name: str
    source_code: str
    fixed_params: tuple[tuple[str, str], ...] = ()
    allow_generic_ma: bool = False


@dataclass(frozen=True, slots=True)
class MxIndicatorFieldContract:
    indicator_id: str
    query_fields: str
    _rules: tuple[_FieldRule, ...]

    def bind_field(self, requested_name: str, field: Mapping[str, object]) -> bool:
        """Accept only the exact provider output and parameter identity expected."""

        rule = next(
            (item for item in self._rules if item.requested_name == requested_name.strip()),
            None,
        )
        if rule is None:
            return False
        source_code = _text(field.get("returnSourceCode"))
        generic_ma = rule.allow_generic_ma and source_code == "MAJDYDPJ"
        if source_code != rule.source_code and not generic_ma:
            return False
        raw_fixed = field.get("fixedParamValue")
        if not rule.fixed_params:
            return raw_fixed is None or (isinstance(raw_fixed, str) and not raw_fixed.strip())
        actual = _fixed_params(raw_fixed)
        # MX uses a generic source code for custom MA periods. Its verified
        # variant still declares the exact N, daily frequency and adjustment.
        if generic_ma and (actual is None or actual.get("newoldtype") != "0"):
            return False
        return actual is not None and all(
            actual.get(key.casefold()) == value for key, value in rule.fixed_params
        )


def unsupported_indicator_reason(indicator_id: str) -> str | None:
    if indicator_id in ACTIVE_SKILL_INDICATORS:
        return None
    return UNSUPPORTED_SKILL_INDICATOR_REASONS.get(
        indicator_id,
        "当前本地东方财富 Skill 尚未登记该指标的真实字段合同。",
    )


def build_indicator_contract(
    indicator_id: str,
    provider_name: str,
    value_names: tuple[str, ...],
) -> MxIndicatorFieldContract:
    """Build the verified query and response contract for one provider binding."""

    reason = unsupported_indicator_reason(indicator_id)
    if reason is not None:
        raise UnsupportedSkillIndicatorError(indicator_id, reason)
    if indicator_id == "technical.ma":
        match = re.fullmatch(r"([1-9]\d*)日移动平均线", provider_name)
        period = int(match.group(1)) if match else 0
        line = f"{period}日MA简单移动平均"
        _require_identity(indicator_id, value_names, ("收盘价", line), valid=period > 0)
        return _contract(
            indicator_id,
            f"后复权收盘价（AdjustFlag=2,CurType=1,Period=1）、"
            f"{line}（N={period},AdjustFlag=2,period=1）",
            _price_rule(),
            _FieldRule(
                line, f"MAJDYDPJ{period}", _params(N=period, AdjustFlag=2, period=1),
                allow_generic_ma=True,
            ),
        )
    if indicator_id == "technical.ma_cross":
        match = re.fullmatch(r"([1-9]\d*)日与([1-9]\d*)日移动平均线", provider_name)
        fast, slow = (int(item) for item in match.groups()) if match else (0, 0)
        names = (f"{fast}日MA简单移动平均", f"{slow}日MA简单移动平均")
        _require_identity(indicator_id, value_names, names, valid=0 < fast < slow)
        rules = tuple(
            _FieldRule(
                name, f"MAJDYDPJ{period}", _params(N=period, AdjustFlag=2, period=1),
                allow_generic_ma=True,
            )
            for name, period in zip(names, (fast, slow), strict=True)
        )
        query = "、".join(
            f"{name}（N={period},AdjustFlag=2,period=1）"
            for name, period in zip(names, (fast, slow), strict=True)
        )
        return _contract(indicator_id, query, *rules)
    if indicator_id == "technical.rsi":
        match = re.fullmatch(r"RSI\(([1-9]\d*)\)", provider_name)
        period = int(match.group(1)) if match else 0
        _require_identity(indicator_id, value_names, ("RSI值",), valid=period > 1)
        return _contract(
            indicator_id,
            f"{period}日RSI相对强弱指标（N={period},AdjustFlag=2,period=1）",
            _FieldRule(
                "RSI值",
                "RSIXDQRZB",
                _params(N=period, AdjustFlag=2, period=1),
            ),
        )

    static = _STATIC_CONTRACTS[indicator_id]
    expected_provider, expected_values, contract = static
    _require_identity(
        indicator_id,
        value_names,
        expected_values,
        valid=provider_name == expected_provider,
    )
    return contract


def _price_rule() -> _FieldRule:
    return _FieldRule(
        "收盘价",
        "CLOSE",
        _params(AdjustFlag=2, CurType=1, Period=1),
    )


def _contract(
    indicator_id: str, query_fields: str, *rules: _FieldRule
) -> MxIndicatorFieldContract:
    return MxIndicatorFieldContract(indicator_id, query_fields, tuple(rules))


def _params(**values: int) -> tuple[tuple[str, str], ...]:
    return tuple((key, str(value)) for key, value in values.items())


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _fixed_params(value: object) -> dict[str, str] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    result: dict[str, str] = {}
    for item in value.split(","):
        key, separator, raw_value = item.partition("=")
        normalized_key = key.strip().casefold()
        normalized_value = raw_value.strip()
        if not separator or not normalized_key or not normalized_value:
            return None
        if normalized_key in result and result[normalized_key] != normalized_value:
            return None
        result[normalized_key] = normalized_value
    return result


def _require_identity(
    indicator_id: str,
    actual_values: tuple[str, ...],
    expected_values: tuple[str, ...],
    *,
    valid: bool,
) -> None:
    if not valid or actual_values != expected_values:
        raise ValueError(f"{indicator_id} provider binding is outside the verified field contract")


_STATIC_CONTRACTS: Mapping[
    str, tuple[str, tuple[str, ...], MxIndicatorFieldContract]
] = {
    "price.close": (
        "收盘价",
        ("收盘价",),
        _contract("price.close", "后复权收盘价（AdjustFlag=2,CurType=1,Period=1）", _price_rule()),
    ),
    "price.amplitude": (
        "振幅",
        ("振幅",),
        _contract(
            "price.amplitude",
            "振幅（Period=1,CurType=2）",
            _FieldRule("振幅", "ZF", _params(Period=1, CurType=2)),
        ),
    ),
    "market.amount": (
        "成交额",
        ("成交额",),
        _contract(
            "market.amount",
            "成交额（CurType=1,Period=1）",
            _FieldRule("成交额", "AMOUNT", _params(CurType=1, Period=1)),
        ),
    ),
    "market.turnover_rate": (
        "换手率",
        ("换手率",),
        _contract(
            "market.turnover_rate",
            "换手率（Period=1）",
            _FieldRule("换手率", "TURN", _params(Period=1)),
        ),
    ),
    "price.consecutive_up": (
        "连续上涨天数",
        ("连续上涨天数",),
        _contract(
            "price.consecutive_up",
            "连涨天数",
            _FieldRule("连续上涨天数", "LZTS"),
        ),
    ),
}
