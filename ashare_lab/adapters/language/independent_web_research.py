"""Credential-free public web search for non-executable research evidence.

DuckDuckGo supplies search results only.  A downstream language model may read
the returned titles and snippets, but this adapter neither calls nor represents
itself as a model-native web-search capability.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import cast
from urllib.parse import parse_qs, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ashare_lab.adapters.language.deepseek_web_research import WebResearchUnavailable
from ashare_lab.adapters.language.vibe_candidates import (
    CandidateJsonTransport,
    CandidateTransportError,
    CandidateTransportRequest,
    CandidateTransportResponse,
)
from ashare_lab.ports.current_fact_research import (
    CurrentFactResearcher,
    CurrentFactResearchRequest,
    CurrentFactResearchResult,
    ResearchFact,
    ResearchSource,
)
from ashare_lab.ports.dialogue_progress import emit_progress

_ENDPOINT = "https://html.duckduckgo.com/html/"
_MODEL = "none"
_MAX_QUERY_CHARS = 500
_QUERY_PROMPT_VERSION = "public-search-query.prompt.v1"
_QUERY_SCHEMA_VERSION = "public-search-query.v1"
_EMPTY_CAPABILITY_HASH = f"sha256:{hashlib.sha256(b'{}').hexdigest()}"
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _SearchHit:
    title: str
    href: str
    snippet: str


class _SearchQueryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    query: str = Field(min_length=2, max_length=120)

    @field_validator("query")
    @classmethod
    def query_is_one_line(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) < 2:
            raise ValueError("planned search query is too short")
        return normalized


class _DuckDuckGoResultParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._hits: list[_SearchHit] = []
        self._title_parts: list[str] = []
        self._snippet_parts: list[str] = []
        self._href = ""
        self._capture: str | None = None
        self._capture_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._capture is not None:
            self._capture_depth += 1
            return
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "a" and "result__a" in classes:
            self._flush()
            self._href = attributes.get("href") or ""
            self._capture = "title"
            self._capture_depth = 1
        elif "result__snippet" in classes and self._href:
            self._capture = "snippet"
            self._capture_depth = 1

    def handle_endtag(self, tag: str) -> None:
        del tag
        if self._capture is None:
            return
        self._capture_depth -= 1
        if self._capture_depth == 0:
            self._capture = None

    def handle_data(self, data: str) -> None:
        if self._capture == "title":
            self._title_parts.append(data)
        elif self._capture == "snippet":
            self._snippet_parts.append(data)

    def results(self) -> tuple[_SearchHit, ...]:
        self._flush()
        return tuple(self._hits)

    def _flush(self) -> None:
        title = _bounded_text(" ".join(self._title_parts), limit=300)
        snippet = _bounded_text(" ".join(self._snippet_parts), limit=500)
        if title and self._href:
            self._hits.append(_SearchHit(title=title, href=self._href, snippet=snippet))
        self._title_parts = []
        self._snippet_parts = []
        self._href = ""


class DuckDuckGoHtmlResearcher:
    """Fetch current public search-result evidence without credentials."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        max_results: int = 5,
        max_response_bytes: int = 512 * 1024,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not 0.25 <= timeout_seconds <= 60.0:
            raise ValueError("public search timeout must be between 0.25 and 60 seconds")
        if not 1 <= max_results <= 10:
            raise ValueError("public search result count must be between 1 and 10")
        if not 1_024 <= max_response_bytes <= 2_097_152:
            raise ValueError("public search response limit is outside the safe range")
        self._timeout = httpx.Timeout(timeout_seconds, connect=min(5.0, timeout_seconds))
        self._max_results = max_results
        self._max_response_bytes = max_response_bytes
        self._transport = transport
        self._clock = clock or (lambda: datetime.now(UTC))

    async def research(
        self,
        request: CurrentFactResearchRequest,
    ) -> CurrentFactResearchResult:
        if len(request.query) > _MAX_QUERY_CHARS:
            raise WebResearchUnavailable("research query is too long for public search")
        emit_progress("web_search", f"已提交公开网页检索：{request.query}")
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
                follow_redirects=False,
                trust_env=False,
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/140.0 Safari/537.36"
                    ),
                },
            ) as client, client.stream(
                "GET", _ENDPOINT, params={"q": request.query}
            ) as response:
                if not 200 <= response.status_code < 300:
                    raise WebResearchUnavailable("public search request failed")
                raw_response = await _read_bounded_response(
                    response,
                    max_bytes=self._max_response_bytes,
                )
            hits = _parse_hits(raw_response, limit=self._max_results)
            if not hits:
                raise WebResearchUnavailable("public search evidence is unavailable")
            emit_progress(
                "web_sources",
                f"已取得 {len(hits)} 条网页摘要与链接："
                + "；".join(hit.title[:80] for hit in hits[:3])
                + "。尚未逐页核验。",
            )
            retrieved_at = self._clock()
            if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
                raise WebResearchUnavailable("research clock must include a timezone")
            digest = hashlib.sha256(raw_response).hexdigest()
            sources = tuple(
                ResearchSource(
                    source_id=f"ddg_web_{index}",
                    title=hit.title,
                    url=hit.href,
                    publisher=urlsplit(hit.href).netloc,
                    published_at=None,
                )
                for index, hit in enumerate(hits, start=1)
            )
            facts = tuple(
                ResearchFact(
                    statement=hit.snippet or hit.title,
                    fact_kind="uncertain",
                    source_ids=(source.source_id,),
                    time_scope=None,
                )
                for source, hit in zip(sources, hits, strict=True)
            )
            _LOGGER.info("public_web_research_ok provider=duckduckgo_html sources=%d", len(sources))
            return CurrentFactResearchResult(
                provider="duckduckgo_html",
                model=_MODEL,
                provider_response_id=f"duckduckgo:{digest[:24]}",
                query=request.query,
                purpose=request.purpose,
                as_of=request.as_of,
                summary=(
                    f"检索到 {len(sources)} 条公开网页结果；"
                    "摘要来自搜索结果页，尚未逐页核对。"
                ),
                facts=facts,
                sources=sources,
                unresolved_questions=("重要事实仍需以来源网页原文为准。",),
                retrieved_at=retrieved_at,
                response_sha256=f"sha256:{digest}",
                search_call_count=1,
            )
        except WebResearchUnavailable as exc:
            emit_progress("failed", "本次搜索未取得可用来源，不会编造联网事实。")
            _LOGGER.info("public web research unavailable reason=%s", str(exc))
            raise
        except (httpx.HTTPError, UnicodeDecodeError, ValueError) as exc:
            emit_progress("failed", "联网搜索未完成，不会编造来源。")
            _LOGGER.info(
                "public web research unavailable reason=transport_or_response type=%s",
                type(exc).__name__,
            )
            raise WebResearchUnavailable("public search response unavailable") from None


class QueryPlanningCurrentFactResearcher:
    """Use a bounded model call to plan a query, then run independent search."""

    def __init__(
        self,
        inner: CurrentFactResearcher,
        transport: CandidateJsonTransport,
    ) -> None:
        self._inner = inner
        self._transport = transport

    async def research(
        self,
        request: CurrentFactResearchRequest,
    ) -> CurrentFactResearchResult:
        transport_request = CandidateTransportRequest(
            utterance=request.query,
            instrument_context=request.instrument_context,
            as_of_date=request.as_of.date(),
            max_candidates=1,
            response_schema=_query_response_schema(),
            capability_matrix={},
            capability_projection_version="none",
            capability_projection_hash=_EMPTY_CAPABILITY_HASH,
            system_contract=(
                "你只负责把用户输入改写成一条中性的公开网页检索词，"
                "不回答问题，不生成或解释交易策略。保留用户明确提及的人物、"
                "机构、公司、政策、事件和时间线索；去掉买卖动作、技术指标参数、"
                "回测区间、本金及收益诉求等与公开事实检索无关的内容。"
                "不得添加用户未提及的实体或具体事件；可加入“最新进展”"
                "或“官方信息”等中性检索限定。"
            ),
            response_schema_name="public_search_query",
            user_payload={
                "originalUtterance": request.query,
                "asOf": request.as_of.isoformat(),
                "instrumentContext": request.instrument_context,
            },
            json_object_contract=(
                "Return exactly one JSON object with the query field from responseSchema. "
                "This is search-query generation, not strategy candidate or source-span "
                "extraction. Do not output candidates, spans, defaults, analysis or strategy."
            ),
            system_footer=(
                f"Search-query contract: {_QUERY_PROMPT_VERSION}; "
                f"schema: {_QUERY_SCHEMA_VERSION}."
            ),
        )
        try:
            payload = await self._transport.generate_json(transport_request)
            planned = _parse_query_plan(payload)
        except (CandidateTransportError, TypeError, ValueError, ValidationError):
            _LOGGER.info("public search query planning unavailable")
            raise WebResearchUnavailable("public search query planning unavailable") from None
        return await self._inner.research(replace(request, query=planned.query))


def _parse_hits(raw_response: bytes, *, limit: int) -> tuple[_SearchHit, ...]:
    parser = _DuckDuckGoResultParser()
    parser.feed(raw_response.decode("utf-8"))
    seen: set[str] = set()
    hits: list[_SearchHit] = []
    for raw_hit in parser.results():
        url = _result_url(raw_hit.href)
        if url is None or url in seen:
            continue
        seen.add(url)
        hits.append(_SearchHit(title=raw_hit.title, href=url, snippet=raw_hit.snippet))
        if len(hits) == limit:
            break
    return tuple(hits)


def _query_response_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["query"],
        "properties": {
            "query": {"type": "string", "minLength": 2, "maxLength": 120},
        },
    }


def _parse_query_plan(payload: CandidateTransportResponse) -> _SearchQueryPlan:
    raw: object = json.loads(payload) if isinstance(payload, bytes | str) else payload
    return _SearchQueryPlan.model_validate(cast(object, raw))


def _result_url(href: str) -> str | None:
    value = href.strip()
    parsed = urlsplit(value)
    is_duckduckgo_redirect = parsed.path.startswith("/l/") and (
        not parsed.netloc or parsed.netloc.casefold().endswith("duckduckgo.com")
    )
    if is_duckduckgo_redirect:
        targets = parse_qs(parsed.query).get("uddg", ())
        if len(targets) != 1:
            return None
        value = targets[0]
        parsed = urlsplit(value)
    if (
        len(value) > 2_048
        or parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return value


def _bounded_text(value: str, *, limit: int) -> str:
    return " ".join(value.split()).strip()[:limit]


async def _read_bounded_response(response: httpx.Response, *, max_bytes: int) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            raise WebResearchUnavailable("public search response length is invalid") from None
        if declared_length < 0 or declared_length > max_bytes:
            raise WebResearchUnavailable("public search response is too large")
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise WebResearchUnavailable("public search response is too large")
        chunks.append(chunk)
    return b"".join(chunks)


__all__ = ["DuckDuckGoHtmlResearcher", "QueryPlanningCurrentFactResearcher"]
