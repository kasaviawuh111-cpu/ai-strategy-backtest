"""Stable, JSON-safe read models for completed daily backtests.

The execution engine returns rich immutable domain objects.  HTTP, workers and
the H5 client consume this deliberately smaller projection so presentation
changes never leak back into matching or accounting.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from math import isfinite
from typing import Any, cast

from ashare_lab.application.daily_backtest import DailyBacktestResult
from ashare_lab.domain.execution import MatchOutcome
from ashare_lab.domain.orders import OrderSide, OrderStatus
from ashare_lab.domain.runs import result_hash
from ashare_lab.domain.shared import OrderId
from ashare_lab.application.execution_feedback import zero_fill_execution_note

RESULT_HASH_SCHEMA_VERSION = "ashare-lab.backtest-result-hash.v1"


def build_result_bundle(
    result: DailyBacktestResult,
    *,
    run_id: str,
    period_start: date,
    period_end: date,
    manifest: Mapping[str, object],
    robustness: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """Project a domain result into the versioned web/API result shape."""

    if not result.equity_curve:
        raise ValueError("a completed backtest must contain an equity curve")

    curve = result.equity_curve
    initial_equity = curve[0].equity
    benchmark_start = next(
        (point.benchmark for point in curve if point.benchmark is not None),
        None,
    )
    running_peak = initial_equity
    series: list[dict[str, object]] = []
    for point in curve:
        running_peak = max(running_peak, point.equity)
        benchmark = (
            None
            if point.benchmark is None or benchmark_start is None
            else _finite_float(point.benchmark / benchmark_start * Decimal("100"))
        )
        series.append(
            {
                "date": point.session_date.isoformat(),
                "equity": _finite_float(point.equity / initial_equity * Decimal("100")),
                "benchmark": benchmark,
                "drawdown": _finite_float(point.equity / running_peak - Decimal("1")),
            }
        )

    metrics = result.metrics
    benchmark_comparison_status = _benchmark_comparison_status(result)
    data_snapshot = _mapping(manifest, "data_snapshot")
    assumptions = _mapping(manifest, "assumptions")
    warnings = _warnings(result, assumptions, benchmark_comparison_status)
    bundle: dict[str, Any] = {
        "summary": {
            "runId": run_id,
            "totalReturn": _optional_metric(metrics.total_return),
            "benchmarkReturn": _optional_metric(metrics.benchmark_return),
            "benchmarkComparisonStatus": benchmark_comparison_status,
            "annualizedReturn": _optional_metric(metrics.annualized_return),
            "maxDrawdown": _optional_metric(metrics.maximum_drawdown),
            "sharpeRatio": _optional_metric(metrics.sharpe_ratio),
            "winRate": _optional_metric(metrics.win_rate),
            "tradeCount": metrics.trade_count,
            "initialCashCny": _finite_float(initial_equity),
            "finalEquityCny": _finite_float(curve[-1].equity),
            "interpretation": _interpretation(result),
            "dataRange": {
                "start": period_start.isoformat(),
                "end": period_end.isoformat(),
                "sessions": max(0, len(curve) - 1),
            },
            "warnings": warnings,
            "runEvidence": {
                "strategyHash": _text(manifest, "strategy_hash"),
                "catalogHash": _text(manifest, "catalog_hash"),
                "dataSnapshotId": _text(data_snapshot, "snapshot_id"),
                "dataSnapshotChecksum": _text(data_snapshot, "checksum"),
                "dataSchemaVersion": _text(data_snapshot, "schema_version"),
                "producerSnapshotSchemaVersion": _optional_text(
                    data_snapshot,
                    "producer_schema_version",
                ),
                "producerSnapshotId": _optional_text(
                    data_snapshot,
                    "producer_snapshot_id",
                ),
                "codeRevision": _text(manifest, "code_revision"),
                "engineVersion": _text(manifest, "engine_version"),
                "executionAssumptions": {
                    str(key): str(value) for key, value in sorted(assumptions.items())
                },
            },
        },
        "series": series,
        "activities": _activities(result),
        "audit": {
            "engineResultHash": result.content_hash,
            "hashSchemaVersion": RESULT_HASH_SCHEMA_VERSION,
            "openPositionShares": sum(
                lot.remaining_quantity.value for lot in result.final_portfolio.lots
            ),
        },
        "robustness": None if robustness is None else dict(robustness),
    }
    note = zero_fill_execution_note(bundle["activities"])
    if note is not None:
        bundle["summary"]["executionNote"] = note
    bundle["audit"]["resultHash"] = calculate_result_bundle_hash(bundle)
    return bundle


def calculate_result_bundle_hash(bundle: Mapping[str, object]) -> str:
    """Hash every persisted result field except the self-referential hash itself."""

    audit = _mapping(bundle, "audit")
    hash_input = dict(bundle)
    hash_input["audit"] = {key: value for key, value in audit.items() if key != "resultHash"}
    return result_hash(
        {
            "bundle": hash_input,
            "schemaVersion": RESULT_HASH_SCHEMA_VERSION,
        }
    )


def _activities(result: DailyBacktestResult) -> list[dict[str, object]]:
    activities: list[dict[str, object]] = []
    trace_by_order = {trace.order.order_id: trace for trace in result.orders}
    attempt_by_order: dict[OrderId, tuple[int, str | None]] = {}
    for decision in result.decisions:
        previous_reason: str | None = None
        for attempt_no, order_id in enumerate(decision.order_ids, start=1):
            attempt_by_order[order_id] = (attempt_no, previous_reason)
            trace = trace_by_order.get(order_id)
            if trace is not None:
                previous_reason = trace.match.reason_code

    for decision in result.decisions:
        side_label = "买入" if decision.side.value == "buy" else "卖出"
        signal_activity_id = f"signal:{decision.decision_id.value}"
        activities.append(
            {
                "id": signal_activity_id,
                "chainId": decision.decision_id.value,
                "decisionId": decision.decision_id.value,
                "kind": "signal",
                "occurredAt": decision.signal.available_at.isoformat(),
                "side": decision.side.value,
                "title": f"{side_label}信号确认",
                "status": "confirmed",
                "reason": decision.signal.reason,
                "signalSemantics": (
                    None if decision.signal_semantics is None else decision.signal_semantics.value
                ),
                "signalValiditySessions": decision.signal_validity_sessions,
                "originSignalId": signal_activity_id,
                "outcomeReason": decision.outcome_reason,
                "evidence": [
                    {
                        "type": item.evidence_type,
                        "id": item.evidence_id,
                        "availableAt": item.available_at.isoformat(),
                        "sourceEventId": item.source_event_id,
                        "provider": item.provider,
                        "sourceUrl": item.source_url,
                        "timeQuality": item.time_quality,
                        "timestampPrecision": item.timestamp_precision,
                        "validationStatus": item.validation_status,
                        "rawResponseSha256": item.raw_response_sha256,
                    }
                    for item in decision.signal.evidence
                ],
            }
        )

    fill_by_order = {fill.order_id: fill for fill in result.fills}
    for trace in result.orders:
        order = trace.order
        side_label = "买入" if order.side.value == "buy" else "卖出"
        decision = next(item for item in result.decisions if item.decision_id == order.decision_id)
        attempt_no, retry_reason = attempt_by_order.get(order.order_id, (1, None))
        origin_signal_id = f"signal:{order.decision_id.value}"
        signal_semantics = (
            None if decision.signal_semantics is None else decision.signal_semantics.value
        )
        activities.append(
            {
                "id": f"order:{order.order_id.value}",
                "chainId": order.decision_id.value,
                "decisionId": order.decision_id.value,
                "orderId": order.order_id.value,
                "parentId": f"signal:{order.decision_id.value}",
                "kind": "order",
                "occurredAt": order.created_at.isoformat(),
                "side": order.side.value,
                "title": f"提交{side_label}委托",
                "price": _finite_float(order.limit_price.amount),
                "quantity": order.quantity.value,
                "status": "submitted",
                "reason": (
                    "09:15 为日线委托提交边界；09:30 仅记录日线开盘价代理，不代表精确成交时点"
                ),
                "signalSemantics": signal_semantics,
                "signalValiditySessions": decision.signal_validity_sessions,
                "attemptNo": attempt_no,
                "originSignalId": origin_signal_id,
                "retryReason": retry_reason,
                "outcomeReason": trace.match.reason_code,
            }
        )
        fill = fill_by_order.get(order.order_id)
        if fill is not None:
            partial = trace.match.outcome is MatchOutcome.PARTIALLY_FILLED
            activities.append(
                {
                    "id": f"fill:{fill.fill_id.value}",
                    "chainId": order.decision_id.value,
                    "decisionId": order.decision_id.value,
                    "orderId": order.order_id.value,
                    "fillId": fill.fill_id.value,
                    "parentId": f"order:{order.order_id.value}",
                    "kind": "partial_fill" if partial else "fill",
                    "occurredAt": fill.filled_at.isoformat(),
                    "side": fill.side.value,
                    "title": f"{side_label}{'部分' if partial else ''}成交",
                    "price": _finite_float(fill.price.amount),
                    "quantity": fill.quantity.value,
                    "status": "partially_filled" if partial else "filled",
                    "reason": trace.match.reason_code,
                    "signalSemantics": signal_semantics,
                    "signalValiditySessions": decision.signal_validity_sessions,
                    "attemptNo": attempt_no,
                    "originSignalId": origin_signal_id,
                    "retryReason": retry_reason,
                    "outcomeReason": trace.match.reason_code,
                    "capacityReasonCode": trace.match.capacity_reason_code,
                    "timeQuality": (
                        None if trace.match.time_quality is None else trace.match.time_quality.value
                    ),
                    "timeSemantics": (
                        "日线 OHLCV 的开盘价代理记录边界，不代表已观测到该时刻的真实成交"
                        if trace.match.time_quality is not None
                        else None
                    ),
                }
            )
        if order.status is not OrderStatus.FILLED:
            expired = order.status is OrderStatus.EXPIRED
            activities.append(
                {
                    "id": f"unfilled:{order.order_id.value}",
                    "chainId": order.decision_id.value,
                    "decisionId": order.decision_id.value,
                    "orderId": order.order_id.value,
                    "parentId": f"order:{order.order_id.value}",
                    "kind": "expired" if expired else "unfilled",
                    "occurredAt": (order.terminal_at or order.valid_until).isoformat(),
                    "side": order.side.value,
                    "title": f"{side_label}委托{'到期' if expired else '未完全成交'}",
                    "price": _finite_float(order.limit_price.amount),
                    "quantity": order.quantity.value - order.filled_quantity.value,
                    "status": "expired" if expired else "cancelled",
                    "reason": trace.match.reason_code,
                    "signalSemantics": signal_semantics,
                    "signalValiditySessions": decision.signal_validity_sessions,
                    "attemptNo": attempt_no,
                    "originSignalId": origin_signal_id,
                    "retryReason": retry_reason,
                    "outcomeReason": trace.match.reason_code,
                }
            )

    return sorted(activities, key=lambda item: (str(item["occurredAt"]), str(item["id"])))


def _warnings(
    result: DailyBacktestResult,
    assumptions: Mapping[str, object],
    benchmark_comparison_status: str,
) -> list[str]:
    warnings = [
        "日线回测只能证明价格区间和成交量，不能证明涨跌停队列中的实际排队位置。",
    ]
    capacity_mode = assumptions.get("capacity_mode")
    if capacity_mode == "point_in_time_volume":
        warnings.append(
            "开盘价代理成交容量使用上一交易日已完成成交量乘参与率，未读取当日收盘后成交量。"
        )
    elif capacity_mode == "unlimited":
        warnings.append("本次运行由用户显式关闭成交容量上限，结果可能高估大额订单可成交性。")
    auction_policy = assumptions.get("opening_auction_policy")
    if auction_policy == (
        "cn.a_share.daily.published_open_proxy.latency_1s.cutoff_0915."
        "recorded_0930.not_exact.day_order.v2"
    ):
        warnings.append(
            "日线事件预留 1 秒处理时间；09:15 前就绪才允许使用当日已发布开盘价代理，"
            "成交时间仅按 09:30 可证明边界记录，不代表真实 09:30 或 09:25 成交，订单当日有效。"
        )
    else:
        warnings.append("本次运行未声明当前日线开盘价代理策略，不应用于正式执行时间比较。")
    corporate_policy = assumptions.get("corporate_action_policy")
    if isinstance(corporate_policy, str) and corporate_policy:
        warnings.append(f"公司行动按已固定账本政策处理：{corporate_policy}。")
    else:
        warnings.append("本次运行清单未声明公司行动账本政策，不应用于正式收益比较。")
    dividend_tax_policy = assumptions.get("dividend_tax_policy")
    if dividend_tax_policy == "gross_research_no_withholding.v1":
        warnings.append(
            "现金分红按税前金额入账，尚未模拟个人投资者按持股期限差异化补税；"
            "含分红的结果不能用于判断个人税后收益。"
        )
    rights_issue_policy = assumptions.get("rights_issue_policy")
    if rights_issue_policy == "decline_no_external_cash.v1":
        warnings.append("配股默认不追加外部资金并放弃认购；该决定会写入公司行动审计轨迹。")
    elif rights_issue_policy == "fail_closed_without_explicit_participation.v1":
        warnings.append("区间若出现配股且没有明确认购与缴款规则，回测会直接拒绝，不会猜测处理。")
    benchmark_policy = assumptions.get("benchmark_policy")
    if isinstance(benchmark_policy, str) and benchmark_policy:
        warnings.append("同期持有为同初始资金、同交易与公司行动规则的资金账户。")
    if result.metrics.statistical_warning == "sample_too_small_for_strong_inference":
        warnings.append("完整交易不足 30 次，胜率、夏普等统计结论不稳健。")
    if result.metrics.benchmark_return is None:
        warnings.append("本次数据快照未包含可比基准，未计算超额收益。")
    elif benchmark_comparison_status == "strategy_entry_not_filled":
        warnings.append("策略没有已成交买入；买入持有收益仅作参考，不计算超额收益。")
    elif benchmark_comparison_status == "benchmark_entry_not_filled":
        warnings.append("买入持有基准未按同一成交规则成交，无法比较超额收益。")
    if result.final_portfolio.lots:
        warnings.append("回测结束时仍有持仓，完整交易次数不包含这笔未平仓交易。")
    return warnings


def _interpretation(result: DailyBacktestResult) -> str:
    metrics = result.metrics
    comparison_status = _benchmark_comparison_status(result)
    if comparison_status == "strategy_entry_not_filled":
        return "策略没有产生已成交买入，无法比较超额收益。"
    if comparison_status == "benchmark_entry_not_filled":
        return "买入持有基准未按同一成交规则成交，无法比较超额收益。"
    if comparison_status == "benchmark_unavailable":
        return "本次没有可审计的买入持有基准，无法比较超额收益。"
    if metrics.trade_count == 0:
        return "这段时间没有形成一笔完整买卖，暂时不能评价策略是否有效。"
    direction = "盈利" if metrics.total_return > 0 else "亏损"
    drawdown = abs(metrics.maximum_drawdown) * 100
    if metrics.benchmark_return is None:
        comparison = "本次没有可用的同期持有基准"
    else:
        excess = metrics.total_return - metrics.benchmark_return
        comparison = "跑赢同资金同规则买入持有" if excess > 0 else "未跑赢同资金同规则买入持有"
    return f"策略期内{direction}，{comparison}；最大回撤约 {drawdown:.1f}%。"


def _benchmark_comparison_status(result: DailyBacktestResult) -> str:
    """Return the only status under which excess return is meaningful.

    An all-cash strategy and a rising buy-and-hold reference are both useful
    observations, but their arithmetic difference is not an investment edge:
    the strategy never entered the market.  Likewise, a reference whose own
    entry did not fill is not a buy-and-hold account.  Neither fact can be
    inferred from a flat equity curve, so both come from audited run artifacts.
    """

    if result.metrics.benchmark_return is None or result.benchmark_entry_filled is None:
        return "benchmark_unavailable"
    if result.benchmark_entry_filled is False:
        return "benchmark_entry_not_filled"
    if not any(fill.side is OrderSide.BUY for fill in result.fills):
        return "strategy_entry_not_filled"
    return "comparable"


def _optional_metric(value: float | None) -> float | None:
    if value is None or not isfinite(value):
        return None
    return value


def _finite_float(value: Decimal) -> float:
    converted = float(value)
    if not isfinite(converted):
        raise ValueError("result projection cannot contain a non-finite number")
    return converted


def _mapping(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    item = value.get(key)
    if not isinstance(item, Mapping):
        raise ValueError(f"result manifest is missing mapping {key!r}")
    raw = cast(Mapping[object, object], item)
    if any(not isinstance(item_key, str) for item_key in raw):
        raise ValueError(f"result manifest mapping {key!r} has non-text keys")
    return cast(Mapping[str, object], raw)


def _text(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError(f"result manifest is missing text {key!r}")
    return item


def _optional_text(value: Mapping[str, object], key: str) -> str | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str) or not item:
        raise ValueError(f"result manifest field {key!r} must be null or non-empty text")
    return item
