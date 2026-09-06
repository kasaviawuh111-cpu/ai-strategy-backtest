"""DeepSeek Responses API adapter for non-executable current-fact research.

This adapter deliberately uses DeepSeek's server-side ``web_search`` tool and
fails closed unless the response proves that a search call completed.  It does
not share the chat-completions path used by the bounded strategy compiler.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from json import JSONDecodeError
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

from ashare_lab.ports.current_fact_research import (
    CurrentFactResearchRequest,
    CurrentFactResearchResult,
    ResearchFact,
    ResearchSource,
)

_SCHEMA_VERSION = "current-fact-research.v1"
_PROMPT_VERSION = "deepseek-web-research.prompt.v1"
_LOGGER = logging.getLogger(__name__)
_EXECUTION_ADVICE_RE = re.compile(
    r"(?:建议|应当|应该|可以|可|宜|立刻|立即|马上)\s*"
    r"(?:买入|卖出|持有|加仓|减仓|下单|开仓|平仓)|"
    r"(?:生成|触发).{0,8}(?:交易信号|买入信号|卖出信号)|"
    r"(?:executable|StrategySpec|执行DSL|可执行策略)",
    re.IGNORECASE,
)


class WebResearchUnavailable(RuntimeError):
    """Sanitized, fail-closed provider or evidence failure."""


class _StrictProviderModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class _ProviderSource(_StrictProviderModel):
    source_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(min_length=1, max_length=300)
    url: str = Field(min_length=8, max_length=2_048)
    publisher: str = Field(min_length=1, max_length=160)
    published_at: str | None = Field(default=None, max_length=80)


class _ProviderFact(_StrictProviderModel):
    statement: str = Field(min_length=1, max_length=500)
    fact_kind: Literal["reported_fact", "inference", "uncertain"]
    source_ids: tuple[str, ...] = Field(min_length=1, max_length=8)
    time_scope: str | None = Field(default=None, max_length=120)


class _ProviderResearchPayload(_StrictProviderModel):
    summary: str = Field(min_length=1, max_length=500)
    facts: tuple[_ProviderFact, ...] = Field(default=(), max_length=12)
    sources: tuple[_ProviderSource, ...] = Field(default=(), max_length=20)
    unresolved_questions: tuple[str, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def has_facts_or_an_explicit_unknown(self) -> _ProviderResearchPayload:
        if not self.facts and not self.unresolved_questions:
            raise ValueError("research must return sourced facts or an explicit unresolved item")
        if len({item.source_id for item in self.sources}) != len(self.sources):
            raise ValueError("research source ids must be unique")
        return self


class _ResponseContent(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    type: str
    text: str | None = None


class _ResponseOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    type: str
    status: str | None = None
    content: tuple[_ResponseContent, ...] = ()


class _ResponseEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str = Field(min_length=1)
    status: str
    model: str = Field(min_length=1)
    output: tuple[_ResponseOutput, ...]


class DeepSeekWebResearcher:
    """Retrieve current public facts without producing executable strategy data."""

    def __init__(
        self,
        *,
        api_key: SecretStr,
        endpoint: str = "https://api.deepseek.com/responses",
        model: str = "deepseek-v4-flash",
        timeout_seconds: float = 30.0,
        max_request_bytes: int = 128 * 1024,
        max_response_bytes: int = 512 * 1024,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("research endpoint must be an absolute HTTP URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("research endpoint cannot contain credentials")
        if parsed.query or parsed.fragment or not parsed.path.rstrip("/").endswith("/responses"):
            raise ValueError("research endpoint must target the Responses API")
        if not api_key.get_secret_value().strip():
            raise ValueError("research API key cannot be empty")
        if model not in {
            "deepseek-v4-flash",
            "deepseek-v4-pro",
            "deepseek-v4-flash-vision-exp",
        }:
            raise ValueError("research model is not supported by DeepSeek Responses API")
        # One web-search round is typically 15-40s (18.5s measured locally);
        # multi-round searches take longer.  Cap at 5 minutes: beyond that it is
        # a network or provider failure, not the model still thinking.
        if not 0.25 <= timeout_seconds <= 300.0:
            raise ValueError("research timeout must be between 0.25 and 300 seconds")
        if not 1_024 <= max_request_bytes <= 1_048_576:
            raise ValueError("research request limit is outside the safe range")
        if not 1_024 <= max_response_bytes <= 2_097_152:
            raise ValueError("research response limit is outside the safe range")
        self._endpoint = endpoint
        self._api_key = api_key
        self._model = model
        self._timeout = httpx.Timeout(timeout_seconds)
        self._max_request_bytes = max_request_bytes
        self._max_response_bytes = max_response_bytes
        self._transport = transport
        self._clock = clock or (lambda: datetime.now(UTC))

    async def research(
        self,
        request: CurrentFactResearchRequest,
    ) -> CurrentFactResearchResult:
        body = self._request_body(request)
        try:
            request_bytes = json.dumps(
                body,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError):
            raise WebResearchUnavailable("research request is invalid") from None
        if len(request_bytes) > self._max_request_bytes:
            raise WebResearchUnavailable("research request is too large")

        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }
        try:
            async with (
                httpx.AsyncClient(
                    timeout=self._timeout,
                    transport=self._transport,
                    follow_redirects=False,
                    trust_env=False,
                ) as client,
                client.stream(
                    "POST",
                    self._endpoint,
                    headers=headers,
                    content=request_bytes,
                ) as response,
            ):
                if not 200 <= response.status_code < 300:
                    raise WebResearchUnavailable("research provider request failed")
                raw_response = await _read_bounded_response(
                    response,
                    max_bytes=self._max_response_bytes,
                )
            envelope = _ResponseEnvelope.model_validate_json(raw_response)
            if envelope.status != "completed":
                raise WebResearchUnavailable("research provider response is incomplete")
            search_call_count = sum(
                item.type == "web_search_call" and item.status == "completed"
                for item in envelope.output
            )
            if search_call_count < 1:
                raise WebResearchUnavailable("search evidence is unavailable")
            payload = _parse_research_payload(envelope)
            _validate_payload(payload)
            retrieved_at = self._clock()
            if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
                raise WebResearchUnavailable("research clock must include a timezone")
            return CurrentFactResearchResult(
                provider="deepseek",
                model=envelope.model,
                provider_response_id=envelope.id,
                query=request.query,
                purpose=request.purpose,
                as_of=request.as_of,
                summary=payload.summary,
                facts=tuple(
                    ResearchFact(
                        statement=item.statement,
                        fact_kind=item.fact_kind,
                        source_ids=item.source_ids,
                        time_scope=item.time_scope,
                    )
                    for item in payload.facts
                ),
                sources=tuple(
                    ResearchSource(
                        source_id=item.source_id,
                        title=item.title,
                        url=item.url,
                        publisher=item.publisher,
                        published_at=item.published_at,
                    )
                    for item in payload.sources
                ),
                unresolved_questions=payload.unresolved_questions,
                retrieved_at=retrieved_at,
                response_sha256=hashlib.sha256(raw_response).hexdigest(),
                search_call_count=search_call_count,
            )
        except WebResearchUnavailable as exc:
            # Every message emitted by this adapter is a static, sanitized
            # reason.  Log that reason (never the response body, URL query or
            # credential) so an optional-search fallback remains diagnosable.
            _LOGGER.info("deepseek web research unavailable reason=%s", str(exc))
            raise
        except (
            httpx.HTTPError,
            JSONDecodeError,
            UnicodeDecodeError,
            ValidationError,
            TypeError,
            ValueError,
        ) as exc:
            _LOGGER.info(
                "deepseek web research unavailable reason=transport_or_response type=%s",
                type(exc).__name__,
            )
            raise WebResearchUnavailable("research provider response unavailable") from None

    def _request_body(self, request: CurrentFactResearchRequest) -> dict[str, Any]:
        return {
            "model": self._model,
            "input": [
                {"role": "system", "content": _system_contract()},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "asOf": request.as_of.isoformat(),
                            "instrumentContext": request.instrument_context,
                            "purpose": request.purpose.value,
                            "query": request.query,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ],
            "tools": [{"type": "web_search"}],
            "tool_choice": {"type": "web_search"},
            "reasoning": {"effort": "low"},
            "temperature": 0,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "ashare_current_fact_research",
                    "schema": _ProviderResearchPayload.model_json_schema(),
                }
            },
        }


def _system_contract() -> str:
    return (
        "你只做当前公开事实检索，用联网搜索核实用户观点、未知实体或时效性事实。"
        "输出必须严格符合 JSON Schema。每条 fact 都必须引用 sources 中存在的 source_id，"
        "来源 URL 必须是搜索实际找到的网页，不得编造。区分 reported_fact、inference 和"
        "uncertain；无法核实就写入 unresolved_questions。instrumentContext 仅是上下文，"
        "不得把未知实体猜成证券，也不得自动映射股票。不得输出投资建议、买卖方向、"
        "阈值、交易规则、DSL、StrategySpec、executable 字段、交易信号或数据事实以外的"
        "执行内容。这些结果只用于解释和澄清，不能直接进入回测。"
        f"契约版本 {_PROMPT_VERSION}；结果版本 {_SCHEMA_VERSION}。"
    )


def _parse_research_payload(envelope: _ResponseEnvelope) -> _ProviderResearchPayload:
    text_parts = [
        content.text
        for item in envelope.output
        if item.type == "message" and item.status == "completed"
        for content in item.content
        if content.type == "output_text" and content.text is not None
    ]
    if len(text_parts) != 1:
        raise WebResearchUnavailable("research provider returned an ambiguous payload")
    try:
        return _ProviderResearchPayload.model_validate_json(_strip_code_fence(text_parts[0]))
    except ValidationError as exc:
        # Log only field paths and error types: enough to see *which* constraint
        # the model broke, never the response body.  Without this, a schema miss
        # is indistinguishable from a network failure at the call site.
        _LOGGER.warning(
            "deepseek web research payload failed schema validation errors=%s",
            [(".".join(map(str, e["loc"])) or "<root>", e["type"]) for e in exc.errors()[:8]],
        )
        raise WebResearchUnavailable("research provider payload failed schema validation") from None


_CODE_FENCE_RE = re.compile(r"\A\s*```(?:json|JSON)?\s*\n?(.*?)\n?\s*```\s*\Z", re.DOTALL)


def _strip_code_fence(text: str) -> str:
    """Unwrap a Markdown ```json fence if the whole payload is one.

    DeepSeek's Responses API does not strictly enforce ``text.format`` and
    routinely wraps otherwise valid JSON in a fence, which makes
    ``model_validate_json`` fail at column 1.  Only a fence spanning the entire
    text is removed; anything else is passed through untouched so a genuinely
    malformed payload still fails closed.
    """

    match = _CODE_FENCE_RE.match(text)
    return match.group(1) if match else text


def _validate_payload(payload: _ProviderResearchPayload) -> None:
    source_ids = {item.source_id for item in payload.sources}
    if any(not set(item.source_ids).issubset(source_ids) for item in payload.facts):
        raise WebResearchUnavailable("research source references are invalid")
    for source in payload.sources:
        parsed = urlsplit(source.url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise WebResearchUnavailable("research source URL is invalid")
    display_text = " ".join(
        (
            payload.summary,
            *(item.statement for item in payload.facts),
            *payload.unresolved_questions,
        )
    )
    if _EXECUTION_ADVICE_RE.search(display_text):
        raise WebResearchUnavailable("execution advice is not allowed")


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


__all__ = ["DeepSeekWebResearcher", "WebResearchUnavailable"]
