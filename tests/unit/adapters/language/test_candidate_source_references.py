from __future__ import annotations

from copy import deepcopy
from datetime import date
from typing import cast

import pytest

from ashare_lab.adapters.language.vibe_candidates import (
    VibeBoundedCandidateGenerator,
    _candidate_source_fragments,
    _resolve_source_reference,
)
from ashare_lab.ports.candidate_generation import CompileInput
from tests.unit.adapters.language.test_vibe_candidates import (
    CAPABILITY_MATRIX,
    _FakeTransport,
    _indicator_payload,
    _SequenceTransport,
)

SCREENSHOT_ENTRY = "14日RSI从30下方上穿30且当日成交额超过5亿元买入"
SCREENSHOT_EXIT = "14日RSI高于55、持仓亏损5%或持有满10个交易日卖出"
SCREENSHOT_UTTERANCE = f"美的集团，{SCREENSHOT_ENTRY}；{SCREENSHOT_EXIT}。"


def _reference(utterance: str, first_text: str, last_text: str | None = None) -> dict[str, str]:
    fragments = _candidate_source_fragments(utterance)
    first = next(key for key, span in fragments.items() if span.text == first_text)
    last = next(key for key, span in fragments.items() if span.text == (last_text or first_text))
    return {"first_fragment": first, "last_fragment": last}


def _screenshot_payload() -> dict[str, object]:
    entry_ref = _reference(SCREENSHOT_UTTERANCE, SCREENSHOT_ENTRY)
    exit_ref = _reference(SCREENSHOT_UTTERANCE, SCREENSHOT_EXIT)
    return {"candidates": [{
        "instrument_name": "美的集团",
        "instrument_span": _reference(SCREENSHOT_UTTERANCE, "美的集团"),
        "entry": [
            _indicator_payload("technical.rsi", "crosses_above", {"period": 14}, value=30),
            _indicator_payload("market.amount", "above", {}, value=500_000_000),
        ],
        "exit": [
            _indicator_payload("technical.rsi", "above", {"period": 14}, value=55),
            {"kind": "position_return", "trigger": "stop_loss", "threshold_pct": 5},
            {"kind": "holding_period", "sessions": 10},
        ],
        "entry_spans": [entry_ref, entry_ref],
        "exit_spans": [exit_ref, exit_ref, exit_ref],
        "entry_join": "all", "exit_join": "any", "confidence": 0.95,
    }]}


def _candidate(payload: dict[str, object]) -> dict[str, object]:
    return cast(list[dict[str, object]], payload["candidates"])[0]


def _request(utterance: str = SCREENSHOT_UTTERANCE) -> CompileInput:
    return CompileInput(utterance=utterance, instrument_context=None, as_of_date=date(2026, 9, 6))


def _generator(transport: _FakeTransport, *, repair: bool = False) -> VibeBoundedCandidateGenerator:
    return VibeBoundedCandidateGenerator(
        transport, capability_matrix=CAPABILITY_MATRIX, repair_invalid_output=repair,
    )


@pytest.mark.asyncio
async def test_screenshot_strategy_uses_server_references_without_changing_conditions() -> None:
    transport = _FakeTransport(_screenshot_payload())
    generated = await _generator(transport).generate(_request())

    candidate = generated[0]
    assert candidate.unsupported_code is None
    assert candidate.instrument_name == "美的集团"
    assert candidate.entry_join == "all" and candidate.exit_join == "any"
    assert len(candidate.entry) == 2 and len(candidate.exit) == 3
    assert candidate.entry[0].value == 30 and candidate.entry[1].value == 500_000_000
    assert candidate.exit[0].value == 55
    evidence = {item.path: item for item in candidate.grounding_evidence}
    assert evidence["/entry/0"].text == evidence["/entry/1"].text == SCREENSHOT_ENTRY
    for item in evidence.values():
        assert SCREENSHOT_UTTERANCE[item.start:item.end] == item.text
    request = transport.requests[0]
    payload = request.user_payload
    assert payload is not None
    assert payload["utterance"] == SCREENSHOT_UTTERANCE
    assert payload["sourceFragments"] == [
        {"id": "s1", "text": "美的集团"},
        {"id": "s2", "text": SCREENSHOT_ENTRY},
        {"id": "s3", "text": SCREENSHOT_EXIT},
    ]
    assert payload["capabilityProjectionHash"] == CAPABILITY_MATRIX.content_hash
    definitions = cast(dict[str, object], request.response_schema["$defs"])
    span_schema = cast(dict[str, object], definitions["CandidateSourceSpan"])
    properties = cast(dict[str, dict[str, object]], span_schema["properties"])
    assert set(properties) == {"first_fragment", "last_fragment"}
    assert properties["first_fragment"]["enum"] == ["s1", "s2", "s3"]
    assert request.json_object_contract
    assert "do not return copied text" in request.json_object_contract


@pytest.mark.asyncio
@pytest.mark.parametrize("declaration_prefix", [False, True])
async def test_rsi_reference_handles_unpunctuated_actions_and_declared_indicator_prefix(
    declaration_prefix: bool,
) -> None:
    utterance = (
        "用14日RSI，低于30买，高于55卖" if declaration_prefix else "用14日RSI低于30买高于55卖"
    )
    entry_ref = (
        _reference(utterance, "用14日RSI", "低于30买")
        if declaration_prefix else _reference(utterance, "用14日RSI低于30买")
    )
    payload = {"candidates": [{
        "entry": [_indicator_payload("technical.rsi", "below", {"period": 14}, value=30)],
        "exit": [_indicator_payload("technical.rsi", "above", {"period": 14}, value=55)],
        "entry_spans": [entry_ref],
        "exit_spans": [_reference(utterance, "高于55卖")], "confidence": 0.95,
    }]}
    generated = await _generator(_FakeTransport(payload)).generate(_request(utterance))

    assert generated[0].unsupported_code is None
    evidence = {item.path: item for item in generated[0].grounding_evidence}
    assert evidence["/entry/0"].text == (
        "用14日RSI，低于30买" if declaration_prefix else "用14日RSI低于30买"
    )
    assert evidence["/exit/0"].text == "高于55卖"


@pytest.mark.asyncio
async def test_unpunctuated_prefix_actions_keep_their_own_condition_evidence() -> None:
    utterance = "买入14日RSI低于30卖出14日RSI高于55"
    payload = {"candidates": [{
        "entry": [_indicator_payload("technical.rsi", "below", {"period": 14}, value=30)],
        "exit": [_indicator_payload("technical.rsi", "above", {"period": 14}, value=55)],
        "entry_spans": [_reference(utterance, "买入", "14日RSI低于30")],
        "exit_spans": [_reference(utterance, "卖出", "14日RSI高于55")],
        "confidence": 0.95,
    }]}
    generated = await _generator(_FakeTransport(payload)).generate(_request(utterance))
    assert generated[0].unsupported_code is None
    evidence = {item.path: item for item in generated[0].grounding_evidence}
    assert evidence["/entry/0"].text == "买入14日RSI低于30"
    assert evidence["/exit/0"].text == "卖出14日RSI高于55"


@pytest.mark.asyncio
@pytest.mark.parametrize(("stock_prefix", "model_name", "valid"), [
    ("请用美的集团的", "美的集团", True),
    ("请用美的集团的", "东方财富", False),
    ("美的集团 美的集团 ", "美的集团", False),
])
async def test_instrument_name_requires_a_unique_exact_match_within_selected_fragment(
    stock_prefix: str, model_name: str, valid: bool,
) -> None:
    entry_text = f"{stock_prefix}{SCREENSHOT_ENTRY}"
    utterance = f"{entry_text}；{SCREENSHOT_EXIT}"
    entry_ref = _reference(utterance, entry_text)
    exit_ref = _reference(utterance, SCREENSHOT_EXIT)
    payload = _screenshot_payload()
    _candidate(payload).update({
        "instrument_name": model_name, "instrument_span": entry_ref,
        "entry_spans": [entry_ref, entry_ref], "exit_spans": [exit_ref, exit_ref, exit_ref],
    })
    generated = await _generator(_FakeTransport(payload)).generate(_request(utterance))
    assert (generated[0].unsupported_code is None) is valid
    if valid:
        evidence = {item.path: item for item in generated[0].grounding_evidence}
        name_span = evidence["/instrument/name"]
        assert name_span.text == "美的集团"
        assert name_span.start == len("请用")
        assert utterance[name_span.start:name_span.end] == model_name


def test_repeated_original_text_is_located_by_id_not_first_text_match() -> None:
    utterance = "MACD金叉买入；MACD金叉买入；MACD死叉卖出"
    fragments = _candidate_source_fragments(utterance)
    assert fragments["s1"].text == fragments["s2"].text == "MACD金叉买入"
    resolved = _resolve_source_reference(
        {"first_fragment": "s2", "last_fragment": "s2"},
        fragments=fragments, utterance=utterance,
    )
    assert resolved == {"start": 9, "end": 17, "text": "MACD金叉买入"}


@pytest.mark.asyncio
@pytest.mark.parametrize("source_name,model_name,valid", [
    ("蓝色 光标", "蓝色光标", True),
    ("怡 亚 通", "怡亚通", True),
    ("贵\u3000州\t茅台", "贵州茅台", True),
    ("蓝色光标", "蓝色 光标", True),
    ("蓝色 光标 蓝色光标", "蓝色光标", False),
    ("蓝色 光标", "贵州茅台", False),
    ("蓝色，光标", "蓝色光标", False),
])
async def test_instrument_reference_tolerates_only_whitespace_and_preserves_source(
    source_name: str, model_name: str, valid: bool,
) -> None:
    utterance = f"{source_name}，{SCREENSHOT_ENTRY}；{SCREENSHOT_EXIT}"
    fragments = _candidate_source_fragments(utterance)
    stock_refs = [key for key, span in fragments.items() if span.end <= len(source_name)]
    entry_ref = _reference(utterance, SCREENSHOT_ENTRY)
    exit_ref = _reference(utterance, SCREENSHOT_EXIT)
    payload = _screenshot_payload()
    _candidate(payload).update({
        "instrument_name": model_name,
        "instrument_span": {"first_fragment": stock_refs[0], "last_fragment": stock_refs[-1]},
        "entry_spans": [entry_ref, entry_ref], "exit_spans": [exit_ref, exit_ref, exit_ref],
    })
    generated = (await _generator(_FakeTransport(payload)).generate(_request(utterance)))[0]
    assert (generated.unsupported_code is None) is valid
    if valid:
        assert generated.instrument_name == source_name
        evidence = next(item for item in generated.grounding_evidence if item.path == "/instrument/name")
        assert utterance[evidence.start:evidence.end] == evidence.text == source_name
        assert generated.entry[0].value == 30 and generated.exit[0].value == 55


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_reference", [
    {"first_fragment": "s999", "last_fragment": "s999"},
    {"first_fragment": "s3", "last_fragment": "s2"},
    {"first_fragment": "s2", "last_fragment": "s2", "text": SCREENSHOT_ENTRY},
])
async def test_invalid_and_mixed_reference_shapes_fail_closed(
    bad_reference: dict[str, str],
) -> None:
    payload = _screenshot_payload()
    _candidate(payload)["entry_spans"] = [bad_reference, bad_reference]
    generated = await _generator(_FakeTransport(payload)).generate(_request())
    assert generated[0].unsupported_code == "candidate_provider_invalid_output"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation", ["wrong_value", "omitted_condition", "opposite_action", "mixed_actions"],
)
async def test_reference_evidence_still_enforces_values_coverage_and_action_direction(
    mutation: str,
) -> None:
    payload = _screenshot_payload()
    candidate = _candidate(payload)
    entries = cast(list[dict[str, object]], candidate["entry"])
    if mutation == "wrong_value":
        entries[1]["value"] = 50_000_000
    elif mutation == "omitted_condition":
        entries.pop()
        cast(list[object], candidate["entry_spans"]).pop()
    else:
        reference = {
            "first_fragment": "s3" if mutation == "opposite_action" else "s2",
            "last_fragment": "s3",
        }
        candidate["entry_spans"] = [reference, reference]
    generated = await _generator(_FakeTransport(payload)).generate(_request())
    if mutation == "mixed_actions":
        result = generated[0]
        assert result.unsupported_code is None
        assert len(result.entry) == 2 and len(result.exit) == 3
        assert result.entry_join == "all" and result.exit_join == "any"
        assert result.entry[0].value == 30 and result.entry[1].value == 500_000_000
        assert dict(result.entry[0].params) == {"period": 14}
        evidence = {item.path: item for item in result.grounding_evidence}
        assert evidence["/entry/0"].text == evidence["/entry/1"].text == SCREENSHOT_ENTRY
        for path in ("/entry/0", "/entry/1"):
            span = evidence[path]
            assert SCREENSHOT_UTTERANCE[span.start:span.end] == span.text
    else:
        assert generated[0].unsupported_code == "candidate_provider_invalid_output"


@pytest.mark.asyncio
@pytest.mark.parametrize("repaired", [True, False])
async def test_reference_schema_failures_get_one_repair_with_same_source_and_safe_logs(
    repaired: bool, caplog: pytest.LogCaptureFixture,
) -> None:
    invalid = _screenshot_payload()
    _candidate(invalid)["entry_spans"] = [{
        "first_fragment": "s2", "last_fragment": "s2",
        "private-provider-key": "private-provider-secret",
    }] * 2
    transport = _SequenceTransport((
        invalid, _screenshot_payload() if repaired else deepcopy(invalid),
    ))
    generated = await _generator(transport, repair=True).generate(_request())

    assert (generated[0].unsupported_code is None) is repaired
    assert len(transport.requests) == 2
    original = transport.requests[0].user_payload
    correction = transport.requests[1].user_payload
    assert original is not None and correction is not None
    for key in ("utterance", "sourceFragments", "instrumentContext", "asOfDate", "maxCandidates",
                "capabilityMatrix", "capabilityProjectionHash", "capabilityProjectionVersion"):
        assert correction[key] == original[key]
    assert correction["validationFeedback"]
    assert "private-provider" not in str(correction["validationFeedback"])
    assert "private-provider" not in caplog.text
    assert "provider_schema_invalid" in caplog.text
