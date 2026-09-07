from __future__ import annotations

from copy import deepcopy
from datetime import date
from typing import cast

import pytest

from ashare_lab.adapters.language.vibe_candidates import (
    _normalize_transport_candidate,
    _validate_candidate_against_matrix,
    _validate_candidate_grounding,
    _validate_transport_payload,
)
from ashare_lab.ports.candidate_generation import CompileInput
from tests.unit.adapters.language.test_candidate_source_references import (
    SCREENSHOT_ENTRY,
    SCREENSHOT_EXIT,
    SCREENSHOT_UTTERANCE,
    _candidate,
    _generator,
    _request,
    _screenshot_payload,
)
from tests.unit.adapters.language.test_vibe_candidates import (
    CAPABILITY_MATRIX,
    DIRECT_UTTERANCE,
    _FakeTransport,
    _indicator_payload,
    _macd_batch,
    _source_span,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["none", "wrong_value", "reverse", "nonexact"])
async def test_broad_exact_quote_is_reduced_only_after_full_leaf_grounding(mutation: str) -> None:
    payload = _screenshot_payload()
    candidate = _candidate(payload)
    broad = _source_span(SCREENSHOT_UTTERANCE, SCREENSHOT_UTTERANCE)
    candidate["entry_spans"] = [deepcopy(broad), deepcopy(broad)]
    candidate["exit_spans"] = [deepcopy(broad) for _ in range(3)]
    if mutation == "wrong_value":
        cast(list[dict[str, object]], candidate["entry"])[1]["value"] = 50_000_000
    elif mutation == "reverse":
        cast(list[dict[str, object]], candidate["entry"])[0]["trigger"] = "crosses_below"
    elif mutation == "nonexact":
        cast(list[dict[str, object]], candidate["entry_spans"])[0]["text"] = "不存在的买卖原文"
    original_rules = deepcopy((candidate["entry"], candidate["exit"]))
    transport = _FakeTransport(payload)
    result = (await _generator(transport).generate(_request()))[0]

    assert len(transport.requests) == 1
    assert (candidate["entry"], candidate["exit"]) == original_rules
    if mutation == "none":
        assert result.unsupported_code is None
        assert result.entry_join == "all" and result.exit_join == "any"
        evidence = {item.path: item.text for item in result.grounding_evidence}
        assert evidence["/entry/0"] == evidence["/entry/1"] == SCREENSHOT_ENTRY
        assert all(evidence[f"/exit/{index}"] == SCREENSHOT_EXIT for index in range(3))
        assert result.entry[1].value == 500_000_000
    else:
        assert result.unsupported_code == "candidate_provider_invalid_output"


def test_two_matching_contained_clauses_are_not_arbitrarily_selected() -> None:
    entry = "14日RSI低于30买入"
    utterance = f"{entry}，{entry}；14日RSI高于55卖出"
    broad = _source_span(utterance, utterance)
    candidate = _validate_transport_payload({"candidates": [{
        "entry": [_indicator_payload("technical.rsi", "below", {"period": 14}, value=30)],
        "exit": [_indicator_payload("technical.rsi", "above", {"period": 14}, value=55)],
        "entry_spans": [broad], "exit_spans": [broad], "confidence": 0.95,
    }]}, utterance=utterance).candidates[0]
    request = _request(utterance)
    normalized = _normalize_transport_candidate(
        candidate, request=request, matrix=CAPABILITY_MATRIX,
    )
    assert normalized.entry_spans == candidate.entry_spans
    with pytest.raises(ValueError, match="mixes entry and exit"):
        _validate_candidate_grounding(normalized, CAPABILITY_MATRIX, request)


@pytest.mark.asyncio
@pytest.mark.parametrize("indicator", ["technical.ma", "technical.ma_cross"])
@pytest.mark.parametrize("mutation", [
    "none", "wrong_price", "explicit_open", "narrow_explicit_open", "missing_period",
])
async def test_ma_close_annotation_does_not_supply_or_replace_trading_parameters(
    indicator: str, mutation: str,
) -> None:
    params: dict[str, object] = ({"period": 20, "price_field": "close"}
                               if indicator == "technical.ma" else
                               {"fast_period": 5, "slow_period": 20, "price_field": "close"})
    entry = ("上穿20日均线买入" if indicator == "technical.ma" else
             "5日均线上穿20日均线买入")
    if mutation == "wrong_price":
        params["price_field"] = "open"
    elif mutation in {"explicit_open", "narrow_explicit_open"}:
        entry = "开盘价" + entry
    elif mutation == "missing_period":
        params.pop("period" if indicator == "technical.ma" else "fast_period")
    exit_text = "持有满10个交易日卖出"
    utterance = f"{entry}；{exit_text}"
    transport = _FakeTransport({"candidates": [{
        "entry": [_indicator_payload(indicator, "price_crosses_above"
                                      if indicator == "technical.ma" else "golden_cross", params)],
        "exit": [{"kind": "holding_period", "sessions": 10}],
        "entry_spans": [_source_span(
            utterance, "买入" if mutation == "narrow_explicit_open" else entry,
        )],
        "exit_spans": [_source_span(utterance, exit_text)], "confidence": 0.95,
    }]})
    result = (await _generator(transport).generate(CompileInput(
        utterance=utterance, instrument_context="300059.SZ", as_of_date=date(2026, 9, 6),
    )))[0]
    assert len(transport.requests) == 1
    if mutation == "none":
        assert result.unsupported_code is None
        assert dict(result.entry[0].params) == params
        assert "/entry/0/params/price_field" in result.defaulted_fields
    else:
        assert result.unsupported_code == "candidate_provider_invalid_output"


@pytest.mark.parametrize(("trigger", "entry"), [
    ("gt_multiple", "成交量超过前20日均量1.5倍买入"),
    ("gte_multiple", "成交量达到前20日均量1.5倍买入"),
    ("lte_multiple", "相对成交量缩量1.5倍且baseline_period为20买入"),
])
def test_single_session_volume_only_fills_unused_catalog_field(trigger: str, entry: str) -> None:
    exit_text = "持有满10个交易日卖出"
    utterance = f"{entry}；{exit_text}"
    payload = {"candidates": [{
        "entry": [_indicator_payload(
            "volume.relative", trigger, {"baseline_period": 20}, value=1.5,
        )],
        "exit": [{"kind": "holding_period", "sessions": 10}],
        "entry_spans": [_source_span(utterance, entry)],
        "exit_spans": [_source_span(utterance, exit_text)], "confidence": 0.95,
    }]}
    original = _validate_transport_payload(payload, utterance=utterance).candidates[0]
    request = _request(utterance)
    normalized = _normalize_transport_candidate(original, request=request, matrix=CAPABILITY_MATRIX)
    _validate_candidate_against_matrix(normalized, CAPABILITY_MATRIX)
    _validate_candidate_grounding(normalized, CAPABILITY_MATRIX, request)
    assert normalized.entry[0].params == {"baseline_period": 20, "consecutive_days": 3}
    assert normalized.entry[0].trigger == trigger and normalized.entry[0].value == 1.5
    assert normalized.defaulted_fields == ("/entry/0/params/consecutive_days",)
    assert original.entry[0].params == {"baseline_period": 20}


@pytest.mark.parametrize(("trigger", "params", "defaulted"), [
    ("consecutive_gte_multiple", {"baseline_period": 20}, False),
    ("gt_multiple", {"baseline_period": 20, "consecutive_days": 0}, False),
    ("gt_multiple", {}, True),
])
def test_volume_default_never_repairs_semantic_or_existing_invalid_fields(
    trigger: str, params: dict[str, int], defaulted: bool,
) -> None:
    text = "相对成交量持续放量1.5倍买入"
    original = _validate_transport_payload({"candidates": [{
        "entry": [_indicator_payload("volume.relative", trigger, params, value=1.5)],
        "entry_spans": [_source_span(text, text)], "confidence": 0.95,
        "exit": [], "exit_spans": [],
    }]}, utterance=text).candidates[0]
    normalized = _normalize_transport_candidate(
        original, request=_request(text), matrix=CAPABILITY_MATRIX,
    )
    assert normalized.entry[0].params == (dict(params, consecutive_days=3) if defaulted else params)
    with pytest.raises(ValueError):
        _validate_candidate_against_matrix(normalized, CAPABILITY_MATRIX)


@pytest.mark.asyncio
@pytest.mark.parametrize("provided", [{}, {"fast": 12}, {"fast": 12, "slow": 26, "signal": 9}])
@pytest.mark.parametrize("utterance", [DIRECT_UTTERANCE, "东方财富 macd 金叉买 死叉卖"])
async def test_plain_macd_uses_standard_catalog_parameters_once(
    provided: dict[str, int], utterance: str,
) -> None:
    payload = _macd_batch()
    candidate = _candidate(payload)
    if utterance != DIRECT_UTTERANCE:
        candidate["entry_spans"] = [_source_span(utterance, "macd 金叉买")]
        candidate["exit_spans"] = [_source_span(utterance, "死叉卖")]
    for side in ("entry", "exit"):
        cast(list[dict[str, object]], candidate[side])[0]["params"] = dict(provided)
    candidate["defaulted_fields"] = []
    transport = _FakeTransport(payload)
    result = (await _generator(transport).generate(_request(utterance)))[0]
    assert len(transport.requests) == 1 and result.unsupported_code is None
    for side, leaves in (("entry", result.entry), ("exit", result.exit)):
        assert dict(leaves[0].params) == {"fast": 12, "slow": 26, "signal": 9}
        assert all(f"/{side}/0/params/{name}" in result.defaulted_fields
                   for name in ("fast", "slow", "signal"))
    assert result.entry[0].trigger == "golden_cross" and result.exit[0].trigger == "death_cross"


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", [
    "MACD(8,21,5)金叉买入", "MACD(8,21)金叉买入", "MACD(8,金叉买入",
    "MACD 8 21金叉买入", "MACD8金叉买入", "MACD参数没写完金叉买入",
    "MACD快线周期8金叉买入",
])
async def test_explicit_or_partial_macd_parameters_never_get_default_repair(entry: str) -> None:
    utterance = f"{entry}，MACD死叉卖出"
    payload = _macd_batch(utterance=utterance)
    candidate = _candidate(payload)
    # Quote just the action: normalization must still inspect the full input.
    candidate["entry_spans"] = [_source_span(utterance, "买入")]
    cast(list[dict[str, object]], candidate["entry"])[0]["params"] = {}
    candidate["defaulted_fields"] = []
    transport = _FakeTransport(payload)
    result = (await _generator(transport).generate(_request(utterance)))[0]
    assert len(transport.requests) == 1
    assert result.unsupported_code == "candidate_provider_invalid_output"


def test_macd_defaults_do_not_overwrite_existing_wrong_values_or_suppress_missing_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = _macd_batch()
    candidate = _candidate(payload)
    cast(list[dict[str, object]], candidate["entry"])[0]["params"] = {"fast": 8}
    candidate["defaulted_fields"] = []
    original = _validate_transport_payload(payload, utterance=DIRECT_UTTERANCE).candidates[0]
    request = _request(DIRECT_UTTERANCE)
    normalized = _normalize_transport_candidate(original, request=request, matrix=CAPABILITY_MATRIX)
    assert normalized.entry[0].params == {"fast": 8, "slow": 26, "signal": 9}
    assert "/entry/0/params/fast" not in normalized.defaulted_fields
    with pytest.raises(ValueError, match="lexical evidence"):
        _validate_candidate_grounding(normalized, CAPABILITY_MATRIX, request)
    with pytest.raises(ValueError, match="omitted a required"):
        _validate_candidate_against_matrix(original, CAPABILITY_MATRIX)
    assert "path=/entry/0/params/slow capability=technical.macd trigger=golden_cross" in caplog.text
    assert ("path=/entry/0/params/signal capability=technical.macd trigger=golden_cross"
            in caplog.text)
    assert "request_id=" in caplog.text and "attempt=" in caplog.text
    assert DIRECT_UTTERANCE not in caplog.text
