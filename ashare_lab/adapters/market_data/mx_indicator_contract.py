"""MX field contracts: verified recipes plus fresh metadata-bound discovery.

Old probe failures are diagnostic history, never a permanent no-query rule.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from ashare_lab.domain.signals.provider_catalog import PROVIDER_INDICATOR_BUILDERS
from ashare_lab.domain.strategy.models import JsonScalar
from ashare_lab.ports.provider_indicator_data import LEGACY_SKILL_PROVIDER_INDICATORS

ACTIVE_SKILL_INDICATORS = LEGACY_SKILL_PROVIDER_INDICATORS

# Retained only as diagnostic history. No admission, query or binding path reads
# this mapping: a prior response failure cannot disable later provider queries.
HISTORICAL_SKILL_PROBE_FAILURES: Mapping[str, str] = {
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
    price_field: str | None = None
    # Canonical raw scale for a verified field when MX returns numeric unit=1
    # without a unit label. Unknown/dynamic fields must supply their own evidence.
    raw_unit: str | None = None


@dataclass(frozen=True, slots=True)
class MxIndicatorFieldContract:
    indicator_id: str
    query_fields: str
    _rules: tuple[_FieldRule, ...]
    provider_name: str | None = None
    condition_params: tuple[tuple[str, JsonScalar], ...] = ()
    parameter_request_present: bool = True

    def raw_unit_for(self, requested_name: str, field: Mapping[str, object]) -> str | None:
        if not self.bind_field(requested_name, field):
            return None
        rule = next((item for item in self._rules if item.requested_name == requested_name), None)
        return rule.raw_unit if rule is not None else None

    def recognizes_field(self, requested_name: str, field: Mapping[str, object]) -> bool:
        """Distinguish an absent column from a present but invalid source field."""
        rule = next((item for item in self._rules if item.requested_name == requested_name), None)
        if field.get("returnSourceCode") == "MAJDYDPJ" and any(
            item.requested_name != requested_name and self.bind_field(item.requested_name, field)
            for item in self._rules
        ):
            # Custom periods share a provider source code. A column already
            # proven to be the other MA operand is not evidence this one exists.
            # Explicit labels naming this operand are checked independently by
            # the adapter, so a returned MA7 with N=3 remains a parameter error.
            return False
        return rule is not None and (
            field.get("returnSourceCode") == rule.source_code
            or (rule.allow_generic_ma and field.get("returnSourceCode") == "MAJDYDPJ")
        )

    def field_query(self, requested_name: str, *, natural: bool = False) -> str:
        """Ask for only an omitted operand using its existing source contract."""
        rule = next((item for item in self._rules if item.requested_name == requested_name), None)
        if rule is not None:
            if natural:
                # Source codes and adjustment enums are validation evidence, not
                # necessary language for the provider's natural-language parser.
                params = dict(rule.fixed_params)
                adjustment = {"1": "不复权的", "2": "后复权的"}.get(
                    params.get("AdjustFlag", ""), "",
                )
                remaining = [
                    f"{key}={value}" for key, value in rule.fixed_params
                    if key.casefold() not in {"adjustflag", "curtype", "period"}
                    and not (key == "N" and requested_name.startswith(f"{value}日"))
                ]
                basis = (
                    f"，以{_PRICE_FIELD_NAMES[rule.price_field]}计算"
                    if rule.price_field not in (None, "close") else ""
                )
                suffix = f"（{','.join(remaining)}）" if remaining else ""
                return f"{adjustment}{requested_name}{suffix}{basis}"
            parameters = ",".join(f"{key}={value}" for key, value in rule.fixed_params)
            if rule.price_field is not None:
                parameters += f",PriceField={rule.price_field}"
            adjustment = "后复权" if ("AdjustFlag", "2") in rule.fixed_params else ""
            return f"{adjustment}{requested_name}（{parameters}）"
        return _natural_indicator_query(
            self.indicator_id, self.provider_name or requested_name, (requested_name,),
            dict(self.condition_params),
        )

    def bind_field(self, requested_name: str, field: Mapping[str, object]) -> bool:
        """Accept only the exact provider output and parameter identity expected."""

        fixed = _fixed_params(field.get("fixedParamValue"))
        if fixed is not None and _merge_returned_parameters(field, fixed) is None:
            return False
        if self.provider_name is not None:
            if not self.parameter_request_present:
                return False
            return _bind_discovered_field(
                self.indicator_id, self.provider_name, requested_name, field,
                dict(self.condition_params),
            )

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
        ) and (rule.price_field is None or _ma_price_basis_matches(field, rule.price_field))


def unsupported_indicator_reason(indicator_id: str) -> str | None:
    if indicator_id in PROVIDER_INDICATOR_BUILDERS:
        return None
    return "当前本地服务尚无该指标的取值与比较定义。"


def build_indicator_contract(
    indicator_id: str,
    provider_name: str,
    value_names: tuple[str, ...],
    condition_params: Mapping[str, JsonScalar] | None = None,
) -> MxIndicatorFieldContract:
    """Build the verified query and response contract for one provider binding."""

    reason = unsupported_indicator_reason(indicator_id)
    if reason is not None:
        raise UnsupportedSkillIndicatorError(indicator_id, reason)
    if indicator_id not in ACTIVE_SKILL_INDICATORS:
        params = dict(condition_params or {})
        return MxIndicatorFieldContract(
            indicator_id=indicator_id,
            query_fields=_natural_indicator_query(indicator_id, provider_name, value_names, params),
            _rules=(), provider_name=provider_name,
            condition_params=tuple(sorted(params.items())),
            parameter_request_present=condition_params is not None,
        )
    if indicator_id == "technical.ma":
        match = re.fullmatch(r"([1-9]\d*)日移动平均线", provider_name)
        period = int(match.group(1)) if match else 0
        line = f"{period}日MA简单移动平均"
        price = value_names[0] if value_names else ""
        requested_basis = (condition_params or {}).get("price_field")
        basis = (
            requested_basis if requested_basis is not None
            else next((key for key, value in _PRICE_FIELD_NAMES.items() if value == price), None)
        )
        _require_identity(
            indicator_id, value_names, (price, line),
            valid=period > 0 and isinstance(basis, str)
            and _PRICE_FIELD_NAMES.get(basis) == price,
        )
        assert isinstance(basis, str)
        basis_query = "" if basis == "close" else f"，以{price}计算（PriceField={basis}）"
        return _contract(
            indicator_id,
            f"后复权{price}（AdjustFlag=2,CurType=1,Period=1）、"
            f"后复权{line}（N={period},AdjustFlag=2,period=1）{basis_query}",
            _price_rule(price),
            _FieldRule(
                line, f"MAJDYDPJ{period}", _params(N=period, AdjustFlag=2, period=1),
                allow_generic_ma=True, price_field=basis, raw_unit="元",
            ),
        )
    if indicator_id == "technical.ma_cross":
        match = re.fullmatch(r"([1-9]\d*)日与([1-9]\d*)日移动平均线", provider_name)
        fast, slow = (int(item) for item in match.groups()) if match else (0, 0)
        names = (f"{fast}日MA简单移动平均", f"{slow}日MA简单移动平均")
        basis = (condition_params or {}).get("price_field", "close")
        _require_identity(
            indicator_id, value_names, names,
            valid=0 < fast < slow and isinstance(basis, str) and basis in _PRICE_FIELD_NAMES,
        )
        assert isinstance(basis, str)
        rules = tuple(
            _FieldRule(
                name, f"MAJDYDPJ{period}", _params(N=period, AdjustFlag=2, period=1),
                allow_generic_ma=True, price_field=basis, raw_unit="元",
            )
            for name, period in zip(names, (fast, slow), strict=True)
        )
        query = "、".join(
            f"后复权{name}（N={period},AdjustFlag=2,period=1）"
            for name, period in zip(names, (fast, slow), strict=True)
        )
        if basis != "close":
            query += f"，两条均线均以{_PRICE_FIELD_NAMES[basis]}计算（PriceField={basis}）"
        return _contract(indicator_id, query, *rules)
    if indicator_id == "technical.rsi":
        match = re.fullmatch(r"RSI\(([1-9]\d*)\)", provider_name)
        period = int(match.group(1)) if match else 0
        _require_identity(indicator_id, value_names, ("RSI值",), valid=period > 1)
        return _contract(
            indicator_id,
            f"后复权{period}日RSI相对强弱指标（N={period},AdjustFlag=2,period=1）",
            _FieldRule(
                "RSI值",
                "RSIXDQRZB",
                _params(N=period, AdjustFlag=2, period=1),
                raw_unit="1",
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


# These are data-provider parameter spellings, not a list of admitted metrics or
# a second parser for user language. Unknown parameter names require an explicit
# returned field key/label instead of a guessed mapping.
_PARAMETER_ALIASES = {
    "period": ("n", "lookback", "window"),
    "baseline_period": ("n", "baselineperiod", "window"),
    "fast": ("fast", "short", "n1"), "slow": ("slow", "long", "n2"),
    "signal": ("signal", "m", "n3"),
    "stddev_multiplier": ("stddevmultiplier", "p", "k"),
    "k_smoothing": ("ksmoothing", "m1"), "d_smoothing": ("dsmoothing", "m2"),
    "k_period": ("kperiod", "n"), "d_period": ("dperiod", "m"),
    "price_field": ("pricefield",),
    "constant": ("constant", "c"),
}
_COMPARISON_PARAMETERS = frozenset({
    "consecutive_days", "volume_multiple", "return_threshold_pct",
})
_PRICE_FIELD_NAMES = {"close": "收盘价", "open": "开盘价", "high": "最高价", "low": "最低价"}
# These are current-session observations, not a new indicator inventory. Window
# parameters belong to the other rolling/relative operand in compound recipes.
_WINDOW_INDEPENDENT_FIELDS = frozenset({"成交量", "成交额", "当日涨跌幅"})


def _natural_indicator_query(
    indicator_id: str, provider_name: str, value_names: tuple[str, ...],
    params: Mapping[str, JsonScalar],
) -> str:
    """Describe numeric operands and their periods to the Skill, not an opaque alias.

    Source codes / metadata remain response evidence; the natural-language
    service should not have to infer a 20-day window from our internal JSON.
    """
    fields = "、".join(value_names)
    if indicator_id == "price.rolling_high" and "period" in params:
        period = params["period"]
        details = (
            f"前{period}日的范围为前一交易日向前数{period}个完整交易日，"
            f"取各日{_PRICE_FIELD_NAMES.get(str(params.get('price_field')), '收盘价')}的最大值"
        )
        query = f"{fields}；{details}"
    elif indicator_id in {"volume.relative", "volume.price_confirmation"} and (
        "baseline_period" in params
    ):
        period = params["baseline_period"]
        query = (
            f"{fields}；平均成交量的范围为前一交易日向前数{period}个有成交的交易日，"
            "取每日成交量的算术平均值；各成交量字段单位均为股"
        )
        if indicator_id == "volume.price_confirmation" and "当日涨跌幅" in value_names:
            query += (
                "；当日涨跌幅为当日后复权收盘价相对前一个有成交交易日后复权收盘价的涨跌百分比"
            )
    else:
        descriptions: list[str] = []
        labels = {
            "period": "计算周期", "baseline_period": "成交量基准周期",
            "fast": "快线周期", "slow": "慢线周期", "signal": "信号线周期",
            "k_smoothing": "K值平滑周期", "d_smoothing": "D值平滑周期",
            "k_period": "K线周期", "d_period": "D线周期",
            "stddev_multiplier": "标准差倍数", "constant": "计算常数",
        }
        for name, value in params.items():
            if name in _COMPARISON_PARAMETERS:
                continue
            if name == "price_field":
                descriptions.append(f"以{_PRICE_FIELD_NAMES.get(str(value), str(value))}计算")
            elif name in labels:
                suffix = "个交易日" if "周期" in labels[name] else ""
                descriptions.append(f"{labels[name]}为{value}{suffix}")
            elif re.fullmatch(r"period_\d+", name):
                descriptions.append(f"第{name.rsplit('_', 1)[1]}条均线周期为{value}个交易日")
            else:
                descriptions.append(f"{name}={value}")
        query = f"{provider_name}的{fields}"
        if descriptions:
            query += "；" + "，".join(descriptions)
    query += "。请逐个交易日返回这些字段的历史数值，并保留各字段的名称、单位和参数"
    if _price_based(indicator_id):
        query += "；价格及价格衍生指标统一使用后复权（AdjustFlag=2）"
    return query


def _price_based(indicator_id: str) -> bool:
    return indicator_id.startswith(("technical.", "price.", "volume.price_"))


def _token(value: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKC", value).casefold()
        if char.isalnum() or char in "+-"
    )


def _bind_discovered_field(
    indicator_id: str, provider_name: str, requested_name: str,
    field: Mapping[str, object], requested_params: Mapping[str, JsonScalar],
) -> bool:
    raw_price = {"收盘价": "CLOSE", "开盘价": "OPEN", "最高价": "HIGH", "最低价": "LOW"}
    if requested_name in raw_price:
        raw_fixed = _fixed_params(field.get("fixedParamValue"))
        return (
            field.get("returnSourceCode") == raw_price[requested_name]
            and raw_fixed is not None and raw_fixed.get("adjustflag") == "2"
            and raw_fixed.get("period") == "1"
        )
    labels = tuple(value for key in ("returnName", "returnSourceName", "returnSourceCode")
                   if isinstance(value := field.get(key), str) and value.strip())
    aliases = {_token(label) for label in labels}
    requested = _token(requested_name)
    prior_window = re.fullmatch(
        r"前([1-9]\d*)日(最高(?:收盘价|开盘价|最高价|最低价)|平均成交量)", requested_name,
    )
    if prior_window is not None:
        # A returned Chinese label can state the exact window without an extra
        # N assignment. This is provider metadata, never an echo of query text.
        aliases.update(
            _token(label).replace("个交易日", "日").replace("交易日", "日")
            .replace("的", "").removeprefix("后复权")
            for label in labels
        )
    family = _token(provider_name.split("(", 1)[0])
    if not aliases.intersection({requested, family + requested, requested + family}):
        return False
    # Unqualified K/D/upper/lower labels cannot establish which indicator they
    # belong to. A provider family name or the full requested value label must.
    if requested_name.endswith("值") and family not in requested and not any(
        family in alias for alias in aliases
    ):
        return False
    raw_fixed = field.get("fixedParamValue")
    fixed = _fixed_params(raw_fixed) if raw_fixed is not None else {}
    if fixed is None:
        return False
    actual = _merge_returned_parameters(field, fixed)
    if actual is None:
        return False
    if "period" in actual and actual["period"] != "1":
        return False
    if any(str(field[key]).upper() not in {"DAY", "DAILY", "1D", "D"}
           for key in ("dateGranularity", "frequency", "timeframe") if key in field):
        return False
    volume_operand = requested_name in {"成交量", "成交额"} or (
        prior_window is not None and prior_window.group(2) == "平均成交量"
    )
    if _price_based(indicator_id) and not volume_operand:
        adjustment = actual.get("adjustflag")
        if adjustment is not None and adjustment != "2":
            return False
        if adjustment is None and not any("后复权" in label for label in labels):
            return False
    for name, expected in requested_params.items():
        if requested_name in _WINDOW_INDEPENDENT_FIELDS:
            break
        if name in _COMPARISON_PARAMETERS:
            continue
        indexed_period = re.fullmatch(r"period_(\d+)", name)
        keys = _PARAMETER_ALIASES.get(name, (
            (f"n{indexed_period.group(1)}", _token(name))
            if indexed_period else (_token(name),)
        ))
        candidates = [actual[key] for key in keys if key in actual]
        if candidates:
            if not all(_same_parameter(value, expected) for value in candidates):
                return False
            continue
        if prior_window is not None and requested in aliases:
            if name in {"period", "baseline_period"} and _same_parameter(
                prior_window.group(1), expected,
            ):
                continue
            if name == "price_field" and prior_window.group(2) == (
                f"最高{_PRICE_FIELD_NAMES.get(str(expected), '')}"
            ):
                continue
        # Only an explicit returned parameter assignment proves an otherwise
        # unknown parameter. Query text is intentionally never examined here.
        assignment = re.compile(
            rf"(?<![\w]){re.escape(name)}\s*=\s*{re.escape(str(expected))}(?![\w.])",
            re.IGNORECASE,
        )
        if not any(assignment.search(label) for label in labels):
            return False
    return True


def _merge_returned_parameters(
    field: Mapping[str, object], fixed: Mapping[str, str],
) -> dict[str, str] | None:
    """Keep one value only when every returned representation agrees."""
    returned = tuple(fixed.items()) + tuple(
        (key, str(value)) for key, value in field.items()
        if isinstance(value, str | int | float) and key not in {
            "returnName", "returnSourceName", "returnSourceCode", "fixedParamValue",
        }
    )
    actual: dict[str, str] = {}
    for key, value in returned:
        normalized = _token(key)
        if normalized in actual:
            if not _same_parameter(value, actual[normalized]):
                return None
        else:
            actual[normalized] = value
    return actual


def _same_parameter(actual: str, expected: JsonScalar) -> bool:
    if isinstance(expected, bool):
        return actual.casefold() == str(expected).casefold()
    try:
        left, right = Decimal(actual), Decimal(str(expected))
        return left.is_finite() and right.is_finite() and left == right
    except InvalidOperation:
        return actual.strip().casefold() == str(expected).strip().casefold()


def _ma_price_basis_matches(field: Mapping[str, object], basis: str) -> bool:
    """Non-close MA requires actual source-basis evidence, not the query's intent."""
    params = _fixed_params(field.get("fixedParamValue")) or {}
    stated = [value for key, value in params.items() if _token(key) == "pricefield"]
    stated.extend(str(value) for key, value in field.items() if _token(key) == "pricefield")
    accepted = {basis, _PRICE_FIELD_NAMES[basis]}
    labels = tuple(value for key in ("returnName", "returnSourceName")
                   if isinstance(value := field.get(key), str))
    explicit = {key for key, label in _PRICE_FIELD_NAMES.items()
                if any(label in value for value in labels)}
    if explicit and explicit != {basis}:
        return False
    if stated:
        return all(value.strip().casefold() in accepted for value in stated)
    if explicit:
        return explicit == {basis}
    # The pre-existing MAJDYDPJ contract is a verified close-MA recipe. It is
    # never evidence for an open/high/low MA just because that was requested.
    return basis == "close"


def _price_rule(price: str = "收盘价") -> _FieldRule:
    return _FieldRule(
        price,
        next(key.upper() for key, name in _PRICE_FIELD_NAMES.items() if name == price),
        _params(AdjustFlag=2, CurType=1, Period=1),
        raw_unit="元",
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
        _contract(
            "price.close", "不复权收盘价（AdjustFlag=1,CurType=1,Period=1）",
            _FieldRule("收盘价", "CLOSE", _params(AdjustFlag=1, CurType=1, Period=1),
                       raw_unit="元"),
        ),
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
            _FieldRule("成交额", "AMOUNT", _params(CurType=1, Period=1), raw_unit="元"),
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
