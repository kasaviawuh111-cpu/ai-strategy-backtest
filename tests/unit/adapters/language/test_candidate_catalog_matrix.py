from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest

from ashare_lab.adapters.language.candidate_semantic_review import SemanticReview
from ashare_lab.adapters.language.vibe_candidates import (
    CandidateCapabilityMatrix,
    CandidateJsonTransport,
    CandidateTransportRequest,
    CandidateTransportResponse,
    VibeBoundedCandidateGenerator,
    _source_action_fragments,
    build_candidate_capability_matrix,
)
from ashare_lab.application.compile_strategy import CompileStatus, StrategyCompiler
from ashare_lab.domain.catalog import (
    TriggerDefinition,
    load_catalog_directory,
    load_coverage_catalog_directory,
)
from ashare_lab.domain.events.catalog import EXECUTABLE_EVENT_DEFINITIONS
from ashare_lab.domain.strategy import AllCondition, AnyCondition, HoldingPeriodExit
from ashare_lab.ports.candidate_generation import CompileInput, EventIntent, IndicatorIntent
from catalogs.tools.build_coverage import STABLE_FORMULAS

ROOT = Path(__file__).parents[4]
CATALOG = load_catalog_directory(ROOT / "catalogs")
CATALOG_RELEASE = next(
    item.release_version for item in CATALOG.manifests if item.catalog_id == "cn_a.signals"
)
COVERAGE = load_coverage_catalog_directory(ROOT / "catalogs" / "coverage")
MATRIX = build_candidate_capability_matrix(CATALOG, COVERAGE)
AS_OF_DATE = date(2026, 8, 30)


class _FakeTransport(CandidateJsonTransport):
    def __init__(self, response: CandidateTransportResponse) -> None:
        self.response = response
        self.requests: list[CandidateTransportRequest] = []

    async def generate_json(
        self,
        request: CandidateTransportRequest,
    ) -> CandidateTransportResponse:
        self.requests.append(request)
        return self.response


def _generator(response: CandidateTransportResponse) -> VibeBoundedCandidateGenerator:
    return VibeBoundedCandidateGenerator(
        _FakeTransport(response),
        capability_matrix=MATRIX,
    )


def _compiler(response: CandidateTransportResponse) -> StrategyCompiler:
    return StrategyCompiler(
        generator=_generator(response),
        catalog=CATALOG,
        catalog_id="cn_a.signals",
        release_version=CATALOG_RELEASE,
        trusted_date_provider=lambda: AS_OF_DATE,
    )


def _request(utterance: str = "用目录里的条件买卖") -> CompileInput:
    return CompileInput(
        utterance=utterance,
        instrument_context="300059.SZ",
        as_of_date=AS_OF_DATE,
    )


@dataclass(frozen=True, slots=True)
class _GroundedCase:
    response: dict[str, object]
    utterance: str


def _batch(
    entry: list[dict[str, object]],
    exit_: list[dict[str, object]] | None = None,
    **candidate_overrides: object,
) -> _GroundedCase:
    candidate: dict[str, object] = {
        "instrument_symbol": None,
        "entry": entry,
        "exit": exit_ or [{"kind": "holding_period", "sessions": 3}],
        "confidence": 0.95,
    }
    candidate.update(candidate_overrides)
    entry_leaves = candidate["entry"]
    exit_leaves = candidate["exit"]
    assert isinstance(entry_leaves, list)
    assert isinstance(exit_leaves, list)
    entry_texts = [_leaf_text(item, "entry") for item in entry_leaves]
    exit_texts = [_leaf_text(item, "exit") for item in exit_leaves]
    entry_connector = "且" if candidate.get("entry_join", "all") == "all" else "或"
    exit_connector = "且" if candidate.get("exit_join", "any") == "all" else "或"
    segments = [entry_connector.join(entry_texts), exit_connector.join(exit_texts)]

    period_text: str | None = None
    lookback = candidate.get("backtest_lookback_years")
    start = candidate.get("backtest_start")
    end = candidate.get("backtest_end")
    if lookback is not None:
        period_text = f"回测近{lookback}年"
    elif start is not None or end is not None:
        period_text = f"回测{start}至{end}"
    if period_text is not None:
        segments.append(period_text)

    instrument_text: str | None = None
    symbol = candidate.get("instrument_symbol")
    if isinstance(symbol, str):
        instrument_text = f"股票{symbol}"
        segments.insert(0, instrument_text)

    utterance = "；".join(segments)
    candidate["entry_spans"] = [_span(utterance, item) for item in entry_texts]
    candidate["exit_spans"] = [_span(utterance, item) for item in exit_texts]
    if period_text is not None:
        candidate["backtest_span"] = _span(utterance, period_text)
    if instrument_text is not None:
        candidate["instrument_span"] = _span(utterance, instrument_text)
    candidate.setdefault("defaulted_fields", _defaulted_parameter_paths(entry_leaves, exit_leaves))
    return _GroundedCase(response={"candidates": [candidate]}, utterance=utterance)


def _span(utterance: str, text: str) -> dict[str, object]:
    start = utterance.index(text)
    return {"start": start, "end": start + len(text), "text": text}


def _leaf_text(leaf: dict[str, object], side: str) -> str:
    action = "买入" if side == "entry" else "卖出"
    kind = leaf["kind"]
    if kind == "holding_period":
        return f"持有{leaf['sessions']}个交易日{action}"
    if kind == "event":
        capability = MATRIX.resolve_event(str(leaf["event_code"]))
        alias = capability.aliases_zh[0] if capability is not None else str(leaf["event_code"])
        attributes = leaf.get("attributes", {})
        assert isinstance(attributes, dict)
        evidence = " ".join(str(item) for item in attributes.values())
        return f"{alias}{evidence}{action}"
    capability = MATRIX.resolve_indicator(str(leaf["indicator_id"]))
    alias = (
        min(capability.aliases_zh, key=len) if capability is not None else str(leaf["indicator_id"])
    )
    value = leaf.get("value")
    value_text = "" if value is None else str(value)
    params = leaf.get("params", {})
    assert isinstance(params, dict)
    definition = CATALOG.resolve_indicator(str(leaf["indicator_id"]))
    explicit_values = ""
    if definition is not None:
        defaults = {item.name: item.default for item in definition.parameters}
        explicit_values = " ".join(
            f"{name}={item}" for name, item in params.items() if item != defaults.get(name)
        )
    return f"{alias} {leaf['trigger']} {value_text} {explicit_values}{action}"


def _defaulted_parameter_paths(
    entry: list[dict[str, object]],
    exit_: list[dict[str, object]],
) -> list[str]:
    paths: list[str] = []
    for side, leaves in (("entry", entry), ("exit", exit_)):
        for index, leaf in enumerate(leaves):
            if leaf.get("kind") != "indicator":
                continue
            definition = CATALOG.resolve_indicator(str(leaf["indicator_id"]))
            if definition is None:
                continue
            params = leaf.get("params", {})
            assert isinstance(params, dict)
            for parameter in definition.parameters:
                if params.get(parameter.name) == parameter.default:
                    paths.append(f"/{side}/{index}/params/{parameter.name}")
    return paths


def _indicator_payload(indicator_id: str, trigger: str) -> dict[str, object]:
    definition = CATALOG.resolve_indicator(indicator_id)
    assert definition is not None
    trigger_definition = next(item for item in definition.triggers if item.id == trigger)
    return {
        "kind": "indicator",
        "indicator_id": definition.id,
        "definition_version": definition.version,
        "trigger": trigger_definition.id,
        "params": {item.name: item.default for item in definition.parameters},
        **(
            {"value": _valid_trigger_value(trigger_definition)}
            if trigger_definition.value_requirement == "required"
            else {}
        ),
    }


def _valid_trigger_value(trigger: TriggerDefinition) -> float:
    minimum = trigger.minimum
    maximum = trigger.maximum
    exclusive_minimum = trigger.exclusive_minimum
    exclusive_maximum = trigger.exclusive_maximum
    if minimum is not None and maximum is not None:
        return float(minimum + (maximum - minimum) / 2)
    if minimum is not None:
        return float(minimum + (1 if exclusive_minimum else 0))
    if maximum is not None:
        return float(maximum - (1 if exclusive_maximum else 0))
    return 1.0


def _event_payload(event_code: str) -> dict[str, object]:
    definition = EXECUTABLE_EVENT_DEFINITIONS[event_code]
    return {
        "kind": "event",
        "event_code": event_code,
        "definition_version": definition.definition_version,
        "trigger": "published",
        "attributes": {},
    }


def test_projection_exactly_matches_current_37_indicator_and_69_event_release() -> None:
    stable_indicators = {item.id: item for item in CATALOG.indicators if item.status == "stable"}
    stable_metrics = {item.id: item for item in COVERAGE.metrics if item.status == "stable"}
    stable_events = {item.id: item for item in COVERAGE.events if item.status == "stable"}

    assert len(MATRIX.indicators) == 37
    assert len(MATRIX.events) == 69
    assert {item.indicator_id for item in MATRIX.indicators} == set(stable_indicators)
    assert set(stable_indicators) == set(stable_metrics)
    assert {item.event_code for item in MATRIX.events} == set(EXECUTABLE_EVENT_DEFINITIONS)
    assert set(EXECUTABLE_EVENT_DEFINITIONS) == set(stable_events)
    assert MATRIX.content_hash.startswith("sha256:")

    for capability in MATRIX.indicators:
        executable = stable_indicators[capability.indicator_id]
        coverage = stable_metrics[capability.indicator_id]
        assert capability.definition_version == executable.version
        assert coverage.name_zh in capability.aliases_zh
        assert {item.id for item in capability.triggers} == {
            item.id for item in executable.triggers
        }
        assert {item.name for item in capability.parameters} == {
            item.name for item in executable.parameters
        }

    for capability in MATRIX.events:
        executable = EXECUTABLE_EVENT_DEFINITIONS[capability.event_code]
        coverage = stable_events[capability.event_code]
        assert capability.definition_version == executable.definition_version
        assert capability.allowed_attributes == executable.allowed_attributes
        assert coverage.name_zh in capability.aliases_zh


@pytest.mark.asyncio
async def test_every_catalog_indicator_trigger_passes_the_bounded_allowlist() -> None:
    observed: set[tuple[str, str]] = set()
    for definition in CATALOG.indicators:
        assert definition.status == "stable"
        for trigger in definition.triggers:
            case = _batch([_indicator_payload(definition.id, trigger.id)])
            candidates = await _generator(case.response).generate(_request(case.utterance))
            assert candidates[0].unsupported_code is None
            intent = candidates[0].entry[0]
            assert isinstance(intent, IndicatorIntent)
            observed.add((intent.indicator_id, intent.trigger))

    assert observed == {
        (definition.id, trigger.id)
        for definition in CATALOG.indicators
        for trigger in definition.triggers
    }


@pytest.mark.asyncio
async def test_every_executable_event_code_passes_allowlist_and_strategy_validation() -> None:
    observed: set[str] = set()
    for event_code in EXECUTABLE_EVENT_DEFINITIONS:
        case = _batch([_event_payload(event_code)])
        candidates = await _generator(case.response).generate(_request(case.utterance))
        assert candidates[0].unsupported_code is None
        intent = candidates[0].entry[0]
        assert isinstance(intent, EventIntent)
        observed.add(intent.event_code)

        outcome = await _compiler(case.response).compile(_request(case.utterance))
        assert outcome.status is CompileStatus.READY

    assert observed == set(EXECUTABLE_EVENT_DEFINITIONS)


@pytest.mark.asyncio
async def test_request_schema_contains_bounded_enums_and_projection_identity() -> None:
    case = _batch([_indicator_payload("technical.macd", "golden_cross")])
    transport = _FakeTransport(case.response)
    generator = VibeBoundedCandidateGenerator(transport, capability_matrix=MATRIX)

    await generator.generate(_request(case.utterance))

    request = transport.requests[0]
    definitions = request.response_schema["$defs"]
    assert isinstance(definitions, dict)
    indicator_schema = definitions["IndicatorCandidate"]
    event_schema = definitions["EventCandidate"]
    assert isinstance(indicator_schema, dict)
    assert isinstance(event_schema, dict)
    indicator_properties = indicator_schema["properties"]
    event_properties = event_schema["properties"]
    assert isinstance(indicator_properties, dict)
    assert isinstance(event_properties, dict)
    assert set(indicator_properties["indicator_id"]["enum"]) == {  # type: ignore[index]
        item.indicator_id for item in MATRIX.indicators
    }
    assert set(event_properties["event_code"]["enum"]) == {  # type: ignore[index]
        item.event_code for item in MATRIX.events
    }
    assert request.capability_projection_version == MATRIX.schema_version
    assert request.capability_projection_hash == MATRIX.content_hash
    assert request.capability_matrix["schema_version"] == MATRIX.schema_version


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("indicator_id", "trigger", "semantic_fragments"),
    (
        ("volume.relative", "gt_multiple", (
            "gt_multiple/gte_multiple/lte_multiple 分别判断当日 RVOL > / >= / <= value",
            "不使用 consecutive_days", "仅 consecutive_gte_multiple 使用 consecutive_days",
        )),
        ("price.rolling_high", "new_high", (
            "同一 price_field 的最大值", "price_field=close（默认）", "此前最高收盘价",
            "不能表达当日收盘价与此前 high 的比较", "不要求上一日未触发",
        )),
        ("technical.donchian", "price_crosses_above_upper", (
            "high 最大值/low 最小值", "当日价格始终为 close", "不支持 price_field 参数",
            "前一日 close <= 前一日上轨", "前一日 close >= 前一日下轨",
            "不复用当日轨道",
        )),
    ),
)
async def test_indicator_formula_semantics_reach_generation_and_semantic_review(
    indicator_id: str, trigger: str, semantic_fragments: tuple[str, ...],
) -> None:
    case = _batch([_indicator_payload(indicator_id, trigger)])

    class ReviewingTransport(_FakeTransport):
        async def generate_json(
            self, request: CandidateTransportRequest,
        ) -> CandidateTransportResponse:
            self.requests.append(request)
            if request.response_schema_name == "strategy_semantic_review":
                return {**SemanticReview(
                    instrument="equivalent", requested_bar_interval="unspecified", differences=[],
                ).model_dump(mode="json"), "requirements": [{
                    "status": "represented", "candidate_path": "/entry",
                    "source_quote": request.utterance,
                    "requested_meaning": "测试条件", "candidate_meaning": "测试条件",
                }]}
            return self.response

    transport = ReviewingTransport(case.response)
    generator = VibeBoundedCandidateGenerator(
        transport, capability_matrix=MATRIX, model_semantic_review=True,
    )
    generated = await generator.generate(_request(case.utterance))

    assert generated[0].unsupported_code is None
    assert len(transport.requests) == 2
    assert transport.requests[1].response_schema_name == "strategy_semantic_review"
    expected = STABLE_FORMULAS[indicator_id]
    assert all(fragment in expected for fragment in semantic_fragments)
    for request in transport.requests:
        capability = next(item for item in request.capability_matrix["indicators"]
                          if item["indicator_id"] == indicator_id)
        assert capability["formula_summary"] == expected
    review_payload = transport.requests[1].user_payload
    assert review_payload is not None
    assert "capabilityMatrix" not in review_payload
    assert next(item for item in review_payload["selectedIndicatorDefinitions"]
                if item["indicator_id"] == indicator_id)["formula_summary"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    (
        "unknown_indicator",
        "unknown_trigger",
        "unknown_parameter",
        "research_event",
        "unknown_event_attribute",
        "unsupported_document_text",
        "minute_timeframe",
        "invented_stop_loss",
    ),
)
async def test_non_catalog_or_unmodeled_provider_output_fails_closed(mutation: str) -> None:
    case = _batch([_indicator_payload("technical.macd", "golden_cross")])
    response = case.response
    candidate = response["candidates"][0]  # type: ignore[index]
    entry = candidate["entry"][0]  # type: ignore[index]
    if mutation == "unknown_indicator":
        entry["indicator_id"] = "technical.not_real"  # type: ignore[index]
    elif mutation == "unknown_trigger":
        entry["trigger"] = "clairvoyant_cross"  # type: ignore[index]
    elif mutation == "unknown_parameter":
        entry["params"]["lookahead"] = 1  # type: ignore[index]
    elif mutation == "research_event":
        research_event = next(item for item in COVERAGE.events if item.status == "research_only")
        candidate["entry"] = [_event_payload("event.financial_results.annual_report")]
        candidate["entry"][0]["event_code"] = research_event.id  # type: ignore[index]
    elif mutation == "unknown_event_attribute":
        candidate["entry"] = [_event_payload("event.financial_results.annual_report")]
        candidate["entry"][0]["attributes"] = {"future_profit": 100}  # type: ignore[index]
    elif mutation == "unsupported_document_text":
        candidate["entry"] = [_event_payload("event.repurchase_capital.repurchase_completion")]
        candidate["entry"][0]["document_text"] = {  # type: ignore[index]
            "term": "AI",
            "match_mode": "ascii_token",
            "comparator": "gte",
            "value": 1,
        }
    elif mutation == "minute_timeframe":
        entry["timeframe"] = "1m"  # type: ignore[index]
    else:
        candidate["stop_loss"] = {"percent": 5}  # type: ignore[index]

    candidates = await _generator(response).generate(_request(case.utterance))

    assert candidates[0].unsupported_code == "candidate_provider_invalid_output"


@pytest.mark.asyncio
async def test_missing_matrix_fails_closed_without_calling_transport() -> None:
    case = _batch([_indicator_payload("technical.macd", "golden_cross")])
    transport = _FakeTransport(case.response)
    generator = VibeBoundedCandidateGenerator(transport)

    candidates = await generator.generate(_request(case.utterance))

    assert candidates[0].unsupported_code == "candidate_capability_matrix_unavailable"
    assert transport.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_field", ["entry", "exit"])
async def test_provider_omitting_a_supplied_rule_is_not_blamed_on_user(
    missing_field: str,
) -> None:
    case = _batch([_indicator_payload("technical.macd", "golden_cross")])
    response = case.response
    candidate = response["candidates"][0]  # type: ignore[index]
    del candidate[missing_field]  # type: ignore[index]

    outcome = await _compiler(response).compile(_request(case.utterance))

    assert outcome.status is CompileStatus.UNSUPPORTED
    assert outcome.diagnostic_code == "candidate_provider_invalid_output"
    assert outcome.strategy is None


@pytest.mark.asyncio
async def test_unspoken_macd_parameter_uses_declared_catalog_default() -> None:
    case = _batch([_indicator_payload("technical.macd", "golden_cross")])
    del case.response["candidates"][0]["entry"][0]["params"]["signal"]  # type: ignore[index]
    candidates = await _generator(case.response).generate(_request(case.utterance))
    assert candidates[0].unsupported_code is None
    capability = MATRIX.resolve_indicator("technical.macd")
    assert capability is not None
    expected = next(parameter.default for parameter in capability.parameters
                    if parameter.name == "signal")
    leaf = candidates[0].entry[0]
    assert isinstance(leaf, IndicatorIntent)
    assert dict(leaf.params)["signal"] == expected
    assert "/entry/0/params/signal" in candidates[0].defaulted_fields


@pytest.mark.asyncio
async def test_provider_future_backtest_end_is_rejected_by_host_compiler() -> None:
    case = _batch(
        [_indicator_payload("technical.macd", "golden_cross")],
        backtest_start="2021-01-01",
        backtest_end="2026-09-01",
        backtest_lookback_years=None,
    )

    outcome = await _compiler(case.response).compile(_request(case.utterance))

    assert outcome.status is CompileStatus.UNSUPPORTED
    assert outcome.diagnostic_code == "backtest_end_after_as_of_date"


@pytest.mark.asyncio
async def test_generic_recommendation_cannot_be_rewritten_into_a_grounded_strategy() -> None:
    case = _batch([_indicator_payload("technical.macd", "golden_cross")])
    response = case.response
    candidate = response["candidates"][0]  # type: ignore[index]
    utterance = "推荐一个好策略"
    fabricated_span = {"start": 0, "end": len(utterance), "text": utterance}
    candidate["entry_spans"] = [fabricated_span]  # type: ignore[index]
    candidate["exit_spans"] = [fabricated_span]  # type: ignore[index]

    candidates = await _generator(response).generate(_request(utterance))

    assert candidates[0].unsupported_code == "candidate_provider_invalid_output"


@pytest.mark.asyncio
async def test_allowed_but_wrong_trigger_cannot_reuse_an_opposite_source_span() -> None:
    case = _batch([_indicator_payload("technical.macd", "golden_cross")])
    response = case.response
    candidate = response["candidates"][0]  # type: ignore[index]
    candidate["entry"][0]["trigger"] = "death_cross"  # type: ignore[index]

    candidates = await _generator(response).generate(_request(case.utterance))

    assert candidates[0].unsupported_code == "candidate_provider_invalid_output"


@pytest.mark.asyncio
async def test_annual_report_alias_cannot_match_inside_semiannual_report() -> None:
    entry_text = "半年报发布后买入"
    exit_text = "持有3个交易日卖出"
    utterance = f"{entry_text}；{exit_text}"
    payload = {
        "candidates": [
            {
                "instrument_symbol": None,
                "entry": [_event_payload("event.financial_results.annual_report")],
                "exit": [{"kind": "holding_period", "sessions": 3}],
                "entry_spans": [_span(utterance, entry_text)],
                "exit_spans": [_span(utterance, exit_text)],
                "confidence": 0.95,
            }
        ]
    }

    candidates = await _generator(payload).generate(_request(utterance))

    assert candidates[0].unsupported_code == "candidate_provider_invalid_output"


@pytest.mark.asyncio
async def test_midyear_report_alias_maps_only_to_semiannual_report() -> None:
    entry_text = "中报发布后买入"
    exit_text = "持有3个交易日卖出"
    utterance = f"{entry_text}；{exit_text}"

    for event_code, expected_code in (
        ("event.financial_results.semiannual_report", None),
        ("event.financial_results.annual_report", "candidate_provider_invalid_output"),
    ):
        payload = {
            "candidates": [
                {
                    "instrument_symbol": None,
                    "entry": [_event_payload(event_code)],
                    "exit": [{"kind": "holding_period", "sessions": 3}],
                    "entry_spans": [_span(utterance, entry_text)],
                    "exit_spans": [_span(utterance, exit_text)],
                    "confidence": 0.95,
                }
            ]
        }

        candidates = await _generator(payload).generate(_request(utterance))

        assert candidates[0].unsupported_code == expected_code


@pytest.mark.asyncio
async def test_ma_alias_cannot_match_inside_ema_entity() -> None:
    entry_text = "EMA股价上穿买入"
    exit_text = "持有3个交易日卖出"
    utterance = f"{entry_text}；{exit_text}"
    indicator = _indicator_payload("technical.ma", "price_crosses_above")
    payload = {
        "candidates": [
            {
                "instrument_symbol": None,
                "entry": [indicator],
                "exit": [{"kind": "holding_period", "sessions": 3}],
                "entry_spans": [_span(utterance, entry_text)],
                "exit_spans": [_span(utterance, exit_text)],
                "confidence": 0.95,
                "defaulted_fields": [
                    "/entry/0/params/period",
                    "/entry/0/params/price_field",
                ],
            }
        ]
    }

    candidates = await _generator(payload).generate(_request(utterance))

    assert candidates[0].unsupported_code == "candidate_provider_invalid_output"


@pytest.mark.asyncio
async def test_macd_positional_parameters_bind_to_catalog_order() -> None:
    entry_text = "MACD(8,21,5)金叉买入"
    exit_text = "持有3个交易日卖出"
    utterance = f"{entry_text}；{exit_text}"
    indicator = _indicator_payload("technical.macd", "golden_cross")
    indicator["params"] = {"fast": 8, "slow": 21, "signal": 5}
    payload = {
        "candidates": [
            {
                "instrument_symbol": None,
                "entry": [indicator],
                "exit": [{"kind": "holding_period", "sessions": 3}],
                "entry_spans": [_span(utterance, entry_text)],
                "exit_spans": [_span(utterance, exit_text)],
                "confidence": 0.95,
            }
        ]
    }

    candidates = await _generator(payload).generate(_request(utterance))

    assert candidates[0].unsupported_code is None
    intent = candidates[0].entry[0]
    assert isinstance(intent, IndicatorIntent)
    assert intent.params_dict() == {"fast": 8, "slow": 21, "signal": 5}


@pytest.mark.asyncio
async def test_macd_positional_numbers_cannot_be_reassigned_to_other_fields() -> None:
    entry_text = "MACD(12,26,9)金叉买入"
    exit_text = "持有3个交易日卖出"
    utterance = f"{entry_text}；{exit_text}"
    indicator = _indicator_payload("technical.macd", "golden_cross")
    indicator["params"] = {"fast": 9, "slow": 26, "signal": 12}
    payload = {
        "candidates": [
            {
                "instrument_symbol": None,
                "entry": [indicator],
                "exit": [{"kind": "holding_period", "sessions": 3}],
                "entry_spans": [_span(utterance, entry_text)],
                "exit_spans": [_span(utterance, exit_text)],
                "confidence": 0.95,
                "defaulted_fields": ["/entry/0/params/slow"],
            }
        ]
    }

    candidates = await _generator(payload).generate(_request(utterance))

    assert candidates[0].unsupported_code == "candidate_provider_invalid_output"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entry_text",
    (
        "MACD(8,21,5)金叉买入",
        "MACD快线8慢线21信号线5金叉买入",
    ),
)
async def test_explicit_macd_parameters_cannot_be_replaced_by_catalog_defaults(
    entry_text: str,
) -> None:
    exit_text = "持有3个交易日卖出"
    utterance = f"{entry_text}；{exit_text}"
    indicator = _indicator_payload("technical.macd", "golden_cross")
    payload = {
        "candidates": [
            {
                "instrument_symbol": None,
                "entry": [indicator],
                "exit": [{"kind": "holding_period", "sessions": 3}],
                "entry_spans": [_span(utterance, entry_text)],
                "exit_spans": [_span(utterance, exit_text)],
                "confidence": 0.95,
                "defaulted_fields": [
                    "/entry/0/params/fast",
                    "/entry/0/params/slow",
                    "/entry/0/params/signal",
                ],
            }
        ]
    }

    candidates = await _generator(payload).generate(_request(utterance))

    assert candidates[0].unsupported_code == "candidate_provider_invalid_output"


def _two_indicator_join_payload(
    *,
    join_word: str,
    candidate_join: str,
) -> tuple[dict[str, object], str]:
    entry_text = f"MACD金叉{join_word}RSI低于30买入"
    exit_text = "持有3个交易日卖出"
    utterance = f"{entry_text}；{exit_text}"
    macd = _indicator_payload("technical.macd", "golden_cross")
    rsi = _indicator_payload("technical.rsi", "below")
    rsi["value"] = 30
    return (
        {
            "candidates": [
                {
                    "instrument_symbol": None,
                    "entry": [macd, rsi],
                    "exit": [{"kind": "holding_period", "sessions": 3}],
                    "entry_join": candidate_join,
                    "entry_spans": [
                        _span(utterance, entry_text),
                        _span(utterance, entry_text),
                    ],
                    "exit_spans": [_span(utterance, exit_text)],
                    "confidence": 0.95,
                    "defaulted_fields": [
                        "/entry/0/params/fast",
                        "/entry/0/params/slow",
                        "/entry/0/params/signal",
                        "/entry/1/params/period",
                    ],
                }
            ]
        },
        utterance,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("join_word", "candidate_join"),
    (("或", "all"), ("且", "any")),
)
async def test_multi_leaf_join_cannot_contradict_source_connector(
    join_word: str,
    candidate_join: str,
) -> None:
    payload, utterance = _two_indicator_join_payload(
        join_word=join_word,
        candidate_join=candidate_join,
    )

    candidates = await _generator(payload).generate(_request(utterance))

    assert candidates[0].unsupported_code == "candidate_provider_invalid_output"


@pytest.mark.asyncio
async def test_multi_leaf_any_join_accepts_explicit_or_connector() -> None:
    payload, utterance = _two_indicator_join_payload(join_word="或", candidate_join="any")

    candidates = await _generator(payload).generate(_request(utterance))

    assert candidates[0].unsupported_code is None


@pytest.mark.asyncio
async def test_provider_cannot_omit_one_explicit_entry_leaf() -> None:
    payload, utterance = _two_indicator_join_payload(join_word="且", candidate_join="all")
    candidate = payload["candidates"][0]  # type: ignore[index]
    candidate["entry"] = [candidate["entry"][0]]  # type: ignore[index]
    candidate["entry_spans"] = [candidate["entry_spans"][0]]  # type: ignore[index]
    defaults = candidate["defaulted_fields"]  # type: ignore[index]
    assert isinstance(defaults, list)
    candidate["defaulted_fields"] = [
        item for item in defaults if not str(item).startswith("/entry/1/")
    ]

    candidates = await _generator(payload).generate(_request(utterance))

    assert candidates[0].unsupported_code == "candidate_provider_invalid_output"


@pytest.mark.asyncio
async def test_provider_cannot_omit_explicit_leaf_after_prefix_entry_action() -> None:
    entry_text = "买入MACD金叉且RSI低于30"
    exit_text = "持有3个交易日卖出"
    utterance = f"{entry_text}；{exit_text}"
    payload = {
        "candidates": [
            {
                "instrument_symbol": None,
                "entry": [_indicator_payload("technical.macd", "golden_cross")],
                "exit": [{"kind": "holding_period", "sessions": 3}],
                "entry_spans": [_span(utterance, entry_text)],
                "exit_spans": [_span(utterance, exit_text)],
                "confidence": 0.95,
                "defaulted_fields": [
                    "/entry/0/params/fast",
                    "/entry/0/params/slow",
                    "/entry/0/params/signal",
                ],
            }
        ]
    }

    candidates = await _generator(payload).generate(_request(utterance))

    assert candidates[0].unsupported_code == "candidate_provider_invalid_output"


def test_unpunctuated_prefix_actions_claim_only_their_following_conditions() -> None:
    utterance = "买入MACD金叉卖出MACD死叉"

    assert _source_action_fragments(utterance, side="entry", matrix=MATRIX) == ("买入MACD金叉",)
    assert _source_action_fragments(utterance, side="exit", matrix=MATRIX) == ("卖出MACD死叉",)


def _three_indicator_join_payload(
    *,
    first_connector: str,
    second_connector: str,
    candidate_join: str,
) -> tuple[dict[str, object], str]:
    entry_text = f"MACD金叉{first_connector}RSI低于30{second_connector}振幅高于5买入"
    exit_text = "持有3个交易日卖出"
    utterance = f"{entry_text}；{exit_text}"
    macd = _indicator_payload("technical.macd", "golden_cross")
    rsi = _indicator_payload("technical.rsi", "below")
    rsi["value"] = 30
    amplitude = _indicator_payload("price.amplitude", "above")
    amplitude["value"] = 5
    return (
        {
            "candidates": [
                {
                    "instrument_symbol": None,
                    "entry": [macd, rsi, amplitude],
                    "exit": [{"kind": "holding_period", "sessions": 3}],
                    "entry_join": candidate_join,
                    "entry_spans": [
                        _span(utterance, entry_text),
                        _span(utterance, entry_text),
                        _span(utterance, entry_text),
                    ],
                    "exit_spans": [_span(utterance, exit_text)],
                    "confidence": 0.95,
                    "defaulted_fields": [
                        "/entry/0/params/fast",
                        "/entry/0/params/slow",
                        "/entry/0/params/signal",
                        "/entry/1/params/period",
                    ],
                }
            ]
        },
        utterance,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_connector", "second_connector", "candidate_join", "is_valid"),
    [
        ("且", "", "all", False),
        ("且", "且", "all", True),
        ("并且", "同时", "all", True),
        ("且", "或", "all", False),
        ("或者", "或", "any", True),
        ("或", "", "any", False),
    ],
)
async def test_three_leaf_join_requires_one_unambiguous_connector_per_gap(
    first_connector: str,
    second_connector: str,
    candidate_join: str,
    is_valid: bool,
) -> None:
    payload, utterance = _three_indicator_join_payload(
        first_connector=first_connector,
        second_connector=second_connector,
        candidate_join=candidate_join,
    )

    candidates = await _generator(payload).generate(_request(utterance))

    if is_valid:
        assert candidates[0].unsupported_code is None
    else:
        assert candidates[0].unsupported_code == "candidate_provider_invalid_output"


def _position_return_payload(
    *,
    take_profit: float,
    stop_loss: float,
) -> tuple[dict[str, object], str]:
    entry_text = "MACD金叉买入"
    exit_text = "止盈20%或止损5%卖出"
    utterance = f"{entry_text}；{exit_text}"
    macd = _indicator_payload("technical.macd", "golden_cross")
    return (
        {
            "candidates": [
                {
                    "instrument_symbol": None,
                    "entry": [macd],
                    "exit": [
                        {
                            "kind": "position_return",
                            "trigger": "take_profit",
                            "threshold_pct": take_profit,
                        },
                        {
                            "kind": "position_return",
                            "trigger": "stop_loss",
                            "threshold_pct": stop_loss,
                        },
                    ],
                    "exit_join": "any",
                    "entry_spans": [_span(utterance, entry_text)],
                    "exit_spans": [
                        _span(utterance, exit_text),
                        _span(utterance, exit_text),
                    ],
                    "confidence": 0.95,
                    "defaulted_fields": [
                        "/entry/0/params/fast",
                        "/entry/0/params/slow",
                        "/entry/0/params/signal",
                    ],
                }
            ]
        },
        utterance,
    )


@pytest.mark.asyncio
async def test_position_return_values_bind_to_take_profit_and_stop_loss_words() -> None:
    payload, utterance = _position_return_payload(take_profit=20, stop_loss=5)

    candidates = await _generator(payload).generate(_request(utterance))

    assert candidates[0].unsupported_code is None


@pytest.mark.asyncio
async def test_position_return_values_cannot_be_swapped_across_shared_span() -> None:
    payload, utterance = _position_return_payload(take_profit=5, stop_loss=20)

    candidates = await _generator(payload).generate(_request(utterance))

    assert candidates[0].unsupported_code == "candidate_provider_invalid_output"


@pytest.mark.asyncio
async def test_provider_cannot_omit_one_explicit_exit_leaf() -> None:
    payload, utterance = _position_return_payload(take_profit=20, stop_loss=5)
    candidate = payload["candidates"][0]  # type: ignore[index]
    candidate["exit"] = [candidate["exit"][0]]  # type: ignore[index]
    candidate["exit_spans"] = [candidate["exit_spans"][0]]  # type: ignore[index]

    candidates = await _generator(payload).generate(_request(utterance))

    assert candidates[0].unsupported_code == "candidate_provider_invalid_output"


@pytest.mark.asyncio
async def test_generic_boolean_words_cannot_invent_event_attribute() -> None:
    entry_text = "公司有重大项目中标后买入"
    exit_text = "持有3个交易日卖出"
    utterance = f"{entry_text}；{exit_text}"
    event = _event_payload("event.contracts_orders.major_contract_won")
    event["attributes"] = {"is_consortium": True}
    payload = {
        "candidates": [
            {
                "instrument_symbol": None,
                "entry": [event],
                "exit": [{"kind": "holding_period", "sessions": 3}],
                "entry_spans": [_span(utterance, entry_text)],
                "exit_spans": [_span(utterance, exit_text)],
                "confidence": 0.95,
            }
        ]
    }

    candidates = await _generator(payload).generate(_request(utterance))

    assert candidates[0].unsupported_code == "candidate_provider_invalid_output"


@pytest.mark.asyncio
async def test_consortium_attribute_accepts_field_specific_word() -> None:
    entry_text = "公司以联合体形式重大项目中标后买入"
    exit_text = "持有3个交易日卖出"
    utterance = f"{entry_text}；{exit_text}"
    event = _event_payload("event.contracts_orders.major_contract_won")
    event["attributes"] = {"is_consortium": True}
    payload = {
        "candidates": [
            {
                "instrument_symbol": None,
                "entry": [event],
                "exit": [{"kind": "holding_period", "sessions": 3}],
                "entry_spans": [_span(utterance, entry_text)],
                "exit_spans": [_span(utterance, exit_text)],
                "confidence": 0.95,
            }
        ]
    }

    candidates = await _generator(payload).generate(_request(utterance))

    assert candidates[0].unsupported_code is None


def _annual_document_payload(
    *,
    entry_text: str,
    term: str,
    comparator: str,
    value: int,
) -> tuple[dict[str, object], str]:
    exit_text = "持有3个交易日卖出"
    utterance = f"{entry_text}；{exit_text}"
    event = _event_payload("event.financial_results.annual_report")
    event["document_text"] = {
        "term": term,
        "match_mode": "ascii_token" if term.isascii() else "literal",
        "comparator": comparator,
        "value": value,
        "case_sensitive": False,
    }
    return (
        {
            "candidates": [
                {
                    "instrument_symbol": None,
                    "entry": [event],
                    "exit": [{"kind": "holding_period", "sessions": 3}],
                    "entry_spans": [_span(utterance, entry_text)],
                    "exit_spans": [_span(utterance, exit_text)],
                    "confidence": 0.95,
                }
            ]
        },
        utterance,
    )


@pytest.mark.asyncio
async def test_document_term_comparator_and_value_bind_to_one_local_clause() -> None:
    payload, utterance = _annual_document_payload(
        entry_text="年报中AI超过5次买入",
        term="AI",
        comparator="gt",
        value=5,
    )

    candidates = await _generator(payload).generate(_request(utterance))

    assert candidates[0].unsupported_code is None


@pytest.mark.asyncio
async def test_document_predicate_fields_cannot_be_cross_bound_between_clauses() -> None:
    payload, utterance = _annual_document_payload(
        entry_text="年报中AI超过5次或风险至少10次就买入",
        term="AI",
        comparator="gte",
        value=10,
    )

    candidates = await _generator(payload).generate(_request(utterance))

    assert candidates[0].unsupported_code == "candidate_provider_invalid_output"


@pytest.mark.asyncio
async def test_document_predicate_cannot_omit_parallel_source_condition() -> None:
    payload, utterance = _annual_document_payload(
        entry_text="年报中AI超过5次或风险至少10次就买入",
        term="AI",
        comparator="gt",
        value=5,
    )

    candidates = await _generator(payload).generate(_request(utterance))

    assert candidates[0].unsupported_code == "candidate_provider_invalid_output"


@pytest.mark.asyncio
async def test_document_predicate_cannot_be_omitted_entirely() -> None:
    payload, utterance = _annual_document_payload(
        entry_text="年报中AI超过5次就买入",
        term="AI",
        comparator="gt",
        value=5,
    )
    candidate = payload["candidates"][0]  # type: ignore[index]
    event = candidate["entry"][0]  # type: ignore[index]
    del event["document_text"]

    candidates = await _generator(payload).generate(_request(utterance))

    assert candidates[0].unsupported_code == "candidate_provider_invalid_output"


@pytest.mark.asyncio
async def test_explicit_non_default_parameter_requires_lexical_evidence() -> None:
    case = _batch([_indicator_payload("technical.macd", "golden_cross")])
    response = case.response
    candidate = response["candidates"][0]  # type: ignore[index]
    entry = candidate["entry"][0]  # type: ignore[index]
    entry["params"]["fast"] = 7  # type: ignore[index]
    defaults = candidate["defaulted_fields"]  # type: ignore[index]
    assert isinstance(defaults, list)
    defaults.remove("/entry/0/params/fast")

    candidates = await _generator(response).generate(_request(case.utterance))

    assert candidates[0].unsupported_code == "candidate_provider_invalid_output"


@pytest.mark.asyncio
async def test_non_default_parameter_with_exact_source_number_is_accepted() -> None:
    indicator = _indicator_payload("technical.macd", "golden_cross")
    indicator["params"]["fast"] = 7  # type: ignore[index]
    case = _batch([indicator])

    candidates = await _generator(case.response).generate(_request(case.utterance))

    assert candidates[0].unsupported_code is None
    intent = candidates[0].entry[0]
    assert isinstance(intent, IndicatorIntent)
    assert intent.params_dict()["fast"] == 7


@pytest.mark.asyncio
async def test_catalog_default_marker_cannot_hide_a_changed_parameter() -> None:
    case = _batch([_indicator_payload("technical.macd", "golden_cross")])
    response = case.response
    candidate = response["candidates"][0]  # type: ignore[index]
    candidate["entry"][0]["params"]["fast"] = 7  # type: ignore[index]

    candidates = await _generator(response).generate(_request(case.utterance))

    assert candidates[0].unsupported_code == "candidate_provider_invalid_output"


@pytest.mark.asyncio
async def test_provider_extracted_instrument_requires_exact_code_evidence() -> None:
    case = _batch(
        [_indicator_payload("technical.macd", "golden_cross")],
        instrument_symbol="300059.SZ",
    )
    response = case.response
    candidate = response["candidates"][0]  # type: ignore[index]
    candidate.pop("instrument_span")  # type: ignore[union-attr]

    candidates = await _generator(response).generate(
        CompileInput(utterance=case.utterance, instrument_context=None, as_of_date=AS_OF_DATE)
    )

    assert candidates[0].unsupported_code == "candidate_provider_invalid_output"


@pytest.mark.asyncio
async def test_grounded_symbol_period_and_leaf_spans_are_preserved_in_candidate_ast() -> None:
    case = _batch(
        [_indicator_payload("technical.macd", "golden_cross")],
        instrument_symbol="300059.SZ",
        backtest_start="2021-01-01",
        backtest_end="2026-08-30",
    )

    candidates = await _generator(case.response).generate(
        CompileInput(utterance=case.utterance, instrument_context=None, as_of_date=AS_OF_DATE)
    )

    assert candidates[0].unsupported_code is None
    evidence = {item.path: item for item in candidates[0].grounding_evidence}
    assert set(evidence) == {"/entry/0", "/exit/0", "/instrument/symbol", "/backtest"}
    assert all(case.utterance[item.start : item.end] == item.text for item in evidence.values())


@pytest.mark.asyncio
async def test_lookback_without_exact_period_span_fails_closed() -> None:
    case = _batch(
        [_indicator_payload("technical.macd", "golden_cross")],
        backtest_lookback_years=5,
    )
    response = case.response
    candidate = response["candidates"][0]  # type: ignore[index]
    candidate.pop("backtest_span")  # type: ignore[union-attr]

    candidates = await _generator(response).generate(_request(case.utterance))

    assert candidates[0].unsupported_code == "candidate_provider_invalid_output"


@pytest.mark.asyncio
@pytest.mark.parametrize("period_kind", ("date_range", "lookback"))
async def test_provider_cannot_omit_explicit_source_backtest_period(period_kind: str) -> None:
    if period_kind == "date_range":
        case = _batch(
            [_indicator_payload("technical.macd", "golden_cross")],
            backtest_start="2021-01-01",
            backtest_end="2026-08-30",
        )
    else:
        case = _batch(
            [_indicator_payload("technical.macd", "golden_cross")],
            backtest_lookback_years=5,
        )
    response = case.response
    candidate = response["candidates"][0]  # type: ignore[index]
    candidate.pop("backtest_start", None)  # type: ignore[union-attr]
    candidate.pop("backtest_end", None)  # type: ignore[union-attr]
    candidate.pop("backtest_lookback_years", None)  # type: ignore[union-attr]
    candidate.pop("backtest_span", None)  # type: ignore[union-attr]

    candidates = await _generator(response).generate(_request(case.utterance))

    assert candidates[0].unsupported_code == "candidate_provider_invalid_output"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("join", "expected_type"),
    (("all", AllCondition), ("any", AnyCondition)),
)
async def test_catalog_conditions_compose_and_fixed_holding_period_remains_first_of(
    join: str,
    expected_type: type[AllCondition] | type[AnyCondition],
) -> None:
    case = _batch(
        [
            _indicator_payload("technical.macd", "golden_cross"),
            _indicator_payload("technical.rsi", "below"),
        ],
        [
            _indicator_payload("technical.macd", "death_cross"),
            {"kind": "holding_period", "sessions": 7},
        ],
        entry_join=join,
        exit_join="any",
    )

    outcome = await _compiler(case.response).compile(_request(case.utterance))

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert isinstance(outcome.strategy.entry, expected_type)
    assert any(
        isinstance(item, HoldingPeriodExit) and item.sessions == 7
        for item in outcome.strategy.exit.children
    )


def test_projection_hash_changes_when_catalog_projection_changes() -> None:
    payload = MATRIX.model_dump(mode="json")
    payload["events"] = payload["events"][:-1]
    changed = CandidateCapabilityMatrix.model_validate(payload)

    assert changed.content_hash != MATRIX.content_hash
