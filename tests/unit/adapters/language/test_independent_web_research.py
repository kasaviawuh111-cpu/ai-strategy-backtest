from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from ashare_lab.adapters.language.independent_web_research import (
    DuckDuckGoHtmlResearcher,
    QueryPlanningCurrentFactResearcher,
)
from ashare_lab.adapters.language.vibe_candidates import CandidateTransportRequest
from ashare_lab.ports.current_fact_research import (
    CurrentFactResearchRequest,
    ResearchPurpose,
)


@pytest.mark.asyncio
async def test_maps_public_search_html_without_claiming_model_provenance() -> None:
    html = b"""
    <html><body>
      <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpolicy">
        Official policy page
      </a>
      <a class="result__snippet">The authority published an updated policy timeline.</a>
      <a class="result__a" href="https://news.example.org/report">Market report</a>
      <div class="result__snippet">The report describes possible sector effects.</div>
    </body></html>
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "html.duckduckgo.com"
        assert request.url.params["q"] == "latest tariff policy"
        assert "Authorization" not in request.headers
        return httpx.Response(200, content=html, headers={"Content-Type": "text/html"})

    result = await DuckDuckGoHtmlResearcher(
        transport=httpx.MockTransport(handler),
        clock=lambda: datetime(2026, 9, 5, 10, 0, tzinfo=UTC),
    ).research(
        CurrentFactResearchRequest(
            query="latest tariff policy",
            purpose=ResearchPurpose.VIEWPOINT,
            as_of=datetime(2026, 9, 5, 9, 59, tzinfo=UTC),
        )
    )

    assert result.provider == "duckduckgo_html"
    assert result.model == "none"
    assert result.search_call_count == 1
    assert [(item.title, item.url) for item in result.sources] == [
        ("Official policy page", "https://example.com/policy"),
        ("Market report", "https://news.example.org/report"),
    ]
    assert [item.statement for item in result.facts] == [
        "The authority published an updated policy timeline.",
        "The report describes possible sector effects.",
    ]
    assert all(item.fact_kind == "uncertain" for item in result.facts)


@pytest.mark.asyncio
async def test_model_plans_small_query_before_independent_search() -> None:
    html = b"""
    <a class="result__a" href="https://example.org/policy">Policy update</a>
    <a class="result__snippet">A public policy page was updated.</a>
    """
    search_queries: list[str] = []

    async def search_handler(request: httpx.Request) -> httpx.Response:
        search_queries.append(request.url.params["q"])
        return httpx.Response(200, content=html)

    class _QueryTransport:
        request: CandidateTransportRequest | None = None

        async def generate_json(self, request: CandidateTransportRequest) -> dict[str, object]:
            self.request = request
            return {"query": "特朗普 关税政策 最新进展"}

    transport = _QueryTransport()
    researcher = QueryPlanningCurrentFactResearcher(
        DuckDuckGoHtmlResearcher(transport=httpx.MockTransport(search_handler)),
        transport,
    )
    result = await researcher.research(
        CurrentFactResearchRequest(
            query="我讨厌特朗普的关税政策，帮我用MA20回测2025至2026年，本金10万元",
            purpose=ResearchPurpose.VIEWPOINT,
            as_of=datetime(2026, 9, 5, 9, 59, tzinfo=UTC),
        )
    )

    assert search_queries == ["特朗普 关税政策 最新进展"]
    assert result.query == "特朗普 关税政策 最新进展"
    assert result.provider == "duckduckgo_html"
    assert result.model == "none"
    assert transport.request is not None
    assert transport.request.response_schema_name == "public_search_query"
    assert transport.request.capability_matrix == {}
    assert "not strategy candidate or source-span extraction" in (
        transport.request.json_object_contract or ""
    )
