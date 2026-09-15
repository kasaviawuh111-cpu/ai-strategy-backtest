"""Tencent WSA service API KEY adapter; search evidence is not trading data.

Contract: https://cloud.tencent.com/document/product/1806/130615
Only the dedicated service key is accepted, never CloudBase deployment tokens.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import cast
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from ashare_lab.adapters.language.deepseek_web_research import WebResearchUnavailable
from ashare_lab.adapters.language.independent_web_research import (
    bounded_search_text,
    read_bounded_search_response,
    validated_search_result_url,
)
from ashare_lab.ports.current_fact_research import (
    CurrentFactResearchRequest,
    CurrentFactResearchResult,
    ResearchFact,
    ResearchSource,
)
from ashare_lab.ports.dialogue_progress import emit_progress
from ashare_lab.ports.request_context import current_request_id

_ENDPOINT = "https://api.wsa.cloud.tencent.com/SearchPro"
_LOGGER = logging.getLogger("uvicorn.error")


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("expected JSON object")
    return cast(dict[str, object], value)


class TencentWebSearchResearcher:
    def __init__(
        self, *, api_key: SecretStr, timeout_seconds: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key.get_secret_value().strip():
            raise ValueError("Tencent search service API KEY is required")
        if not 0.25 <= timeout_seconds <= 300:
            raise ValueError("search timeout outside supported range")
        self._key = api_key
        self._timeout = httpx.Timeout(timeout_seconds, connect=min(5.0, timeout_seconds))
        self._transport = transport

    async def research(self, request: CurrentFactResearchRequest) -> CurrentFactResearchResult:
        emit_progress("web_search", f"正在通过腾讯云联网搜索：{request.query}")
        raw, attempts = await self._fetch(request.query)
        try:
            decoded = _object(json.loads(raw))
            payload = _object(decoded.get("Response"))
            error = payload.get("Error")
            if error:
                code = _object(error).get("Code", "unknown")
                safe_code = str(code)[:80]
                if not safe_code.replace(".", "").replace("_", "").isalnum():
                    safe_code = "unknown"
                _LOGGER.warning("tencent_search_failed request_id=%s code=%s",
                                current_request_id(), safe_code)
                raise WebResearchUnavailable(f"Tencent search failed: {safe_code}")
            pages = payload.get("Pages")
            if not isinstance(pages, list):
                raise ValueError("missing Pages")
            sources: list[ResearchSource] = []
            facts: list[ResearchFact] = []
            seen: set[str] = set()
            for page in cast(list[object], pages)[:50]:
                if not isinstance(page, str):
                    continue
                item = _object(json.loads(page))
                href, title = item.get("url"), item.get("title")
                if not isinstance(href, str) or not isinstance(title, str):
                    continue
                url = validated_search_result_url(href)
                if url is None or url in seen or not title.strip():
                    continue
                snippet = next((
                    normalized for field in (item.get("passage"), item.get("content"), title)
                    if isinstance(field, str)
                    and (normalized := bounded_search_text(field, limit=10_000))
                ), "")
                if not snippet:
                    continue
                date = item.get("date")
                seen.add(url)
                source_id = f"tencent_web_{len(sources) + 1}"
                sources.append(ResearchSource(
                    source_id=source_id, title=bounded_search_text(title, limit=300), url=url,
                    publisher=bounded_search_text(urlsplit(url).netloc, limit=160),
                    published_at=(
                        bounded_search_text(date, limit=80) if isinstance(date, str) else None
                    ),
                ))
                facts.append(ResearchFact(
                    statement=snippet,
                    fact_kind="uncertain",
                    source_ids=(source_id,), time_scope=None,
                ))
                if len(sources) == 5:
                    break
            if not sources:
                raise ValueError("no usable sources")
        except (ValueError, TypeError):
            raise WebResearchUnavailable("Tencent search returned no usable evidence") from None
        digest = hashlib.sha256(raw).hexdigest()
        provider_id = payload.get("RequestId")
        _LOGGER.info("public_web_research_ok request_id=%s provider=tencent_wsa sources=%d",
                     current_request_id(), len(sources))
        emit_progress(
            "web_sources", f"腾讯云搜索返回 {len(sources)} 条网页摘要与链接，尚未逐页核验。",
        )
        return CurrentFactResearchResult(
            provider="tencent_wsa", model="none",
            provider_response_id=provider_id if isinstance(provider_id, str) else digest[:24],
            query=request.query, purpose=request.purpose, as_of=request.as_of,
            summary=f"腾讯云联网搜索返回 {len(sources)} 条网页资料；摘要不等于已核验事实。",
            facts=tuple(facts), sources=tuple(sources),
            unresolved_questions=("重要事实需以原始来源为准。",),
            retrieved_at=datetime.now(UTC), response_sha256=f"sha256:{digest}",
            search_call_count=attempts,
        )

    async def _fetch(self, query: str) -> tuple[bytes, int]:
        async with httpx.AsyncClient(
            timeout=self._timeout, transport=self._transport, trust_env=False,
            follow_redirects=False,
        ) as client:
            for attempt in range(2):
                try:
                    async with client.stream(
                        "POST", _ENDPOINT, json={"Query": query},
                        headers={"Authorization": f"Bearer {self._key.get_secret_value()}"},
                    ) as response:
                        if response.status_code == 200:
                            raw = await read_bounded_search_response(
                                response, max_bytes=512 * 1024,
                            )
                            return raw, attempt + 1
                        if response.status_code != 429 and response.status_code < 500:
                            raise WebResearchUnavailable(
                                f"Tencent search HTTP {response.status_code}",
                            )
                except httpx.HTTPError as exc:
                    _LOGGER.warning("tencent_search_transport_failed request_id=%s type=%s",
                                    current_request_id(), type(exc).__name__)
                if attempt == 0:
                    emit_progress("web_search_retry", "腾讯云搜索暂时未响应，正在重试（1/1）。")
                    await asyncio.sleep(0.5)
        raise WebResearchUnavailable("Tencent search unavailable after retry")
