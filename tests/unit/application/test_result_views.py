from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from ashare_lab.api.result_schemas import BacktestResultBundle
from ashare_lab.application.result_views import (
    RESULT_HASH_SCHEMA_VERSION,
    build_result_bundle,
    calculate_result_bundle_hash,
)
from ashare_lab.domain.signals import SignalEvidence

from .test_daily_backtest import (
    START,
    TZ,
    bars,
    event_strategy,
    holding_strategy,
    run,
    sessions,
)

MANIFEST = {
    "strategy_hash": "sha256:" + "a" * 64,
    "catalog_hash": "sha256:" + "b" * 64,
    "data_snapshot": {
        "snapshot_id": "snapshot:test",
        "checksum": "sha256:" + "c" * 64,
        "schema_version": "market-data.v1",
        "producer_schema_version": "producer-snapshot.v2",
        "producer_snapshot_id": "composite:" + "d" * 64,
    },
    "code_revision": "0123456789abcdef0123456789abcdef01234567",
    "engine_version": "2.0.0a0",
    "assumptions": {
        "benchmark_policy": "funded_buy_and_hold.same_execution_ledger.v1",
        "capacity_mode": "point_in_time_volume",
        "corporate_action_policy": (
            "cn.a_share.timeline_entitlement_receivable_settlement."
            "date_only_cash_after_close_conservative.v2"
        ),
        "dividend_tax_policy": "gross_research_no_withholding.v1",
        "opening_auction_policy": (
            "cn.a_share.daily.published_open_proxy.latency_1s.cutoff_0915."
            "recorded_0930.not_exact.day_order.v2"
        ),
        "participation_rate": "0.05",
        "rights_issue_policy": "fail_closed_without_explicit_participation.v1",
    },
}


def test_result_bundle_is_json_safe_and_normalized() -> None:
    result = run()

    bundle = build_result_bundle(
        result,
        run_id="run:test",
        period_start=START,
        period_end=START + timedelta(days=5),
        manifest=MANIFEST,
    )

    assert bundle["summary"]["runId"] == "run:test"
    assert bundle["summary"]["initialCashCny"] == 100_000.0
    assert bundle["summary"]["finalEquityCny"] > 0
    assert bundle["summary"]["benchmarkReturn"] is None
    assert bundle["summary"]["benchmarkComparisonStatus"] == "benchmark_unavailable"
    assert bundle["series"][0]["equity"] == 100.0
    assert bundle["series"][0]["benchmark"] is None
    assert bundle["audit"]["resultHash"] == calculate_result_bundle_hash(bundle)
    assert bundle["audit"]["engineResultHash"] == result.content_hash
    assert bundle["audit"]["hashSchemaVersion"] == RESULT_HASH_SCHEMA_VERSION
    assert bundle["summary"]["runEvidence"]["dataSnapshotId"] == "snapshot:test"
    assert bundle["summary"]["runEvidence"]["dataSchemaVersion"] == "market-data.v1"
    assert (
        bundle["summary"]["runEvidence"]["producerSnapshotSchemaVersion"] == "producer-snapshot.v2"
    )
    assert bundle["summary"]["runEvidence"]["producerSnapshotId"] == "composite:" + "d" * 64
    warnings = bundle["summary"]["warnings"]
    assert any("上一交易日" in warning and "当日收盘后" in warning for warning in warnings)
    assert any(
        "09:15" in warning and "09:30" in warning and "不代表" in warning for warning in warnings
    )
    assert any("公司行动" in warning for warning in warnings)
    assert any(
        "税前金额" in warning
        and "持股期限" in warning
        and "个人税后收益" in warning
        and "演示" not in warning
        for warning in warnings
    )
    assert any("配股" in warning and "直接拒绝" in warning for warning in warnings)
    assert any("同初始资金" in warning for warning in warnings)
    fill_activity = next(item for item in bundle["activities"] if item["kind"] == "fill")
    assert fill_activity["timeQuality"] == "daily_bar_open_proxy"
    assert "不代表" in fill_activity["timeSemantics"]
    BacktestResultBundle.model_validate(bundle)


def test_result_bundle_with_no_strategy_entry_does_not_claim_excess_return() -> None:
    source = bars()
    funded = tuple(
        (bar.session_date, Decimal("100000") + Decimal(index * 1000))
        for index, bar in enumerate(source)
    )
    result = run(
        source_bars=source,
        source_sessions=sessions(source),
        spec=event_strategy(),
        benchmark_equity=funded,
        benchmark_initial_equity=Decimal("100000"),
        benchmark_entry_filled=True,
    )

    bundle = build_result_bundle(
        result,
        run_id="run:no-entry",
        period_start=START,
        period_end=START + timedelta(days=5),
        manifest=MANIFEST,
    )

    summary = bundle["summary"]
    assert result.fills == ()
    assert summary["totalReturn"] == 0.0
    assert summary["benchmarkReturn"] > 0
    assert summary["benchmarkComparisonStatus"] == "strategy_entry_not_filled"
    assert summary["interpretation"] == "策略没有产生已成交买入，无法比较超额收益。"
    assert any("仅作参考，不计算超额收益" in item for item in summary["warnings"])
    BacktestResultBundle.model_validate(bundle)


def test_open_strategy_position_can_still_be_compared_to_funded_benchmark() -> None:
    source = bars(closes=("10", "9", "11", "12", "12", "12"))
    funded = tuple(
        (bar.session_date, Decimal("100000") + Decimal(index * 500))
        for index, bar in enumerate(source)
    )
    result = run(
        source_bars=source,
        source_sessions=sessions(source),
        spec=holding_strategy(holding_sessions=20, end=source[-1].session_date),
        benchmark_equity=funded,
        benchmark_initial_equity=Decimal("100000"),
        benchmark_entry_filled=True,
    )

    bundle = build_result_bundle(
        result,
        run_id="run:open-position",
        period_start=START,
        period_end=source[-1].session_date,
        manifest=MANIFEST,
    )

    assert result.metrics.trade_count == 0
    assert any(fill.side.value == "buy" for fill in result.fills)
    assert bundle["summary"]["benchmarkComparisonStatus"] == "comparable"


def test_unfilled_benchmark_is_not_presented_as_buy_and_hold() -> None:
    source = bars()
    funded = tuple((bar.session_date, Decimal("100000")) for bar in source)
    result = run(
        source_bars=source,
        source_sessions=sessions(source),
        benchmark_equity=funded,
        benchmark_initial_equity=Decimal("100000"),
        benchmark_entry_filled=False,
    )

    bundle = build_result_bundle(
        result,
        run_id="run:benchmark-no-entry",
        period_start=START,
        period_end=START + timedelta(days=5),
        manifest=MANIFEST,
    )

    summary = bundle["summary"]
    assert summary["benchmarkComparisonStatus"] == "benchmark_entry_not_filled"
    assert summary["interpretation"] == "买入持有基准未按同一成交规则成交，无法比较超额收益。"


def test_result_hash_covers_every_persisted_result_layer() -> None:
    result = run()
    robustness = {
        "profile": "execution.v1",
        "selectionPolicy": "predeclared_scenarios_no_optimization",
        "totalReturnRange": {"min": -0.1, "max": 0.1},
        "scenarios": [
            {
                "id": "base",
                "configHash": "sha256:" + "d" * 64,
                "participationRate": 0.05,
                "slippageBps": 5.0,
                "totalReturn": 0.1,
                "maxDrawdown": -0.1,
                "tradeCount": 1,
                "finalEquityCny": 110_000.0,
            }
        ],
    }
    bundle = build_result_bundle(
        result,
        run_id="run:hash-coverage",
        period_start=START,
        period_end=START + timedelta(days=5),
        manifest=MANIFEST,
        robustness=robustness,
    )
    baseline = bundle["audit"]["resultHash"]

    mutations = (
        ("summary", "totalReturn"),
        ("summary", "benchmarkComparisonStatus"),
        ("series", 0, "equity"),
        ("activities", 0, "reason"),
        ("robustness", "scenarios", 0, "tradeCount"),
        ("summary", "runEvidence", "engineVersion"),
        ("summary", "runEvidence", "producerSnapshotSchemaVersion"),
        ("summary", "runEvidence", "producerSnapshotId"),
        ("audit", "openPositionShares"),
    )
    for path in mutations:
        changed = deepcopy(bundle)
        target: Any = changed
        for key in path[:-1]:
            target = target[key]
        leaf = path[-1]
        target[leaf] = f"changed:{path}" if isinstance(target[leaf], str) else target[leaf] + 1
        assert calculate_result_bundle_hash(changed) != baseline, path


def test_result_hash_is_deterministic_across_mapping_order() -> None:
    bundle = build_result_bundle(
        run(),
        run_id="run:canonical-result",
        period_start=START,
        period_end=START + timedelta(days=5),
        manifest=MANIFEST,
    )
    reordered = dict(reversed(tuple(bundle.items())))
    reordered["audit"] = dict(reversed(tuple(bundle["audit"].items())))

    assert calculate_result_bundle_hash(reordered) == bundle["audit"]["resultHash"]


def test_result_bundle_keeps_signal_order_fill_and_unfilled_separate() -> None:
    result = run()

    bundle = build_result_bundle(
        result,
        run_id="run:test",
        period_start=START,
        period_end=START + timedelta(days=5),
        manifest=MANIFEST,
    )

    kinds = {item["kind"] for item in bundle["activities"]}
    assert {"signal", "order", "fill"} <= kinds
    assert all(item["reason"] for item in bundle["activities"])


def test_result_bundle_projects_signal_validity_and_retry_causality() -> None:
    from ashare_lab.application.daily_backtest import DailyBacktestConfig
    source = bars(closes=("10", "9", "11", "12", "12", "12", "12"))
    source_sessions = list(sessions(source))
    for offset in (3, 4):
        source_sessions[offset] = replace(
            source_sessions[offset],
            upper_limit=source[offset].open,
        )
    result = run(source_bars=source, source_sessions=tuple(source_sessions),
                 config=DailyBacktestConfig(edge_entry_validity_sessions=3))

    bundle = build_result_bundle(
        result,
        run_id="run:retry-causality",
        period_start=START,
        period_end=START + timedelta(days=6),
        manifest=MANIFEST,
    )
    entry = result.decisions[0]
    chain = [item for item in bundle["activities"] if item["chainId"] == entry.decision_id.value]
    signal = next(item for item in chain if item["kind"] == "signal")
    orders = [item for item in chain if item["kind"] == "order"]

    assert signal["signalSemantics"] == "edge"
    assert signal["signalValiditySessions"] == 3
    assert signal["originSignalId"] == signal["id"]
    assert [item["attemptNo"] for item in orders] == [1, 2, 3]
    assert [item["retryReason"] for item in orders] == [
        None,
        "one_price_limit_up",
        "one_price_limit_up",
    ]
    assert orders[-1]["outcomeReason"] == "matched_at_open"
    assert "09:15" in orders[0]["reason"]
    assert "09:30" in orders[0]["reason"]
    assert "不代表精确成交时点" in orders[0]["reason"]
    BacktestResultBundle.model_validate(bundle)


def test_signal_activity_uses_when_the_fact_was_available() -> None:
    source = list(bars())
    available_at = datetime.combine(
        START + timedelta(days=3),
        datetime.min.time().replace(hour=10),
        tzinfo=TZ,
    )
    source[2] = replace(source[2], available_at=available_at)
    actual_bars = tuple(source)
    result = run(source_bars=actual_bars, source_sessions=sessions(actual_bars))

    bundle = build_result_bundle(
        result,
        run_id="run:test",
        period_start=START,
        period_end=START + timedelta(days=5),
        manifest=MANIFEST,
    )
    signal = next(item for item in bundle["activities"] if item["kind"] == "signal")

    assert signal["occurredAt"] == available_at.isoformat()


def test_signal_activity_retains_second_level_source_evidence() -> None:
    result = run()
    first = result.decisions[0]
    evidence_time = datetime(2025, 1, 2, 20, 57, 33, tzinfo=TZ)
    signal = replace(
        first.signal,
        evidence=(
            SignalEvidence(
                evidence_type="event",
                evidence_id="event:test",
                source_event_id="AN202403141626765931",
                available_at=evidence_time,
                provider="eastmoney",
                source_url="https://example.test/announcement",
                time_quality="vendor_observed",
                timestamp_precision="second",
                validation_status="validated",
                raw_response_sha256="a" * 64,
            ),
        ),
    )
    enriched = replace(
        result,
        decisions=(replace(first, signal=signal), *result.decisions[1:]),
    )

    bundle = build_result_bundle(
        enriched,
        run_id="run:event-evidence",
        period_start=START,
        period_end=START + timedelta(days=5),
        manifest=MANIFEST,
    )
    item = next(activity for activity in bundle["activities"] if activity["kind"] == "signal")

    assert item["occurredAt"] == signal.available_at.isoformat()
    assert item["evidence"][0]["availableAt"] == "2025-01-02T20:57:33+08:00"
    assert item["evidence"][0]["provider"] == "eastmoney"
    assert item["evidence"][0]["timeQuality"] == "vendor_observed"
    assert item["evidence"][0]["timestampPrecision"] == "second"
    BacktestResultBundle.model_validate(bundle)
