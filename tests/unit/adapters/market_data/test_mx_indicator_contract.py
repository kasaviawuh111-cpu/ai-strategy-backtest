import pytest

from ashare_lab.adapters.market_data.mx_indicator_contract import build_indicator_contract


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
