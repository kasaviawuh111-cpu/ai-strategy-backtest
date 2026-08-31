"""Bounded OpenAI-compatible transport for untrusted strategy candidates.

Only strict JSON is returned to :mod:`vibe_candidates`.  This module does not
compile or execute provider-authored code, and its public identity deliberately
excludes endpoints, headers, tokens, and user utterances.
"""

from __future__ import annotations

import json
from json import JSONDecodeError
from typing import Literal, cast
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from .vibe_candidates import (
    CandidateProviderIdentityView,
    CandidateTransportError,
    CandidateTransportRequest,
    CandidateTransportResponse,
)


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
        if not 0.25 <= timeout_seconds <= 30.0:
            raise ValueError("candidate provider timeout must be between 0.25 and 30 seconds")
        if response_mode not in {"json_schema", "json_object"}:
            raise ValueError("candidate provider response mode is unsupported")
        if not 1_024 <= max_request_bytes <= 1_048_576:
            raise ValueError("candidate provider request limit is outside the safe range")
        if not 1_024 <= max_response_bytes <= 1_048_576:
            raise ValueError("candidate provider response limit is outside the safe range")
        self._endpoint = endpoint
        self._api_key = api_key
        self._timeout = httpx.Timeout(timeout_seconds)
        self._response_mode: Literal["json_schema", "json_object"] = response_mode
        self._max_request_bytes = max_request_bytes
        self._max_response_bytes = max_response_bytes
        self._transport = transport
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

    async def generate_json(
        self,
        request: CandidateTransportRequest,
    ) -> CandidateTransportResponse:
        if not 1 <= request.max_candidates <= 3:
            raise CandidateProviderTransportError("candidate request exceeds the bounded limit")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key.get_secret_value()}"
        response_format: dict[str, object]
        if self._response_mode == "json_schema":
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "ashare_bounded_strategy_candidates",
                    "strict": True,
                    "schema": request.response_schema,
                },
            }
        else:
            # Compatibility mode is still JSON-only.  The provider sees the
            # same contract and the local VibeBounded/Catalog validators remain
            # authoritative; there is deliberately no free-text fallback.
            response_format = {"type": "json_object"}
        body = {
            "model": self._identity.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"{request.system_contract}\n"
                        f"Prompt contract: {self._identity.prompt_version}; "
                        f"candidate schema: {self._identity.schema_version}; "
                        f"return at most {request.max_candidates} candidates."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "utterance": request.utterance,
                            "instrumentContext": request.instrument_context,
                            "asOfDate": request.as_of_date.isoformat(),
                            "maxCandidates": request.max_candidates,
                            "capabilityProjectionVersion": (request.capability_projection_version),
                            "capabilityProjectionHash": request.capability_projection_hash,
                            "capabilityMatrix": request.capability_matrix,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ],
            "temperature": 0,
            "n": 1,
            "response_format": response_format,
        }
        try:
            request_bytes = json.dumps(
                body,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            if len(request_bytes) > self._max_request_bytes:
                raise CandidateProviderTransportError("candidate provider request is too large")
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
                if response.status_code < 200 or response.status_code >= 300:
                    raise CandidateProviderTransportError("candidate provider request failed")
                raw = await _read_bounded_response(
                    response,
                    max_bytes=self._max_response_bytes,
                )
            envelope = _ChatCompletion.model_validate_json(raw)
            candidate_payload = json.loads(
                envelope.choices[0].message.content,
                parse_constant=_reject_non_json_constant,
            )
            if not isinstance(candidate_payload, dict):
                raise CandidateProviderTransportError(
                    "candidate provider response is not a JSON object"
                )
            return cast(dict[str, object], candidate_payload)
        except CandidateProviderTransportError:
            raise
        except (
            httpx.HTTPError,
            JSONDecodeError,
            UnicodeDecodeError,
            ValidationError,
            TypeError,
            ValueError,
        ):
            raise CandidateProviderTransportError(
                "candidate provider response unavailable"
            ) from None


async def _read_bounded_response(
    response: httpx.Response,
    *,
    max_bytes: int,
) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            raise CandidateProviderTransportError(
                "candidate provider response length is invalid"
            ) from None
        if declared_length < 0 or declared_length > max_bytes:
            raise CandidateProviderTransportError("candidate provider response is too large")
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise CandidateProviderTransportError("candidate provider response is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _reject_non_json_constant(value: str) -> None:
    del value
    raise ValueError("non-standard JSON constant")
