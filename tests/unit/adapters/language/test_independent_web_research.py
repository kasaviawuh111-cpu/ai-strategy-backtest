from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import httpx
import pytest

from ashare_lab.adapters.language.deepseek_web_research import WebResearchUnavailable
from ashare_lab.adapters.language.independent_web_research import (
    BingRssResearcher,
    DuckDuckGoHtmlResearcher,
    FailoverWebResearcher,
    QueryPlanningCurrentFactResearcher,
)
from ashare_lab.adapters.language.vibe_candidates import CandidateTransportRequest
from ashare_lab.ports.current_fact_research import (
    CurrentFactResearchRequest,
    CurrentFactResearchResult,
    ResearchFact,
    ResearchPurpose,
    ResearchSource,
)
from ashare_lab.ports.dialogue_progress import progress_sink


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "empty", "blocked"])
async def test_search_changes_source_after_unavailable_primary(failure: str) -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url.host))
        assert request.url.params["q"] == "policy context"
        if request.url.host == "html.duckduckgo.com":
            if failure == "timeout":
                raise httpx.ConnectTimeout("unavailable")
            return httpx.Response(403 if failure == "blocked" else 200, content=b"no results")
        assert request.url.params["format"] == "rss"
        return httpx.Response(200, content=b'''<rss><channel><item>
            <title>Policy report</title><link>https://example.org/policy</link>
            <description>Search summary only</description>
            </item></channel></rss>''')

    transport = httpx.MockTransport(handler)
    result = await FailoverWebResearcher(
        DuckDuckGoHtmlResearcher(transport=transport, report_failure=False),
        BingRssResearcher(transport=transport),
    ).research(CurrentFactResearchRequest(
        query="policy context", purpose=ResearchPurpose.VIEWPOINT,
        as_of=datetime(2026, 9, 7, tzinfo=UTC),
    ))
    assert calls[-1] == "www.bing.com"
    assert result.provider == "bing_rss"
    assert result.sources[0].url == "https://example.org/policy"
    assert result.facts[0].fact_kind == "uncertain"
    assert result.executable_strategy is None


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"<html>challenge</html>", b"<rss><channel/></rss>",
    b'<!DOCTYPE rss [<!ENTITY x "bad">]><rss><channel/></rss>'])
async def test_unusable_backup_cannot_fabricate_search_success(body: bytes) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    with pytest.raises(WebResearchUnavailable):
        await BingRssResearcher(transport=httpx.MockTransport(handler)).research(
            CurrentFactResearchRequest(query="policy context", purpose=ResearchPurpose.VIEWPOINT,
                                       as_of=datetime(2026, 9, 7, tzinfo=UTC)),
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
        def __init__(self) -> None:
            self.requests: list[CandidateTransportRequest] = []

        async def generate_json(self, request: CandidateTransportRequest) -> dict[str, object]:
            self.requests.append(request)
            if request.response_schema_name == "public_search_query":
                return {"query": "特朗普 关税政策 最新进展"}
            return {
                "summary": (
                    "来源摘要只说明一则政策页面有更新，未披露政策内容与具体发布时间，"
                    "不能确认近期政策变化。"
                ),
                "source_ids": ["duckduckgo_html_1"],
                "unresolved_questions": ["政策原文内容和发布时间"],
            }

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
    assert len(transport.requests) == 2
    assert transport.requests[0].response_schema_name == "public_search_query"
    assert transport.requests[0].capability_matrix == {}
    assert "not strategy candidate or source-span extraction" in (
        transport.requests[0].json_object_contract or ""
    )
    analysis_request = transport.requests[1]
    assert analysis_request.response_schema_name == "public_search_evidence_analysis"
    assert analysis_request.user_payload is not None
    assert analysis_request.user_payload["originalUtterance"].startswith("我讨厌特朗普")
    assert "具体发布时间" in result.summary
    assert result.facts[0].statement == "A public policy page was updated."
    assert result.signal_records == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_separate_analysis_transport_preserves_search_on_failure(fails: bool) -> None:
    from ashare_lab.adapters.language.vibe_candidates import CandidateTransportError

    calls: list[str] = []

    async def search_handler(request: httpx.Request) -> httpx.Response:
        calls.append("search")
        return httpx.Response(200, text=(
            '<a class="result__a" href="https://example.org/notice">股东增持公告</a>'
            '<a class="result__snippet">基金增持后持股5.0314%。</a>'
        ))

    class QueryTransport:
        async def generate_json(self, request: CandidateTransportRequest) -> dict[str, object]:
            assert request.response_schema_name == "public_search_query"
            calls.append("query")
            return {"query": "东方财富 大股东 增持"}

    class AnalysisTransport:
        async def generate_json(self, request: CandidateTransportRequest) -> dict[str, object]:
            assert request.response_schema_name == "public_search_evidence_analysis"
            calls.append("analysis")
            if fails:
                raise CandidateTransportError("temporarily unavailable")
            return {"summary": "资料记载基金增持后持股5.0314%，尚未明确公告披露日。",
                    "source_ids": ["duckduckgo_html_1"],
                    "unresolved_questions": ["原始公告披露日尚未确认"]}

    result = await QueryPlanningCurrentFactResearcher(
        DuckDuckGoHtmlResearcher(transport=httpx.MockTransport(search_handler)),
        QueryTransport(), analysis_transport=AnalysisTransport(),
        analysis_model="reasoning-profile",
    ).research(CurrentFactResearchRequest(
        query="东方财富大股东增持公告后次日买入，持有30个交易日卖出",
        purpose=ResearchPurpose.CURRENT_FACT, as_of=datetime(2026, 9, 8, tzinfo=UTC),
    ))
    assert calls == ["query", "search", "analysis"] + (["analysis"] if fails else [])
    assert result.facts[0].statement == "基金增持后持股5.0314%。"
    assert result.sources[0].url == "https://example.org/notice"
    assert result.search_call_count == 1
    assert result.signal_records == ()
    if fails:
        assert "自动分析暂未完成" in result.summary
    else:
        assert result.model == "reasoning-profile"
        assert "5.0314%" in result.summary


@pytest.mark.asyncio
async def test_event_research_analyzes_original_rule_and_keeps_source_dates_separate() -> None:
    utterance = "东方财富发布大股东增持公告后次日买入，持有30个交易日卖出"
    excerpt = (
        "基金2013-7-4增持，持股比例由4.7338%变为5.0314%；"
        "本次增持不影响控股股东结构。摘要未标明公告发布日期。"
    )

    async def search_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=(
            '<a class="result__a" href="https://example.org/notice">测试公告</a>'
            f'<a class="result__snippet">{excerpt}</a>'
        ))

    class Transport:
        async def generate_json(self, request: CandidateTransportRequest) -> dict[str, object]:
            if request.response_schema_name == "public_search_query":
                return {"query": "东方财富 大股东 增持 公告"}
            assert request.utterance == utterance
            assert request.user_payload is not None
            assert request.user_payload["originalUtterance"] == utterance
            assert request.user_payload["sources"][0]["publishedAt"] is None
            assert request.user_payload["sources"][0]["displayReference"] == "[1]"
            assert request.user_payload["facts"][0]["statement"] == excerpt
            assert "用户未要求的限制" in request.system_contract
            assert "事件发生日、公告披露日" in request.system_contract
            return {
                "summary": (
                    "公告摘要[1]记载基金增持后持股5.0314%；控股股东结构未变仅是背景说明，"
                    "不能据此排除大股东增持。保留公告后次日买入、持有30个交易日卖出的规则；"
                    "尚缺回测区间完整历史公告清单与发布日期，目前暂不能回测。"
                ),
                "source_ids": ["duckduckgo_html_1"],
                "unresolved_questions": ["回测区间完整历史公告清单与发布日期"],
            }

    result = await QueryPlanningCurrentFactResearcher(
        DuckDuckGoHtmlResearcher(transport=httpx.MockTransport(search_handler)), Transport(),
        analysis_model="test-analysis-model",
    ).research(CurrentFactResearchRequest(
        query=utterance, purpose=ResearchPurpose.CURRENT_FACT,
        as_of=datetime(2026, 9, 8, tzinfo=UTC), instrument_context="300059.SZ",
    ))
    assert "持有30个交易日" in result.summary
    assert "历史公告清单" in result.summary
    assert result.model == "test-analysis-model"
    assert result.sources[0].published_at is None
    assert result.facts[0].fact_kind == "uncertain"
    assert result.facts[0].statement == excerpt
    assert "控股股东结构未变仅是背景说明" in result.summary
    assert result.signal_records == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("source_prefix", ["tencent_web", "duckduckgo_html"])
async def test_analysis_renders_known_source_ids_without_changing_evidence(
    source_prefix: str,
) -> None:
    as_of = datetime(2026, 9, 8, tzinfo=UTC)
    sources = tuple(
        ResearchSource(f"{source_prefix}_{index}", f"公告{index}",
                       f"https://example.org/{index}", "example.org", None)
        for index in range(1, 11)
    )
    facts = (ResearchFact("已取得的原文摘要。", "uncertain", (sources[0].source_id,), None),)
    original_result = CurrentFactResearchResult(
        provider=source_prefix, model="none", provider_response_id="test-response",
        query="公司 公告", purpose=ResearchPurpose.CURRENT_FACT, as_of=as_of,
        summary="已取得资料。", facts=facts, sources=sources, unresolved_questions=(),
        retrieved_at=as_of, response_sha256="sha256:test", search_call_count=1,
    )

    class Search:
        async def research(self, _request: CurrentFactResearchRequest) -> CurrentFactResearchResult:
            return original_result

    class Transport:
        async def generate_json(self, request: CandidateTransportRequest) -> dict[str, object]:
            if request.response_schema_name == "public_search_query":
                return {"query": "公司 公告"}
            return {
                "summary": f"资料（{source_prefix}_1、{source_prefix}_10）提供公告线索。",
                "source_ids": [sources[0].source_id, sources[-1].source_id],
                "unresolved_questions": [f"{source_prefix}_10尚缺披露日期。"],
            }

    result = await QueryPlanningCurrentFactResearcher(Search(), Transport()).research(
        CurrentFactResearchRequest(query="公司事件策略", purpose=ResearchPurpose.CURRENT_FACT,
                                   as_of=as_of),
    )
    assert result.summary == "资料（[1]、[10]）提供公告线索。"
    assert result.unresolved_questions == ("[10]尚缺披露日期。",)
    assert result.sources == sources
    assert result.facts == facts
    assert result.response_sha256 == original_result.response_sha256
    assert result.search_call_count == 1
    assert result.signal_records == ()


@pytest.mark.asyncio
async def test_analysis_failure_preserves_successful_search_evidence() -> None:
    from ashare_lab.adapters.language.vibe_candidates import CandidateTransportError

    async def search_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=(
            '<a class="result__a" href="https://example.org/notice">测试公告</a>'
            '<a class="result__snippet">已返回的实际资料摘要。</a>'
        ))

    class Transport:
        async def generate_json(self, request: CandidateTransportRequest) -> dict[str, object]:
            if request.response_schema_name == "public_search_query":
                return {"query": "公司 公告"}
            raise CandidateTransportError("temporarily unavailable")

    result = await QueryPlanningCurrentFactResearcher(
        DuckDuckGoHtmlResearcher(transport=httpx.MockTransport(search_handler)), Transport(),
    ).research(CurrentFactResearchRequest(
        query="公司事件策略", purpose=ResearchPurpose.CURRENT_FACT,
        as_of=datetime(2026, 9, 8, tzinfo=UTC),
    ))
    assert "已取得公开资料" in result.summary
    assert result.sources[0].title == "测试公告"
    assert result.facts[0].statement == "已返回的实际资料摘要。"
    assert "自动分析暂未完成" in result.summary
    assert result.search_call_count == 1
    assert result.sources


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["connection_failed", "timeout", "invalid_response"])
async def test_query_rewrite_failure_searches_original_words_and_still_analyzes(
    failure: Literal["connection_failed", "timeout", "invalid_response"],
) -> None:
    from ashare_lab.adapters.language.vibe_candidates import CandidateTransportError

    utterance = "东方财富发布大股东增持公告后次日买入，持有30个交易日卖出"
    queries: list[str] = []
    stages: list[str] = []
    purposes: list[str] = []

    async def search_handler(request: httpx.Request) -> httpx.Response:
        queries.append(request.url.params["q"])
        return httpx.Response(200, text=(
            '<a class="result__a" href="https://example.org/notice">测试公告</a>'
            '<a class="result__snippet">测试公告摘要，未说明完整历史覆盖。</a>'
        ))

    class Transport:
        async def generate_json(self, request: CandidateTransportRequest) -> dict[str, object]:
            purposes.append(request.response_schema_name)
            if request.response_schema_name == "public_search_query":
                if failure == "invalid_response":
                    return {"unexpected": "invalid query format"}
                raise CandidateTransportError("unavailable", failure_kind=failure)
            assert request.utterance == utterance
            assert request.instrument_context == "300059.SZ"
            return {
                "summary": "已取得公告线索，但尚缺完整历史公告清单与发布日期，暂不能回测。",
                "source_ids": ["duckduckgo_html_1"],
                "unresolved_questions": ["缺完整历史公告清单与发布日期"],
            }

    token = progress_sink.set(lambda stage, _message: stages.append(stage))
    try:
        result = await QueryPlanningCurrentFactResearcher(
            DuckDuckGoHtmlResearcher(transport=httpx.MockTransport(search_handler)), Transport(),
        ).research(CurrentFactResearchRequest(
            query=utterance, purpose=ResearchPurpose.CURRENT_FACT,
            as_of=datetime(2026, 9, 8, tzinfo=UTC), instrument_context="300059.SZ",
        ))
    finally:
        progress_sink.reset(token)
    assert queries == [f"300059.SZ {utterance}"]
    assert purposes == ["public_search_query", "public_search_evidence_analysis"]
    assert "web_search_retry" in stages
    assert "已取得公告线索" in result.summary
    assert result.search_call_count == 1
    assert result.sources[0].url == "https://example.org/notice"
    assert result.signal_records == ()


@pytest.mark.asyncio
async def test_analysis_retries_transient_connection_using_same_evidence_without_searching_again(
) -> None:
    from ashare_lab.adapters.language.vibe_candidates import CandidateTransportError

    searches = 0

    async def search_handler(request: httpx.Request) -> httpx.Response:
        nonlocal searches
        searches += 1
        return httpx.Response(200, text=(
            '<a class="result__a" href="https://example.org/notice">测试公告</a>'
            '<a class="result__snippet">测试资料。</a>'
        ))

    class Transport:
        def __init__(self) -> None:
            self.analysis_requests: list[CandidateTransportRequest] = []

        async def generate_json(self, request: CandidateTransportRequest) -> dict[str, object]:
            if request.response_schema_name == "public_search_query":
                return {"query": "公司 公告"}
            self.analysis_requests.append(request)
            if len(self.analysis_requests) == 1:
                raise CandidateTransportError("unavailable", failure_kind="connection_failed")
            return {
                "summary": "已取得公告线索，但尚缺完整历史日期序列，目前暂不能回测。",
                "source_ids": ["duckduckgo_html_1"],
                "unresolved_questions": ["缺完整历史日期序列"],
            }

    transport = Transport()
    result = await QueryPlanningCurrentFactResearcher(
        DuckDuckGoHtmlResearcher(transport=httpx.MockTransport(search_handler)), transport,
    ).research(CurrentFactResearchRequest(
        query="公司事件策略", purpose=ResearchPurpose.CURRENT_FACT,
        as_of=datetime(2026, 9, 8, tzinfo=UTC),
    ))
    assert searches == 1
    assert len(transport.analysis_requests) == 2
    assert transport.analysis_requests[0] == transport.analysis_requests[1]
    assert "缺完整历史日期序列" in result.summary
    assert result.signal_records == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "disconnect", "503", "429"])
async def test_transient_search_failure_recovers_once_with_visible_progress(failure: str) -> None:
    attempts = 0
    events: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("timeout", request=request)
            if failure == "disconnect":
                raise httpx.RemoteProtocolError("disconnect", request=request)
            return httpx.Response(int(failure))
        return httpx.Response(200, text=(
            '<a class="result__a" href="https://example.org/source">Source</a>'
        ))

    token = progress_sink.set(lambda stage, _message: events.append(stage))
    try:
        result = await DuckDuckGoHtmlResearcher(transport=httpx.MockTransport(handler)).research(
            CurrentFactResearchRequest(query="特朗普 最新进展", purpose=ResearchPurpose.VIEWPOINT,
                                       as_of=datetime.now(UTC)),
        )
    finally:
        progress_sink.reset(token)
    assert attempts == 2
    assert result.search_call_count == 2
    assert result.sources[0].url == "https://example.org/source"
    assert events == ["web_search", "web_search_retry", "web_sources"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,attempts_expected", [(403, 1), (200, 1), (503, 2)])
async def test_search_failure_is_bounded_and_never_fabricates_sources(
    status: int, attempts_expected: int,
) -> None:
    attempts = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(status, text="no search evidence")

    with pytest.raises(WebResearchUnavailable):
        await DuckDuckGoHtmlResearcher(transport=httpx.MockTransport(handler)).research(
            CurrentFactResearchRequest(query="特朗普 最新进展", purpose=ResearchPurpose.VIEWPOINT,
                                       as_of=datetime.now(UTC)),
        )
    assert attempts == attempts_expected
