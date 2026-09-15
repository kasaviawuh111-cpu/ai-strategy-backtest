"""Bounded OpenAI-compatible transport for untrusted strategy candidates.

Only strict JSON is returned to :mod:`vibe_candidates`.  This module does not
compile or execute provider-authored code, and its public identity deliberately
excludes endpoints, headers, tokens, and user utterances.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from json import JSONDecodeError
from time import monotonic
from typing import Literal, cast
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from ashare_lab.ports.dialogue_progress import emit_model_reasoning, emit_progress
from ashare_lab.ports.request_context import current_candidate_attempt, current_request_id

from .vibe_candidates import (
    CandidateFailureKind,
    CandidateProviderIdentityView,
    CandidateTransportError,
    CandidateTransportRequest,
    CandidateTransportResponse,
    IdentifiedCandidatePayload,
)

_LOGGER = logging.getLogger(__name__)
_PAYLOAD_SIZE_FIELDS = (
    "utterance", "previousResponse", "capabilityMatrix", "responseSchema",
    "verifiedRows", "supplementalData", "dataSnapshot", "backtestResults", "recentTurns",
)
_THINKING_LANGUAGE_CONTRACT = (
    "\n思考语言要求：思考通道 reasoning_content 中的自然语言请使用简体中文，"
    "不要用整段英文展开分析。必要的指标名（如 RSI、MACD）、证券代码、公式、"
    "JSON 字段名和枚举值保持原样；引用原文时保留原文。"
    "此要求仅约束思考文本的语言，不改变输出结构，"
    "不要将思考过程复制到最终 JSON 中。"
)


def _safe_request_purpose(name: str) -> str:
    if len(name) <= 80 and name.isascii() and name.replace("_", "").isalnum():
        return name
    return "other"


def _parse_candidate_json(content: str) -> object:
    """Accept the provider's JSON object with an optional Markdown fence.

    Some JSON-only gateways still wrap a valid object in one ``json`` code
    fence.  The object remains subject to every downstream schema and Catalog
    check; accepting the wrapper avoids discarding an otherwise complete
    response.  Arbitrary prose or trailing material stays invalid.
    """

    stripped = content.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        first_newline = stripped.find("\n")
        if first_newline > 0 and stripped[:first_newline].casefold() in {"```", "```json"}:
            stripped = stripped[first_newline + 1:-3].strip()
    return json.loads(stripped, parse_constant=_reject_non_json_constant)


class CandidateProviderTransportError(CandidateTransportError):
    """Sanitized provider failure safe to cross the candidate boundary."""


CandidateProviderIdentity = CandidateProviderIdentityView


class _ProviderModel(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class _ChatMessage(_ProviderModel):
    content: str = Field(min_length=1)


class _ChatChoice(_ProviderModel):
    message: _ChatMessage


class _ChatCompletion(_ProviderModel):
    choices: tuple[_ChatChoice, ...] = Field(min_length=1, max_length=1)


class DisabledCandidateJsonTransport:
    """Explicitly report that deterministic misses have no configured provider."""

    def __init__(self, *, prompt_version: str, schema_version: str) -> None:
        self._identity = CandidateProviderIdentity(
            provider="disabled",
            model="unconfigured",
            prompt_version=prompt_version,
            schema_version=schema_version,
        )

    @property
    def identity(self) -> CandidateProviderIdentity:
        return self._identity

    async def generate_json(
        self,
        request: CandidateTransportRequest,
    ) -> CandidateTransportResponse:
        del request
        raise CandidateProviderTransportError("candidate provider is not configured")


class OpenAICompatibleCandidateTransport:
    """POST one bounded chat-completions request and return its JSON object."""

    def __init__(
        self,
        *,
        endpoint: str,
        provider: str,
        model: str,
        prompt_version: str,
        schema_version: str,
        api_key: SecretStr | None = None,
        timeout_seconds: float = 8.0,
        response_mode: Literal["json_schema", "json_object"] = "json_schema",
        thinking: Literal["disabled", "enabled"] = "disabled",
        reasoning_effort: Literal["low", "high", "max"] | None = None,
        max_request_bytes: int = 256 * 1024,
        max_response_bytes: int = 256 * 1024,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("candidate provider endpoint must be an absolute HTTP URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("candidate provider endpoint cannot contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("candidate provider endpoint cannot contain query or fragment")
        if not provider or not model or not prompt_version or not schema_version:
            raise ValueError("candidate provider identity fields cannot be empty")
        if not 0.25 <= timeout_seconds <= 300.0:
            raise ValueError("candidate provider timeout must be between 0.25 and 300 seconds")
        if response_mode not in {"json_schema", "json_object"}:
            raise ValueError("candidate provider response mode is unsupported")
        if thinking not in {"disabled", "enabled"}:
            raise ValueError("candidate provider thinking mode is unsupported")
        if reasoning_effort not in {None, "low", "high", "max"}:
            raise ValueError("candidate provider reasoning effort is unsupported")
        is_deepseek = provider.casefold().startswith("deepseek")
        if thinking == "enabled" and not is_deepseek:
            raise ValueError("candidate provider thinking is only supported for DeepSeek")
        if thinking == "enabled" and reasoning_effort is None:
            raise ValueError("enabled thinking requires a reasoning effort")
        if thinking == "disabled" and reasoning_effort is not None:
            raise ValueError("reasoning effort requires enabled thinking")
        if not 1_024 <= max_request_bytes <= 1_048_576:
            raise ValueError("candidate provider request limit is outside the safe range")
        if not 1_024 <= max_response_bytes <= 1_048_576:
            raise ValueError("candidate provider response limit is outside the safe range")
        self._endpoint = endpoint
        self._api_key = api_key
        self._timeout = (
            httpx.Timeout(
                connect=min(10.0, timeout_seconds),
                read=timeout_seconds,
                write=min(30.0, timeout_seconds),
                pool=min(10.0, timeout_seconds),
            )
            if is_deepseek
            else httpx.Timeout(timeout_seconds)
        )
        self._timeout_seconds = timeout_seconds
        self._response_mode: Literal["json_schema", "json_object"] = response_mode
        self._thinking: Literal["disabled", "enabled"] = thinking
        self._reasoning_effort: Literal["low", "high", "max"] | None = reasoning_effort
        self._max_request_bytes = max_request_bytes
        self._max_response_bytes = max_response_bytes
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._client_loop: asyncio.AbstractEventLoop | None = None
        self._timeout_fallback: OpenAICompatibleCandidateTransport | None = None
        self._identity = CandidateProviderIdentity(
            provider=provider,
            model=model,
            prompt_version=prompt_version,
            schema_version=schema_version,
        )

    @property
    def identity(self) -> CandidateProviderIdentity:
        return self._identity

    @property
    def response_mode(self) -> Literal["json_schema", "json_object"]:
        return self._response_mode

    def use_timeout_fallback(self, transport: OpenAICompatibleCandidateTransport) -> None:
        """Reuse a separately owned model pool; never install a recursive route."""
        if transport is self or transport._timeout_fallback is not None:
            raise ValueError("timeout fallback must be a distinct single-hop transport")
        self._timeout_fallback = transport

    @property
    def timeout_fallback_identity(self) -> CandidateProviderIdentity | None:
        return self._timeout_fallback.identity if self._timeout_fallback is not None else None

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self._timeout,
            # Retry only TCP/TLS establishment, never an HTTP response or a
            # partially delivered stream. Reuse healthy connections across turns.
            transport=self._transport or httpx.AsyncHTTPTransport(
                retries=2, trust_env=False,
                limits=httpx.Limits(keepalive_expiry=30.0),
            ),
            follow_redirects=False,
            trust_env=False,
        )

    async def startup(self) -> None:
        """Own one connection pool on the application's long-lived event loop."""
        loop = asyncio.get_running_loop()
        if self._client is not None:
            if self._client_loop is not loop:
                raise RuntimeError("candidate client belongs to another event loop")
            return
        self._client = self._new_client()
        self._client_loop = loop

    async def aclose(self) -> None:
        if self._client is not None:
            if self._client_loop is not asyncio.get_running_loop():
                raise RuntimeError("candidate client must close on its owner event loop")
            await self._client.aclose()
            self._client = None
            self._client_loop = None

    @asynccontextmanager
    async def _request_client(self) -> AsyncGenerator[httpx.AsyncClient]:
        if self._client is not None and self._client_loop is asyncio.get_running_loop():
            yield self._client
        else:
            # Backtest workers use short-lived asyncio.run loops. Their clients
            # must close before those loops stop, without touching the ASGI pool.
            async with self._new_client() as client:
                yield client

    def _default_system_footer(self, request: CandidateTransportRequest) -> str:
        return (
            f"Prompt contract: {self._identity.prompt_version}; "
            f"candidate schema: {self._identity.schema_version}; "
            f"return at most {request.max_candidates} candidates."
        )

    async def generate_json(
        self,
        request: CandidateTransportRequest,
    ) -> CandidateTransportResponse:
        try:
            return await self._generate_json_once(request)
        except CandidateTransportError as exc:
            fallback = self._timeout_fallback
            if fallback is None or not exc.timed_out:
                raise
            # Generation is side-effect free. Discard any partial primary JSON;
            # the secondary receives the identical contract, not partial output.
            # Cancellation, authentication, billing and validation failures do not
            # enter this branch. No second fallback or parallel duplicate call.
            _LOGGER.warning(
                "candidate_timeout_fallback request_id=%s attempt=%d purpose=%s "
                "from_model=%s to_model=%s",
                current_request_id(), current_candidate_attempt(),
                _safe_request_purpose(request.response_schema_name),
                self.identity.model, fallback.identity.model,
            )
            emit_progress("model_fallback", "快速模型响应超时，已交由 Pro 继续处理，无需重新输入。")
            return await fallback._generate_json_once(request)

    async def _generate_json_once(
        self, request: CandidateTransportRequest,
    ) -> CandidateTransportResponse:
        if not 1 <= request.max_candidates <= 3:
            raise CandidateProviderTransportError("candidate request exceeds the bounded limit")
        is_deepseek = self._identity.provider.casefold().startswith("deepseek")
        request_timeout = self._timeout
        total_timeout = None if is_deepseek else self._timeout_seconds
        if (
            is_deepseek
            and request.response_schema_name == "ashare_clarification_dialogue"
            and (request.user_payload or {}).get("responseOnly") is True
        ):
            # Give optional display generation more time to connect, not a
            # shorter read budget or a new deadline on an active model stream.
            request_timeout = httpx.Timeout(
                connect=20.0,
                read=self._timeout.read,
                write=self._timeout.write,
                pool=self._timeout.pool,
            )
        model_label = "DeepSeek" if is_deepseek else "模型"
        progress_message = {
            "public_search_query": f"已发起 {model_label} 检索词生成请求，等待响应。",
            "strategy_ideas": f"已发起 {model_label} 策略候选生成请求，等待响应。",
            "backtest_review": f"已发起 {model_label} 回测分析请求，等待响应。",
        }.get(request.response_schema_name, f"已发起 {model_label} 策略解析请求，等待响应。")
        emit_progress("model", progress_message)
        headers = {
            "Accept": "text/event-stream" if is_deepseek else "application/json",
            "Content-Type": "application/json",
        }
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key.get_secret_value()}"
        response_format: dict[str, object]
        if self._response_mode == "json_schema":
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.response_schema_name,
                    "strict": True,
                    "schema": request.response_schema,
                },
            }
        else:
            # Compatibility mode is still JSON-only.  The provider sees the
            # same contract and the local VibeBounded/Catalog validators remain
            # authoritative; there is deliberately no free-text fallback.
            response_format = {"type": "json_object"}
        user_payload: dict[str, object] = (
            dict(request.user_payload)
            if request.user_payload is not None
            else {
                "utterance": request.utterance,
                "instrumentContext": request.instrument_context,
                "asOfDate": request.as_of_date.isoformat(),
                "maxCandidates": request.max_candidates,
                "capabilityProjectionVersion": request.capability_projection_version,
                "capabilityProjectionHash": request.capability_projection_hash,
                "capabilityMatrix": request.capability_matrix,
            }
        )
        schema_contract = ""
        if self._response_mode == "json_object":
            # JSON-object gateways (including DeepSeek) do not receive the
            # schema through ``response_format``.  Put the existing bounded
            # schema in the prompt rather than asking the model to guess a DSL.
            user_payload["responseSchema"] = request.response_schema
            schema_contract = request.json_object_contract or (
                " The responseSchema field in the user JSON is authoritative. "
                "Return exactly one JSON object matching it; do not rename keys, "
                "add wrapper/action/reasoning fields, or use aliases in place of "
                "Catalog ids and triggers. Source-span start/end are zero-based "
                "Unicode character offsets in the complete utterance, and text "
                "must equal utterance[start:end]. Every entry/exit span must include "
                "the matching buy/sell action; when multiple leaves share one action, "
                "repeat the same complete source clause, including that action, for "
                "every leaf instead of splitting the clause into fragments. The phrase "
                "'成交量是过去 N 日平均的 M 倍' selects Catalog indicator "
                "volume.relative with trigger gte_multiple, baseline_period N, value M; "
                "strictly exceeding that mean multiple uses gt_multiple, not gte_multiple; "
                "and every other required Catalog parameter at its declared default; "
                "it does not select market.volume. When instrumentContext "
                "is present, set instrument_symbol and instrument_span to null because "
                "the host context is authoritative. When instrumentContext is null "
                "and the utterance explicitly names a stock code, instead extract "
                "instrument_symbol with its exchange suffix and exact instrument_span; "
                "do not omit an explicitly supplied stock. Supply every required parameter; "
                "use an exact Catalog default only when the utterance omitted it, and "
                "list only that field in defaulted_fields. In RSI clauses, wording "
                "such as '回到/到 N 上方' or '回到/到 N 下方' denotes the "
                "corresponding crosses_above or crosses_below transition, not a static "
                "above or below state."
            )
        body: dict[str, object] = {
            "model": self._identity.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"{request.system_contract}\n"
                        f"{request.system_footer or self._default_system_footer(request)}"
                        f"{schema_contract}"
                        f"{_THINKING_LANGUAGE_CONTRACT if self._thinking == 'enabled' else ''}"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        user_payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ],
            "n": 1,
            "response_format": response_format,
        }
        if is_deepseek:
            body["thinking"] = {"type": self._thinking}
            body["stream"] = True
        if is_deepseek and self._thinking == "enabled":
            # DeepSeek V4 thinking mode rejects or ignores sampling controls.
            # Keep them absent rather than relying on provider-side coercion.
            body["reasoning_effort"] = self._reasoning_effort
        else:
            # Preserve the existing deterministic non-thinking request.
            body["temperature"] = 0
        started_at = monotonic()
        request_size = 0
        response_size = 0
        response_status: int | None = None
        connection_retries = 0

        async def connection_progress(name: str, info: dict[str, object]) -> None:
            nonlocal connection_retries
            # httpcore emits this only before retrying TCP/TLS establishment.
            # Trace info can contain hostnames, headers or errors: never use it.
            del info
            if name == "connection.retry.started":
                connection_retries += 1
                try:
                    emit_progress(
                        "model_retry",
                        f"模型连接暂时未建立，正在自动重连（第 {connection_retries + 1}/3 次）。",
                    )
                except Exception:
                    # A disconnected progress consumer must not break recovery.
                    _LOGGER.warning("candidate_reconnect_progress_unavailable")

        try:
            request_bytes = json.dumps(
                body,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            request_size = len(request_bytes)
            if len(request_bytes) > self._max_request_bytes:
                # Only fixed field names and byte counts; never log user/model/data content.
                field_bytes = {
                    key: len(json.dumps(
                        user_payload[key], ensure_ascii=False, separators=(",", ":"),
                    ).encode("utf-8"))
                    for key in _PAYLOAD_SIZE_FIELDS if key in user_payload
                }
                _LOGGER.warning(
                    "candidate_request_oversized request_id=%s attempt=%d purpose=%s "
                    "request_bytes=%d max_request_bytes=%d payload_field_bytes=%s",
                    current_request_id(), current_candidate_attempt(),
                    _safe_request_purpose(request.response_schema_name), request_size,
                    self._max_request_bytes, json.dumps(field_bytes, separators=(",", ":")),
                )
                raise CandidateProviderTransportError("candidate provider request is too large")
            # All DeepSeek streams retain their original inactivity bound with
            # no total deadline. Other providers retain their total deadline.
            async with (
                self._request_client() as client,
                asyncio.timeout(total_timeout),
                client.stream(
                    "POST",
                    self._endpoint,
                    headers=headers,
                    content=request_bytes,
                    timeout=request_timeout,
                    extensions={"trace": connection_progress},
                ) as response,
            ):
                response_status = response.status_code
                if response.status_code < 200 or response.status_code >= 300:
                    raise CandidateProviderTransportError(
                        "candidate provider request failed",
                        failure_kind=self._http_failure_kind(response.status_code),
                        http_status=response.status_code,
                    )
                if is_deepseek:
                    candidate_content, response_size = await _read_deepseek_stream(
                        response,
                        max_bytes=self._max_response_bytes,
                        progress_timeout=self._timeout_seconds,
                    )
                else:
                    raw = await _read_bounded_response(
                        response,
                        max_bytes=self._max_response_bytes,
                    )
                    response_size = len(raw)
                    envelope = _ChatCompletion.model_validate_json(raw)
                    candidate_content = envelope.choices[0].message.content
            candidate_payload = _parse_candidate_json(candidate_content)
            if not isinstance(candidate_payload, dict):
                raise CandidateProviderTransportError(
                    "candidate provider response is not a JSON object"
                )
            _LOGGER.warning(
                "candidate_transport_ok request_id=%s attempt=%d "
                "provider=%s model=%s status=%s elapsed_ms=%d "
                "request_bytes=%d response_bytes=%d response_mode=%s thinking=%s purpose=%s",
                current_request_id(),
                current_candidate_attempt(),
                self._identity.provider,
                self._identity.model,
                response_status,
                round((monotonic() - started_at) * 1000),
                request_size,
                response_size,
                self._response_mode,
                self._thinking,
                _safe_request_purpose(request.response_schema_name),
            )
            emit_progress("validation", "模型已返回结果，正在校验结构与可执行条件。")
            return IdentifiedCandidatePayload(
                cast(dict[str, object], candidate_payload), self.identity,
            )
        except CandidateProviderTransportError as exc:
            failure = _classified_transport_failure(exc, response_status)
            if not (failure.timed_out and self._timeout_fallback is not None):
                emit_progress("failed", "模型调用未完成，正在返回错误说明。")
            _LOGGER.warning(
                "candidate_transport_failed request_id=%s attempt=%d "
                "provider=%s model=%s status=%s elapsed_ms=%d "
                "request_bytes=%d response_bytes=%d response_mode=%s thinking=%s reason=%s "
                "purpose=%s",
                current_request_id(),
                current_candidate_attempt(),
                self._identity.provider,
                self._identity.model,
                response_status,
                round((monotonic() - started_at) * 1000),
                request_size,
                response_size,
                self._response_mode,
                self._thinking,
                failure.failure_kind if failure.is_classified else _transport_failure_reason(exc),
                _safe_request_purpose(request.response_schema_name),
            )
            raise failure from None
        except (
            httpx.HTTPError,
            TimeoutError,
            JSONDecodeError,
            UnicodeDecodeError,
            ValidationError,
            TypeError,
            ValueError,
        ) as exc:
            timed_out = isinstance(exc, (httpx.TimeoutException, TimeoutError))
            failure_kind: CandidateFailureKind = (
                "timeout" if timed_out else "connection_failed"
                if isinstance(exc, httpx.HTTPError) else "invalid_response"
            )
            if not (timed_out and self._timeout_fallback is not None):
                emit_progress(
                    "failed",
                    "模型请求超时，正在返回错误说明。"
                    if timed_out else "模型连接未完成，正在返回错误说明。"
                    if failure_kind == "connection_failed"
                    else "模型响应未通过检查，正在返回错误说明。",
                )
            _LOGGER.warning(
                "candidate_transport_failed request_id=%s attempt=%d "
                "provider=%s model=%s status=%s elapsed_ms=%d "
                "request_bytes=%d response_bytes=%d response_mode=%s thinking=%s "
                "reason=%s error_type=%s purpose=%s",
                current_request_id(),
                current_candidate_attempt(),
                self._identity.provider,
                self._identity.model,
                response_status,
                round((monotonic() - started_at) * 1000),
                request_size,
                response_size,
                self._response_mode,
                self._thinking,
                failure_kind,
                type(exc).__name__,
                _safe_request_purpose(request.response_schema_name),
            )
            raise CandidateProviderTransportError(
                "candidate provider response unavailable", timed_out=timed_out,
                failure_kind=failure_kind, http_status=response_status,
            ) from None

    def _http_failure_kind(self, status: int) -> CandidateFailureKind:
        if status == 401:
            return "authentication_failed"
        if status == 403:
            return "permission_denied"
        if status == 402:
            official_deepseek = (
                self._identity.provider.casefold() == "deepseek"
                and urlsplit(self._endpoint).hostname == "api.deepseek.com"
            )
            return "insufficient_balance" if official_deepseek else "billing_restricted"
        if status == 429:
            return "rate_limited"
        if 500 <= status <= 599:
            return "service_unavailable"
        return "unknown"


def _classified_transport_failure(
    exc: CandidateProviderTransportError, status: int | None,
) -> CandidateProviderTransportError:
    if exc.is_classified:
        return exc
    reason = _transport_failure_reason(exc)
    kind: CandidateFailureKind = "unknown"
    if reason in {"stream_truncated", "stream_incomplete", "stream_empty"}:
        kind = "incomplete_response"
    elif reason in {
        "non_object_payload", "response_length_invalid", "response_too_large",
        "stream_content_type_invalid", "stream_event_invalid", "stream_error",
    }:
        kind = "invalid_response"
    return CandidateProviderTransportError(
        str(exc), timed_out=exc.timed_out, failure_kind=kind, http_status=status,
    )


async def _read_bounded_response(
    response: httpx.Response,
    *,
    max_bytes: int,
) -> bytes:
    _validate_declared_length(response, max_bytes=max_bytes)
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise CandidateProviderTransportError("candidate provider response is too large")
        chunks.append(chunk)
    return b"".join(chunks)


async def _read_deepseek_stream(
    response: httpx.Response,
    *,
    max_bytes: int,
    progress_timeout: float | None = None,
) -> tuple[str, int]:
    """Keep final content separate; optionally stream reasoning to the local UI."""

    wire_limit = max_bytes * 32
    event_limit = max_bytes * 2
    _validate_declared_length(response, max_bytes=wire_limit)
    content_type = response.headers.get("Content-Type", "").casefold()
    if "text/event-stream" not in content_type:
        raise CandidateProviderTransportError(
            "candidate provider stream content type is invalid"
        )

    buffer = b""
    event_data: list[bytes] = []
    content_parts: list[str] = []
    wire_total = 0
    semantic_total = 0
    last_progress = monotonic()
    done = False
    finish_reasons: list[str] = []
    reasoning_last_emit: float | None = None
    content_last_emit: float | None = None

    def emit_delta_status(kind: Literal["reasoning", "content"]) -> None:
        nonlocal reasoning_last_emit, content_last_emit
        now = monotonic()
        if kind == "reasoning":
            if reasoning_last_emit is None or now - reasoning_last_emit >= 15:
                emit_progress("model_reasoning", "已收到模型推理流，仍在生成。")
                reasoning_last_emit = now
            return
        if content_last_emit is None:
            emit_progress("model_output", "模型开始返回结果。")
            content_last_emit = now
        elif now - content_last_emit >= 15:
            emit_progress("model_output", "模型仍在返回结果。")
            content_last_emit = now

    def dispatch_event() -> None:
        nonlocal done, semantic_total
        if not event_data:
            return
        raw_event = b"\n".join(event_data)
        event_data.clear()
        if raw_event.strip() == b"[DONE]":
            done = True
            return
        try:
            decoded = json.loads(raw_event)
        except (JSONDecodeError, UnicodeDecodeError):
            raise CandidateProviderTransportError(
                "candidate provider stream event is invalid"
            ) from None
        if not isinstance(decoded, dict):
            raise CandidateProviderTransportError("candidate provider stream failed")
        event = cast(dict[str, object], decoded)
        if "error" in event:
            raise CandidateProviderTransportError("candidate provider stream failed")
        raw_choices = event.get("choices")
        if not isinstance(raw_choices, list):
            raise CandidateProviderTransportError(
                "candidate provider stream event is invalid"
            )
        choices = cast(list[object], raw_choices)
        if len(choices) > 1:
            raise CandidateProviderTransportError(
                "candidate provider stream event is invalid"
            )
        if not choices:
            return
        raw_choice = choices[0]
        if not isinstance(raw_choice, dict):
            raise CandidateProviderTransportError(
                "candidate provider stream event is invalid"
            )
        choice = cast(dict[str, object], raw_choice)
        raw_delta = choice.get("delta")
        if not isinstance(raw_delta, dict):
            raise CandidateProviderTransportError(
                "candidate provider stream event is invalid"
            )
        delta = cast(dict[str, object], raw_delta)
        reasoning_content = delta.get("reasoning_content")
        content = delta.get("content")
        if reasoning_content is not None and not isinstance(reasoning_content, str):
            raise CandidateProviderTransportError(
                "candidate provider stream event is invalid"
            )
        if content is not None and not isinstance(content, str):
            raise CandidateProviderTransportError(
                "candidate provider stream event is invalid"
            )
        if reasoning_content:
            semantic_total += len(reasoning_content.encode("utf-8"))
            if semantic_total > max_bytes:
                raise CandidateProviderTransportError(
                    "candidate provider response is too large"
                )
            emit_delta_status("reasoning")
            emit_model_reasoning(reasoning_content)
        if content:
            semantic_total += len(content.encode("utf-8"))
            if semantic_total > max_bytes:
                raise CandidateProviderTransportError(
                    "candidate provider response is too large"
                )
            content_parts.append(content)
            emit_delta_status("content")
        current_finish = choice.get("finish_reason")
        if current_finish is not None:
            if not isinstance(current_finish, str) or (
                finish_reasons and finish_reasons[0] != current_finish
            ):
                raise CandidateProviderTransportError(
                    "candidate provider stream event is invalid"
                )
            if not finish_reasons:
                finish_reasons.append(current_finish)

    def consume_line(line: bytes) -> None:
        if not line or line.startswith(b":"):
            if not line:
                dispatch_event()
            return
        if line.startswith(b"data:"):
            value = line[5:]
            event_data.append(value[1:] if value.startswith(b" ") else value)
            if sum(len(item) for item in event_data) > event_limit:
                raise CandidateProviderTransportError(
                    "candidate provider response is too large"
                )

    stream = response.aiter_bytes().__aiter__()
    while True:
        remaining = (
            None if progress_timeout is None
            else progress_timeout - (monotonic() - last_progress)
        )
        if remaining is not None and remaining <= 0:
            raise TimeoutError("model stream made no semantic progress")
        try:
            # Bound the wait itself, not just arrival of the next heartbeat.
            async with asyncio.timeout(remaining):
                chunk = await anext(stream)
        except StopAsyncIteration:
            break
        except TimeoutError:
            raise TimeoutError("model stream made no semantic progress") from None
        previous_semantic_total = semantic_total
        wire_total += len(chunk)
        if wire_total > wire_limit:
            raise CandidateProviderTransportError("candidate provider response is too large")
        buffer += chunk
        while b"\n" in buffer:
            raw_line, buffer = buffer.split(b"\n", 1)
            consume_line(raw_line[:-1] if raw_line.endswith(b"\r") else raw_line)
            if done:
                break
        if semantic_total > previous_semantic_total:
            last_progress = monotonic()
        if len(buffer) > event_limit:
            raise CandidateProviderTransportError("candidate provider response is too large")
        if done:
            break
    if not done and buffer:
        consume_line(buffer[:-1] if buffer.endswith(b"\r") else buffer)
    if not done and event_data:
        dispatch_event()
    if not done:
        raise CandidateProviderTransportError(
            "candidate provider stream ended before DONE"
        )
    if finish_reasons != ["stop"]:
        raise CandidateProviderTransportError(
            "candidate provider stream did not finish normally"
        )
    if not content_parts:
        raise CandidateProviderTransportError("candidate provider stream content is empty")
    return "".join(content_parts), wire_total


def _validate_declared_length(response: httpx.Response, *, max_bytes: int) -> None:
    content_length = response.headers.get("Content-Length")
    if content_length is None:
        return
    try:
        declared_length = int(content_length)
    except ValueError:
        raise CandidateProviderTransportError(
            "candidate provider response length is invalid"
        ) from None
    if declared_length < 0 or declared_length > max_bytes:
        raise CandidateProviderTransportError("candidate provider response is too large")


def _reject_non_json_constant(value: str) -> None:
    del value
    raise ValueError("non-standard JSON constant")


def _transport_failure_reason(exc: CandidateProviderTransportError) -> str:
    """Map only our static sanitized messages into log-safe reason enums."""

    return {
        "candidate provider request is too large": "request_too_large",
        "candidate provider request failed": "http_status_error",
        "candidate provider response is not a JSON object": "non_object_payload",
        "candidate provider response length is invalid": "response_length_invalid",
        "candidate provider response is too large": "response_too_large",
        "candidate provider stream content type is invalid": "stream_content_type_invalid",
        "candidate provider stream event is invalid": "stream_event_invalid",
        "candidate provider stream failed": "stream_error",
        "candidate provider stream ended before DONE": "stream_truncated",
        "candidate provider stream did not finish normally": "stream_incomplete",
        "candidate provider stream content is empty": "stream_empty",
    }.get(str(exc), "transport_error")
