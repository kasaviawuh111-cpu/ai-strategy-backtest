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
    VibeBoundedCandidateGenerator,
    _candidate_repair_hints,
    _candidate_schema_feedback,
    _safe_validation_reason,
    _validate_candidate_integrity,
    _validate_transport_payload,
)
from ashare_lab.adapters.market_data.instrument_name_chain import InstrumentNameAmbiguousError
from ashare_lab.application.compile_strategy import CompileStatus, StrategyCompiler
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
    CAPABILITY_MATRIX,
    CATALOG,
    CATALOG_RELEASE,
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


@pytest.mark.asyncio
@pytest.mark.parametrize(("case", "name", "expected_code"), [
    ("quoted_name", "贵州茅台", None),
    ("existing_name", "贵州茅台", None),
    ("different_name", "东方财富", "instrument_context_mismatch"),
    ("unknown_name", "未知公司", "instrument_unconfirmed"),
    ("ambiguous_name", "重名公司", "instrument_unconfirmed"),
    ("conflicting_symbol", "贵州茅台", "candidate_provider_invalid_output"),
    ("invalid_quote", "贵州茅台", "candidate_provider_invalid_output"),
])
async def test_semantic_mode_host_echo_preserves_name_lookup_without_schema_repair(
    case: str, name: str, expected_code: str | None,
) -> None:
    entry = "14日RSI低于30买入"
    exit_text = "持有30个交易日卖出"
    utterance = f"{name}，{entry}，{exit_text}"
    payload = {"candidates": [{
        "instrument_symbol": "300059.SZ" if case == "conflicting_symbol" else "600519.SH",
        "instrument_name": name if case in {
            "existing_name", "different_name", "unknown_name", "ambiguous_name",
        } else None,
        "instrument_span": _reference(utterance, name),
        "entry": [_indicator_payload("technical.rsi", "below", {"period": 14}, value=30)],
        "exit": [{"kind": "holding_period", "sessions": 30}],
        "entry_spans": [_reference(utterance, entry)],
        "exit_spans": [_reference(utterance, exit_text)], "confidence": 0.95,
    }]}
    if case == "invalid_quote":
        payload["candidates"][0]["instrument_span"] = {
            "text": "虚构名称", "start": 0, "end": 4,
        }
    requests: list[CandidateTransportRequest] = []
    lookups: list[str] = []

    class Transport:
        async def generate_json(self, request: CandidateTransportRequest) -> dict[str, object]:
            requests.append(request)
            if request.response_schema_name == "strategy_semantic_review":
                assert request.user_payload is not None
                reviewed = request.user_payload["candidate"]
                assert reviewed["instrument_symbol"] == (
                    "600519.SH" if case == "quoted_name" else None
                )
                assert reviewed["instrument_name"] == (None if case == "quoted_name" else name)
                return {
                    "instrument": "equivalent", "requested_bar_interval": "unspecified",
                    "requirements": [{
                        "status": "represented", "candidate_path": "/entry",
                        "source_quote": entry, "requested_meaning": "原规则",
                        "candidate_meaning": "原规则",
                    }], "differences": [],
                }
            return payload

    def resolve_name(value: str) -> str:
        lookups.append(value)
        if case == "unknown_name":
            raise LookupError("unknown")
        if case == "ambiguous_name":
            raise InstrumentNameAmbiguousError("ambiguous")
        return "300059.SZ" if case == "different_name" else "600519.SH"

    generator = HybridCandidateGenerator(
        deterministic=_UnexpectedDeterministicGenerator(),
        bounded_fallback=VibeBoundedCandidateGenerator(
            Transport(), capability_matrix=CAPABILITY_MATRIX,
            repair_invalid_output=True, model_semantic_review=True,
        ),
        instrument_name_resolver=resolve_name, model_first=True,
    )
    outcome = await StrategyCompiler(
        generator=generator, catalog=CATALOG,
        catalog_id="cn_a.signals", release_version=CATALOG_RELEASE,
    ).compile(CompileInput(
        utterance=utterance, instrument_context="600519.SH", as_of_date=date(2026, 9, 8),
    ))

    assert outcome.diagnostic_code == expected_code
    if case in {"conflicting_symbol", "invalid_quote"}:
        assert lookups == []
        assert all(request.response_schema_name != "strategy_semantic_review"
                   for request in requests)
    else:
        assert lookups == ([] if case == "quoted_name" else [name])
        assert [request.response_schema_name for request in requests] == [
            "ashare_bounded_strategy_candidates", "strategy_semantic_review",
        ]
    if expected_code is None:
        assert outcome.status is CompileStatus.READY
        assert outcome.strategy is not None and outcome.strategy_hash is not None
        strategy = outcome.strategy.model_dump(mode="json")
        assert strategy["instrument"]["symbol"] == "600519.SH"
        assert strategy["entry"]["indicator_id"] == "technical.rsi"
        assert strategy["entry"]["params"] == {"period": 14}
        assert strategy["entry"]["value"] == 30
        assert strategy["exit"]["children"][0]["sessions"] == 30
        assert any(item.path == "/instrument/symbol" and item.text == name
                   for item in outcome.candidate_grounding)
    else:
        assert outcome.status is not CompileStatus.READY
        assert outcome.strategy is None and outcome.strategy_hash is None
    assert not outcome.run_requested


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["300059.SZ", None])
@pytest.mark.parametrize(("semantic_mode", "instrument_verdict", "ready"), [
    (True, "equivalent", True),
    (True, "mismatch", False),
    (True, "uncertain", False),
    (False, "equivalent", False),
])
async def test_full_clause_host_reference_requires_semantic_identity_approval(
    semantic_mode: bool, instrument_verdict: str, ready: bool, symbol: str | None,
) -> None:
    entry = "东方财富发布大股东增持公告后次日买入"
    exit_text = "持有30个交易日卖出"
    utterance = f"{entry}，{exit_text}"
    payload = {"candidates": [{
        "instrument_symbol": symbol, "instrument_name": None,
        "instrument_span": _reference(utterance, entry),
        "entry": [{
            "kind": "event",
            "event_code": "event.shareholder_holdings.major_holder_increase_progress",
            "definition_version": "1.0.0", "trigger": "published", "attributes": {},
        }],
        "exit": [{"kind": "holding_period", "sessions": 30}],
        "entry_spans": [_reference(utterance, entry)],
        "exit_spans": [_reference(utterance, exit_text)], "confidence": 0.95,
    }]}
    compile_input = CompileInput(
        utterance=utterance, instrument_context="300059.SZ", as_of_date=date(2026, 9, 8),
    )
    decoded = _validate_transport_payload(payload, utterance=utterance).candidates[0]
    with pytest.raises(ValueError, match="instrument source span does not contain the host code|unused instrument evidence"):
        _validate_candidate_integrity(decoded, CAPABILITY_MATRIX, compile_input)
    _validate_candidate_integrity(
        decoded, CAPABILITY_MATRIX, compile_input, allow_host_reference=True,
    )
    requests: list[CandidateTransportRequest] = []
    lookups: list[str] = []

    class Transport:
        async def generate_json(self, request: CandidateTransportRequest) -> dict[str, object]:
            requests.append(request)
            if request.response_schema_name == "strategy_semantic_review":
                assert request.user_payload is not None
                assert request.user_payload["originalUtterance"] == utterance
                assert request.user_payload["candidate"]["instrument_name"] is None
                assert request.user_payload["candidate"]["instrument_symbol"] == symbol
                assert "本轮明确选择的股票优先" in request.system_contract
                assert "整句，不等于提取的股票名称" in request.system_contract
                return {
                    "instrument": instrument_verdict, "requested_bar_interval": "unspecified",
                    "requirements": [{
                        "status": "represented", "candidate_path": "/entry",
                        "source_quote": entry, "requested_meaning": "原规则",
                        "candidate_meaning": "原规则",
                    }], "differences": [],
                }
            return payload

    def resolve_name(name: str) -> str:
        lookups.append(name)
        raise LookupError("a whole trading clause must not be queried as a stock name")

    outcome = await StrategyCompiler(
        generator=HybridCandidateGenerator(
            deterministic=_UnexpectedDeterministicGenerator(),
            bounded_fallback=VibeBoundedCandidateGenerator(
                Transport(), capability_matrix=CAPABILITY_MATRIX,
                model_semantic_review=semantic_mode,
            ),
            instrument_name_resolver=resolve_name, model_first=True,
        ),
        catalog=CATALOG, catalog_id="cn_a.signals", release_version=CATALOG_RELEASE,
    ).compile(compile_input)
    assert lookups == []
    assert (outcome.status is CompileStatus.READY) is ready
    assert len(requests) == (2 if semantic_mode else 1)
    if ready:
        assert outcome.strategy is not None and outcome.strategy_hash is not None
        assert outcome.strategy.instrument.symbol == "300059.SZ"
        assert outcome.strategy.execution.entry_policy == "next_tradable_session_open"
        assert outcome.strategy.model_dump()["exit"]["children"][0]["sessions"] == 30
        assert any(item.path == "/instrument/symbol" and item.text == entry
                   for item in outcome.candidate_grounding)
    else:
        assert outcome.strategy is None and outcome.strategy_hash is None
    assert not outcome.run_requested


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
    ("provider-extracted backtest period requires exact source evidence", "period_evidence_missing", "backtest_span"),
    ("lookback period lacks lexical evidence", "period_evidence_mismatch", "backtest_span"),
    ("daily position-return observation lacks lexical evidence", "daily_protection_not_requested", "minute_bar"),
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
