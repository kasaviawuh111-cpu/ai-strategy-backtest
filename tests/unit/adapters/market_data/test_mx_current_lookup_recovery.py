"""Bounded current-data recovery; these fixtures are not live supplier evidence."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re

import httpx
import pytest

from ashare_lab.adapters.market_data.mx_saas import (
    MxSaasMarketDataClient,
    MxSaasProviderAuthError,
    MxSaasProviderDataError,
    MxSaasProviderUnavailableError,
)


def _screen(codes: list[str], *, metric: bool = False) -> dict[str, object]:
    columns = [{"field": "code", "displayName": "证券代码"},
               {"field": "name", "displayName": "证券简称"}]
    if metric:
        columns.append({"field": "pe", "displayName": "市盈率", "unit": "倍"})
    return {"code": 200, "data": {"allResults": {"result": {
        "columns": columns,
        "dataList": [{"code": code, "name": "股票" + code, "pe": "12.3"} for code in codes],
    }}}}


@pytest.mark.parametrize("first_failure", ["empty", "timeout"])
def test_only_failed_batch_changes_channel_and_completed_screen_is_preserved(
    first_failure: str,
) -> None:
    codes = [f"{600000 + i:06d}" for i in range(10)]
    calls: list[tuple[str, str]] = []
    alternate_hash = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal alternate_hash
        query = str(json.loads(request.content)["query"])
        channel = request.url.path.rsplit("/", 1)[-1]
        calls.append((channel, query))
        if query == "筛选十只股票":
            return httpx.Response(200, json=_screen(codes))
        batch = re.findall(r"\((\d{6})\)", query)
        if channel == "selectSecurity":
            response = httpx.Response(200, json=_screen(batch, metric=True))
            alternate_hash = "sha256:" + hashlib.sha256(response.content).hexdigest()
            return response
        if codes[5] in batch:
            if first_failure == "timeout":
                raise httpx.ReadTimeout("fixture timeout", request=request)
            return httpx.Response(200, json={"code": 200, "dataTableDTOList": []})
        return httpx.Response(200, json={"code": 200, "dataTableDTOList": [{
            "entityCodes": batch, "columns": ["证券代码", "市盈率"],
            "rows": [[code, 12.3] for code in batch],
        }]})

    client = MxSaasMarketDataClient(
        api_key="fixture", transport=httpx.MockTransport(handler), max_attempts=1,
    )
    result = asyncio.run(client.screen_then_query_finance(
        screening_query="筛选十只股票", asset_type="A股", indicators="最新市盈率",
    ))
    assert [entity.code for entity in result.entities] == codes
    assert len(result.batches) == 2 and len(calls) == 4
    assert sum(query == "筛选十只股票" for _, query in calls) == 1
    assert result.batches[0].provider == "eastmoney_mx_finance_data"
    alternate = result.batches[1]
    assert alternate.provider == "eastmoney_mx_screener"
    assert alternate.provenance.response_sha256 == alternate_hash
    failed_query = next(query for channel, query in calls
                        if channel == "searchData" and codes[5] in query)
    alternate_query = next(query for channel, query in calls
                           if channel == "selectSecurity" and query != "筛选十只股票")
    assert alternate_query == failed_query
    assert alternate.tables[0]["entityCodes"] == codes[5:]
    assert alternate.tables[0]["dataScope"] == "current_query_only"
    assert "rawTable" not in alternate.tables[0]  # no invented historical axis/values
    assert alternate.tables[0]["rows"][0]["市盈率"] == "12.3"


@pytest.mark.parametrize("failure", ["auth", "corrupt", "both_unavailable"])
def test_current_recovery_does_not_hide_auth_corruption_or_loop(failure: str) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path.rsplit("/", 1)[-1])
        if failure == "auth":
            return httpx.Response(403)
        if failure == "corrupt":
            return httpx.Response(200, text="not json")
        raise httpx.ReadTimeout("fixture timeout", request=request)

    client = MxSaasMarketDataClient(
        api_key="fixture", transport=httpx.MockTransport(handler), max_attempts=1,
    )
    expected = {"auth": MxSaasProviderAuthError, "corrupt": MxSaasProviderDataError,
                "both_unavailable": MxSaasProviderUnavailableError}[failure]
    with pytest.raises(expected):
        asyncio.run(client.query_current_finance(query="东方财富市盈率", indicators="市盈率"))
    assert calls == (["searchData", "selectSecurity"] if failure == "both_unavailable"
                     else ["searchData"])
