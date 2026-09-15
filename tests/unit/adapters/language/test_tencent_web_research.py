import json
from dataclasses import asdict
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from ashare_lab.adapters.language.deepseek_web_research import WebResearchUnavailable
from ashare_lab.adapters.language.tencent_web_research import TencentWebSearchResearcher
from ashare_lab.api.schemas import IdeaResearchPayload
from ashare_lab.ports.current_fact_research import CurrentFactResearchRequest, ResearchPurpose
from ashare_lab.settings import AppSettings


def request() -> CurrentFactResearchRequest:
    return CurrentFactResearchRequest(
        query="政策原文", purpose=ResearchPurpose.VIEWPOINT, as_of=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_tencent_service_key_and_official_response_mapping() -> None:
    async def handler(req: httpx.Request) -> httpx.Response:
        assert str(req.url) == "https://api.wsa.cloud.tencent.com/SearchPro"
        assert req.headers["Authorization"] == "Bearer fixture-service-key"
        assert json.loads(req.content) == {"Query": "政策原文"}
        return httpx.Response(200, json={"Response": {"RequestId": "provider-trace", "Pages": [
            json.dumps({"title": "政策原文", "url": "https://example.org/policy",
                        "date": "2026-09-07", "passage": "检索摘要" * 400}),
            json.dumps({"title": "重复", "url": "https://example.org/policy"}),
            json.dumps({"title": "无效链接", "url": "javascript:alert(1)"}),
        ]}})
    result = await TencentWebSearchResearcher(
        api_key=SecretStr("fixture-service-key"), transport=httpx.MockTransport(handler),
    ).research(request())
    assert result.provider == "tencent_wsa"
    assert result.provider_response_id == "provider-trace"
    assert len(result.sources) == 1
    assert result.sources[0].published_at == "2026-09-07"
    assert result.facts[0].fact_kind == "uncertain"
    assert result.executable_strategy is None
    assert result.facts[0].statement == "检索摘要" * 400
    IdeaResearchPayload.model_validate(asdict(result))


@pytest.mark.asyncio
@pytest.mark.parametrize("passage", ["   ", None, {"unexpected": "object"}])
async def test_empty_or_nontext_snippet_uses_title_without_breaking_api(passage: object) -> None:
    async def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Response": {"Pages": [json.dumps({
            "title": "真实标题", "url": "https://example.org/source", "passage": passage,
        })]}})
    result = await TencentWebSearchResearcher(
        api_key=SecretStr("fixture-service-key"), transport=httpx.MockTransport(handler),
    ).research(request())
    assert result.facts[0].statement == "真实标题"
    IdeaResearchPayload.model_validate(asdict(result))


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"Response": {"Error": {"Code": "UnauthorizedOperation"}}},
    {"Response": {"Pages": []}}, {"Response": {"Pages": ["bad json"]}}, {},
])
async def test_business_errors_and_empty_sources_are_not_success(payload: object) -> None:
    calls = 0
    async def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=payload)
    with pytest.raises(WebResearchUnavailable):
        await TencentWebSearchResearcher(
            api_key=SecretStr("fixture-service-key"), transport=httpx.MockTransport(handler),
        ).research(request())
    assert calls == 1


def test_tencent_mode_requires_dedicated_key_and_fixed_endpoint() -> None:
    with pytest.raises(ValidationError):
        AppSettings(research_provider_mode="tencent_web_search")
    with pytest.raises(ValidationError):
        AppSettings(research_provider_mode="tencent_web_search",
                    research_provider_api_key=SecretStr("fixture"),
                    research_provider_endpoint="https://example.org/SearchPro")
    settings = AppSettings(research_provider_mode="tencent_web_search",
                           research_provider_api_key=SecretStr("fixture"))
    assert "fixture" not in settings.model_dump_json()


@pytest.mark.asyncio
async def test_unauthorized_tencent_and_unavailable_duckduckgo_reach_bing() -> None:
    from ashare_lab.adapters.language.independent_web_research import (
        BingRssResearcher, DuckDuckGoHtmlResearcher, FailoverWebResearcher,
    )

    calls = []

    async def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.host)
        if req.url.host == "api.wsa.cloud.tencent.com":
            return httpx.Response(200, json={"Response": {
                "Error": {"Code": "UnauthorizedOperation"},
            }})
        assert "Authorization" not in req.headers
        if req.url.host == "html.duckduckgo.com":
            return httpx.Response(403)
        return httpx.Response(200, content=b'<rss><channel><item><title>Policy</title>'
            b'<link>https://example.org/policy</link><description>Summary</description>'
            b'</item></channel></rss>')

    transport = httpx.MockTransport(handler)
    chain = FailoverWebResearcher(
        TencentWebSearchResearcher(api_key=SecretStr("fixture"), transport=transport),
        FailoverWebResearcher(
            DuckDuckGoHtmlResearcher(transport=transport, report_failure=False),
            BingRssResearcher(transport=transport),
        ),
    )
    result = await chain.research(request())
    assert calls == ["api.wsa.cloud.tencent.com", "html.duckduckgo.com", "www.bing.com"]
    assert result.provider == "bing_rss"
    assert result.sources[0].url == "https://example.org/policy"
