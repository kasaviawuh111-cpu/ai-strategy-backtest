from __future__ import annotations

from copy import deepcopy
from datetime import date
from typing import cast

import pytest
from pydantic import ValidationError

from ashare_lab.adapters.language.vibe_candidates import (
    CandidateTransportRequest,
    CandidateTransportResponse,
    HybridCandidateGenerator,
    _candidate_repair_hints,
    _candidate_schema_feedback,
    _safe_validation_reason,
    _validate_transport_payload,
)
from ashare_lab.ports.candidate_generation import CompileInput
from ashare_lab.ports.request_context import (
    candidate_attempt,
    current_candidate_attempt,
    request_id,
)
from tests.unit.adapters.language.test_candidate_source_references import (
    SCREENSHOT_UTTERANCE,
    _candidate,
    _generator,
    _reference,
    _request,
    _screenshot_payload,
)
from tests.unit.adapters.language.test_vibe_candidates import (
    _FakeTransport,
    _indicator_payload,
    _SequenceTransport,
    _UnexpectedDeterministicGenerator,
)

CHIP_ENTRY = "收盘价创前20日新高且成交量超过前20日均量1.5倍买入"
CHIP_EXIT = "从持仓后最高收盘价回撤8%或持有满40个交易日卖出"
CHIP_UTTERANCE = f"贵州茅台，{CHIP_ENTRY}；{CHIP_EXIT}。"


def _chip_payload() -> dict[str, object]:
    entry_ref = _reference(CHIP_UTTERANCE, CHIP_ENTRY)
    exit_ref = _reference(CHIP_UTTERANCE, CHIP_EXIT)
    return {"candidates": [{
        "instrument_name": "贵州茅台", "instrument_symbol": "600519.SH",
        "instrument_span": _reference(CHIP_UTTERANCE, "贵州茅台"),
        "entry": [
            _indicator_payload("price.rolling_high", "new_high",
                               {"period": 20, "price_field": "close"}),
            _indicator_payload("volume.relative", "gt_multiple",
                               {"baseline_period": 20, "consecutive_days": 3}, value=1.5),
        ],
        "exit": [
            {"kind": "trailing_drawdown", "threshold_pct": 8},
            {"kind": "holding_period", "sessions": 40},
        ],
        "entry_spans": [entry_ref, entry_ref], "exit_spans": [exit_ref, exit_ref],
        "entry_join": "all", "exit_join": "any", "confidence": 0.95,
        "defaulted_fields": ["/entry/1/params/consecutive_days"],
    }]}


@pytest.mark.asyncio
@pytest.mark.parametrize(("case", "context", "expected_code", "resolves_name"), [
    ("matching_echo", "600519.SH", None, True),
    ("no_host_context", None, "candidate_provider_invalid_output", False),
    ("conflicting_echo", "600519.SH", "candidate_provider_invalid_output", False),
    ("wrong_name_evidence", "600519.SH", "candidate_provider_invalid_output", False),
    ("resolved_name_conflicts", "600519.SH", "instrument_context_mismatch", True),
])
async def test_home_chip_host_code_echo_still_requires_exact_name_and_trusted_resolution(
    case: str, context: str | None, expected_code: str | None, resolves_name: bool,
) -> None:
    payload = _chip_payload()
    candidate = _candidate(payload)
    if case == "conflicting_echo":
        candidate["instrument_symbol"] = "300059.SZ"
    elif case == "wrong_name_evidence":
        candidate["instrument_name"] = "东方财富"
    calls: list[str] = []

    def resolver(name: str) -> str:
        calls.append(name)
        return "300059.SZ" if case == "resolved_name_conflicts" else "600519.SH"

    generator = HybridCandidateGenerator(
        deterministic=_UnexpectedDeterministicGenerator(),
        bounded_fallback=_generator(_FakeTransport(payload)),
        instrument_name_resolver=resolver, model_first=True,
    )
    generated = await generator.generate(CompileInput(
        utterance=CHIP_UTTERANCE, instrument_context=context, as_of_date=date(2026, 9, 6),
    ))

    assert generated[0].unsupported_code == expected_code
    assert calls == (["贵州茅台"] if resolves_name else [])
    if expected_code is None:
        assert generated[0].instrument_symbol == "600519.SH"
        assert generated[0].instrument_name is None
        assert any(item.path == "/instrument/symbol" and item.text == "贵州茅台"
                   for item in generated[0].grounding_evidence)
        assert len(generated[0].entry) == len(generated[0].exit) == 2
        assert generated[0].entry_join == "all" and generated[0].exit_join == "any"
        assert generated[0].entry[0].indicator_id == "price.rolling_high"
        assert dict(generated[0].entry[0].params) == {"period": 20, "price_field": "close"}
        assert generated[0].entry[1].trigger == "gt_multiple"
        assert generated[0].entry[1].value == 1.5
        assert generated[0].exit[0].threshold_pct == 8
        assert generated[0].exit[1].sessions == 40
    elif case == "resolved_name_conflicts":
        assert generated[0].instrument_symbol is None


@pytest.mark.parametrize(("mutation", "code", "hint"), [
    ("period", "period_mode_conflict", "只能采用一种表示"),
    ("entry", "entry_span_count_mismatch", "entry_spans"),
    ("exit", "exit_span_count_mismatch", "exit_spans"),
    ("defaults", "duplicate_defaulted_field", "每个参数路径只保留一次"),
])
def test_candidate_object_errors_preserve_distinct_safe_repair_codes(
    mutation: str, code: str, hint: str,
) -> None:
    payload = _screenshot_payload()
    candidate = _candidate(payload)
    if mutation == "period":
        candidate.update(backtest_lookback_years=1, backtest_start="2025-09-06")
    elif mutation in {"entry", "exit"}:
        cast(list[object], candidate[f"{mutation}_spans"]).pop()
    else:
        candidate["defaulted_fields"] = ["/entry/0/params/period"] * 2
    with pytest.raises(ValidationError) as captured:
        _validate_transport_payload(payload, utterance=SCREENSHOT_UTTERANCE)

    feedback = _candidate_schema_feedback(captured.value)
    assert f"candidates/0:{code}" in feedback
    assert "candidates/0:value_error" not in feedback
    assert hint in " ".join(_candidate_repair_hints([feedback]))
    assert SCREENSHOT_UTTERANCE not in feedback


@pytest.mark.parametrize(("message", "code", "hint"), [
    ("amount comparator or CNY value differs from source", "amount_source_mismatch", "人民币元"),
    ("relative-volume comparator differs from source",
     "relative_volume_comparator_mismatch", "gt_multiple"),
    ("RSI transition wording cannot ground a static threshold trigger",
     "rsi_transition_trigger_mismatch", "crosses_above"),
    ("candidate MA crossover direction differs from source",
     "ma_crossover_direction_mismatch", "交叉方向"),
    ("position-return exit lacks lexical evidence", "position_return_evidence_missing", "持仓"),
    ("a stock name cannot authorize a model-invented security code",
     "instrument_name_code_invented", "留空"),
])
def test_common_grounding_failures_get_static_diagnostics_and_targeted_hints(
    message: str, code: str, hint: str,
) -> None:
    assert _safe_validation_reason(ValueError(message)) == code
    assert hint in " ".join(_candidate_repair_hints([f"candidate/1:{code}"]))


def test_unknown_exception_content_never_becomes_feedback_or_repair_instructions() -> None:
    secret = "private-provider-secret; arbitrary user instructions"
    error = ValueError(secret)
    assert _safe_validation_reason(error) == "unclassified_validation_error"
    assert _candidate_schema_feedback(error) == "schema_invalid:ValueError"
    assert _candidate_repair_hints([secret]) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("repair_preserves_values", [True, False])
async def test_one_repair_gets_exact_count_hint_but_cannot_bypass_value_validation(
    repair_preserves_values: bool, caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid = _screenshot_payload()
    cast(list[object], _candidate(invalid)["entry_spans"]).pop()
    repaired = deepcopy(_screenshot_payload())
    if not repair_preserves_values:
        cast(list[dict[str, object]], _candidate(repaired)["entry"])[1]["value"] = 50_000_000
    transport = _SequenceTransport((invalid, repaired))
    original_generate = transport.generate_json
    observed_attempts: list[int] = []

    async def record_attempt(request: CandidateTransportRequest) -> CandidateTransportResponse:
        observed_attempts.append(current_candidate_attempt())
        return await original_generate(request)

    monkeypatch.setattr(transport, "generate_json", record_attempt)
    parent_attempt = candidate_attempt.set(9)
    request_token = request_id.set("repair-diagnostics-test")
    try:
        generated = await _generator(transport, repair=True).generate(_request())
        assert current_candidate_attempt() == 9
    finally:
        candidate_attempt.reset(parent_attempt)
        request_id.reset(request_token)

    assert len(transport.requests) == 2
    assert observed_attempts == [1, 2]
    correction = transport.requests[1].user_payload
    assert correction is not None
    assert "entry_span_count_mismatch" in str(correction["validationFeedback"])
    assert "不得删除或合并条件" in str(correction["repairHints"])
    assert "entry_span_count_mismatch" in caplog.text
    assert SCREENSHOT_UTTERANCE not in caplog.text
    gate_logs = [record.message for record in caplog.records
                 if "candidate_gate_rejected" in record.message]
    assert all("request_id=repair-diagnostics-test" in line for line in gate_logs)
    assert "attempt=1" in gate_logs[0]
    if repair_preserves_values:
        assert generated[0].unsupported_code is None
        assert len(generated[0].entry) == 2 and len(generated[0].exit) == 3
        assert generated[0].entry_join == "all" and generated[0].exit_join == "any"
        assert generated[0].entry[1].value == 500_000_000
    else:
        assert generated[0].unsupported_code == "candidate_provider_invalid_output"
        assert "amount_source_mismatch" in caplog.text
        assert "semantic_validation_failed" in gate_logs[-1] and "attempt=2" in gate_logs[-1]


@pytest.mark.asyncio
async def test_leaf_and_parameter_failure_logs_keep_request_and_attempt_without_source_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    invalid = _screenshot_payload()
    cast(list[dict[str, object]], _candidate(invalid)["entry"])[0]["params"] = {"period": 13}
    transport = _SequenceTransport((invalid, deepcopy(invalid)))
    request_token = request_id.set("grounding-detail-correlation")
    try:
        generated = await _generator(transport, repair=True).generate(_request())
    finally:
        request_id.reset(request_token)

    assert generated[0].unsupported_code == "candidate_provider_invalid_output"
    assert len(transport.requests) == 2
    for event in ("candidate_grounding_leaf_failed", "candidate_grounding_parameter_failed"):
        detail_logs = [record.message for record in caplog.records if event in record.message]
        assert detail_logs
        assert all("request_id=grounding-detail-correlation" in line for line in detail_logs)
        assert {line.rsplit("attempt=", 1)[-1] for line in detail_logs} == {"1", "2"}
    assert SCREENSHOT_UTTERANCE not in caplog.text
