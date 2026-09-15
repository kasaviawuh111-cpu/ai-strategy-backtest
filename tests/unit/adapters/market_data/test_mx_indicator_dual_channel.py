"""Both Skill routes may return data; only actual dated fields become history."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import date
from decimal import Decimal

import httpx
import pytest

from ashare_lab.adapters.market_data.mx_saas import MxSaasMarketDataClient, MxSaasProviderDataError

_DATES = ("2026-09-02", "2026-09-03")


def _finance_missing_average() -> dict[str, object]:
    return {"code": 200, "dataTableDTOList": [{
        "entityCode": "300059.SZ", "dateGranularity": "DAY",
        "fieldSet": [{"returnCode": "volume", "returnName": "成交量",
                      "returnSourceCode": "VOLUME", "fixedParamValue": "Period=1", "unit": "股"}],
        "rawTable": {"headName": list(_DATES), "volume": ["10", "11"]},
    }]}


def _screen_payload(case: str) -> dict[str, object]:
    columns: list[dict[str, str]] = [
        {"key": "SECURITY_CODE", "indexName": "SECURITY_CODE", "title": "证券代码"},
    ]
    row = {"SECURITY_CODE": "300059"}
    for i, day in enumerate(_DATES):
        for metric, title, index_name, value in (
            ("current", "成交量", "VOLUME", f"1.{6 + i}万"),
            ("average", "前5日平均成交量" if case == "wrong_period" else "前20日平均成交量",
             "PREVIOUS_AVERAGE_VOLUME", "1.00万"),
        ):
            key = f"{metric}_{i}"
            columns.append({"key": key, "indexName": index_name, "title": title,
                            "dateMsg": "2026-09-02 - 2026-09-03"
                            if case == "interval_summary" else day, "unit": "股"})
            row[key] = value
    return {"code": 200, "data": {"allResults": {"result": {
        "columns": columns, "dataList": [row],
    }}}}


@pytest.mark.parametrize("case", ["correct", "wrong_period", "interval_summary"])
def test_dated_screener_result_can_complete_history_without_inventing_parameters(case: str) -> None:
    calls: list[tuple[str, str]] = []
    hashes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        channel = request.url.path.rsplit("/", 1)[1]
        query = str(json.loads(request.content)["query"])
        calls.append((channel, query))
        payload = (_screen_payload(case) if channel == "selectSecurity"
                   else _finance_missing_average())
        response = httpx.Response(200, json=payload)
        hashes.append("sha256:" + hashlib.sha256(response.content).hexdigest())
        return response

    client = MxSaasMarketDataClient(
        api_key="fixture-only", transport=httpx.MockTransport(handler),
        strict_indicator_contracts=True,
    )
    request = client.query_indicator_history(
        instrument_id="300059.SZ", indicator_id="volume.relative",
        provider_indicator_name="当日成交量与前20日平均成交量",
        value_names=("成交量", "前20日平均成交量"),
        condition_params={"baseline_period": 20, "consecutive_days": 3},
        start=date(2026, 9, 2), end=date(2026, 9, 3),
    )
    if case == "correct":
        series = asyncio.run(request)
        assert series.provider == "eastmoney_mx_screener"
        assert series.response_sha256 == hashes[-1]
        assert series.query == calls[-1][1]
        assert tuple(point.session_date.isoformat() for point in series.points) == _DATES
        assert tuple(tuple(value.value for value in point.values) for point in series.points) == (
            (Decimal(16000), Decimal(10000)), (Decimal(17000), Decimal(10000)),
        )
        assert all(value.unit == "股" and value.source_parameters is None
                   for point in series.points for value in point.values)
        assert series.points[0].values[1].source_field_name == "前20日平均成交量"
    else:
        with pytest.raises(MxSaasProviderDataError) as caught:
            asyncio.run(request)
        assert caught.value.data_reason == "data_field_binding_mismatch"
    assert [channel for channel, _ in calls] == ["searchData", "searchData", "selectSecurity"]
    assert calls[-1][1] == calls[0][1]
    assert all("前20日平均成交量" in query and "2026-09-02至2026-09-03" in query
               for _, query in calls)


@pytest.mark.parametrize("first_shape", ["interval", "unrelated_fields"])
def test_missing_daily_data_retries_another_skill_without_broadcasting_summary(
    first_shape: str,
) -> None:
    channels: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        channel = request.url.path.rsplit("/", 1)[-1]
        channels.append(channel)
        if channel == "selectSecurity":
            return httpx.Response(200, json=_screen_payload("correct"))
        return httpx.Response(200, json={"code": 200, "dataTableDTOList": [{
            "entityCode": "300059.SZ",
            "fieldSet": [{"returnCode": "v", "returnName": "成交量"
                          if first_shape == "interval" else "无关字段",
                          "returnSourceCode": "VOLUME" if first_shape == "interval" else "OTHER",
                          "fixedParamValue": "Period=1", "unitName": "股"}],
            "rawTable": {"headName": ["2026-09-02至2026-09-03"]
                         if first_shape == "interval" else list(_DATES),
                         "v": [999999] if first_shape == "interval" else [999999, 999999]},
        }]})

    client = MxSaasMarketDataClient(
        api_key="fixture-only", strict_indicator_contracts=True,
        transport=httpx.MockTransport(handler),
    )
    series = asyncio.run(client.query_indicator_history(
        instrument_id="300059.SZ", indicator_id="volume.relative",
        provider_indicator_name="当日成交量与前20日平均成交量",
        value_names=("成交量", "前20日平均成交量"),
        condition_params={"baseline_period": 20},
        start=date(2026, 9, 2), end=date(2026, 9, 3),
    ))
    assert channels == ["searchData", "selectSecurity"]
    assert series.provider == "eastmoney_mx_screener"
    assert [p.session_date.isoformat() for p in series.points] == list(_DATES)
    assert [p.values[0].value for p in series.points] == [Decimal(16000), Decimal(17000)]
