"""Only timeout may retry once on the existing Pro; provenance stays request-local."""

import asyncio
import json
from datetime import date

import httpx
import pytest
from pydantic import SecretStr

from ashare_lab.adapters.language.openai_compatible import OpenAICompatibleCandidateTransport
from ashare_lab.adapters.language.vibe_candidates import (
    CandidateTransportError,
    CandidateTransportRequest,
    response_provider_identity,
)
from ashare_lab.bootstrap import _configure_candidate_timeout_fallback
from ashare_lab.ports.dialogue_progress import progress_sink


def request():
    return CandidateTransportRequest(
        utterance="ROE高于1买入，死叉卖出", instrument_context="300059.SZ",
        as_of_date=date(2026, 9, 14), max_candidates=1,
        response_schema={"type": "object"}, capability_matrix={},
        capability_projection_version="v1", capability_projection_hash="sha256:test",
        system_contract="Return JSON only.", response_schema_name="skill_metric_binding_review",
    )


def completion():
    event = {"choices": [{"delta": {"content": '{"ok":true}'}, "finish_reason": "stop"}]}
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          content=f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n")


def model(name, handler):
    return OpenAICompatibleCandidateTransport(
        endpoint="https://model.example.test/chat/completions", provider="deepseek",
        model=name, api_key=SecretStr("not-for-logs"), prompt_version="p1", schema_version="s1",
        response_mode="json_object", timeout_seconds=0.25,
        thinking="enabled" if name == "deepseek-v4-pro" else "disabled",
        reasoning_effort="high" if name == "deepseek-v4-pro" else None,
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("first_fault", [
    "timeout", "success", 401, 402, 403, 429, 500, "json", "cancel",
])
async def test_only_timeout_uses_pro_once_with_unchanged_contract(first_fault, caplog):
    calls = []
    progress = []

    async def handler(req):
        body = json.loads(req.content)
        calls.append(body)
        if body["model"] == "deepseek-v4-pro":
            return completion()
        if first_fault == "timeout":
            raise httpx.ReadTimeout("secret-must-not-leak", request=req)
        if first_fault == "cancel":
            raise asyncio.CancelledError()
        if isinstance(first_fault, int):
            return httpx.Response(first_fault)
        if first_fault == "json":
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content='data: invalid\n\n')
        return completion()

    flash, pro = model("deepseek-v4-flash", handler), model("deepseek-v4-pro", handler)
    _configure_candidate_timeout_fallback(flash, pro)
    token = progress_sink.set(lambda stage, message: progress.append((stage, message)))
    try:
        if first_fault == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await flash.generate_json(request())
        elif first_fault not in {"success", "timeout"}:
            with pytest.raises(CandidateTransportError):
                await flash.generate_json(request())
        else:
            result = await flash.generate_json(request())
            assert result == {"ok": True}
            actual = pro.identity if first_fault == "timeout" else flash.identity
            assert response_provider_identity(result, flash.identity) == actual
            assert json.dumps(result) == '{"ok": true}'  # Metadata never alters schema.
            assert not any(stage == "failed" for stage, _ in progress)
    finally:
        progress_sink.reset(token)
    assert len(calls) == (2 if first_fault == "timeout" else 1)
    assert pro.timeout_fallback_identity is None
    if len(calls) == 2:
        assert calls[0]["messages"][1] == calls[1]["messages"][1]
        assert calls[0]["response_format"] == calls[1]["response_format"]
        assert calls[1]["thinking"] == {"type": "enabled"}
        assert calls[1]["reasoning_effort"] == "high"
        assert sum(stage == "model_fallback" for stage, _ in progress) == 1
        assert "from_model=deepseek-v4-flash to_model=deepseek-v4-pro" in caplog.text
    assert "not-for-logs" not in caplog.text
    assert "secret-must-not-leak" not in caplog.text
    assert "ROE高于1" not in caplog.text


@pytest.mark.asyncio
async def test_secondary_timeout_does_not_loop():
    calls = []

    async def handler(req):
        calls.append(json.loads(req.content)["model"])
        raise httpx.ReadTimeout("unavailable", request=req)

    flash, pro = model("deepseek-flash", handler), model("deepseek-v4-pro", handler)
    _configure_candidate_timeout_fallback(flash, pro)
    with pytest.raises(CandidateTransportError) as exc:
        await flash.generate_json(request())
    assert exc.value.timed_out
    assert calls == ["deepseek-flash", "deepseek-v4-pro"]


@pytest.mark.asyncio
async def test_partial_stream_is_closed_before_secondary_and_not_combined():
    class Stalled(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"{bad partial"}}]}\n\n'
            await asyncio.sleep(1)

        async def aclose(self):
            self.closed = True

    stream = Stalled()

    async def handler(req):
        if json.loads(req.content)["model"] == "deepseek-v4-pro":
            assert stream.closed
            assert "bad partial" not in req.content.decode()
            return completion()
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    flash, pro = model("deepseek-flash", handler), model("deepseek-v4-pro", handler)
    _configure_candidate_timeout_fallback(flash, pro)
    assert await flash.generate_json(request()) == {"ok": True}


def test_provider_metadata_cannot_be_forged_and_unrelated_profiles_are_unchanged():
    flash = model("other-model", lambda _: completion())
    other = model("deepseek-v4-pro", lambda _: completion())
    _configure_candidate_timeout_fallback(flash, other)
    assert flash.timeout_fallback_identity is None
    assert response_provider_identity(
        {"provider_identity": "forged"}, flash.identity,
    ) == flash.identity
