"""Project minute replay into the existing report bundle, without daily fake times."""
import json
from collections.abc import Mapping
from hashlib import sha256
from dataclasses import asdict
from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from ashare_lab.api.result_schemas import BacktestResultBundle
from ashare_lab.application.minute_grid_replay import GridReplayResult
from ashare_lab.application.result_views import RESULT_HASH_SCHEMA_VERSION, calculate_result_bundle_hash
from ashare_lab.domain.strategy import StrategySpec, canonical_hash
from ashare_lab.application.execution_feedback import UNFILLED_REASONS, zero_fill_execution_note


def attach_minute_robustness(*, base: BacktestResultBundle, stressed: BacktestResultBundle,
                             config, stress_slippage_bps: Decimal) -> BacktestResultBundle:
    """Attach the same predeclared higher-friction check used by daily Skill runs."""
    scenario = {
        "id": "higher_slippage",
        "configHash": canonical_hash({
            "participationRate": str(config.participation_rate),
            "slippageBps": str(stress_slippage_bps),
            "slippageCny": str(config.slippage_cny * 2),
            "capacityMode": config.capacity_mode.value,
            "limitHandling": config.limit_handling.value,
        }),
        "participationRate": float(config.participation_rate),
        "slippageBps": float(stress_slippage_bps),
        **({"slippageCny": float(config.slippage_cny * 2)} if config.slippage_cny else {}),
        "totalReturn": stressed.summary.total_return,
        "maxDrawdown": stressed.summary.max_drawdown,
        "tradeCount": stressed.summary.trade_count,
        "finalEquityCny": stressed.summary.final_equity_cny,
    }
    returns = (base.summary.total_return, stressed.summary.total_return)
    updated = BacktestResultBundle.model_validate({
        **base.model_dump(mode="json", by_alias=True),
        "robustness": {
            "profile": "execution.v1",
            "selectionPolicy": "predeclared_scenarios_no_optimization",
            "totalReturnRange": {"min": min(returns), "max": max(returns)},
            "scenarios": [scenario],
        },
    })
    updated.audit.result_hash = calculate_result_bundle_hash(
        updated.model_dump(mode="json", by_alias=True)
    )
    return updated


def _price_adjustment_note(result, strategy):
    """Disclose only in-window rebases, not historical warm-up events."""
    plan = strategy.trading_plan if strategy else None
    if plan is None or plan.kind not in {"grid", "conditional"}:
        return None
    if plan.kind == "conditional" and not any(
        r.target_price is not None or r.limit_price is not None
        or r.activation_price is not None or (r.gap is not None and r.gap_unit == "cny")
        for r in plan.parameters.rules
    ):
        return None
    active = [r for r in result.price_rebases
              if strategy.backtest.start <= r.ex_date <= strategy.backtest.end
              and r.factor != 1]
    if not active:
        return None
    return ("区间内发生除权除息，相关固定价位已同步调整；卡片保留原始设置，成交记录使用当时实际价格。"
            "到价是触发条件，不保证以触发价成交；指定限价的委托仍受限价约束。")


def minute_result_bundle(*, run_id: str, result: GridReplayResult,
                         initial_cash: Decimal, snapshot_id: str,
                         reconciliation: tuple = (),
                         source_evidence: dict | None = None,
                         strategy: StrategySpec | None = None,
                         runtime_evidence: Mapping[str, str] | None = None) -> BacktestResultBundle:
    initial_equity = result.initial_equity_cny if result.initial_equity_cny is not None else initial_cash
    if not result.equity or initial_cash < 0 or initial_equity <= 0:
        raise ValueError("report requires replay equity and initial cash")
    zone = ZoneInfo("Asia/Shanghai")
    by_day = {point.observed_at.astimezone(zone).date(): point for point in result.equity}
    has_benchmark = result.benchmark_portfolio is not None
    peak = initial_equity
    max_drawdown = Decimal(0)
    intraday = result.execution_time_quality == "minute_bar_end_proxy"
    first_clock = result.equity[0].observed_at.astimezone(zone)
    initial_clock = first_clock.replace(hour=9, minute=30, second=0, microsecond=0)
    if intraday and initial_clock >= first_clock:
        raise ValueError("minute equity must be observed after session opening")
    series = [dict(date=initial_clock if intraday else next(iter(by_day)) - timedelta(days=1), equity=Decimal(100),
                   drawdown=Decimal(0), benchmark=Decimal(100) if has_benchmark else None)]
    day_drawdowns = {}
    point_drawdowns = {}
    for point in result.equity:
        peak = max(peak, point.equity)
        drawdown = point.equity / peak - 1
        max_drawdown = min(max_drawdown, drawdown)
        day_drawdowns[point.observed_at.astimezone(zone).date()] = drawdown
        point_drawdowns[point.observed_at] = drawdown
    observations = ((point.observed_at.astimezone(zone), point) for point in result.equity) if intraday else by_day.items()
    for stamp, point in observations:
        drawdown = point_drawdowns[point.observed_at] if intraday else day_drawdowns[stamp]
        benchmark = None
        if has_benchmark:
            benchmark = (point.benchmark_equity if point.benchmark_equity is not None else initial_equity) / initial_equity * 100
        series.append(dict(date=stamp, equity=point.equity / initial_equity * 100,
                           drawdown=drawdown, benchmark=benchmark))
    activities = []
    closed = result.closed_position_cycles
    reasons = {**UNFILLED_REASONS,
               "due_inventory_changed": "到期持仓已被其他成交改变，撤销原期限委托，避免卖出尚未到期的批次。",
               "due_inventory_resized": "按仍然到期的实际持仓重新确定委托数量。"}
    for index, event in enumerate(result.events):
        reason = reasons.get(event.reason, event.reason)
        if event.reason == "maximum_inventory_exceeded" and event.sizing_details:
            d = event.sizing_details
            if all(key in d for key in ("currentPositionQuantity", "proposedFillQuantity",
                                       "projectedPositionQuantity", "maximumPositionQuantity")):
                reason = (f"当前持仓{d['currentPositionQuantity']}股，本次拟买{d['proposedFillQuantity']}股，"
                          f"合计{d['projectedPositionQuantity']}股，超过持仓上限{d['maximumPositionQuantity']}股。"
                          "本期整笔跳过，不自动缩量或补投；账户有现金也不能突破持仓上限。")
        if event.reason == "budget_below_minimum_order" and event.sizing_details:
            d = event.sizing_details
            reason = (f"本次预算{d['budgetCny']}元；按撮合价{d['sizingPrice']}元，"
                      f"最低买入{d['minimumOrderQuantity']}股含费用需{d['minimumOrderCostCny']}元。"
                      "本次买入预算不足，已跳过，不自动追加预算。")
        fill = event.fill
        submitted = event.status in {"submitted", "working", "retry_pending"}
        cancelled = event.status == "cancelled"
        kind = ("partial_fill" if fill and event.status == "partially_filled" else "fill" if fill
                else "order" if submitted else "expired" if cancelled else "unfilled")
        status = ("partially_filled" if fill and event.status == "partially_filled" else "filled" if fill
                  else "submitted" if submitted else "cancelled" if cancelled
                  else "rejected" if event.status in {"rejected", "skipped"} else "expired")
        activities.append(dict(
            id=f"{run_id}:minute:{index}", chainId=event.order.order_id, orderId=event.order.order_id,
            kind=kind, status=status, occurredAt=event.observed_at, side=event.order.side,
            title="模拟成交" if fill else "模拟委托" if submitted else "委托结束",
            quantity=fill.quantity.value if fill else event.order.quantity or None,
            price=fill.price.amount if fill else event.order.limit_price,
            notionalCny=fill.gross_amount.amount if fill else None,
            fillId=str(fill.fill_id) if fill else None,
            reason=reason, outcomeReason=event.reason,
            timeQuality=event.time_quality or result.execution_time_quality,
            timeSemantics=("市场交易日开闭市时钟；该时间用于委托生命周期，不代表实际成交或证券停牌"
                           if event.time_quality == "market_session_clock" else
                           "日线计划使用O/C；开盘限价如通过H/L撮合，具体成交时刻未知，时间仅为代理"
                           if result.execution_time_quality == "daily_bar_proxy" else
                           "完成分钟线回放，时间为bar结束代理，非券商逐笔成交时间"),
            executionDetails=dict(signalBar=event.order.signal_bar, effectiveBar=event.order.effective_bar,
                                  signalAt=event.signal_at.isoformat() if event.signal_at else None,
                                  effectiveAt=event.effective_at.isoformat() if event.effective_at else None,
                                  observedBar=None if event.time_quality == "market_session_clock" else event.bar_index,
                                  rawStatus=event.status,
                                  commissionCny=fill.fees.commission.amount if fill else None,
                                  stampTaxCny=fill.fees.stamp_tax.amount if fill else None,
                                  transferFeeCny=fill.fees.transfer_fee.amount if fill else None,
                                  otherFeesCny=fill.fees.other.amount if fill else None,
                                  totalFeesCny=fill.fees.total.amount if fill else None,
                                  cashDeltaCny=((fill.gross_amount.amount if event.order.side == "sell"
                                                 else -fill.gross_amount.amount) - fill.fees.total.amount
                                                if fill else None),
                                  ambiguousIntrabarOrder=int(event.ambiguous_intrabar_order),
                                  **(event.sizing_details or {})),
        ))
    last = result.equity[-1]
    small_budget = [a for a in activities if a["outcomeReason"] == "budget_below_minimum_order"]
    execution_note = zero_fill_execution_note(activities)
    if small_budget:
        prefix = "本次没有成交；" if not result.portfolio.fills else ""
        execution_note = (f"{prefix}{len(small_budget)}次买入因本次预算不足而跳过。"
                          f"首笔：{small_budget[0]['reason']}可在策略设置调整单次预算后重新回测。")
    adjustment_note = _price_adjustment_note(result, strategy)
    if adjustment_note:
        execution_note = (execution_note + " " if execution_note else "") + adjustment_note
    warnings = [("日线OHLC" if result.execution_time_quality == "daily_bar_proxy" else "分钟OHLC")
                + "研究撮合；未重建盘口队列，容量假设为" + result.capacity_assumption]
    if result.grid_policy:
        warnings.append("到价触发网格：触发时更新基准，成交不重设；未成交余量当日结束，不自动跨日重报。"
                        "分钟OHLC以开盘越线或单边触线确认，按格线价更新，每分钟最多一次；双边先后不明则跳过。"
                        "单侧或双侧休眠本次不自动恢复，未报单不计未成交；并行旧单与分钟代理属于研究约定。")
        asleep = [e for e in result.monitoring_events if e["state"] == "sleeping"]
        if asleep:
            from collections import Counter
            counts = Counter(e["reason"] for e in asleep)
            explanation = "；".join(f"{reasons.get(reason, reason)}（{count}次进入休眠）"
                                    for reason, count in counts.items())
            execution_note = (execution_note + " " if execution_note else "") + (
                "网格监控：" + explanation + "。休眠侧本次不自动恢复，不重复报单；未休眠的一侧仍可触发。")
    if source_evidence and source_evidence.get("minute", {}).get("status") == "not_required_no_trading_sessions":
        warnings = ["区间内证券均不可交易，未使用分钟行情、未模拟成交；按来源日线记录估值，初始建仓未执行。"]
    if result.corporate_action_policy:
        warnings.append("公司行动使用独立账户权益登记；仅有日期的现金到账按收盘后处理。分红按税前研究口径，未模拟差异化股息税，配股不参与。")
    elif source_evidence and source_evidence.get("corporateActions", {}).get("status") == "not_connected":
        warnings.append("公司行动数据尚未接入；本结果未核验分红送转影响，不能作为跨除权区间的完整回测结论。")
    if last.dividend_receivable:
        warnings.append(f"期末资产含应收分红{last.dividend_receivable}元，到账前不计入可用资金。")
    if last.pending_share_delta:
        warnings.append(f"期末资产含尚未到账的股份净变动{last.pending_share_delta}股，未提前计入可卖数量。")
    if any(item.approximate for item in reconciliation):
        warnings.append("分钟与日线量额存在研究容差内差异，原始差值已保留在审计记录。")
    if result.unfinished_exit_quantity:
        warnings.append(f"期末仍有{result.unfinished_exit_quantity}股保护退出意图未完成，未强制平仓。")
    if result.unfinished_holding_exit_quantity:
        warnings.append(f"期末仍有{result.unfinished_holding_exit_quantity}股已到持有期限但未卖出；"
                        "该数量可能与保护退出重合，不重复累计，未强制平仓。")
    run_evidence = None
    if strategy is not None and runtime_evidence is not None and source_evidence:
        # This identifies the complete input manifest, not only one minute file:
        # calendar, daily controls and corporate-action sources affect execution too.
        inputs = dict(snapshotId=snapshot_id, sourceEvidence=source_evidence)
        checksum = sha256(json.dumps(inputs, default=str, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()
        run_evidence = dict(
            strategyHash=canonical_hash(strategy.model_dump(mode="json")),
            catalogHash=runtime_evidence["catalog_hash"],
            codeRevision=runtime_evidence["code_revision"],
            engineVersion=runtime_evidence["engine_version"],
            dataSnapshotId=f"price-plan-input:{checksum}", dataSnapshotChecksum=f"sha256:{checksum}",
            dataSchemaVersion="price-plan-input.v1",
            executionAssumptions=dict(timeQuality=result.execution_time_quality,
                capacity=result.capacity_assumption, producerSnapshotId=snapshot_id,
                **({"gridPolicy": result.grid_policy} if result.grid_policy else {}),
                corporateActions=str(source_evidence.get("corporateActions", {}).get("status", "unknown"))),
        )
    bundle = BacktestResultBundle.model_validate(dict(
        summary=dict(runId=run_id, totalReturn=last.equity / initial_equity - 1,
                     benchmarkReturn=series[-1]["benchmark"] / 100 - 1 if has_benchmark else None,
                     benchmarkComparisonStatus="comparable" if has_benchmark else "strategy_entry_not_filled",
                     annualizedReturn=None, maxDrawdown=max_drawdown, sharpeRatio=None, winRate=None,
                     tradeCount=closed, tradeCountSemantics="closed_position_cycles", initialCashCny=initial_cash,
                     initialEquityCny=initial_equity,
                     finalEquityCny=last.equity, interpretation=("日线定期计划与共享账户回放；" if result.execution_time_quality == "daily_bar_proxy" else "订单与共享账户分钟回放；") + (
                         "收益以期初总资产为基数，同期持有基准复用期初现金与持仓，之后不交易。"
                         if initial_equity != initial_cash else
                         "同期持有基准复用首笔买入及其费用，之后不交易。"
                     ) + "回撤按日终净值统计。",
                     dataRange=dict(start=next(iter(by_day)), end=next(reversed(by_day)), sessions=len(by_day)),
                     warnings=warnings, runEvidence=run_evidence, executionNote=execution_note),
        series=series, activities=activities,
        audit=dict(hashSchemaVersion=RESULT_HASH_SCHEMA_VERSION, openPositionShares=last.shares,
                   openPositionNotionalCny=last.shares * last.close,
                   pricePlanLedger=json.dumps(dict(snapshotId=snapshot_id, sourceEvidence=source_evidence,
                       reconciliation=[asdict(r) for r in reconciliation], replay=asdict(result)), default=str)),
    ))
    bundle.audit.result_hash = calculate_result_bundle_hash(bundle.model_dump(mode="json", by_alias=True))
    return bundle
