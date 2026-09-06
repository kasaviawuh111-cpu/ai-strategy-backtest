"""Volcengine Web Search adapter for non-executable public-fact research.

The implementation is a thin, typed wrapper around the official
``byted-web-search`` request contract.  Search results remain evidence for the
dialogue layer only; they never become market data, a signal, or an executable
strategy.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from json import JSONDecodeError
from typing import cast
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from ashare_lab.adapters.language.deepseek_web_research import WebResearchUnavailable
from ashare_lab.ports.current_fact_research import (
    CurrentFactResearchRequest,
    CurrentFactResearchResult,
    ResearchFact,
    ResearchSource,
)

_DEFAULT_ENDPOINT = "https://open.feedcoopapi.com/search_api/web_search"
_TRAFFIC_TAG = "skill_web_search_common"
_MODEL_ID = "web-search"
_MAX_QUERY_CHARS = 100
_LOGGER = logging.getLogger(__name__)


class VolcengineWebSearchResearcher:
    """Fetch current public web evidence through Volcengine's search API."""

    def __init__(
        self,
        *,
        api_key: SecretStr,
        endpoint: str = _DEFAULT_ENDPOINT,
        timeout_seconds: float = 30.0,
        count: int = 10,
        max_request_bytes: int = 16 * 1024,
        max_response_bytes: int = 512 * 1024,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("research endpoint must be an absolute HTTP URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("research endpoint cannot contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("research endpoint cannot contain query or fragment")
        if parsed.path.rstrip("/") != "/search_api/web_search":
            raise ValueError("research endpoint must target the Web Search API")
        if not api_key.get_secret_value().strip():
            raise ValueError("research API key cannot be empty")
        if not 0.25 <= timeout_seconds <= 300.0:
            raise ValueError("research timeout must be between 0.25 and 300 seconds")
        if not 1 <= count <= 50:
            raise ValueError("web search count must be between 1 and 50")
        if not 1_024 <= max_request_bytes <= 1_048_576:
            raise ValueError("research request limit is outside the safe range")
        if not 1_024 <= max_response_bytes <= 2_097_152:
            raise ValueError("research response limit is outside the safe range")
        self._api_key = api_key
        self._endpoint = endpoint
        self._timeout = httpx.Timeout(timeout_seconds)
        self._count = count
        self._max_request_bytes = max_request_bytes
        self._max_response_bytes = max_response_bytes
        self._transport = transport
        self._clock = clock or (lambda: datetime.now(UTC))

    async def research(
        self,
        request: CurrentFactResearchRequest,
    ) -> CurrentFactResearchResult:
        if len(request.query) > _MAX_QUERY_CHARS:
            raise WebResearchUnavailable("research query is too long for web search")
        request_bytes = self._encode_request(request)
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._api_key.get_secret_value()}",
            "Content-Type": "application/json",
            "X-Traffic-Tag": _TRAFFIC_TAG,
        }

        try:
            raw_response = await self._request_with_one_retry(
                headers=headers,
                request_bytes=request_bytes,
            )
            decoded: object = json.loads(raw_response)
            payload = _object_dict(decoded)
            if payload is None:
                raise WebResearchUnavailable("research provider response is invalid")
            metadata = _object_dict(payload.get("ResponseMetadata"))
            if metadata is not None and metadata.get("Error"):
                error = _object_dict(metadata.get("Error"))
                code = error.get("Code") if error is not None else None
                safe_code = (
                    code
                    if isinstance(code, str)
                    and len(code) < 64
                    and code.replace("_", "").replace(".", "").isalnum()
                    else "unknown"
                )
                _LOGGER.warning("web_research_business_error code=%s", safe_code)
                raise WebResearchUnavailable("research provider returned a business error")

            sources, facts = _map_web_results(payload)
            if not sources or not facts:
                raise WebResearchUnavailable("search evidence is unavailable")
            _LOGGER.warning("web_research_ok provider=volcengine sources=%s", len(sources))
            retrieved_at = self._clock()
            if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
                raise WebResearchUnavailable("research clock must include a timezone")
            digest = hashlib.sha256(raw_response).hexdigest()
            response_id = _provider_response_id(metadata, digest=digest)
            return CurrentFactResearchResult(
                provider="volcengine",
                model=_MODEL_ID,
                provider_response_id=response_id,
                query=request.query,
                purpose=request.purpose,
                as_of=request.as_of,
                summary=f"检索到 {len(sources)} 条与问题相关的公开网页资料。",
                facts=facts,
                sources=sources,
                unresolved_questions=(),
                retrieved_at=retrieved_at,
                response_sha256=f"sha256:{digest}",
                search_call_count=1,
            )
        except WebResearchUnavailable as exc:
            _LOGGER.warning("volcengine web research unavailable reason=%s", str(exc))
            raise
        except (
            httpx.HTTPError,
            JSONDecodeError,
            UnicodeDecodeError,
            TypeError,
            ValueError,
        ) as exc:
            _LOGGER.warning(
                "volcengine web research unavailable reason=transport_or_response type=%s",
                type(exc).__name__,
            )
            raise WebResearchUnavailable("research provider response unavailable") from None

    def _encode_request(self, request: CurrentFactResearchRequest) -> bytes:
        body = {
            "Query": request.query,
            "SearchType": "web",
            "Count": self._count,
            "NeedSummary": True,
            "QueryControl": {"QueryRewrite": True},
        }
        try:
            encoded = json.dumps(
                body,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError):
            raise WebResearchUnavailable("research request is invalid") from None
        if len(encoded) > self._max_request_bytes:
            raise WebResearchUnavailable("research request is too large")
        return encoded

    async def _request_with_one_retry(
        self,
        *,
        headers: dict[str, str],
        request_bytes: bytes,
    ) -> bytes:
        async with httpx.AsyncClient(
            timeout=self._timeout,
            transport=self._transport,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            for attempt in range(2):
                try:
                    async with client.stream(
                        "POST",
                        self._endpoint,
                        headers=headers,
                        content=request_bytes,
                    ) as response:
                        status = response.status_code
                        if 200 <= status < 300:
                            return await _read_bounded_response(
                                response,
                                max_bytes=self._max_response_bytes,
                            )
                        if status in {401, 403}:
                            raise WebResearchUnavailable("research provider authentication failed")
                        if status == 429 or 500 <= status < 600:
                            if attempt == 0:
                                continue
                            raise WebResearchUnavailable(
                                "research provider is temporarily unavailable"
                            )
                        raise WebResearchUnavailable("research provider request failed")
                except (httpx.TimeoutException, httpx.ConnectError):
                    if attempt == 0:
                        continue
                    raise WebResearchUnavailable(
                        "research provider is temporarily unavailable"
                    ) from None
        raise WebResearchUnavailable("research provider is temporarily unavailable")


def _map_web_results(
    payload: dict[str, object],
) -> tuple[tuple[ResearchSource, ...], tuple[ResearchFact, ...]]:
    result = _object_dict(payload.get("Result"))
    if result is None:
        return (), ()
    raw_results = result.get("WebResults")
    if not isinstance(raw_results, list):
        return (), ()

    sources: list[ResearchSource] = []
    facts: list[ResearchFact] = []
    for raw_value in cast(list[object], raw_results):
        raw = _object_dict(raw_value)
        if raw is None:
            continue
        title = _bounded_text(raw.get("Title"), limit=300)
        url = _bounded_text(raw.get("Url"), limit=2_048)
        parsed = urlsplit(url)
        if (
            not title
            or parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
        ):
            continue
        publisher = _bounded_text(raw.get("SiteName"), limit=160) or parsed.netloc
        statement = _bounded_text(raw.get("Summary") or raw.get("Snippet"), limit=500)
        if not statement:
            statement = title
        published_at = (
            _bounded_text(
                raw.get("PublishTime") or raw.get("PublishedTime") or raw.get("PublishDate"),
                limit=80,
            )
            or None
        )
        source_id = f"volc_web_{len(sources) + 1}"
        sources.append(
            ResearchSource(
                source_id=source_id,
                title=title,
                url=url,
                publisher=publisher,
                published_at=published_at,
            )
        )
        facts.append(
            ResearchFact(
                statement=statement,
                fact_kind="reported_fact",
                source_ids=(source_id,),
                time_scope=published_at,
            )
        )
    return tuple(sources), tuple(facts)


def _provider_response_id(metadata: object, *, digest: str) -> str:
    metadata_object = _object_dict(metadata)
    if metadata_object is not None:
        request_id = _bounded_text(metadata_object.get("RequestId"), limit=160)
        if request_id:
            return request_id
    return f"volcengine:{digest[:24]}"


def _object_dict(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return cast(dict[str, object], value)


def _bounded_text(value: object, *, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    normalized = " ".join(value.split()).strip()
    return normalized[:limit]


async def _read_bounded_response(response: httpx.Response, *, max_bytes: int) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            raise WebResearchUnavailable("research response length is invalid") from None
        if declared_length < 0 or declared_length > max_bytes:
            raise WebResearchUnavailable("research response is too large")
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise WebResearchUnavailable("research response is too large")
        chunks.append(chunk)
    return b"".join(chunks)


__all__ = ["VolcengineWebSearchResearcher"]
