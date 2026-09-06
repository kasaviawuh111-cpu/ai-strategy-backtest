from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import cast

import pytest

from ashare_lab.adapters.language.vibe_backtest_review import (
    VibeBacktestReviewAdvisor,
    _narrative_fact_errors,
    _ProviderNarrative,
)
from ashare_lab.adapters.language.vibe_candidates import (
    CandidateProviderIdentityView,
    CandidateTransportRequest,
    CandidateTransportResponse,
    build_candidate_capability_matrix,
)
from ashare_lab.domain.catalog import load_catalog_directory, load_coverage_catalog_directory
from ashare_lab.domain.strategy import StrategySpec
from ashare_lab.ports.backtest_review import BacktestReviewRequest

ROOT = Path(__file__).parents[4]


@pytest.mark.parametrize("analysis", [
    "策略最大回撤22.31%，需要检验退出条件。",
    "本次回测胜率33.33%，不能据此解释亏损原因。",
])
def test_narrative_does_not_treat_other_metrics_as_total_return(analysis: str) -> None:
    assert not _narrative_fact_errors(
        _ProviderNarrative(analysis=analysis, conclusion="先比较调整后的实际回测结果。"),
        {"summary": {"totalReturn": -0.13, "maxDrawdown": -0.2231, "winRate": 1 / 3}},
    )


class _Transport:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.requests: list[CandidateTransportRequest] = []

    async def generate_json(
        self,
        request: CandidateTransportRequest,
    ) -> CandidateTransportResponse:
        self.requests.append(request)
        return cast(
            CandidateTransportResponse,
            self.responses[min(len(self.requests) - 1, len(self.responses) - 1)],
        )


def _baseline_strategy() -> StrategySpec:
    payload = StrategySpec.model_validate_json(
        (ROOT / "contracts/examples/strategy.macd-volume.daily.v1.json").read_text(encoding="utf-8")
    ).model_dump(mode="json")
    payload["backtest"]["start"] = "2025-08-27"
    return StrategySpec.model_validate(payload)


def _strategy_variant(*, fast: int, slow: int, signal: int) -> StrategySpec:
    payload = _baseline_strategy().model_dump(mode="json")
    payload["entry"]["children"][0]["params"] = {
        "fast": fast,
        "slow": slow,
        "signal": signal,
    }
    return StrategySpec.model_validate(payload)


def _payload() -> dict[str, object]:
    return {
        "analysis": "策略自身仍亏损，这版没有达到盈利目标，需要继续比较调整后的结果。",
        "conclusion": "先分别检验趋势确认和退出速度，不把样本内较优参数当作结论。",
        "proposals": [
            {
                "title": "缩短趋势确认周期",
                "diagnosis": "20 日均线可能对这段快速变化反应较慢。",
                "change_dimension": "entry",
                "expected_effect": "更早识别趋势切换，但只是假设，不能保证收益。",
                "tradeoff": "触发次数可能增加，成本和假信号也可能上升。",
                "suggested_utterance": (
                    "300059.SZ的MACD参数8、21、5金叉且成交量达20日均量2倍买入，"
                    "MACD参数12、26、9死叉卖出，回测2025-08-27至2026-08-27，"
                    "本金1000000元"
                ),
                "strategy": _strategy_variant(fast=8, slow=21, signal=5).model_dump(mode="json"),
            },
            {
                "title": "放慢趋势确认周期",
                "diagnosis": "原入场参数可能在震荡区间触发过多。",
                "change_dimension": "entry",
                "expected_effect": "检验更慢参数是否减少假突破。",
                "tradeoff": "确认更慢可能错过趋势早期涨幅。",
                "suggested_utterance": (
                    "300059.SZ的MACD参数10、30、8金叉且成交量达20日均量2倍买入，"
                    "MACD参数12、26、9死叉卖出，回测2025-08-27至2026-08-27，"
                    "本金1000000元"
                ),
                "strategy": _strategy_variant(fast=10, slow=30, signal=8).model_dump(mode="json"),
            },
        ],
    }


def _request() -> BacktestReviewRequest:
    strategy = _baseline_strategy()
    return BacktestReviewRequest(
        run_id="run:verified",
        instrument_symbol="300059.SZ",
        as_of_date=date(2026, 9, 4),
        strategy_payload=cast(
            dict[str, object],
            strategy.model_dump(mode="json"),
        ),
        result_facts={"summary": {"tradeCount": 14, "totalReturn": -0.17}},
        evidence_grade="limited",
        evidence_reasons=("完整交易仅 14 次",),
    )


@pytest.mark.asyncio
async def test_verified_result_and_evidence_gate_are_sent_to_deep_model() -> None:
    transport = _Transport([_payload()])
    matrix = build_candidate_capability_matrix(
        load_catalog_directory(ROOT / "catalogs"),
        load_coverage_catalog_directory(ROOT / "catalogs" / "coverage"),
    )
    advisor = VibeBacktestReviewAdvisor(
        transport,
        capability_matrix=matrix,
        provider_identity=CandidateProviderIdentityView(
            provider="deepseek",
            model="deepseek-v4-pro",
            prompt_version="plan.prompt.v1",
            schema_version="plan.schema.v1",
        ),
    )

    result = await advisor.review(_request())

    assert result is not None
    assert result.provider == "deepseek"
    assert result.model == "deepseek-v4-pro"
    assert result.response_hash.startswith("sha256:")
    assert [item.change_dimension for item in result.proposals] == ["entry", "entry"]
    assert result.proposals[0].strategy == _strategy_variant(fast=8, slow=21, signal=5)
    assert result.proposals[1].strategy == _strategy_variant(fast=10, slow=30, signal=8)
    request = transport.requests[0]
    assert request.user_payload is not None
    assert request.user_payload["evidenceGrade"] == "limited"
    assert request.user_payload["verifiedResultFacts"] == {
        "summary": {"tradeCount": 14, "totalReturn": -0.17}
    }
    assert request.response_schema["additionalProperties"] is False
    proposal_schema = request.response_schema["$defs"]["_ProviderProposal"]
    assert "strategy" in proposal_schema["required"]
    assert "不得把样本内表现称为有效" in request.system_contract
    assert "不再把你的文字交给第二个模型" in request.system_contract
    assert "analysis禁止解释亏损原因或行情路径" in request.system_contract


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("total_return", "benchmark_return", "comparison_status", "expected_excess"),
    [
        (-0.1049, -0.2694, "comparable", "+16.45个百分点"),
        (-0.1049, 0.2694, "comparable", "-37.43个百分点"),
        (-0.1049, None, "benchmark_unavailable", None),
        (0.0, -0.2694, "strategy_entry_not_filled", None),
    ],
)
async def test_return_comparison_distinguishes_benchmark_percent_from_excess_percentage_points(
    total_return: float, benchmark_return: float | None,
    comparison_status: str, expected_excess: str | None,
) -> None:
    transport = _Transport([_payload()])
    advisor = VibeBacktestReviewAdvisor(
        transport,
        capability_matrix=build_candidate_capability_matrix(
            load_catalog_directory(ROOT / "catalogs"),
            load_coverage_catalog_directory(ROOT / "catalogs" / "coverage"),
        ),
        provider_identity=CandidateProviderIdentityView(
            provider="test", model="fixture", prompt_version="test", schema_version="test",
        ),
    )
    facts = {
        "summary": {"totalReturn": total_return, "benchmarkReturn": benchmark_return,
                    "benchmarkComparisonStatus": comparison_status},
        "benchmarkDefinition": {"type": "same_instrument_buy_and_hold"},
    }
    result = await advisor.review(replace(_request(), result_facts=facts))
    assert result is not None
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.user_payload is not None
    assert request.user_payload["verifiedResultFacts"] == facts
    assert request.user_payload["returnComparison"] == {
        "comparisonStatus": comparison_status,
        "strategyReturnPercent": f"{total_return * 100:+.2f}%",
        "benchmarkReturnPercent": (f"{benchmark_return * 100:+.2f}%"
                                   if benchmark_return is not None else None),
        "excessReturnPercentagePoints": expected_excess,
    }
    assert "基准收益不等于超额收益" in request.system_contract
    assert "不能把基准收益的绝对值当成跑赢幅度" in request.system_contract
    assert "comparisonStatus=comparable" in request.system_contract


@pytest.mark.asyncio
async def test_incomplete_or_guaranteed_model_review_fails_closed_without_retry() -> None:
    invalid = _payload()
    proposals = cast(list[dict[str, object]], invalid["proposals"])
    proposals[0]["expected_effect"] = "保证盈利"
    proposals[1]["suggested_utterance"] = "股价上穿20日均线买入"
    transport = _Transport([invalid])
    matrix = build_candidate_capability_matrix(
        load_catalog_directory(ROOT / "catalogs"),
        load_coverage_catalog_directory(ROOT / "catalogs" / "coverage"),
    )
    advisor = VibeBacktestReviewAdvisor(
        transport,
        capability_matrix=matrix,
        provider_identity=CandidateProviderIdentityView(
            provider="deepseek",
            model="deepseek-v4-pro",
            prompt_version="plan.prompt.v1",
            schema_version="plan.schema.v1",
        ),
    )

    assert await advisor.review(_request()) is None
    assert len(transport.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_analysis", [
    "策略亏13.07%，仍跑赢同股持有138.87个百分点；仅9次交易。",
    "策略亏13.07%，跑输同股持有13.87个百分点；仅9次交易。",
    "策略亏13.07%，跑赢同股持有13.87%；仅9次交易。",
    "策略亏23.07%，仍跑赢同股持有13.87个百分点；仅9次交易。",
])
async def test_wrong_report_numbers_are_rewritten_by_model_once_without_changing_proposals(
    bad_analysis: str,
) -> None:
    payload = {**_payload(), "analysis": bad_analysis}
    corrected = {"analysis": "这版亏损13.07%，没有达到盈利目标；同股持有亏损26.94%。",
                 "conclusion": "先比较下面三种调整的回测结果，不能把跑赢基准当成盈利。"}
    transport = _Transport([payload, corrected])
    advisor = VibeBacktestReviewAdvisor(
        transport,
        capability_matrix=build_candidate_capability_matrix(
            load_catalog_directory(ROOT / "catalogs"),
            load_coverage_catalog_directory(ROOT / "catalogs" / "coverage"),
        ),
        provider_identity=CandidateProviderIdentityView(
            provider="test", model="fixture", prompt_version="test", schema_version="test",
        ),
    )
    request = replace(_request(), result_facts={"summary": {
        "totalReturn": -0.130675990386398, "benchmarkReturn": -0.2693720547218151,
        "benchmarkComparisonStatus": "comparable", "tradeCount": 9,
    }}, user_request="改下，我要一个盈利的策略")
    result = await advisor.review(request)
    assert result is not None
    assert result.analysis == corrected["analysis"]
    assert result.proposals[0].strategy == _strategy_variant(fast=8, slow=21, signal=5)
    assert len(transport.requests) == 2
    assert transport.requests[0].user_payload is not None
    assert transport.requests[0].user_payload["userRequest"] == request.user_request
    assert set(transport.requests[1].response_schema["properties"]) == {"analysis", "conclusion"}


@pytest.mark.asyncio
async def test_repeated_numeric_error_is_not_shown_and_does_not_loop() -> None:
    payload = {**_payload(), "analysis": "策略跑赢基准138.87个百分点，但没有盈利。"}
    transport = _Transport([payload, {key: payload[key] for key in ("analysis", "conclusion")}])
    advisor = VibeBacktestReviewAdvisor(
        transport,
        capability_matrix=build_candidate_capability_matrix(
            load_catalog_directory(ROOT / "catalogs"),
            load_coverage_catalog_directory(ROOT / "catalogs" / "coverage"),
        ),
        provider_identity=CandidateProviderIdentityView(
            provider="test", model="fixture", prompt_version="test", schema_version="test",
        ),
    )
    request = replace(_request(), result_facts={"summary": {
        "totalReturn": -0.130675990386398, "benchmarkReturn": -0.2693720547218151,
        "benchmarkComparisonStatus": "comparable",
    }})
    assert await advisor.review(request) is None
    assert len(transport.requests) == 2
