"""Bounded HTTP + business recovery; mock responses, not live acceptance."""

import asyncio
import json

import httpx
import pytest

from ashare_lab.adapters.market_data.mx_saas import (
    MxSaasMarketDataClient,
    MxSaasProviderAuthError,
    MxSaasProviderDataError,
    limit_mx_attempts,
    observe_mx_retries,
)
from ashare_lab.ports.dialogue_progress import progress_sink


def success(channel):
    if channel == "screen":
        return {
            "data": {
                "allResults": {
                    "result": {
                        "columns": [{"field": "code", "displayName": "证券代码"}],
                        "dataList": [{"code": "300059"}],
                    }
                }
            }
        }
    return {"dataTableDTOList": [{"entityCodes": ["300059.SZ"], "rawTable": {"data": [[12.3]]}}]}


async def query(client, channel):
    if channel == "screen":
        return await client.screen(query="沪深A股影视或旅游主营业务相关公司", asset_type="A股")
    return await client.query_finance(query="东方财富的市盈率", indicators="市盈率")


def test_empty_screen_preserves_parsed_conditions_without_exposing_them_as_error():
    condition = "主营产品包含操作系统 且 主营产品包含办公软件"
    client = MxSaasMarketDataClient(api_key="fixture", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"data": {
            "totalCondition": condition, "securityCount": 0,
            "allResults": {"result": {"columns": [], "dataList": []}},
        }}),
    ))
    async def run():
        with limit_mx_attempts(1):
            with pytest.raises(MxSaasProviderDataError) as caught:
                await query(client, "screen")
        assert condition in str(caught.value.screen_conditions)
        assert condition not in str(caught.value)
        assert caught.value.data_reason == "data_no_results"
    asyncio.run(run())


@pytest.mark.parametrize("channel", ["screen", "finance"])
@pytest.mark.parametrize("failure", ["sql", "empty", "non_json", "mixed"])
def test_two_failures_then_success_share_three_attempt_budget(channel, failure):
    requests, delays, progress, events = [], [], [], []

    async def sleep(delay):
        delays.append(delay)

    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) < 3:
            if failure == "mixed" and len(requests) == 1:
                raise httpx.ReadTimeout("private token=secret", request=request)
            if failure in {"sql", "mixed"}:
                return httpx.Response(
                    200, json={"code": 500, "message": "SQL execution failed: secret"}
                )
            if failure == "non_json":
                return httpx.Response(200, text="private broken response")
            return httpx.Response(200, json={"dataTableDTOList": []})
        return httpx.Response(200, json=success(channel))

    client = MxSaasMarketDataClient(
        api_key="secret", transport=httpx.MockTransport(handler), sleeper=sleep
    )
    token = progress_sink.set(lambda stage, message: progress.append((stage, message)))
    try:
        with observe_mx_retries(events.append):
            result = asyncio.run(query(client, channel))
    finally:
        progress_sink.reset(token)
    assert result is not None and len(requests) == 3
    assert delays == [1.0, 2.0]
    assert len({r["query"] for r in requests}) == 1
    assert [e.retry_number for e in events if not e.recovered] == [1, 2]
    assert events[-1].recovered
    assert any("2/2" in message for _, message in progress)
    if failure == "sql":
        assert "服务暂时不稳定" in progress[0][1]
        assert "SQL" not in progress[0][1]
    if failure == "empty":
        assert all("SQL" not in message and "Key" not in message for _, message in progress)
    assert "secret" not in str(progress)


@pytest.mark.parametrize("channel", ["screen", "finance"])
@pytest.mark.parametrize("failure", ["sql", "auth_http", "auth_business"])
def test_exhaustion_is_classified_auth_stops_and_no_raw_details_escape(channel, failure, caplog):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            401 if failure == "auth_http" else 200,
            json={
                "code": 401 if failure == "auth_business" else 500,
                "message": "SQL execution failed with secret-key",
            },
        )

    async def sleep(_delay):
        pass

    client = MxSaasMarketDataClient(
        api_key="secret-key", transport=httpx.MockTransport(handler), sleeper=sleep
    )
    with pytest.raises(
        MxSaasProviderDataError if failure == "sql" else MxSaasProviderAuthError
    ) as caught:
        asyncio.run(query(client, channel))
    assert len(calls) == (3 if failure == "sql" else 1)
    assert caught.value.attempts == len(calls)
    if failure == "sql":
        assert caught.value.data_reason == "provider_sql_error"
        assert "reason=provider_sql_error" in caplog.text
    assert "secret-key" not in str(caught.value) + caplog.text


def test_scoped_single_attempt_does_not_leak_to_later_requests():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"code": 500, "message": "SQL执行失败"})

    async def sleep(_delay):
        pass

    async def scenario():
        client = MxSaasMarketDataClient(
            api_key="test", transport=httpx.MockTransport(handler), sleeper=sleep
        )
        with limit_mx_attempts(1), pytest.raises(MxSaasProviderDataError):
            await query(client, "screen")
        assert len(calls) == 1
        with pytest.raises(MxSaasProviderDataError):
            await query(client, "finance")
        assert len(calls) == 4

    asyncio.run(scenario())
