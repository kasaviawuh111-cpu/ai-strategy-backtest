from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import cast

import pytest

from ashare_lab.adapters.language.rule_based import RuleBasedCandidateGenerator
from ashare_lab.adapters.language.vibe_candidates import (
    CandidateProviderIdentityView,
    CandidateTransportError,
    CandidateTransportRequest,
    CandidateTransportResponse,
    HybridCandidateGenerator,
    VibeBoundedCandidateGenerator,
    build_candidate_capability_matrix,
)
from ashare_lab.application.compile_strategy import CompileStatus, StrategyCompiler
from ashare_lab.domain.catalog import load_catalog_directory, load_coverage_catalog_directory
from ashare_lab.domain.strategy import (
    AllCondition,
    EventCondition,
    HoldingPeriodExit,
    IndicatorCondition,
    PositionReturnExit,
    TrailingDrawdownExit,
)
from ashare_lab.ports.candidate_generation import CompileInput

ROOT = Path(__file__).parents[4]
CATALOG = load_catalog_directory(ROOT / "catalogs")
CATALOG_RELEASE = next(
    item.release_version for item in CATALOG.manifests if item.catalog_id == "cn_a.signals"
)
CAPABILITY_MATRIX = build_candidate_capability_matrix(
    CATALOG,
    load_coverage_catalog_directory(ROOT / "catalogs" / "coverage"),
)
DIRECT_UTTERANCE = "MACD金叉买入，MACD死叉卖出"
FALLBACK_UTTERANCE = "指数平滑异同移动平均线快线上穿时买入，指数平滑异同移动平均线快线下穿时卖出"


class _FakeTransport:
    def __init__(self, response: CandidateTransportResponse) -> None:
        self.response = response
        self.requests: list[CandidateTransportRequest] = []

    async def generate_json(
        self,
        request: CandidateTransportRequest,
    ) -> CandidateTransportResponse:
        self.requests.append(request)
        return self.response


class _SequenceTransport(_FakeTransport):
    def __init__(self, responses: tuple[CandidateTransportResponse, ...]) -> None:
        if not responses:
            raise ValueError("responses must not be empty")
        super().__init__(responses[0])
        self.responses = responses

    async def generate_json(
        self,
        request: CandidateTransportRequest,
    ) -> CandidateTransportResponse:
        response_index = len(self.requests)
        if response_index >= len(self.responses):
            raise AssertionError("candidate provider must not make a third attempt")
        self.requests.append(request)
        return self.responses[response_index]


class _FailingTransport:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def generate_json(
        self,
        request: CandidateTransportRequest,
    ) -> CandidateTransportResponse:
        del request
        raise self.error


@dataclass(frozen=True)
class _FakeIdentity:
    provider: str = "fixture-provider"
    model: str = "fixture-model"
    prompt_version: str = "fixture-prompt.v1"
    schema_version: str = "fixture-schema.v1"


def _bounded(transport: _FakeTransport) -> VibeBoundedCandidateGenerator:
    return VibeBoundedCandidateGenerator(
        transport,
        capability_matrix=CAPABILITY_MATRIX,
    )


@pytest.mark.asyncio
async def test_declared_transport_failure_degrades_to_provider_unavailable() -> None:
    generator = VibeBoundedCandidateGenerator(
        _FailingTransport(CandidateTransportError("sanitized unavailable")),
        capability_matrix=CAPABILITY_MATRIX,
    )

    candidates = await generator.generate(
        CompileInput(
            utterance=DIRECT_UTTERANCE,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert candidates[0].unsupported_code == "candidate_provider_unavailable"


@pytest.mark.asyncio
async def test_unexpected_transport_bug_is_not_disguised_as_unavailable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_message = "programming bug with provider-secret"
    generator = VibeBoundedCandidateGenerator(
        _FailingTransport(AssertionError(secret_message)),
        capability_matrix=CAPABILITY_MATRIX,
    )

    with pytest.raises(AssertionError, match="programming bug"):
        await generator.generate(
            CompileInput(
                utterance=DIRECT_UTTERANCE,
                instrument_context="300059.SZ",
                as_of_date=date(2026, 8, 30),
            )
        )

    assert "AssertionError" in caplog.text
    assert secret_message not in caplog.text


def _source_span(utterance: str, text: str) -> dict[str, object]:
    start = utterance.index(text)
    return {"start": start, "end": start + len(text), "text": text}


def _macd_batch(
    *,
    symbol: str | None = None,
    utterance: str = DIRECT_UTTERANCE,
) -> dict[str, object]:
    entry_text, exit_text = utterance.split("，", 1)
    return {
        "candidates": [
            {
                "instrument_symbol": symbol,
                "entry": [
                    {
                        "kind": "indicator",
                        "indicator_id": "technical.macd",
                        "definition_version": "1.0.0",
                        "trigger": "golden_cross",
                        "params": {"fast": 12, "slow": 26, "signal": 9},
                    }
                ],
                "exit": [
                    {
                        "kind": "indicator",
                        "indicator_id": "technical.macd",
                        "definition_version": "1.0.0",
                        "trigger": "death_cross",
                        "params": {"fast": 12, "slow": 26, "signal": 9},
                    }
                ],
                "entry_spans": [_source_span(utterance, entry_text)],
                "exit_spans": [_source_span(utterance, exit_text)],
                "confidence": 0.91,
                "defaulted_fields": [
                    "/entry/0/params/fast",
                    "/entry/0/params/signal",
                    "/entry/0/params/slow",
                    "/exit/0/params/fast",
                    "/exit/0/params/signal",
                    "/exit/0/params/slow",
                ],
            }
        ]
    }


def _macd_batch_with_ungrounded_entry(*, utterance: str) -> dict[str, object]:
    payload = deepcopy(_macd_batch(utterance=utterance))
    candidates = cast(list[object], payload["candidates"])
    candidate = cast(dict[str, object], candidates[0])
    exit_text = utterance.split("，", 1)[1]
    candidate["entry_spans"] = [_source_span(utterance, exit_text)]
    return payload


def _compiler(generator: HybridCandidateGenerator) -> StrategyCompiler:
    return StrategyCompiler(
        generator=generator,
        catalog=CATALOG,
        catalog_id="cn_a.signals",
        release_version=CATALOG_RELEASE,
        trusted_date_provider=lambda: date(2026, 8, 30),
    )


@pytest.mark.asyncio
async def test_unrecognized_phrase_uses_bounded_json_and_compiles_current_dsl() -> None:
    transport = _FakeTransport(json.dumps(_macd_batch(utterance=FALLBACK_UTTERANCE)))
    generator = HybridCandidateGenerator(
        deterministic=RuleBasedCandidateGenerator(),
        bounded_fallback=_bounded(transport),
    )

    outcome = await _compiler(generator).compile(
        CompileInput(
            utterance=FALLBACK_UTTERANCE,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert outcome.strategy.instrument.symbol == "300059.SZ"
    assert isinstance(outcome.strategy.entry, IndicatorCondition)
    assert outcome.strategy.entry.indicator_id == "technical.macd"
    assert outcome.strategy.entry.trigger == "golden_cross"
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.max_candidates == 3
    assert "不得生成 Python" in request.system_contract
    assert request.capability_projection_version == "candidate-capabilities.v1"
    assert request.capability_projection_hash == CAPABILITY_MATRIX.content_hash
    assert request.upstream_pattern_commit.startswith("e90b6c6")


@pytest.mark.asyncio
async def test_grounding_invalid_first_attempt_retries_once_and_accepts_valid_payload() -> None:
    transport = _SequenceTransport(
        (
            _macd_batch_with_ungrounded_entry(utterance=FALLBACK_UTTERANCE),
            _macd_batch(utterance=FALLBACK_UTTERANCE),
        )
    )
    generator = HybridCandidateGenerator(
        deterministic=RuleBasedCandidateGenerator(),
        bounded_fallback=_bounded(transport),
    )

    outcome = await _compiler(generator).compile(
        CompileInput(
            utterance=FALLBACK_UTTERANCE,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert len(transport.requests) == 2


@pytest.mark.asyncio
async def test_two_grounding_invalid_attempts_fail_closed_without_third_call() -> None:
    invalid = _macd_batch_with_ungrounded_entry(utterance=FALLBACK_UTTERANCE)
    transport = _SequenceTransport((invalid, deepcopy(invalid)))
    generator = HybridCandidateGenerator(
        deterministic=RuleBasedCandidateGenerator(),
        bounded_fallback=_bounded(transport),
    )

    outcome = await _compiler(generator).compile(
        CompileInput(
            utterance=FALLBACK_UTTERANCE,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.UNSUPPORTED
    assert outcome.diagnostic_code == "candidate_provider_invalid_output"
    assert len(transport.requests) == 2


@pytest.mark.asyncio
async def test_known_expression_stays_on_deterministic_fast_path() -> None:
    transport = _FakeTransport(_macd_batch())
    generator = HybridCandidateGenerator(
        deterministic=RuleBasedCandidateGenerator(),
        bounded_fallback=_bounded(transport),
    )

    outcome = await _compiler(generator).compile(
        CompileInput(
            utterance="MACD 金叉买入，死叉卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert transport.requests == []


@pytest.mark.asyncio
async def test_bounded_event_candidate_compiles_without_generated_code() -> None:
    utterance = "年度报告发布后买入，持有3个交易日卖出"
    entry_text, exit_text = utterance.split("，", 1)
    transport = _FakeTransport(
        {
            "candidates": [
                {
                    "instrument_symbol": None,
                    "entry": [
                        {
                            "kind": "event",
                            "event_code": "event.financial_results.annual_report",
                            "definition_version": "1.0.0",
                            "trigger": "published",
                            "attributes": {},
                        }
                    ],
                    "exit": [{"kind": "holding_period", "sessions": 3}],
                    "entry_spans": [_source_span(utterance, entry_text)],
                    "exit_spans": [_source_span(utterance, exit_text)],
                    "confidence": 0.88,
                }
            ]
        }
    )
    generator = _bounded(transport)

    outcome = await StrategyCompiler(
        generator=generator,
        catalog=CATALOG,
        catalog_id="cn_a.signals",
        release_version=CATALOG_RELEASE,
        trusted_date_provider=lambda: date(2026, 8, 30),
    ).compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert isinstance(outcome.strategy.entry, EventCondition)
    assert outcome.strategy.entry.event_code == "event.financial_results.annual_report"
    assert isinstance(outcome.strategy.exit.children[0], HoldingPeriodExit)
    assert outcome.strategy.exit.children[0].sessions == 3


@pytest.mark.asyncio
async def test_bounded_position_risk_exits_require_exact_user_grounding() -> None:
    utterance = "MACD金叉买入，止盈20%或从高点回撤8%卖出"
    entry_text, _exit_text = utterance.split("，", 1)
    exit_text = "止盈20%或从高点回撤8%卖出"
    payload = {
        "candidates": [
            {
                "instrument_symbol": None,
                "entry": [
                    {
                        "kind": "indicator",
                        "indicator_id": "technical.macd",
                        "definition_version": "1.0.0",
                        "trigger": "golden_cross",
                        "params": {"fast": 12, "slow": 26, "signal": 9},
                    }
                ],
                "exit": [
                    {
                        "kind": "position_return",
                        "trigger": "take_profit",
                        "threshold_pct": 20,
                    },
                    {"kind": "trailing_drawdown", "threshold_pct": 8},
                ],
                "entry_spans": [_source_span(utterance, entry_text)],
                "exit_spans": [
                    _source_span(utterance, exit_text),
                    _source_span(utterance, exit_text),
                ],
                "confidence": 0.95,
                "defaulted_fields": [
                    "/entry/0/params/fast",
                    "/entry/0/params/signal",
                    "/entry/0/params/slow",
                ],
            }
        ]
    }
    generator = VibeBoundedCandidateGenerator(
        _FakeTransport(payload),
        capability_matrix=CAPABILITY_MATRIX,
        provider_identity=_FakeIdentity(),
    )

    outcome = await StrategyCompiler(
        generator=generator,
        catalog=CATALOG,
        catalog_id="cn_a.signals",
        release_version=CATALOG_RELEASE,
        trusted_date_provider=lambda: date(2026, 8, 30),
    ).compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert isinstance(outcome.strategy.exit.children[0], PositionReturnExit)
    assert isinstance(outcome.strategy.exit.children[1], TrailingDrawdownExit)
    assert outcome.candidate_provenance is not None
    assert outcome.candidate_provenance.provider == "fixture-provider"
    assert outcome.candidate_provenance.capability_projection_hash == (
        CAPABILITY_MATRIX.content_hash
    )


@pytest.mark.asyncio
async def test_bounded_provider_exit_and_cannot_be_rewritten_as_first_of() -> None:
    utterance = "MACD金叉买入，止盈20%且止损5%卖出"
    entry_text, exit_text = utterance.split("，", 1)
    payload = {
        "candidates": [
            {
                "instrument_symbol": None,
                "entry": [
                    {
                        "kind": "indicator",
                        "indicator_id": "technical.macd",
                        "definition_version": "1.0.0",
                        "trigger": "golden_cross",
                        "params": {"fast": 12, "slow": 26, "signal": 9},
                    }
                ],
                "exit": [
                    {
                        "kind": "position_return",
                        "trigger": "take_profit",
                        "threshold_pct": 20,
                    },
                    {
                        "kind": "position_return",
                        "trigger": "stop_loss",
                        "threshold_pct": 5,
                    },
                ],
                "entry_spans": [_source_span(utterance, entry_text)],
                "exit_spans": [
                    _source_span(utterance, exit_text),
                    _source_span(utterance, exit_text),
                ],
                "exit_join": "all",
                "confidence": 0.95,
                "defaulted_fields": [
                    "/entry/0/params/fast",
                    "/entry/0/params/signal",
                    "/entry/0/params/slow",
                ],
            }
        ]
    }
    generator = VibeBoundedCandidateGenerator(
        _FakeTransport(payload),
        capability_matrix=CAPABILITY_MATRIX,
        provider_identity=_FakeIdentity(),
    )

    outcome = await StrategyCompiler(
        generator=generator,
        catalog=CATALOG,
        catalog_id="cn_a.signals",
        release_version=CATALOG_RELEASE,
        trusted_date_provider=lambda: date(2026, 8, 30),
    ).compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.UNSUPPORTED
    assert outcome.strategy is None
    assert outcome.diagnostic_code == "position_aware_exit_and_not_supported"
    assert outcome.candidate_provenance is not None
    assert outcome.candidate_provenance.provider == "fixture-provider"
    assert [item.diagnostic_code for item in outcome.candidate_rejections] == [
        "position_aware_exit_and_not_supported"
    ]


@pytest.mark.asyncio
async def test_provider_cannot_replace_host_instrument() -> None:
    transport = _FakeTransport(_macd_batch(symbol="600519.SH"))
    generator = _bounded(transport)

    candidates = await generator.generate(
        CompileInput(
            utterance=DIRECT_UTTERANCE,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert candidates[0].unsupported_code == "candidate_provider_invalid_output"


@pytest.mark.asyncio
async def test_arbitrary_generated_code_fails_closed() -> None:
    payload = _macd_batch()
    candidate = payload["candidates"][0]  # type: ignore[index]
    assert isinstance(candidate, dict)
    candidate["python"] = "import os; os.system('echo unsafe')"
    generator = _bounded(_FakeTransport(payload))

    candidates = await generator.generate(
        CompileInput(
            utterance=DIRECT_UTTERANCE,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert candidates[0].unsupported_code == "candidate_provider_invalid_output"


@pytest.mark.asyncio
async def test_missing_exit_does_not_bypass_existing_clarification() -> None:
    transport = _FakeTransport(_macd_batch())
    generator = HybridCandidateGenerator(
        deterministic=RuleBasedCandidateGenerator(),
        bounded_fallback=_bounded(transport),
    )

    outcome = await _compiler(generator).compile(
        CompileInput(
            utterance="MACD 金叉买入",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "exit_rule_not_recognized"
    assert transport.requests == []


@pytest.mark.asyncio
async def test_missing_entry_does_not_bypass_existing_clarification() -> None:
    transport = _FakeTransport(_macd_batch())
    generator = HybridCandidateGenerator(
        deterministic=RuleBasedCandidateGenerator(),
        bounded_fallback=_bounded(transport),
    )

    outcome = await _compiler(generator).compile(
        CompileInput(
            utterance="MACD 死叉卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "entry_rule_not_recognized"
    assert transport.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "utterance",
    (
        "MACD金叉就上车，MACD死叉就走",
        "MACD金叉买入，MACD死叉就收手",
    ),
)
async def test_source_complete_colloquial_actions_use_bounded_fallback(
    utterance: str,
) -> None:
    transport = _FakeTransport(_macd_batch(utterance=utterance))
    generator = HybridCandidateGenerator(
        deterministic=RuleBasedCandidateGenerator(),
        bounded_fallback=_bounded(transport),
    )

    outcome = await _compiler(generator).compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert len(transport.requests) == 1


@dataclass(frozen=True)
class _ExactFiveCase:
    utterance: str
    local_diagnostic: str
    payload: dict[str, object]
    expected_entry: tuple[object, ...]
    expected_exit: tuple[tuple[object, ...], ...]


def _indicator_payload(
    indicator_id: str,
    trigger: str,
    params: Mapping[str, object],
    *,
    value: float | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": "indicator",
        "indicator_id": indicator_id,
        "definition_version": "1.0.0",
        "trigger": trigger,
        "params": dict(params),
    }
    if value is not None:
        payload["value"] = value
    return payload


def _five_case_payload(
    *,
    utterance: str,
    entry: list[dict[str, object]],
    exit: list[dict[str, object]],
    entry_texts: list[str],
    exit_texts: list[str],
    backtest_text: str,
    defaulted_fields: list[str] | None = None,
) -> dict[str, object]:
    return {
        "candidates": [
            {
                "instrument_symbol": None,
                "entry": entry,
                "exit": exit,
                "entry_spans": [_source_span(utterance, text) for text in entry_texts],
                "exit_spans": [_source_span(utterance, text) for text in exit_texts],
                "backtest_lookback_years": 5,
                "backtest_span": _source_span(utterance, backtest_text),
                "confidence": 0.91,
                "defaulted_fields": defaulted_fields or [],
            }
        ]
    }


def _condition_signature(condition: object) -> tuple[object, ...]:
    if isinstance(condition, IndicatorCondition):
        return (
            "indicator",
            condition.indicator_id,
            condition.trigger,
            tuple(sorted(condition.params.items())),
            condition.value,
        )
    if isinstance(condition, EventCondition):
        return ("event", condition.event_code, condition.trigger)
    if isinstance(condition, HoldingPeriodExit):
        return ("holding_period", condition.sessions)
    if isinstance(condition, AllCondition):
        return ("all", tuple(_condition_signature(child) for child in condition.children))
    raise AssertionError(f"unexpected strategy node: {type(condition).__name__}")


def _exact_five_cases() -> tuple[_ExactFiveCase, ...]:
    ma_utterance = "东方财富最近五年，价格强势站上20日均线时上车，跌回这条线下就走。"
    ma_params = {"period": 20, "price_field": "close"}
    macd_utterance = "东方财富的 MACD 快线往上穿过慢线就买，往下穿回去就卖，回测近五年。"
    macd_params = {"fast": 12, "slow": 26, "signal": 9}
    rsi_utterance = "东方财富跌得很猛后，RSI 从30以下重新回到30上方就买，到70上方就收手，回测五年。"
    rsi_params = {"period": 14}
    volume_utterance = (
        "东方财富收盘创20日新高、成交量是过去20日平均的1.5倍才买；MACD 转弱卖，回测五年。"
    )
    event_utterance = "东方财富的年报公开以后就买，持有三个交易日后卖，近五年。"
    return (
        _ExactFiveCase(
            utterance=ma_utterance,
            local_diagnostic="strategy_rule_incomplete",
            payload=_five_case_payload(
                utterance=ma_utterance,
                entry=[_indicator_payload("technical.ma", "price_crosses_above", ma_params)],
                exit=[_indicator_payload("technical.ma", "price_crosses_below", ma_params)],
                entry_texts=["价格强势站上20日均线时上车"],
                exit_texts=["跌回这条线下就走"],
                backtest_text="最近五年",
                defaulted_fields=[
                    "/entry/0/params/price_field",
                    "/exit/0/params/price_field",
                ],
            ),
            expected_entry=(
                "indicator",
                "technical.ma",
                "price_crosses_above",
                (("period", 20), ("price_field", "close")),
                None,
            ),
            expected_exit=(
                (
                    "indicator",
                    "technical.ma",
                    "price_crosses_below",
                    (("period", 20), ("price_field", "close")),
                    None,
                ),
            ),
        ),
        _ExactFiveCase(
            utterance=macd_utterance,
            local_diagnostic="ambiguous_macd_trigger",
            payload=_five_case_payload(
                utterance=macd_utterance,
                entry=[_indicator_payload("technical.macd", "golden_cross", macd_params)],
                exit=[_indicator_payload("technical.macd", "death_cross", macd_params)],
                entry_texts=["MACD 快线往上穿过慢线就买"],
                exit_texts=["往下穿回去就卖"],
                backtest_text="回测近五年",
                defaulted_fields=[
                    f"/{side}/0/params/{name}"
                    for side in ("entry", "exit")
                    for name in ("fast", "signal", "slow")
                ],
            ),
            expected_entry=(
                "indicator",
                "technical.macd",
                "golden_cross",
                (("fast", 12), ("signal", 9), ("slow", 26)),
                None,
            ),
            expected_exit=(
                (
                    "indicator",
                    "technical.macd",
                    "death_cross",
                    (("fast", 12), ("signal", 9), ("slow", 26)),
                    None,
                ),
            ),
        ),
        _ExactFiveCase(
            utterance=rsi_utterance,
            local_diagnostic="exit_rule_not_recognized",
            payload=_five_case_payload(
                utterance=rsi_utterance,
                entry=[_indicator_payload("technical.rsi", "crosses_above", rsi_params, value=30)],
                exit=[_indicator_payload("technical.rsi", "crosses_above", rsi_params, value=70)],
                entry_texts=["RSI 从30以下重新回到30上方就买"],
                exit_texts=["到70上方就收手"],
                backtest_text="回测五年",
                defaulted_fields=[
                    "/entry/0/params/period",
                    "/exit/0/params/period",
                ],
            ),
            expected_entry=(
                "indicator",
                "technical.rsi",
                "crosses_above",
                (("period", 14),),
                30.0,
            ),
            expected_exit=(
                (
                    "indicator",
                    "technical.rsi",
                    "crosses_above",
                    (("period", 14),),
                    70.0,
                ),
            ),
        ),
        _ExactFiveCase(
            utterance=volume_utterance,
            local_diagnostic="ambiguous_boolean_expression",
            payload=_five_case_payload(
                utterance=volume_utterance,
                entry=[
                    _indicator_payload(
                        "price.rolling_high",
                        "new_high",
                        {"period": 20, "price_field": "close"},
                    ),
                    _indicator_payload(
                        "volume.relative",
                        "gte_multiple",
                        {"baseline_period": 20, "consecutive_days": 3},
                        value=1.5,
                    ),
                ],
                exit=[_indicator_payload("technical.macd", "death_cross", macd_params)],
                entry_texts=[
                    "东方财富收盘创20日新高、成交量是过去20日平均的1.5倍才买",
                    "东方财富收盘创20日新高、成交量是过去20日平均的1.5倍才买",
                ],
                exit_texts=["MACD 转弱卖"],
                backtest_text="回测五年",
                defaulted_fields=[
                    "/entry/1/params/consecutive_days",
                    "/exit/0/params/fast",
                    "/exit/0/params/signal",
                    "/exit/0/params/slow",
                ],
            ),
            expected_entry=(
                "all",
                (
                    (
                        "indicator",
                        "price.rolling_high",
                        "new_high",
                        (("period", 20), ("price_field", "close")),
                        None,
                    ),
                    (
                        "indicator",
                        "volume.relative",
                        "gte_multiple",
                        (("baseline_period", 20), ("consecutive_days", 3)),
                        1.5,
                    ),
                ),
            ),
            expected_exit=(
                (
                    "indicator",
                    "technical.macd",
                    "death_cross",
                    (("fast", 12), ("signal", 9), ("slow", 26)),
                    None,
                ),
            ),
        ),
        _ExactFiveCase(
            utterance=event_utterance,
            local_diagnostic="exit_rule_not_recognized",
            payload=_five_case_payload(
                utterance=event_utterance,
                entry=[
                    {
                        "kind": "event",
                        "event_code": "event.financial_results.annual_report",
                        "definition_version": "1.0.0",
                        "trigger": "published",
                        "attributes": {},
                    }
                ],
                exit=[{"kind": "holding_period", "sessions": 3}],
                entry_texts=["东方财富的年报公开以后就买"],
                exit_texts=["持有三个交易日后卖"],
                backtest_text="近五年",
            ),
            expected_entry=(
                "event",
                "event.financial_results.annual_report",
                "published",
            ),
            expected_exit=(("holding_period", 3),),
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _exact_five_cases())
async def test_exact_original_five_route_through_bounded_provider(
    case: _ExactFiveCase,
) -> None:
    snapshot_end = date(2026, 8, 20)
    input_request = CompileInput(
        utterance=case.utterance,
        instrument_context="300059.SZ",
        as_of_date=date(2026, 8, 30),
    )
    local = await RuleBasedCandidateGenerator().generate(
        CompileInput(
            utterance=case.utterance,
            instrument_context="300059.SZ",
            as_of_date=snapshot_end,
        )
    )
    assert local[0].unsupported_code == case.local_diagnostic

    transport = _FakeTransport(case.payload)
    bounded = VibeBoundedCandidateGenerator(
        transport,
        capability_matrix=CAPABILITY_MATRIX,
        provider_identity=CandidateProviderIdentityView(
            provider="fixture-provider",
            model="fixture-model",
            prompt_version="fixture-prompt.v1",
            schema_version="fixture-schema.v1",
        ),
    )
    outcome = await StrategyCompiler(
        generator=HybridCandidateGenerator(
            deterministic=RuleBasedCandidateGenerator(),
            bounded_fallback=bounded,
        ),
        catalog=CATALOG,
        catalog_id="cn_a.signals",
        release_version=CATALOG_RELEASE,
        trusted_date_provider=lambda: date(2026, 8, 30),
        backtest_anchor_date=snapshot_end,
    ).compile(input_request)

    assert len(transport.requests) == 1
    assert transport.requests[0].as_of_date == snapshot_end
    assert outcome.status is CompileStatus.READY
    assert outcome.candidate_provenance is not None
    assert outcome.candidate_provenance.source == "bounded_provider"
    assert outcome.strategy is not None
    assert outcome.strategy.backtest.start == date(2021, 8, 20)
    assert outcome.strategy.backtest.end == snapshot_end
    assert _condition_signature(outcome.strategy.entry) == case.expected_entry
    assert tuple(_condition_signature(item) for item in outcome.strategy.exit.children) == (
        case.expected_exit
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "deviation",
    (
        "catalog_alias_instead_of_id",
        "chinese_alias_instead_of_trigger",
        "missing_exact_span",
        "paraphrased_span",
        "extra_reasoning_field",
        "numeric_parameter_as_string",
        "unclaimed_catalog_defaults",
        "noncanonical_join",
    ),
)
async def test_common_json_object_semantic_deviations_fail_closed(
    deviation: str,
) -> None:
    payload = deepcopy(_macd_batch())
    candidates = cast(list[object], payload["candidates"])
    candidate = cast(dict[str, object], candidates[0])
    entry = cast(list[object], candidate["entry"])
    entry_leaf = cast(dict[str, object], entry[0])
    if deviation == "catalog_alias_instead_of_id":
        entry_leaf["indicator_id"] = "MACD"
    elif deviation == "chinese_alias_instead_of_trigger":
        entry_leaf["trigger"] = "金叉"
    elif deviation == "missing_exact_span":
        candidate.pop("entry_spans")
    elif deviation == "paraphrased_span":
        entry_spans = cast(list[object], candidate["entry_spans"])
        entry_span = cast(dict[str, object], entry_spans[0])
        entry_span["text"] = "MACD向上交叉买入"
    elif deviation == "extra_reasoning_field":
        candidate["reasoning"] = "模型的解释不属于受限契约"
    elif deviation == "numeric_parameter_as_string":
        params = cast(dict[str, object], entry_leaf["params"])
        params["fast"] = "12"
    elif deviation == "unclaimed_catalog_defaults":
        candidate["defaulted_fields"] = []
    elif deviation == "noncanonical_join":
        candidate["entry_join"] = "AND"
    else:  # pragma: no cover - the parameter table is closed above
        raise AssertionError(f"unknown deviation: {deviation}")

    generated = await _bounded(_FakeTransport(payload)).generate(
        CompileInput(
            utterance=DIRECT_UTTERANCE,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 20),
        )
    )

    assert generated[0].unsupported_code == "candidate_provider_invalid_output"


@pytest.mark.asyncio
async def test_json_object_repairs_only_exact_unique_offsets_and_explicit_default_claims() -> None:
    case = _exact_five_cases()[0]
    payload = deepcopy(case.payload)
    candidates = cast(list[object], payload["candidates"])
    candidate = cast(dict[str, object], candidates[0])
    for key in ("entry_spans", "exit_spans"):
        spans = cast(list[object], candidate[key])
        for raw_span in spans:
            span = cast(dict[str, object], raw_span)
            span["start"] = 0
            span["end"] = len(cast(str, span["text"]))
    candidate["instrument_symbol"] = "300059.SZ"
    candidate["instrument_span"] = {
        "start": 9,
        "end": 13,
        "text": "东方财富",
    }
    backtest_span = cast(dict[str, object], candidate["backtest_span"])
    backtest_span.update(start=0, end=4)
    defaulted_fields = cast(list[object], candidate["defaulted_fields"])
    defaulted_fields.extend(
        (
            "/entry/0/params/period",
            "/exit/0/params/period",
        )
    )

    generated = await _bounded(_FakeTransport(payload)).generate(
        CompileInput(
            utterance=case.utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 20),
        )
    )

    assert generated[0].unsupported_code is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case_index", "deviation"),
    (
        (0, "pronoun_changes_indicator_parameters"),
        (3, "drops_one_and_condition"),
        (4, "adds_natural_day_unit"),
    ),
)
async def test_exact_five_provider_shortcuts_fail_closed(
    case_index: int,
    deviation: str,
) -> None:
    case = _exact_five_cases()[case_index]
    payload = deepcopy(case.payload)
    candidates = cast(list[object], payload["candidates"])
    candidate = cast(dict[str, object], candidates[0])
    if deviation == "pronoun_changes_indicator_parameters":
        exit_leaves = cast(list[object], candidate["exit"])
        exit_leaf = cast(dict[str, object], exit_leaves[0])
        params = cast(dict[str, object], exit_leaf["params"])
        params["period"] = 10
    elif deviation == "drops_one_and_condition":
        entry_leaves = cast(list[object], candidate["entry"])
        entry_spans = cast(list[object], candidate["entry_spans"])
        entry_leaves.pop()
        entry_spans.pop()
        defaulted_fields = cast(list[object], candidate["defaulted_fields"])
        candidate["defaulted_fields"] = [
            item for item in defaulted_fields if not str(item).startswith("/entry/1/")
        ]
    elif deviation == "adds_natural_day_unit":
        exit_leaves = cast(list[object], candidate["exit"])
        exit_leaf = cast(dict[str, object], exit_leaves[0])
        exit_leaf["unit"] = "natural_days"
    else:  # pragma: no cover - the parameter table is closed above
        raise AssertionError(f"unknown deviation: {deviation}")

    generated = await _bounded(_FakeTransport(payload)).generate(
        CompileInput(
            utterance=case.utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 20),
        )
    )

    assert generated[0].unsupported_code == "candidate_provider_invalid_output"


@pytest.mark.asyncio
async def test_rsi_recovery_cannot_be_downgraded_to_static_above() -> None:
    case = _exact_five_cases()[2]
    payload = deepcopy(case.payload)
    candidates = cast(list[object], payload["candidates"])
    candidate = cast(dict[str, object], candidates[0])
    exit_leaves = cast(list[object], candidate["exit"])
    exit_leaf = cast(dict[str, object], exit_leaves[0])
    exit_leaf["trigger"] = "above"

    generated = await _bounded(_FakeTransport(payload)).generate(
        CompileInput(
            utterance=case.utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 20),
        )
    )

    assert generated[0].unsupported_code == "candidate_provider_invalid_output"


@pytest.mark.asyncio
async def test_context_cannot_silently_discard_unused_instrument_evidence() -> None:
    utterance = "600519.SH MACD金叉买入，MACD死叉卖出"
    payload = _macd_batch(utterance=utterance)
    candidates = cast(list[object], payload["candidates"])
    candidate = cast(dict[str, object], candidates[0])
    candidate["instrument_symbol"] = None
    candidate["instrument_span"] = _source_span(utterance, "600519.SH")

    generated = await _bounded(_FakeTransport(payload)).generate(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 20),
        )
    )

    assert generated[0].unsupported_code == "candidate_provider_invalid_output"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("utterance", "entry", "exit", "entry_text", "exit_text", "defaults"),
    (
        (
            "MACD金叉就买，跌回这条线下就走",
            _indicator_payload(
                "technical.macd",
                "golden_cross",
                {"fast": 12, "slow": 26, "signal": 9},
            ),
            _indicator_payload(
                "technical.ma",
                "price_crosses_below",
                {"period": 20, "price_field": "close"},
            ),
            "MACD金叉就买",
            "跌回这条线下就走",
            [
                "/entry/0/params/fast",
                "/entry/0/params/signal",
                "/entry/0/params/slow",
                "/exit/0/params/period",
                "/exit/0/params/price_field",
            ],
        ),
        (
            "RSI低于30就买，往下穿回去就卖",
            _indicator_payload(
                "technical.rsi",
                "below",
                {"period": 14},
                value=30,
            ),
            _indicator_payload(
                "technical.macd",
                "death_cross",
                {"fast": 12, "slow": 26, "signal": 9},
            ),
            "RSI低于30就买",
            "往下穿回去就卖",
            [
                "/entry/0/params/period",
                "/exit/0/params/fast",
                "/exit/0/params/signal",
                "/exit/0/params/slow",
            ],
        ),
        (
            "MACD金叉就买，到70上方就收手",
            _indicator_payload(
                "technical.macd",
                "golden_cross",
                {"fast": 12, "slow": 26, "signal": 9},
            ),
            _indicator_payload(
                "technical.rsi",
                "crosses_above",
                {"period": 14},
                value=70,
            ),
            "MACD金叉就买",
            "到70上方就收手",
            [
                "/entry/0/params/fast",
                "/entry/0/params/signal",
                "/entry/0/params/slow",
                "/exit/0/params/period",
            ],
        ),
    ),
)
async def test_contextual_reference_cannot_name_a_different_indicator(
    utterance: str,
    entry: dict[str, object],
    exit: dict[str, object],
    entry_text: str,
    exit_text: str,
    defaults: list[str],
) -> None:
    response: dict[str, object] = {
        "candidates": [
            {
                "instrument_symbol": None,
                "entry": [entry],
                "exit": [exit],
                "entry_spans": [_source_span(utterance, entry_text)],
                "exit_spans": [_source_span(utterance, exit_text)],
                "confidence": 0.91,
                "defaulted_fields": defaults,
            }
        ]
    }
    candidates = await _bounded(_FakeTransport(response)).generate(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 20),
        )
    )

    assert candidates[0].unsupported_code == "candidate_provider_invalid_output"


@pytest.mark.asyncio
async def test_bounded_grounding_accepts_safe_complete_buy_sell_phrases() -> None:
    utterance = "MACD金叉才买，MACD死叉转弱 卖"
    generator = _bounded(_FakeTransport(_macd_batch(utterance=utterance)))

    candidates = await generator.generate(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert candidates[0].unsupported_code is None


@pytest.mark.asyncio
async def test_generic_entry_placeholder_does_not_use_bounded_fallback() -> None:
    utterance = "随便上车，MACD死叉卖出"
    transport = _FakeTransport(_macd_batch())
    generator = HybridCandidateGenerator(
        deterministic=RuleBasedCandidateGenerator(),
        bounded_fallback=_bounded(transport),
    )

    outcome = await _compiler(generator).compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "entry_rule_not_recognized"
    assert transport.requests == []


@pytest.mark.asyncio
async def test_generic_unrecognized_placeholder_does_not_use_bounded_fallback() -> None:
    transport = _FakeTransport(_macd_batch())
    generator = HybridCandidateGenerator(
        deterministic=RuleBasedCandidateGenerator(),
        bounded_fallback=_bounded(transport),
    )

    outcome = await _compiler(generator).compile(
        CompileInput(
            utterance="你看着随便帮我交易",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.UNSUPPORTED
    assert outcome.diagnostic_code == "no_supported_signal_recognized"
    assert transport.requests == []


@pytest.mark.asyncio
async def test_explicitly_unsupported_semantics_never_use_bounded_fallback() -> None:
    utterance = "业绩预告净利润增长超过30%买入，MACD死叉卖出"
    transport = _FakeTransport(_macd_batch())
    generator = HybridCandidateGenerator(
        deterministic=RuleBasedCandidateGenerator(),
        bounded_fallback=_bounded(transport),
    )

    outcome = await _compiler(generator).compile(
        CompileInput(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.UNSUPPORTED
    assert outcome.diagnostic_code == "event_attribute_filter_not_supported"
    assert transport.requests == []


@pytest.mark.asyncio
async def test_candidates_are_ranked_and_low_confidence_requests_one_clarification() -> None:
    weak = _macd_batch()["candidates"][0]  # type: ignore[index]
    assert isinstance(weak, dict)
    weak["confidence"] = 0.41
    strong = _macd_batch()["candidates"][0]  # type: ignore[index]
    assert isinstance(strong, dict)
    strong["confidence"] = 0.93
    transport = _FakeTransport({"candidates": [weak, strong]})
    generator = _bounded(transport)

    ranked = await generator.generate(
        CompileInput(
            utterance=DIRECT_UTTERANCE,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert [item.confidence for item in ranked] == [0.93, 0.0]
    assert ranked[1].unsupported_code == "candidate_provider_low_confidence"

    low_transport = _FakeTransport({"candidates": [weak]})
    outcome = await StrategyCompiler(
        generator=_bounded(low_transport),
        catalog=CATALOG,
        catalog_id="cn_a.signals",
        release_version=CATALOG_RELEASE,
        trusted_date_provider=lambda: date(2026, 8, 30),
    ).compile(
        CompileInput(
            utterance=DIRECT_UTTERANCE,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert outcome.diagnostic_code == "candidate_provider_low_confidence"
    assert outcome.clarification is not None
    assert "严格按你提供的条件进行回测" in outcome.clarification
    assert "不擅自补充默认策略" in outcome.clarification


@pytest.mark.asyncio
async def test_catalog_invalid_first_candidate_does_not_hide_valid_second_candidate() -> None:
    invalid = _macd_batch()["candidates"][0]  # type: ignore[index]
    valid = _macd_batch()["candidates"][0]  # type: ignore[index]
    assert isinstance(invalid, dict)
    assert isinstance(valid, dict)
    invalid["confidence"] = 0.99
    invalid_entry = invalid["entry"]  # type: ignore[index]
    assert isinstance(invalid_entry, list)
    assert isinstance(invalid_entry[0], dict)
    invalid_entry[0]["trigger"] = "invented_trigger"
    valid["confidence"] = 0.91
    transport = _FakeTransport({"candidates": [invalid, valid]})
    generator = _bounded(transport)

    outcome = await StrategyCompiler(
        generator=generator,
        catalog=CATALOG,
        catalog_id="cn_a.signals",
        release_version=CATALOG_RELEASE,
        trusted_date_provider=lambda: date(2026, 8, 30),
    ).compile(
        CompileInput(
            utterance=DIRECT_UTTERANCE,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert outcome.status is CompileStatus.READY
    assert outcome.strategy is not None
    assert outcome.candidate_rejections[0].candidate_rank == 1
    assert outcome.candidate_rejections[0].diagnostic_code == ("candidate_provider_invalid_output")
