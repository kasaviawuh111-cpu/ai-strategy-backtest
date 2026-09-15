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
    _return_comparison,
)
from ashare_lab.adapters.language.vibe_candidates import (
    CandidateProviderIdentityView,
    CandidateTransportError,
    CandidateTransportRequest,
    CandidateTransportResponse,
    build_candidate_capability_matrix,
)
from ashare_lab.domain.catalog import load_catalog_directory, load_coverage_catalog_directory
from ashare_lab.domain.strategy import StrategySpec
from ashare_lab.ports.backtest_review import BacktestReviewContentError, BacktestReviewRequest

ROOT = Path(__file__).parents[4]


@pytest.mark.parametrize(("ratio", "expected"), [
    (-0.00000844, "-0.00084%"), (0.00000844, "+0.00084%"),
    (-0.00000000001, "-0.000000001%"),
    (0.0, "+0.00%"), (-0.0, "+0.00%"), (-0.01234, "-1.23%"),
    (None, None), (float("nan"), None),
])
def test_review_percent_preserves_small_nonzero_returns(ratio, expected) -> None:
    comparison = _return_comparison({"summary": {
        "totalReturn": ratio, "benchmarkReturn": ratio,
        "benchmarkComparisonStatus": "comparable",
    }})
    assert comparison["strategyReturnPercent"] == expected
    assert comparison["benchmarkReturnPercent"] == expected
    assert comparison["excessReturnPercent"] == ("+0.00%" if expected else None)


def test_review_rejects_rounding_nonzero_return_to_zero() -> None:
    facts = {"summary": {"totalReturn": -0.00000844}}
    assert _narrative_fact_errors(_ProviderNarrative(
        analysis="策略收益-0.00%。", conclusion="需核对执行口径。"), facts)
    assert not _narrative_fact_errors(_ProviderNarrative(
        analysis="策略收益-0.00084%。", conclusion="需核对执行口径。"), facts)
    assert _narrative_fact_errors(_ProviderNarrative(
        analysis="实际收益约-0.00%。", conclusion="需核对执行口径。"), facts)


@pytest.mark.parametrize(("text", "valid"), [
    ("策略亏10.49%，跑赢同股持有22.52%。", True),
    ("策略亏10.49%，超额收益+22.52%。", True),
    ("策略亏10.49%，跑赢同股持有22.52个百分点。", False),
    ("策略亏10.49%，跑赢同股持有16.45%。", False),
    ("策略亏10.49%，跑输同股持有22.52%。", False),
    ("策略亏10.49%，超额收益-22.52%。", False),
])
def test_compounded_excess_checks_number_unit_and_direction(text, valid) -> None:
    errors = _narrative_fact_errors(_ProviderNarrative(
        analysis=text, conclusion="需核对执行口径。"), {"summary": {
            "totalReturn": -0.1049, "benchmarkReturn": -0.2694,
            "benchmarkComparisonStatus": "comparable",
        }})
    assert (not errors) == valid


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
@pytest.mark.parametrize("recover", [True, False])
async def test_price_plan_reply_network_check_retries_once_and_preserves_cause(recover) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    error = CandidateTransportError("temporary link failure", failure_kind="connection_failed")
    check = AsyncMock(side_effect=[error, {
        "facts": "supported", "state_and_authority": "supported",
        "user_intent_and_tone": "supported",
    } if recover else error])
    advisor = VibeBacktestReviewAdvisor(
        _Transport([{"analysis": "本次策略仍然亏损，需要核对执行口径。",
                     "conclusion": "修改参数后可再比较。", "proposals": []}]),
        review_transport=SimpleNamespace(generate_json=check), model_semantic_review=True,
        capability_matrix=build_candidate_capability_matrix(
            load_catalog_directory(ROOT / "catalogs"),
            load_coverage_catalog_directory(ROOT / "catalogs" / "coverage"),
        ), provider_identity=CandidateProviderIdentityView(
            provider="test", model="deep", prompt_version="test", schema_version="test",
        ),
    )
    original = _request()
    request = replace(original, strategy_payload={
        **original.strategy_payload, "entry": None, "exit": None, "trading_plan": {"kind": "grid"},
    })
    if recover:
        assert await advisor.review(request) is not None
    else:
        with pytest.raises(CandidateTransportError) as raised:
            await advisor.review(request)
        assert raised.value.failure_kind == "connection_failed"
    assert check.await_count == 2


@pytest.mark.asyncio
async def test_price_plan_review_answers_without_inventing_indicator_revisions() -> None:
    transport = _Transport([{
        "analysis": "本次策略收益为负，尚未实现正收益。",
        "conclusion": "可以调整网格参数后，再比较同一段历史结果。",
        "proposals": [],
    }])
    advisor = VibeBacktestReviewAdvisor(
        transport, capability_matrix=build_candidate_capability_matrix(
            load_catalog_directory(ROOT / "catalogs"),
            load_coverage_catalog_directory(ROOT / "catalogs" / "coverage"),
        ), provider_identity=CandidateProviderIdentityView(
            provider="test", model="deep", prompt_version="test", schema_version="test",
        ),
    )
    request = _request()
    result = await advisor.review(replace(request, strategy_payload={
        **request.strategy_payload, "entry": None, "exit": None,
        "trading_plan": {"kind": "grid"},
    }))
    assert result is not None and not result.proposals
    assert transport.requests[0].response_schema["properties"]["proposals"]["maxItems"] == 0
    assert "不得添加指标条件" in transport.requests[0].system_contract
    assert "不能把所有价格计划统称日线" in transport.requests[0].system_contract
    assert "策略配置表示请求口径，不能替代实际执行证据" in transport.requests[0].system_contract
    assert "按日线观察和开盘价代理，不能冒称分钟" not in transport.requests[0].system_contract


@pytest.mark.asyncio
@pytest.mark.parametrize("price_plan", [False, True])
@pytest.mark.parametrize("field", ["analysis", "conclusion"])
@pytest.mark.parametrize("repair_succeeds", [False, True])
async def test_long_narrative_gets_one_bounded_repair_without_changing_rules(
    price_plan: bool, field: str, repair_succeeds: bool,
) -> None:
    payload = _payload()
    if price_plan:
        payload["proposals"] = []
    corrected = {key: payload[key] for key in ("analysis", "conclusion")}
    payload[field] = "这只是待验证的说明，需要核对实际执行记录。" * 5
    if not repair_succeeds:
        corrected[field] = payload[field]
    transport = _Transport([payload, corrected])
    advisor = VibeBacktestReviewAdvisor(
        transport, capability_matrix=build_candidate_capability_matrix(
            load_catalog_directory(ROOT / "catalogs"),
            load_coverage_catalog_directory(ROOT / "catalogs" / "coverage"),
        ), provider_identity=CandidateProviderIdentityView(
            provider="test", model="fixture", prompt_version="test", schema_version="test",
        ),
    )
    request = _request()
    if price_plan:
        request = replace(request, strategy_payload={
            **request.strategy_payload, "entry": None, "exit": None,
            "trading_plan": {"kind": "scheduled"},
        })
    if repair_succeeds:
        result = await advisor.review(request)
        assert result is not None and getattr(result, field) == corrected[field]
        assert all(len(getattr(result, key)) <= 56 for key in corrected)
        assert [p.strategy.model_dump(mode="json") for p in result.proposals] == [
            p["strategy"] for p in payload["proposals"]
        ]
    else:
        with pytest.raises(BacktestReviewContentError):
            await advisor.review(request)
    assert len(transport.requests) == 2
    assert set(transport.requests[1].response_schema["properties"]) == {"analysis", "conclusion"}


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [True, False])
async def test_separate_review_transport_preserves_generation_and_gate(
    monkeypatch: pytest.MonkeyPatch, approved: bool,
) -> None:
    from unittest.mock import AsyncMock

    deep = _Transport([_payload()])
    fast = _Transport([])
    audit = AsyncMock(return_value=approved)
    monkeypatch.setattr(
        "ashare_lab.adapters.language.vibe_backtest_review.review_display_semantics", audit,
    )
    advisor = VibeBacktestReviewAdvisor(
        deep, review_transport=fast, model_semantic_review=True,
        capability_matrix=build_candidate_capability_matrix(
            load_catalog_directory(ROOT / "catalogs"),
            load_coverage_catalog_directory(ROOT / "catalogs" / "coverage"),
        ),
        provider_identity=CandidateProviderIdentityView(
            provider="test", model="deep", prompt_version="test", schema_version="test",
        ),
    )
    if approved:
        assert await advisor.review(_request()) is not None
    else:
        with pytest.raises(BacktestReviewContentError):
            await advisor.review(_request())
    assert len(deep.requests) == 1
    audit.assert_awaited_once()
    assert audit.call_args.args[0] is fast
    assert audit.call_args.kwargs["verified_context"]["candidateExecutionState"] == (
        "new_proposals_not_executed"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["connection_failed", "timeout", "unknown"])
async def test_review_transport_failures_are_not_reported_as_content_failures(
    monkeypatch: pytest.MonkeyPatch, failure_kind: str,
) -> None:
    from unittest.mock import AsyncMock

    from ashare_lab.adapters.language.vibe_candidates import CandidateFailureKind

    failure = CandidateTransportError(
        "sanitized transport failure", failure_kind=cast(CandidateFailureKind, failure_kind),
    )
    transport = _Transport([_payload()])
    monkeypatch.setattr(
        "ashare_lab.adapters.language.vibe_backtest_review.review_display_semantics",
        AsyncMock(side_effect=failure),
    )
    advisor = VibeBacktestReviewAdvisor(
        transport, model_semantic_review=True,
        capability_matrix=build_candidate_capability_matrix(
            load_catalog_directory(ROOT / "catalogs"),
            load_coverage_catalog_directory(ROOT / "catalogs" / "coverage"),
        ),
        provider_identity=CandidateProviderIdentityView(
            provider="test", model="fixture", prompt_version="test", schema_version="test",
        ),
    )
    if failure.is_classified:
        with pytest.raises(CandidateTransportError) as raised:
            await advisor.review(_request())
        assert raised.value is failure
    else:
        assert await advisor.review(_request()) is None
    assert len(transport.requests) == 1


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
        (-0.1049, -0.2694, "comparable", "+22.52%"),
        (-0.1049, 0.2694, "comparable", "-29.49%"),
        (-0.1049, None, "benchmark_unavailable", None),
        (0.0, -0.2694, "strategy_entry_not_filled", None),
    ],
)
async def test_return_comparison_uses_compounded_relative_excess_return(
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
        "excessReturnPercent": expected_excess,
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

    with pytest.raises(BacktestReviewContentError):
        await advisor.review(_request())
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
    with pytest.raises(BacktestReviewContentError):
        await advisor.review(request)
    assert len(transport.requests) == 2


@pytest.mark.asyncio
async def test_review_receives_executed_versions_and_unrun_proposals_and_checks_previous_numbers(
) -> None:
    previous = {"runId": "run:previous", "summary": {"totalReturn": -0.20,
                                                     "maxDrawdown": -0.15}}
    current = {"runId": "run:verified", "summary": {"totalReturn": -0.17,
                                                     "maxDrawdown": -0.10}}
    proposals = ({"sourceRunId": "run:previous", "status": "unrun",
                  "completedRunIds": [], "strategy": _baseline_strategy().model_dump(mode="json")},)
    references = {"current": current, "previousDifferentStrategy": previous,
                  "earliest": {"runId": "run:earliest"},
                  "strategyVersionComparison": {
                      "comparisonStatus": "limited", "sourceIdentityStatus": "missing",
                  }}
    payload = {**_payload(), "analysis": "上一版亏损20%，这版亏损17%，仍未达到盈利目标。",
               "conclusion": "上版最大回撤15%，本次最大回撤10%，继续检验其他方向。"}
    transport = _Transport([payload])
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
    request = replace(_request(), completed_runs=(previous, current), exposed_proposals=proposals,
                      report_references=references, result_facts=current)
    result = await advisor.review(request)
    assert result is not None
    assert len(transport.requests) == 1
    sent = transport.requests[0].user_payload
    assert sent is not None
    assert sent["completedRuns"] == [previous, current]
    assert sent["exposedOptimizationProposals"] == list(proposals)
    assert sent["reportReferences"] == references
    contract = transport.requests[0].system_contract
    assert "用户并非质疑亏损且有上一版时" in contract
    assert "数据版本尚未核齐" in contract
    assert "不能将差异完全归因于策略" in contract
    assert "不输出工程字段名" in contract
    assert "只改该参数不算新方案" in contract
    assert "第一句只评价实际盈亏" not in contract
    assert _narrative_fact_errors(
        _ProviderNarrative(analysis="上一版亏损30%，这版亏损17%。",
                           conclusion="继续检验其他方向。"),
        current, references,
    ) == ("previous_totalReturn_number_or_direction",)
    assert _narrative_fact_errors(
        _ProviderNarrative(analysis=payload["analysis"], conclusion="继续检验其他方向。"),
        current,
    ) == ("previous_totalReturn_number_or_direction",)
