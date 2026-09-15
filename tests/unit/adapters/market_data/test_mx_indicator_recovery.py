"""Deterministic missing-column recovery; not live supplier acceptance."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import cast

import httpx
import pytest

from ashare_lab.adapters.market_data.mx_saas import MxSaasMarketDataClient, MxSaasProviderDataError
from ashare_lab.adapters.market_data.provider_indicator_cache import (
    FileCachedHistoricalIndicatorData,
)

_CLOSE = {"returnCode": "close", "returnName": "收盘价", "returnSourceCode": "CLOSE",
          "fixedParamValue": "AdjustFlag=2,CurType=1,Period=1", "unitName": "元"}
_MA = {"returnCode": "ma", "returnName": "20日MA简单移动平均",
       "returnSourceCode": "MAJDYDPJ20", "unitName": "元",
       "fixedParamValue": "N=20,period=1,AdjustFlag=2,NewOldType=0"}
_VOLUME = {"returnCode": "volume", "returnName": "成交量", "returnSourceCode": "VOLUME",
           "fixedParamValue": "Period=1", "unitName": "股"}
_AVERAGE = {"returnCode": "average", "returnName": "20日平均成交量",
            "returnSourceCode": "AVG_VOLUME", "fixedParamValue": "N=20,Period=1", "unitName": "股"}
_RELATIVE_AVERAGE = {
    "returnCode": "average", "returnName": "前20日平均成交量",
    "returnSourceCode": "PREVIOUS_AVERAGE_VOLUME", "fixedParamValue": "N=20,Period=1",
    "unitName": "股",
}
_DATES = ("2026-09-02", "2026-09-03")


@pytest.mark.parametrize("repaired", [True, False])
def test_single_operand_wrong_period_receives_bounded_targeted_requery(repaired: bool) -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path.rsplit("/", 1)[-1], json.loads(request.content)["query"]))
        period = 14 if repaired and len(calls) == 2 else 6
        return httpx.Response(200, json=_payload({
            "returnCode": "rsi", "returnName": "RSI相对强弱指标",
            "returnSourceCode": "RSIXDQRZB", "unit": "1",
            "fixedParamValue": f"N={period},period=1,AdjustFlag=2,Period=1,NewOldType=0",
        }))

    client = MxSaasMarketDataClient(api_key="fixture-only", strict_indicator_contracts=True,
                                   transport=httpx.MockTransport(handler))
    request = client.query_indicator_history(
        instrument_id="300059.SZ", indicator_id="technical.rsi", provider_indicator_name="RSI(14)",
        value_names=("RSI值",), condition_params={"period": 14},
        start=date(2026, 9, 2), end=date(2026, 9, 3),
    )
    if repaired:
        result = asyncio.run(request)
        assert len(result.points) == 2
        assert "field_parameter_mismatch" in result.query
    else:
        with pytest.raises(MxSaasProviderDataError) as caught:
            asyncio.run(request)
        assert caught.value.data_reason == "data_field_binding_mismatch"
    assert [channel for channel, _ in calls] == (
        ["searchData", "searchData"] if repaired else ["searchData", "searchData", "selectSecurity"]
    )
    assert "N=14" in calls[1][1]
    assert "2026-09-02至2026-09-03" in calls[1][1]


def _payload(
    *fields: dict[str, str], dates: tuple[str, ...] = _DATES, symbol: str = "300059.SZ",
) -> dict[str, object]:
    return {"code": 200, "dataTableDTOList": [{
        "entityCode": symbol, "dateGranularity": "DAY", "fieldSet": list(fields),
        "rawTable": {"headName": list(dates), **{
            field["returnCode"]: [str(i + 10) for i in range(len(dates))] for field in fields
        }},
    }]}


def _screen_volume_payload(dates: tuple[str, ...]) -> dict[str, object]:
    columns: list[dict[str, str]] = [
        {"key": "SECURITY_CODE", "indexName": "SECURITY_CODE", "title": "证券代码"},
    ]
    row = {"SECURITY_CODE": "300059"}
    for index, day in enumerate(dates):
        for metric, source, title, value in (
            ("current", "VOLUME", "成交量", f"1.{6 + index}万"),
            ("average", "PREVIOUS_AVERAGE_VOLUME", "前20日平均成交量", "1.00万"),
        ):
            key = f"{metric}_{index}"
            columns.append({
                "key": key, "indexName": source, "title": title,
                "dateMsg": day, "unit": "股",
            })
            row[key] = value
    return {"code": 200, "data": {"allResults": {"result": {
        "columns": columns, "dataList": [row],
    }}}}


@pytest.mark.parametrize("case", ["missing_close", "missing_ma", "missing_average"])
def test_only_missing_operand_is_queried_once_and_both_sources_survive_cache(
    case: str, tmp_path: Path,
) -> None:
    fields = (_CLOSE, _MA) if case != "missing_average" else (_VOLUME, _AVERAGE)
    present, missing = (fields[1], fields[0]) if case == "missing_close" else fields
    calls: list[dict[str, object]] = []
    hashes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        response = httpx.Response(200, json=_payload(present if len(calls) == 1 else missing))
        hashes.append("sha256:" + hashlib.sha256(response.content).hexdigest())
        return response

    client = MxSaasMarketDataClient(
        api_key="fixture-only", transport=httpx.MockTransport(handler),
        strict_indicator_contracts=True,
    )
    cache = FileCachedHistoricalIndicatorData(client, root=tmp_path)
    kwargs = {
        "instrument_id": "300059.SZ", "indicator_id": "technical.ma",
        "provider_indicator_name": "20日移动平均线",
        "value_names": ("收盘价", "20日MA简单移动平均"),
        "start": date(2026, 9, 2), "end": date(2026, 9, 3),
    }
    if case == "missing_average":
        kwargs.update({"indicator_id": "market.volume", "provider_indicator_name": "成交量",
                       "value_names": ("成交量", "20日平均成交量"),
                       "condition_params": {"baseline_period": 20}})
    result = asyncio.run(cache.query_indicator_history(**kwargs))
    assert len(calls) == 2
    assert missing["returnName"] in str(calls[1]["query"])
    if case != "missing_average":
        assert present["returnName"] not in str(calls[1]["query"])
    assert "每个交易日" in str(calls[1]["query"])
    assert "2026-09-02至2026-09-03" in str(calls[1]["query"])
    assert tuple(point.session_date.isoformat() for point in result.points) == _DATES
    assert tuple(value.value for value in result.points[-1].values) == (Decimal(11), Decimal(11))
    sources = {value.field_name: json.loads(value.source_parameters or "{}")
               for value in result.points[0].values}
    assert sources[present["returnName"]]["responseSha256"] == hashes[0]
    assert sources[missing["returnName"]]["responseSha256"] == hashes[1]
    assert sources[missing["returnName"]]["fixedParamValue"] == missing["fixedParamValue"]
    assert sources[missing["returnName"]]["query"] == calls[1]["query"]
    reloaded = asyncio.run(FileCachedHistoricalIndicatorData(None, root=tmp_path)
                           .query_indicator_history(**kwargs))
    assert reloaded.cache_status == "disk" and len(calls) == 2
    assert reloaded.points == result.points and reloaded.response_sha256 == result.response_sha256


@pytest.mark.parametrize("failure", ["extra_session", "unit_mismatch", "missing_unit"])
def test_recoverable_history_contract_failures_use_other_skill_once(failure: str) -> None:
    """Invalid primary data is replaced, never relaxed into a valid backtest."""

    channels: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        channel = request.url.path.rsplit("/", 1)[1]
        channels.append(channel)
        volume = dict(_VOLUME)
        if failure == "unit_mismatch":
            volume["unitName"] = "元"
        elif failure == "missing_unit":
            volume.pop("unitName")
        payload = (_payload(
            volume,
            _RELATIVE_AVERAGE,
            dates=("2026-09-04", "2026-09-05") if failure == "extra_session"
            else ("2026-09-04",),
        )
                   if channel == "searchData" else _screen_volume_payload(("2026-09-04",)))
        return httpx.Response(200, json=payload)

    client = MxSaasMarketDataClient(
        api_key="fixture-only", transport=httpx.MockTransport(handler),
        strict_indicator_contracts=True,
    )
    series = asyncio.run(client.query_indicator_history(
        instrument_id="300059.SZ", indicator_id="volume.relative",
        provider_indicator_name="当日成交量与前20日平均成交量",
        value_names=("成交量", "前20日平均成交量"),
        start=date(2026, 9, 4), end=date(2026, 9, 5),
        condition_params={"baseline_period": 20},
        expected_session_dates=(date(2026, 9, 4),),
    ))

    assert channels == ["searchData", "selectSecurity"]
    assert series.provider == "eastmoney_mx_screener"
    assert tuple(point.session_date for point in series.points) == (date(2026, 9, 4),)


def test_missing_sessions_remain_missing_without_retry_or_synthetic_rows() -> None:
    channels: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        channels.append(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(200, json=_payload(
            _VOLUME, _RELATIVE_AVERAGE, dates=("2026-09-02", "2026-09-04"),
        ))

    client = MxSaasMarketDataClient(
        api_key="fixture-only", transport=httpx.MockTransport(handler),
        strict_indicator_contracts=True,
    )
    series = asyncio.run(client.query_indicator_history(
        instrument_id="300059.SZ", indicator_id="volume.relative",
        provider_indicator_name="当日成交量与前20日平均成交量",
        value_names=("成交量", "前20日平均成交量"),
        start=date(2026, 9, 2), end=date(2026, 9, 4),
        condition_params={"baseline_period": 20},
        expected_session_dates=tuple(date(2026, 9, day) for day in (2, 3, 4)),
    ))
    assert channels == ["searchData"]
    assert tuple(point.session_date for point in series.points) == (
        date(2026, 9, 2), date(2026, 9, 4),
    )


def test_both_skills_with_extra_sessions_fail_closed_after_one_recovery() -> None:
    channels: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        channel = request.url.path.rsplit("/", 1)[1]
        channels.append(channel)
        payload = (_payload(_VOLUME, _RELATIVE_AVERAGE,
                            dates=("2026-09-04", "2026-09-05"))
                   if channel == "searchData" else
                   _screen_volume_payload(("2026-09-04", "2026-09-05")))
        return httpx.Response(200, json=payload)

    client = MxSaasMarketDataClient(
        api_key="fixture-only", transport=httpx.MockTransport(handler),
        strict_indicator_contracts=True,
    )
    with pytest.raises(MxSaasProviderDataError) as caught:
        asyncio.run(client.query_indicator_history(
            instrument_id="300059.SZ", indicator_id="volume.relative",
            provider_indicator_name="当日成交量与前20日平均成交量",
            value_names=("成交量", "前20日平均成交量"),
            start=date(2026, 9, 4), end=date(2026, 9, 5),
            condition_params={"baseline_period": 20},
            expected_session_dates=(date(2026, 9, 4),),
        ))

    assert channels == ["searchData", "selectSecurity"]
    assert caught.value.data_reason == "data_dates_mismatch"


@pytest.mark.parametrize("failure,expected_calls", [
    ("wrong_parameters", 3), ("ambiguous", 1), ("all_missing", 2),
    ("still_missing", 3), ("wrong_security", 3), ("different_dates", 2), ("wrong_adjustment", 3),
])
def test_recovery_is_bounded_and_does_not_weaken_source_validation(
    failure: str, expected_calls: int,
) -> None:
    call_ids: list[str] = []
    channels: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        call_ids.append(json.loads(request.content)["toolContext"]["callId"])
        channels.append(request.url.path.rsplit("/", 1)[1])
        if len(call_ids) == 1:
            payload = _payload(_MA)
            if failure == "wrong_parameters":
                payload = _payload(_CLOSE, {**_MA, "fixedParamValue": "N=5,period=1,AdjustFlag=2"})
            elif failure == "ambiguous":
                payload = _payload(_CLOSE, _MA, {**_MA, "returnCode": "second_ma"})
            elif failure == "all_missing":
                payload = _payload(_VOLUME)
        else:
            payload = _payload(_CLOSE)
            if failure == "still_missing":
                payload = _payload(_VOLUME)
            elif failure == "wrong_security":
                payload = _payload(_CLOSE, symbol="600519.SH")
            elif failure == "different_dates":
                payload = _payload(_CLOSE, dates=("2026-09-03",))
            elif failure == "wrong_adjustment":
                payload = _payload({**_CLOSE, "fixedParamValue": "AdjustFlag=1,CurType=1,Period=1"})
        return httpx.Response(200, json=payload)

    client = MxSaasMarketDataClient(
        api_key="fixture-only", transport=httpx.MockTransport(handler),
        strict_indicator_contracts=True,
    )
    with pytest.raises(MxSaasProviderDataError) as caught:
        asyncio.run(client.query_indicator_history(
            instrument_id="300059.SZ", indicator_id="technical.ma",
            provider_indicator_name="20日移动平均线", value_names=("收盘价", "20日MA简单移动平均"),
            start=date(2026, 9, 2), end=date(2026, 9, 3),
        ))
    assert len(call_ids) == expected_calls
    if failure == "wrong_security":
        assert channels == ["searchData"] * 3
        assert caught.value.data_reason == "data_security_mismatch"
        assert caught.value.attempts == 2
    elif expected_calls == 3:
        assert channels == ["searchData", "searchData", "selectSecurity"]
        # An unusable alternate channel must not mask the original actionable
        # field mismatch or make bad values usable merely by trying again.
        assert caught.value.call_id == call_ids[-2]
        assert caught.value.data_reason == "data_field_binding_mismatch"
    elif failure == "all_missing":
        assert channels == ["searchData", "selectSecurity"]
        assert caught.value.call_id == call_ids[0]
        assert caught.value.data_reason == "data_fields_missing"
    else:
        assert channels == ["searchData"] * expected_calls
        assert caught.value.call_id == call_ids[-1]


@pytest.mark.parametrize("recovered", [True, False])
def test_wrong_stock_response_retries_once_without_using_its_values(recovered: bool) -> None:
    calls = []
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        wrong = len(calls) == 1 or not recovered
        return httpx.Response(200, json=_payload(
            _CLOSE, _MA, symbol="600519.SH" if wrong else "300059.SZ",
        ))
    client = MxSaasMarketDataClient(
        api_key="fixture-only", transport=httpx.MockTransport(handler),
        strict_indicator_contracts=True,
    )
    request = client.query_indicator_history(
        instrument_id="300059.SZ", indicator_id="technical.ma",
        provider_indicator_name="20日移动平均线", value_names=("收盘价", "20日MA简单移动平均"),
        start=date(2026, 9, 2), end=date(2026, 9, 3),
    )
    if recovered:
        series = asyncio.run(request)
        assert series.instrument_id == "300059.SZ"
    else:
        with pytest.raises(MxSaasProviderDataError) as caught:
            asyncio.run(request)
        assert caught.value.data_reason == "data_security_mismatch"
    assert len(calls) == 2
    assert calls[0]["query"] == calls[1]["query"]
    assert calls[0]["toolContext"]["callId"] != calls[1]["toolContext"]["callId"]


@pytest.mark.parametrize("close_adjustment,ma_adjustment", [(2, 2), (1, 3), (2, 3), (1, 2)])
def test_same_axis_split_response_preserves_each_fields_actual_adjustment(
    close_adjustment: int, ma_adjustment: int,
) -> None:
    """Mirror the real two-table shape; wrong provider flags stay rejected."""
    requests: list[str] = []
    channels: list[str] = []
    fields = (
        {**_CLOSE, "fixedParamValue": f"AdjustFlag={close_adjustment},CurType=1,Period=1"},
        {**_MA, "fixedParamValue": f"N=20,period=1,AdjustFlag={ma_adjustment},NewOldType=0"},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(json.loads(request.content)["query"]))
        channels.append(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(200, json={"code": 200, "dataTableDTOList": [
            cast(list[object], _payload(field)["dataTableDTOList"])[0] for field in fields
        ]})

    client = MxSaasMarketDataClient(
        api_key="fixture-only", transport=httpx.MockTransport(handler),
        strict_indicator_contracts=True,
    )
    response = client.query_indicator_history(
        instrument_id="300059.SZ", indicator_id="technical.ma",
        provider_indicator_name="20日移动平均线", value_names=("收盘价", "20日MA简单移动平均"),
        start=date(2026, 9, 2), end=date(2026, 9, 3),
    )
    if close_adjustment == ma_adjustment == 2:
        result = asyncio.run(response)
        assert len(result.points[0].values) == 2
        assert {value.source_parameters for value in result.points[0].values} == {
            field["fixedParamValue"] for field in fields
        }
    else:
        with pytest.raises(MxSaasProviderDataError) as caught:
            asyncio.run(response)
        assert caught.value.data_reason == "data_field_binding_mismatch"
    assert channels == (["searchData"] if close_adjustment == ma_adjustment == 2
                        else ["searchData", "searchData", "selectSecurity"])
    assert "后复权收盘价" in requests[0]
    assert "后复权20日MA简单移动平均" in requests[0]


def test_misparameterised_compound_query_recovers_each_operand_without_relaxing_contract() -> None:
    queries: list[str] = []
    hashes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(str(json.loads(request.content)["query"]))
        if len(queries) == 1:
            payload = _payload(
                {**_CLOSE, "fixedParamValue": "AdjustFlag=1,CurType=1,Period=1"},
                {**_MA, "fixedParamValue": "N=20,AdjustFlag=3,period=1"},
            )
        else:
            expected = _CLOSE if len(queries) == 2 else _MA
            assert expected["returnName"] in queries[-1]
            payload = _payload(expected)
        response = httpx.Response(200, json=payload)
        hashes.append("sha256:" + hashlib.sha256(response.content).hexdigest())
        return response

    client = MxSaasMarketDataClient(
        api_key="fixture-only", transport=httpx.MockTransport(handler),
        strict_indicator_contracts=True,
    )
    series = asyncio.run(client.query_indicator_history(
        instrument_id="300059.SZ", indicator_id="technical.ma",
        provider_indicator_name="20日移动平均线", value_names=("收盘价", "20日MA简单移动平均"),
        start=date(2026, 9, 2), end=date(2026, 9, 3),
    ))
    assert len(queries) == 3
    assert len(series.points) == 2
    for i, value in enumerate(series.points[0].values):
        source = json.loads(value.source_parameters or "{}")
        assert "AdjustFlag=2" in source["fixedParamValue"]
        assert source["retryReason"] == "field_parameter_mismatch"
        assert source["responseSha256"] == hashes[i + 1]
        assert source["initialResponse"]["responseSha256"] == hashes[0]


@pytest.mark.parametrize("wrong_target_period", [False, True])
def test_custom_ma_source_alias_recovers_only_a_truly_missing_other_operand(
    wrong_target_period: bool,
) -> None:
    queries: list[str] = []

    def field(period: int, *, actual_period: int | None = None) -> dict[str, str]:
        return {
            "returnCode": f"ma{period}", "returnName": f"{period}日MA简单移动平均",
            "returnSourceCode": "MAJDYDPJ", "unitName": "元",
            "fixedParamValue": (
                f"N={actual_period or period},Period=1,AdjustFlag=2,NewOldType=0"
            ),
        }

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(str(json.loads(request.content)["query"]))
        if len(queries) > 1:
            return httpx.Response(200, json=_payload(field(7)))
        fields = (field(3), field(7, actual_period=3)) if wrong_target_period else (field(3),)
        return httpx.Response(200, json=_payload(*fields))

    client = MxSaasMarketDataClient(
        api_key="fixture-only", transport=httpx.MockTransport(handler),
        strict_indicator_contracts=True,
    )
    response = client.query_indicator_history(
        instrument_id="300059.SZ", indicator_id="technical.ma_cross",
        provider_indicator_name="3日与7日移动平均线",
        value_names=("3日MA简单移动平均", "7日MA简单移动平均"),
        start=date(2026, 9, 2), end=date(2026, 9, 3),
    )
    if wrong_target_period:
        with pytest.raises(MxSaasProviderDataError) as caught:
            asyncio.run(response)
        assert caught.value.data_reason == "data_field_binding_mismatch"
        assert len(queries) == 1
    else:
        result = asyncio.run(response)
        assert len(queries) == 2 and "7日MA简单移动平均" in queries[1]
        assert "3日MA简单移动平均" not in queries[1]
        sources = {value.field_name: json.loads(value.source_parameters or "{}")
                   for value in result.points[0].values}
        assert sources["3日MA简单移动平均"]["fixedParamValue"] == field(3)["fixedParamValue"]
        assert sources["7日MA简单移动平均"]["fixedParamValue"] == field(7)["fixedParamValue"]
