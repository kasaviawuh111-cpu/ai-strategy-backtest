from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from ashare_lab.adapters.language.deepseek_web_research import WebResearchUnavailable
from ashare_lab.adapters.language.volcengine_web_research import (
    VolcengineWebSearchResearcher,
)
from ashare_lab.ports.current_fact_research import (
    CurrentFactResearchRequest,
    ResearchPurpose,
)


def _request() -> CurrentFactResearchRequest:
    return CurrentFactResearchRequest(
        query="特朗普关税政策最新进展",
        purpose=ResearchPurpose.VIEWPOINT,
        as_of=datetime(2026, 9, 4, 12, 0, tzinfo=UTC),
        instrument_context="300059.SZ",
    )


def _success_payload() -> dict[str, object]:
    return {
        "ResponseMetadata": {"RequestId": "volc-request-1"},
        "Result": {
            "ResultCount": 2,
            "TimeCost": 23,
            "WebResults": [
                {
                    "SortId": 1,
                    "Title": "政策发布页",
                    "Url": "https://example.gov.cn/policy/1",
                    "SiteName": "示例政府网站",
                    "Summary": "主管部门公布了政策时间表。",
                    "PublishTime": "2026-09-03 18:30:00",
                },
                {
                    "SortId": 2,
                    "Title": "后续解读",
                    "Url": "https://news.example.cn/article/2",
                    "SiteName": "示例媒体",
                    "Snippet": "报道梳理了政策可能影响的行业。",
                },
            ],
        },
    }


@pytest.mark.asyncio
async def test_maps_web_results_and_sends_official_bearer_request() -> None:
    secret = "volc-secret-must-not-leak"
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url == "https://open.feedcoopapi.com/search_api/web_search"
        assert request.headers["Authorization"] == f"Bearer {secret}"
        assert request.headers["X-Traffic-Tag"] == "skill_web_search_common"
        assert json.loads(request.content) == {
            "Query": "特朗普关税政策最新进展",
            "SearchType": "web",
            "Count": 10,
            "NeedSummary": True,
            "QueryControl": {"QueryRewrite": True},
        }
        return httpx.Response(200, json=_success_payload())

    result = await VolcengineWebSearchResearcher(
        api_key=SecretStr(secret),
        transport=httpx.MockTransport(handler),
        clock=lambda: datetime(2026, 9, 4, 12, 1, tzinfo=UTC),
    ).research(_request())

    assert len(seen) == 1
    assert result.provider == "volcengine"
    assert result.model == "web-search"
    assert result.provider_response_id == "volc-request-1"
    assert result.search_call_count == 1
    assert result.summary == "检索到 2 条与问题相关的公开网页资料。"
    assert [item.title for item in result.sources] == ["政策发布页", "后续解读"]
    assert [item.publisher for item in result.sources] == ["示例政府网站", "示例媒体"]
    assert result.sources[0].published_at == "2026-09-03 18:30:00"
    assert [item.statement for item in result.facts] == [
        "主管部门公布了政策时间表。",
        "报道梳理了政策可能影响的行业。",
    ]
    assert result.response_sha256.startswith("sha256:")
    assert secret not in repr(result)


@pytest.mark.asyncio
async def test_timeout_is_retried_only_once() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("fixture timeout", request=request)
        return httpx.Response(200, json=_success_payload())

    result = await VolcengineWebSearchResearcher(
        api_key=SecretStr("fixture-secret"),
        transport=httpx.MockTransport(handler),
    ).research(_request())

    assert calls == 2
    assert result.provider_response_id == "volc-request-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [429, 500, 503])
async def test_retryable_http_status_is_retried_only_once(status_code: int) -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(status_code, json={"message": "do not expose this"})
        return httpx.Response(200, json=_success_payload())

    await VolcengineWebSearchResearcher(
        api_key=SecretStr("fixture-secret"),
        transport=httpx.MockTransport(handler),
    ).research(_request())

    assert calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403])
async def test_authentication_failure_is_not_retried(status_code: int) -> None:
    calls = 0
    secret = "secret-never-in-error"

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status_code, text=f"invalid key {secret}")

    with pytest.raises(WebResearchUnavailable) as caught:
        await VolcengineWebSearchResearcher(
            api_key=SecretStr(secret),
            transport=httpx.MockTransport(handler),
        ).research(_request())

    assert calls == 1
    assert secret not in str(caught.value)


@pytest.mark.asyncio
async def test_business_error_is_not_retried_and_fails_closed() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "ResponseMetadata": {
                    "RequestId": "failed-request",
                    "Error": {"Code": "10403", "Message": "invalid api key"},
                }
            },
        )

    with pytest.raises(WebResearchUnavailable, match="business error"):
        await VolcengineWebSearchResearcher(
            api_key=SecretStr("fixture-secret"),
            transport=httpx.MockTransport(handler),
        ).research(_request())

    assert calls == 1


@pytest.mark.asyncio
async def test_second_retryable_failure_stops_after_two_total_attempts() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, text="still unavailable")

    with pytest.raises(WebResearchUnavailable, match="temporarily unavailable"):
        await VolcengineWebSearchResearcher(
            api_key=SecretStr("fixture-secret"),
            transport=httpx.MockTransport(handler),
        ).research(_request())

    assert calls == 2
