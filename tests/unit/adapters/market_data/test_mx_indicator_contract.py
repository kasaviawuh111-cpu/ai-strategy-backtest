import pytest

from ashare_lab.adapters.market_data.mx_indicator_contract import (
    build_indicator_contract,
    unsupported_indicator_reason,
)
from ashare_lab.domain.signals.provider_catalog import provider_binding_for_condition
from ashare_lab.domain.strategy import IndicatorCondition
from ashare_lab.domain.strategy.models import JsonScalar


def test_real_ma_cross_and_rsi_field_contracts_reject_wrong_rsi_parameters() -> None:
    ma = build_indicator_contract(
        "technical.ma_cross",
        "5日与20日移动平均线",
        ("5日MA简单移动平均", "20日MA简单移动平均"),
    )
    assert ma.bind_field(
        "5日MA简单移动平均",
        {
            "returnName": "5日MA简单移动平均",
            "returnSourceCode": "MAJDYDPJ5",
            "fixedParamValue": "N=5,period=1,AdjustFlag=2,NewOldType=0",
        },
    )
    assert ma.bind_field(
        "20日MA简单移动平均",
        {
            "returnName": "20日MA简单移动平均",
            "returnSourceCode": "MAJDYDPJ20",
            "fixedParamValue": "N=20,period=1,AdjustFlag=2,NewOldType=0",
        },
    )

    rsi = build_indicator_contract("technical.rsi", "RSI(14)", ("RSI值",))
    assert rsi.bind_field(
        "RSI值",
        {
            "returnName": "RSI相对强弱指标",
            "returnSourceCode": "RSIXDQRZB",
            "fixedParamValue": "period=1,N=14,AdjustFlag=2,Period=1,NewOldType=0",
        },
    )
    assert not rsi.bind_field(
        "RSI值",
        {
            "returnName": "RSI相对强弱指标",
            "returnSourceCode": "RSIXDQRZB",
            "fixedParamValue": "N=6,AdjustFlag=1,period=1",
        },
    )


@pytest.mark.parametrize("adjustment", [1, 2, 3])
def test_absolute_close_threshold_uses_raw_price_while_ma_keeps_adjusted_price(
    adjustment: int,
) -> None:
    close = build_indicator_contract("price.close", "收盘价", ("收盘价",))
    ma = build_indicator_contract(
        "technical.ma", "20日移动平均线", ("收盘价", "20日MA简单移动平均"),
    )
    field = {
        "returnName": "收盘价", "returnSourceCode": "CLOSE",
        "fixedParamValue": f"AdjustFlag={adjustment},CurType=1,Period=1",
    }
    assert "不复权收盘价" in close.query_fields
    assert close.bind_field("收盘价", field) is (adjustment == 1)
    assert ma.bind_field("收盘价", field) is (adjustment == 2)


@pytest.mark.parametrize("period", [3, 7])
@pytest.mark.parametrize("indicator", ["technical.ma", "technical.ma_cross"])
def test_generic_ma_alias_retains_exact_custom_period(period: int, indicator: str) -> None:
    line = f"{period}日MA简单移动平均"
    provider_name = (
        f"{period}日移动平均线" if indicator == "technical.ma"
        else f"{period}日与10日移动平均线"
    )
    names = ("收盘价", line) if indicator == "technical.ma" else (line, "10日MA简单移动平均")
    contract = build_indicator_contract(indicator, provider_name, names)
    assert contract.bind_field(line, {
        "returnSourceCode": "MAJDYDPJ",
        "fixedParamValue": f"N={period},period=1,AdjustFlag=2,NewOldType=0",
    })
    if indicator == "technical.ma_cross":
        assert contract.bind_field("10日MA简单移动平均", {
            "returnSourceCode": "MAJDYDPJ10",
            "fixedParamValue": "N=10,period=1,AdjustFlag=2,NewOldType=0",
        })


@pytest.mark.parametrize("parameters", [
    "N=5,period=1,AdjustFlag=2,NewOldType=0",
    "N=3,period=5,AdjustFlag=2,NewOldType=0",
    "N=3,period=1,AdjustFlag=1,NewOldType=0",
    "N=3,period=1,AdjustFlag=2,NewOldType=1",
    "N=3,period=1,AdjustFlag=2",
    "period=1,AdjustFlag=2,NewOldType=0",
    "N=3,N=5,period=1,AdjustFlag=2,NewOldType=0",
])
def test_generic_ma_alias_rejects_missing_or_different_parameters(parameters: str) -> None:
    contract = build_indicator_contract(
        "technical.ma_cross", "3日与10日移动平均线",
        ("3日MA简单移动平均", "10日MA简单移动平均"),
    )
    assert not contract.bind_field("3日MA简单移动平均", {
        "returnSourceCode": "MAJDYDPJ", "fixedParamValue": parameters,
    })


def test_generic_ma_alias_does_not_accept_other_source_codes_or_other_indicators() -> None:
    ma = build_indicator_contract(
        "technical.ma", "3日移动平均线", ("收盘价", "3日MA简单移动平均"),
    )
    for code in ("MAJDYDPJ5", "EMA", "MAJDYDPJ_UNKNOWN"):
        assert not ma.bind_field("3日MA简单移动平均", {
            "returnSourceCode": code,
            "fixedParamValue": "N=3,period=1,AdjustFlag=2,NewOldType=0",
        })
    assert not ma.bind_field("收盘价", {
        "returnSourceCode": "MAJDYDPJ",
        "fixedParamValue": "CurType=1,Period=1,AdjustFlag=2,NewOldType=0",
    })
    rsi = build_indicator_contract("technical.rsi", "RSI(14)", ("RSI值",))
    assert not rsi.bind_field("RSI值", {
        "returnSourceCode": "MAJDYDPJ",
        "fixedParamValue": "N=14,period=1,AdjustFlag=2,NewOldType=0",
    })


def test_old_probe_failure_does_not_prevent_new_macd_query_or_bind_actual_metadata() -> None:
    assert unsupported_indicator_reason("technical.macd") is None
    contract = build_indicator_contract(
        "technical.macd", "MACD(12,26,9)", ("DIF值", "DEA值"),
        condition_params={"fast": 12, "slow": 26, "signal": 9},
    )
    assert "12" in contract.query_fields and "AdjustFlag=2" in contract.query_fields
    field = {
        "returnName": "MACD(DIF值)", "returnSourceCode": "MACD_DIF",
        "fixedParamValue": "N1=12,N2=26,M=9,AdjustFlag=2,Period=1",
    }
    assert contract.bind_field("DIF值", field)
    assert not contract.bind_field("DEA值", field)
    for fixed in (
        "N1=12,N2=26,M=9,AdjustFlag=1,Period=1",
        "N1=5,N2=26,M=9,AdjustFlag=2,Period=1",
        "N1=12,N2=26,M=9,AdjustFlag=2,Period=5",
        "N1=12,N2=26,M=9,Period=1", "AdjustFlag=2,Period=1",
    ):
        assert not contract.bind_field("DIF值", {**field, "fixedParamValue": fixed})


def test_returned_parameters_not_request_echo_establish_custom_metric_period() -> None:
    contract = build_indicator_contract(
        "technical.atr", "ATR(17)", ("ATR值",), condition_params={"period": 17},
    )
    field = {"returnName": "ATR值", "fixedParamValue": "AdjustFlag=2,Period=1"}
    assert not contract.bind_field("ATR值", field)
    assert not contract.bind_field("ATR值", {**field, "N": 14})
    assert contract.bind_field("ATR值", {**field, "N": 17})
    assert not contract.bind_field("ATR值", {**field, "query": contract.query_fields})
    # Explicit parameter assignments in actual field metadata are accepted, but
    # a different unrelated field cannot acquire identity from such assignments.
    assert contract.bind_field("ATR值", {**field, "returnSourceName": "ATR period=17"})
    assert not contract.bind_field("ATR值", {**field, "returnName": "收盘价", "N": 17})


def test_parameterized_indicator_without_thread_parameters_cannot_silently_default() -> None:
    contract = build_indicator_contract("technical.atr", "ATR(17)", ("ATR值",))
    assert "ATR(17)" in contract.query_fields
    assert not contract.bind_field("ATR值", {
        "returnName": "ATR值", "fixedParamValue": "N=17,AdjustFlag=2,Period=1",
    })


@pytest.mark.parametrize("fixed,extra,expected", [
    ("N=14,AdjustFlag=2,Period=1", {"N": 17}, False),
    ("N=17,AdjustFlag=1,Period=1", {"AdjustFlag": 2}, False),
    ("N=17,AdjustFlag=2,Period=1", {"N": "17.0", "AdjustFlag": 2, "Period": 1.0}, True),
])
def test_discovered_field_rejects_conflicting_metadata_without_rejecting_equivalent_duplicates(
    fixed: str, extra: dict[str, str | int | float], expected: bool,
) -> None:
    contract = build_indicator_contract(
        "technical.atr", "ATR(17)", ("ATR值",), condition_params={"period": 17},
    )
    assert contract.bind_field("ATR值", {
        "returnName": "ATR值", "fixedParamValue": fixed, **extra,
    }) is expected


@pytest.mark.parametrize("indicator,provider,values,adjustment,params", [
    ("price.close", "收盘价", ("收盘价",), 1, {}),
    ("technical.ma", "20日移动平均线", ("收盘价", "20日MA简单移动平均"), 2, {}),
    ("technical.ema", "EMA(20)", ("收盘价", "EMA值"), 2, {"period": 20}),
])
def test_all_price_field_branches_reject_conflicts_and_still_require_fixed_parameters(
    indicator: str, provider: str, values: tuple[str, ...], adjustment: int,
    params: dict[str, int],
) -> None:
    contract = build_indicator_contract(indicator, provider, values, condition_params=params)
    field = {
        "returnName": "收盘价", "returnSourceCode": "CLOSE",
        "fixedParamValue": f"AdjustFlag={adjustment},CurType=1,Period=1",
    }
    assert contract.bind_field("收盘价", {**field, "AdjustFlag": float(adjustment), "Period": 1.0})
    assert not contract.bind_field("收盘价", {**field, "AdjustFlag": 3})
    assert not contract.bind_field("收盘价", {**field, "Period": 5})
    assert not contract.bind_field("收盘价", {
        "returnName": "收盘价", "returnSourceCode": "CLOSE", "AdjustFlag": adjustment,
        "CurType": 1, "Period": 1,
    })


@pytest.mark.parametrize("basis,price", [("open", "开盘价"), ("high", "最高价"), ("low", "最低价")])
@pytest.mark.parametrize("indicator", ["technical.ma", "technical.ma_cross"])
def test_nonclose_ma_queries_and_binds_actual_underlying_price_basis(
    basis: str, price: str, indicator: str,
) -> None:
    name = "3日移动平均线" if indicator == "technical.ma" else "3日与10日移动平均线"
    line = "3日MA简单移动平均"
    values = (price, line) if indicator == "technical.ma" else (line, "10日MA简单移动平均")
    contract = build_indicator_contract(
        indicator, name, values, condition_params={"price_field": basis},
    )
    assert f"PriceField={basis}" in contract.query_fields
    field = {
        "returnSourceCode": "MAJDYDPJ3", "returnName": line,
        "fixedParamValue": "N=3,Period=1,AdjustFlag=2",
    }
    assert not contract.bind_field(line, field)
    assert not contract.bind_field(line, {**field, "PriceField": "close"})
    assert contract.bind_field(line, {**field, "PriceField": basis})
    assert contract.bind_field(line, {**field, "returnSourceName": f"以{price}计算的均线"})
    assert not contract.bind_field(line, {
        **field, "PriceField": basis, "returnSourceName": "以收盘价计算的均线",
    })
    if indicator == "technical.ma":
        raw = {
            "returnSourceCode": basis.upper(), "fixedParamValue": "AdjustFlag=2,CurType=1,Period=1",
        }
        assert contract.bind_field(price, raw)
        assert not contract.bind_field(price, {**raw, "returnSourceCode": "CLOSE"})


def test_volume_window_is_checked_on_average_not_raw_current_session_volume() -> None:
    contract = build_indicator_contract(
        "market.volume", "成交量与20日平均成交量", ("成交量", "20日平均成交量"),
        condition_params={"baseline_period": 20},
    )
    raw = {"returnName": "成交量", "returnSourceCode": "VOLUME", "fixedParamValue": "Period=1"}
    average = {"returnName": "20日平均成交量", "fixedParamValue": "N=20,Period=1"}
    assert contract.bind_field("成交量", raw)
    assert not contract.bind_field("成交量", {**raw, "fixedParamValue": "Period=5"})
    assert contract.bind_field("20日平均成交量", average)
    assert not contract.bind_field("20日平均成交量", {**average, "fixedParamValue": "Period=1"})
    assert not contract.bind_field("20日平均成交量", {
        **average, "fixedParamValue": "N=5,Period=1",
    })


@pytest.mark.parametrize("period", [15, 20, 60])
@pytest.mark.parametrize("indicator,basis,trigger", [
    ("price.rolling_high", "close", "new_high"),
    ("price.rolling_high", "high", "new_high"),
    ("volume.relative", None, "gt_multiple"),
])
def test_window_operand_query_and_actual_label_preserve_natural_language_meaning(
    period: int, indicator: str, basis: str | None, trigger: str,
) -> None:
    params: dict[str, JsonScalar] = (
        {"period": period, "price_field": basis} if basis is not None
        else {"baseline_period": period, "consecutive_days": 3}
    )
    condition = IndicatorCondition(
        indicator_id=indicator, definition_version="1.0.0", params=params,
        trigger=trigger, value=None if basis is not None else 1.5,
    )
    binding = provider_binding_for_condition(condition)
    contract = build_indicator_contract(
        indicator, binding.provider_indicator_name, binding.value_names, condition_params=params,
    )
    operand = binding.value_names[1]
    for query in (contract.query_fields, contract.field_query(operand, natural=True)):
        assert operand in query
        assert f"前一交易日向前数{period}个" in query
        assert "近期创阶段新高" not in query and "量比" not in query
        assert "策略取值参数为{" not in query
    field = {
        "returnName": operand,
        "fixedParamValue": "AdjustFlag=2,Period=1" if basis is not None else "Period=1",
        "dateGranularity": "DAY",
    }
    # The returned field states both period and prior-window convention. No
    # redundant N/price_field assignment is needed to prove that same identity.
    assert contract.bind_field(operand, field)
    assert contract.bind_field(operand, {
        **field, "returnName": operand.replace("日", "个交易日的"),
    })
    assert not contract.bind_field(operand, {**field, "N": 5})
    assert not contract.bind_field(operand, {
        **field, "returnName": operand.replace(f"前{period}", "前5"),
    })
    assert not contract.bind_field(operand, {
        **field, "returnName": operand.removeprefix("前"),
    })
    assert not contract.bind_field(operand, {
        **field, "returnName": "近期创阶段新高" if basis is not None else "量比", "N": period,
    })
    if basis is not None:
        assert not contract.bind_field(operand, {**field, "PriceField": "open"})
        assert not contract.bind_field(operand, {
            **field, "fixedParamValue": "AdjustFlag=1,Period=1",
        })


def test_dynamic_query_describes_parameters_without_internal_json_only_recipe() -> None:
    contract = build_indicator_contract(
        "technical.macd", "MACD(5,35,7)", ("DIF值", "DEA值"),
        condition_params={"fast": 5, "slow": 35, "signal": 7},
    )
    assert "快线周期为5个交易日" in contract.query_fields
    assert "慢线周期为35个交易日" in contract.query_fields
    assert "信号线周期为7个交易日" in contract.query_fields
    assert "后复权" in contract.query_fields


def test_volume_price_confirmation_reuses_average_without_price_checks_on_share_units() -> None:
    condition = IndicatorCondition(
        indicator_id="volume.price_confirmation", definition_version="1.0.0", trigger="surge_up",
        params={"baseline_period": 15, "volume_multiple": 2, "return_threshold_pct": 3},
    )
    binding = provider_binding_for_condition(condition)
    contract = build_indicator_contract(
        condition.indicator_id, binding.provider_indicator_name, binding.value_names,
        condition_params=condition.params,
    )
    assert binding.value_names == ("成交量", "前15日平均成交量", "当日涨跌幅")
    assert "前一交易日向前数15个有成交的交易日" in contract.query_fields
    assert "前一个有成交交易日后复权收盘价" in contract.query_fields
    assert "相对成交量" not in contract.query_fields
    for name in ("成交量", "前15日平均成交量"):
        assert contract.bind_field(name, {
            "returnName": name, "fixedParamValue": "Period=1", "unit": "股",
        })
    assert not contract.bind_field("前15日平均成交量", {
        "returnName": "前15日平均成交量", "fixedParamValue": "N=5,Period=1", "unit": "股",
    })
    assert contract.bind_field("当日涨跌幅", {
        "returnName": "当日涨跌幅", "fixedParamValue": "AdjustFlag=2,Period=1", "unit": "%",
    })
    assert not contract.bind_field("当日涨跌幅", {
        "returnName": "当日涨跌幅", "fixedParamValue": "AdjustFlag=1,Period=1", "unit": "%",
    })
