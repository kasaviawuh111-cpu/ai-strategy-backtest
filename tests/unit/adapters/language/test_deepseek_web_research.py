from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from ashare_lab.adapters.language.deepseek_web_research import (
    DeepSeekWebResearcher,
    WebResearchUnavailable,
)
from ashare_lab.ports.current_fact_research import (
    CurrentFactResearchRequest,
    ResearchPurpose,
)


def _request() -> CurrentFactResearchRequest:
    return CurrentFactResearchRequest(
        query="我讨厌特朗普，这对 A 股可能有什么影响？",
        purpose=ResearchPurpose.VIEWPOINT,
        as_of=datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
        instrument_context=None,
    )


def _provider_payload() -> dict[str, object]:
    return {
        "summary": "这是一项需要先核实近期政策与市场事实的观点。",
        "facts": [
            {
                "statement": "近期公开政策讨论涉及对华贸易与关税。",
                "fact_kind": "reported_fact",
                "source_ids": ["src_1"],
                "time_scope": "截至 2026-09-03",
            }
        ],
        "sources": [
            {
                "source_id": "src_1",
                "title": "Official policy release",
                "url": "https://example.gov/policy",
                "publisher": "example.gov",
                "published_at": "2026-09-02T14:00:00Z",
            }
        ],
        "unresolved_questions": ["该政策是否已正式生效仍需以主管部门文件为准。"],
    }


def _response(
    payload: dict[str, object],
    *,
    with_search: bool = True,
    text_override: str | None = None,
) -> bytes:
    output: list[dict[str, object]] = []
    if with_search:
        output.append(
            {
                "type": "web_search_call",
                "id": "ws_1",
                "status": "completed",
                "action": {"type": "search", "query": "特朗普 对华关税 近期政策"},
            }
        )
    output.append(
        {
            "type": "message",
            "id": "msg_1",
            "status": "completed",
            "role": "assistant",
            "content": [
                {
                    "type": "output_text",
                    "text": (
                        json.dumps(payload, ensure_ascii=False)
                        if text_override is None
                        else text_override
                    ),
                    "annotations": [],
                }
            ],
        }
    )
    return json.dumps(
        {
            "id": "resp_1",
            "object": "response",
            "created_at": 1_788_436_800,
            "status": "completed",
            "model": "deepseek-v4-flash",
            "output": output,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 20,
                "total_tokens": 30,
            },
        },
        ensure_ascii=False,
    ).encode()


@pytest.mark.asyncio
async def test_responses_api_forces_server_web_search_and_returns_sourced_facts() -> None:
    raw_response = _response(_provider_payload())

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://api.deepseek.com/responses")
        assert request.headers["Authorization"] == "Bearer secret-value"
        assert request.extensions["timeout"]["read"] == 30.0
        body = json.loads(request.content)
        assert body["model"] == "deepseek-v4-flash"
        assert body["tools"] == [{"type": "web_search"}]
        assert body["tool_choice"] == {"type": "web_search"}
        assert body["text"]["format"]["type"] == "json_schema"
        assert "messages" not in body
        assert "response_format" not in body
        assert body["input"][1]["role"] == "user"
        user_payload = json.loads(body["input"][1]["content"])
        assert user_payload == {
            "asOf": "2026-09-03T12:00:00+00:00",
            "instrumentContext": None,
            "purpose": "viewpoint",
            "query": "我讨厌特朗普，这对 A 股可能有什么影响？",
        }
        return httpx.Response(200, content=raw_response)

    researcher = DeepSeekWebResearcher(
        api_key=SecretStr("secret-value"),
        transport=httpx.MockTransport(handler),
        clock=lambda: datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
    )

    result = await researcher.research(_request())

    assert result.provider == "deepseek"
    assert result.model == "deepseek-v4-flash"
    assert result.provider_response_id == "resp_1"
    assert result.response_sha256 == hashlib.sha256(raw_response).hexdigest()
    assert result.search_call_count == 1
    assert result.facts[0].source_ids == ("src_1",)
    assert result.sources[0].url == "https://example.gov/policy"
    assert result.executable_strategy is None
    assert result.signal_records == ()


@pytest.mark.asyncio
async def test_provider_failure_logs_only_a_sanitized_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b'provider-body-secret')

    researcher = DeepSeekWebResearcher(
        api_key=SecretStr("api-key-secret"),
        transport=httpx.MockTransport(handler),
    )

    with (
        caplog.at_level(logging.INFO),
        pytest.raises(WebResearchUnavailable, match="provider request failed"),
    ):
        await researcher.research(_request())

    assert "reason=research provider request failed" in caplog.text
    assert "provider-body-secret" not in caplog.text
    assert "api-key-secret" not in caplog.text


@pytest.mark.asyncio
async def test_rejects_answer_when_provider_did_not_execute_web_search() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_response(_provider_payload(), with_search=False))

    researcher = DeepSeekWebResearcher(
        api_key=SecretStr("secret-value"),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(WebResearchUnavailable, match="search evidence is unavailable"):
        await researcher.research(_request())


@pytest.mark.asyncio
async def test_rejects_unknown_source_reference_and_execution_advice() -> None:
    unknown_source = _provider_payload()
    unknown_source["facts"] = [
        {
            "statement": "近期存在相关政策讨论。",
            "fact_kind": "reported_fact",
            "source_ids": ["src_missing"],
            "time_scope": None,
        }
    ]
    execution_advice = _provider_payload()
    execution_advice["summary"] = "建议立即买入相关股票。"

    responses = iter((_response(unknown_source), _response(execution_advice)))

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=next(responses))

    researcher = DeepSeekWebResearcher(
        api_key=SecretStr("secret-value"),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(WebResearchUnavailable, match="source references are invalid"):
        await researcher.research(_request())
    with pytest.raises(WebResearchUnavailable, match="execution advice is not allowed"):
        await researcher.research(_request())


@pytest.mark.asyncio
async def test_unwraps_markdown_code_fence_around_payload() -> None:
    # DeepSeek does not strictly honour text.format and routinely returns
    # ```json ... ``` around otherwise valid JSON; that must not fail closed.
    fenced = "```json\n" + json.dumps(_provider_payload(), ensure_ascii=False) + "\n```"
    raw_response = _response(_provider_payload(), text_override=fenced)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=raw_response)

    researcher = DeepSeekWebResearcher(
        api_key=SecretStr("secret-value"),
        transport=httpx.MockTransport(handler),
        clock=lambda: datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
    )

    result = await researcher.research(_request())

    assert result.facts[0].source_ids == ("src_1",)
    assert result.sources[0].url == "https://example.gov/policy"


@pytest.mark.asyncio
async def test_partial_code_fence_still_fails_closed() -> None:
    # Only a fence spanning the whole text is unwrapped; trailing prose after
    # the closing fence means the payload is not clean JSON and must be rejected.
    partial = (
        "```json\n"
        + json.dumps(_provider_payload(), ensure_ascii=False)
        + "\n```\n以上为检索结果。"
    )
    raw_response = _response(_provider_payload(), text_override=partial)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=raw_response)

    researcher = DeepSeekWebResearcher(
        api_key=SecretStr("secret-value"),
        transport=httpx.MockTransport(handler),
        clock=lambda: datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
    )

    with pytest.raises(WebResearchUnavailable):
        await researcher.research(_request())


def test_request_requires_timezone_aware_as_of() -> None:
    with pytest.raises(ValueError, match="as_of must include an explicit timezone"):
        CurrentFactResearchRequest(
            query="查询最近事实",
            purpose=ResearchPurpose.CURRENT_FACT,
            as_of=datetime(2026, 9, 3, 12, 0),
        )
