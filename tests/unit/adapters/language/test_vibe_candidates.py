from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest

from ashare_lab.adapters.language.rule_based import RuleBasedCandidateGenerator
from ashare_lab.adapters.language.vibe_candidates import (
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
    assert "不会替你猜默认策略" in outcome.clarification


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
