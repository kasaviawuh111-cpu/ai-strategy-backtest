from __future__ import annotations

from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any, cast

import pytest

from ashare_lab.adapters.language.vibe_candidates import (
    CandidateTransportRequest,
    CandidateTransportResponse,
    IndicatorCandidate,
    VibeBoundedCandidateGenerator,
    _bounded_response_schema,  # pyright: ignore[reportPrivateUsage]
    _validate_indicator_candidate,  # pyright: ignore[reportPrivateUsage]
    build_candidate_capability_matrix,
)
from ashare_lab.application.skill_numeric_catalog import extend_skill_numeric_catalogs
from ashare_lab.domain.catalog import load_catalog_directory, load_coverage_catalog_directory
from ashare_lab.domain.strategy.models import JsonScalar
from ashare_lab.ports.candidate_generation import CompileInput, IndicatorIntent

ROOT = Path(__file__).parents[4]
CATALOG, COVERAGE = extend_skill_numeric_catalogs(
    load_catalog_directory(ROOT / "catalogs"),
    load_coverage_catalog_directory(ROOT / "catalogs" / "coverage"),
)
MATRIX = build_candidate_capability_matrix(CATALOG, COVERAGE)


def test_model_schema_binds_same_trigger_to_indicator_value_requirement() -> None:
    schema = cast(dict[str, Any], _bounded_response_schema(MATRIX))
    rules = schema["$defs"]["IndicatorCandidate"]["allOf"][0]["anyOf"]
    matches: dict[str, list[dict[str, Any]]] = {}
    for indicator in ("provider.numeric", "provider.series_compare"):
        matches[indicator] = [
            rule for rule in rules
            if any(indicator in case["properties"]["indicator_id"]["enum"]
                   and "above" in case["properties"]["trigger"]["enum"]
                   for case in rule["anyOf"])
        ]
    assert len(matches["provider.numeric"]) == len(matches["provider.series_compare"]) == 1
    assert matches["provider.numeric"][0]["properties"]["value"] == {"type": "number"}
    assert matches["provider.numeric"][0]["required"] == ["value"]
    assert matches["provider.series_compare"][0]["properties"]["value"] == {"type": "null"}
    assert "required" not in matches["provider.series_compare"][0]


def test_schema_covers_every_catalog_pair_once_with_exact_value_bounds() -> None:
    schema = cast(dict[str, Any], _bounded_response_schema(MATRIX))
    rules = schema["$defs"]["IndicatorCandidate"]["allOf"][0]["anyOf"]
    bindings: dict[tuple[str, str], dict[str, Any]] = {}
    for rule in rules:
        for case in rule["anyOf"]:
            for indicator in case["properties"]["indicator_id"]["enum"]:
                for trigger in case["properties"]["trigger"]["enum"]:
                    pair = (indicator, trigger)
                    assert pair not in bindings
                    bindings[pair] = rule
    expected_pairs = {(item.indicator_id, trigger.id)
                      for item in MATRIX.indicators for trigger in item.triggers}
    assert set(bindings) == expected_pairs
    for item in MATRIX.indicators:
        for trigger in item.triggers:
            rule = bindings[(item.indicator_id, trigger.id)]
            expected: dict[str, object] = {
                "type": "number" if trigger.value_requirement == "required" else "null",
            }
            if trigger.minimum is not None:
                expected["exclusiveMinimum" if trigger.exclusive_minimum else "minimum"] = (
                    trigger.minimum
                )
            if trigger.maximum is not None:
                expected["exclusiveMaximum" if trigger.exclusive_maximum else "maximum"] = (
                    trigger.maximum
                )
            assert rule["properties"]["value"] == expected
            assert ("value" in rule.get("required", [])) == (
                trigger.value_requirement == "required"
            )


def test_json_schema_accepts_every_legal_catalog_pair() -> None:
    validator_type = pytest.importorskip("jsonschema").Draft202012Validator
    schema = cast(dict[str, Any], _bounded_response_schema(MATRIX))
    validator = validator_type({**schema["$defs"]["IndicatorCandidate"], "$defs": schema["$defs"]})
    for capability in MATRIX.indicators:
        for trigger in capability.triggers:
            # This checks pair/value projection only; parameter semantics remain
            # independently checked by the Catalog validator and representative tests.
            candidate: dict[str, object] = {
                "indicator_id": capability.indicator_id, "trigger": trigger.id,
                "definition_version": capability.definition_version,
            }
            if trigger.value_requirement == "required":
                if trigger.minimum is not None and trigger.maximum is not None:
                    value = (trigger.minimum + trigger.maximum) / 2
                elif trigger.minimum is not None:
                    value = trigger.minimum + 1
                elif trigger.maximum is not None:
                    value = trigger.maximum - 1
                else:
                    value = 1
                candidate["value"] = value
            assert validator.is_valid(candidate), (capability.indicator_id, trigger.id)


@pytest.mark.parametrize(("indicator_id", "trigger", "value", "allowed"), [
    ("technical.ma", "price_above", None, True),
    ("technical.ma", "above", 30, False),
    ("technical.rsi", "above", 70, True),
    ("technical.rsi", "above", 0, True),
    ("technical.rsi", "above", 100, True),
    ("technical.rsi", "above", 101, False),
    ("technical.atr", "below", 2, True),
    ("technical.atr", "below", -1, False),
    ("price.close", "above", 0, False),
    ("price.close", "above", 0.01, True),
    ("provider.numeric", "above", None, False),
    ("provider.numeric", "above", 0, True),
    ("provider.series_compare", "above", None, True),
    ("provider.series_compare", "above", 0, False),
])
def test_json_schema_and_catalog_agree_for_pairs_and_thresholds(
    indicator_id: str, trigger: str, value: float | None, allowed: bool,
) -> None:
    # jsonschema is an optional local QA tool, not a production dependency.
    # The exhaustive Catalog projection check above never depends on it.
    validator_type = pytest.importorskip("jsonschema").Draft202012Validator
    capability = MATRIX.resolve_indicator(indicator_id)
    assert capability is not None
    params: dict[str, JsonScalar] = {
        p.name: p.default for p in capability.parameters if p.default is not None
    }
    if indicator_id == "provider.numeric":
        params = {"metric_query": "成交量", "unit": "股"}
    elif indicator_id == "provider.series_compare":
        params = {"left_metric_query": "成交量", "right_metric_query": "前10日均量", "unit": "股"}
    candidate = IndicatorCandidate(
        indicator_id=indicator_id, definition_version=capability.definition_version,
        trigger=trigger, params=params, value=value,
    )
    schema = cast(dict[str, Any], _bounded_response_schema(MATRIX))
    indicator_schema = schema["$defs"]["IndicatorCandidate"]
    validator = validator_type({**indicator_schema, "$defs": schema["$defs"]})
    assert validator.is_valid(candidate.model_dump(mode="json")) == allowed
    if allowed:
        _validate_indicator_candidate(candidate, MATRIX, path="/entry/0")
    else:
        with pytest.raises(ValueError):
            _validate_indicator_candidate(candidate, MATRIX, path="/entry/0")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_values_remain_rejected_by_catalog(value: float) -> None:
    candidate = IndicatorCandidate(
        indicator_id="provider.numeric", trigger="above",
        params={"metric_query": "成交量", "unit": "股"}, value=value,
    )
    with pytest.raises(ValueError, match="must be finite"):
        _validate_indicator_candidate(candidate, MATRIX, path="/entry/0")


@pytest.mark.asyncio
@pytest.mark.parametrize("repair_succeeds", [True, False])
@pytest.mark.parametrize("problem", ["required", "forbidden"])
@pytest.mark.parametrize("invalid_units", [True, False])
async def test_trigger_value_repair_is_targeted_and_never_invents_values(
    repair_succeeds: bool, problem: str, invalid_units: bool, caplog: pytest.LogCaptureFixture,
) -> None:
    utterance = "成交量大于前10日均量买入，市盈率大于25倍卖出"
    entry_text, exit_text = utterance.split("，")

    def span(text: str) -> dict[str, object]:
        start = utterance.index(text)
        return {"start": start, "end": start + len(text), "text": text}

    valid: dict[str, Any] = {"candidates": [{
        "entry": [{"kind": "indicator", "indicator_id": "provider.series_compare",
                   "trigger": "above", "params": {
                       "left_metric_query": "成交量", "right_metric_query": "前10日均量",
                       "unit": "股",
                   }, "value": None}],
        "exit": [{"kind": "indicator", "indicator_id": "provider.numeric",
                  "trigger": "above", "params": {"metric_query": "市盈率", "unit": "倍"},
                  "value": 25}],
        "entry_spans": [span(entry_text)], "exit_spans": [span(exit_text)],
        "confidence": 0.95,
    }]}
    invalid = deepcopy(valid)
    if problem == "required":
        invalid["candidates"][0]["exit"][0]["value"] = None
        expected_path, indicator = "/exit/0/value", "provider.numeric"
    else:
        invalid["candidates"][0]["entry"][0]["value"] = 999
        expected_path, indicator = "/entry/0/value", "provider.series_compare"
    if invalid_units:
        del invalid["candidates"][0]["entry"][0]["params"]["unit"]
        invalid["candidates"][0]["exit"][0]["params"]["unit"] = 12

    class Transport:
        def __init__(self) -> None:
            self.requests: list[CandidateTransportRequest] = []
            self.review_count = 0

        async def generate_json(
            self, request: CandidateTransportRequest,
        ) -> CandidateTransportResponse:
            if request.response_schema_name == "strategy_semantic_review":
                self.review_count += 1
                return {"instrument": "equivalent", "requested_bar_interval": "unspecified",
                        "requirements": [{"status": "represented", "candidate_path": "/entry",
                            "source_quote": utterance, "requested_meaning": "测试规则",
                            "candidate_meaning": "测试规则"}], "differences": []}
            self.requests.append(request)
            assert len(self.requests) <= 2
            return valid if repair_succeeds and len(self.requests) == 2 else invalid

    transport = Transport()
    result = await VibeBoundedCandidateGenerator(
        transport, capability_matrix=MATRIX, repair_invalid_output=True,
        model_semantic_review=True,
    ).generate(CompileInput(
        utterance=utterance, instrument_context="300059.SZ", as_of_date=date(2026, 9, 8),
    ))
    assert len(transport.requests) == 2
    repair_payload = transport.requests[1].user_payload
    assert repair_payload is not None
    feedback = str(repair_payload["validationFeedback"])
    hints = str(repair_payload["repairHints"])
    assert expected_path in feedback and f"indicator_id={indicator}" in feedback
    assert f"value_requirement={problem}" in feedback
    if invalid_units:
        assert "/entry/0/params/unit" in feedback
        assert "/exit/0/params/unit" in feedback
        assert "catalog_parameter_type" in feedback and "string" in feedback
        assert "catalog_parameter_required" in feedback
    assert hints != "[]"
    assert "999" not in feedback and "999" not in caplog.text
    assert f"path={expected_path}" in caplog.text
    assert result[0].unsupported_code == (
        None if repair_succeeds else "candidate_provider_invalid_output"
    )
    assert transport.review_count == int(repair_succeeds)
    if repair_succeeds:
        assert isinstance(result[0].entry[0], IndicatorIntent)
        assert isinstance(result[0].exit[0], IndicatorIntent)
        assert result[0].entry[0].value is None
        assert result[0].exit[0].value == 25
    else:
        assert not result[0].entry and not result[0].exit
    assert invalid["candidates"][0]["exit"][0]["value"] == (
        None if problem == "required" else 25
    )


@pytest.mark.asyncio
async def test_count_existence_zero_survives_projection_without_literal_zero_in_source() -> None:
    utterance = "事件甲发生过就买入，事件乙未发生就卖出"
    entry_text, exit_text = utterance.split("，")
    entry = {"kind": "indicator", "indicator_id": "provider.numeric", "trigger": "above",
             "params": {"metric_query": "每日事件甲发生次数", "unit": "次"}, "value": 0}
    exit_ = {"kind": "indicator", "indicator_id": "provider.numeric", "trigger": "at_most",
             "params": {"metric_query": "每日事件乙发生次数", "unit": "次"}, "value": 0}
    payload = {"candidates": [{"entry": [entry], "exit": [exit_], "confidence": 0.95,
        "entry_spans": [{"start": 0, "end": len(entry_text), "text": entry_text}],
        "exit_spans": [{"start": len(entry_text) + 1, "end": len(utterance), "text": exit_text}],
    }]}

    class Transport:
        async def generate_json(
            self, request: CandidateTransportRequest,
        ) -> CandidateTransportResponse:
            if request.response_schema_name != "strategy_semantic_review":
                return payload
            review_payload = cast(dict[str, Any], request.user_payload)
            for side, expected in (("entry", entry), ("exit", exit_)):
                projected = review_payload["candidate"][side][0]
                assert projected["value"] == 0
                assert projected["params"] == expected["params"]
                assert projected["trigger"] == expected["trigger"]
            assert "0是逻辑常数" in request.system_contract
            assert "不能改变事件发生时点" in request.system_contract
            return {"instrument": "equivalent", "requested_bar_interval": "unspecified",
                    "requirements": [{"status": "represented", "candidate_path": f"/{side}",
                        "source_quote": utterance, "requested_meaning": "次数存在性",
                        "candidate_meaning": "次数存在性"} for side in ("entry", "exit")],
                    "differences": []}

    result = await VibeBoundedCandidateGenerator(
        Transport(), capability_matrix=MATRIX, model_semantic_review=True,
    ).generate(CompileInput(
        utterance=utterance, instrument_context="300059.SZ", as_of_date=date(2026, 9, 8),
    ))
    assert result[0].unsupported_code is None
    assert isinstance(result[0].entry[0], IndicatorIntent)
    assert isinstance(result[0].exit[0], IndicatorIntent)
    assert result[0].entry[0].value == result[0].exit[0].value == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_changes", [False, True])
async def test_semantic_verdict_reuse_is_exact_and_request_local(candidate_changes: bool) -> None:
    utterance = "事件甲发生过就买入，买入后第二个交易日事件乙未发生就卖出"
    entry_text, exit_text = utterance.split("，")
    condition = {"kind": "indicator", "indicator_id": "provider.numeric", "trigger": "above",
                 "params": {"metric_query": "每日事件甲发生次数", "unit": "次"}, "value": 0}
    missing_time: dict[str, Any] = {"candidates": [{
        "entry": [condition], "exit": [{**condition, "trigger": "at_most",
            "params": {"metric_query": "每日事件乙发生次数", "unit": "次"}}],
        "entry_spans": [{"start": 0, "end": len(entry_text), "text": entry_text}],
        "exit_spans": [{"start": len(entry_text) + 1, "end": len(utterance), "text": exit_text}],
        "confidence": 0.95,
    }]}
    corrected = deepcopy(missing_time)
    corrected["candidates"][0]["exit"].append({"kind": "holding_period", "sessions": 2})
    corrected["candidates"][0]["exit_spans"] *= 2
    corrected["candidates"][0]["exit_join"] = "all"

    class Transport:
        def __init__(self) -> None:
            self.generation_count = 0
            self.review_count = 0

        async def generate_json(
            self, request: CandidateTransportRequest,
        ) -> CandidateTransportResponse:
            if request.response_schema_name != "strategy_semantic_review":
                self.generation_count += 1
                assert self.generation_count <= 2
                return (corrected if candidate_changes and self.generation_count == 2
                        else missing_time)
            self.review_count += 1
            rejected = self.generation_count == 1
            difference = {"candidate_path": "/exit", "source_quote": exit_text,
                          "requested_meaning": "第二个交易日后判断",
                          "candidate_meaning": "未限制持有时长"}
            # Deliberately unstable mock: a second review approves even if unchanged.
            return {"instrument": "equivalent", "requested_bar_interval": "unspecified",
                    "requirements": [{**difference,
                        "status": "different" if rejected else "represented"}],
                    "differences": [difference] if rejected else []}

    transport = Transport()
    generator = VibeBoundedCandidateGenerator(
        transport, capability_matrix=MATRIX, model_semantic_review=True,
        repair_invalid_output=True,
    )
    for _ in range(2):  # A second request must not inherit this request's cache.
        transport.generation_count = 0
        before_reviews = transport.review_count
        result = await generator.generate(CompileInput(
            utterance=utterance, instrument_context="300059.SZ", as_of_date=date(2026, 9, 8),
        ))
        assert transport.generation_count == 2
        assert transport.review_count - before_reviews == (2 if candidate_changes else 1)
        assert result[0].unsupported_code == (
            None if candidate_changes else "semantic_confirmation_required"
        )
        assert len(result[0].exit) == (2 if candidate_changes else 1)
