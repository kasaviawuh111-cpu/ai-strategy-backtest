"""Project the price-plan ledger into the existing persisted run/report API."""
import json
from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime, time, timedelta
from decimal import Decimal
from hashlib import sha256
from zoneinfo import ZoneInfo

from ashare_lab.adapters.market_data.mx_daily_history import MxDailyHistory
from ashare_lab.api.result_schemas import BacktestResultBundle
from ashare_lab.application.backtest_submission import BacktestRunConfig
from ashare_lab.application.conditional_orders import run_conditional_backtest
from ashare_lab.application.execution_feedback import UNFILLED_REASONS, zero_fill_execution_note
from ashare_lab.application.grid_strategy import run_grid_backtest
from ashare_lab.application.minute_grid_replay import GridReplayResult
from ashare_lab.domain.strategy import StrategySpec, canonical_hash
from ashare_lab.domain.strategy.price_plans import GridPlan, GridSpecificationError, ScheduledPlan

_UNFILLED_REASONS = UNFILLED_REASONS


def execute_price_plan(run_id: object, strategy: StrategySpec,
                       history: MxDailyHistory,
                       config: BacktestRunConfig | None = None, *,
                       minute_result: GridReplayResult | None = None,
                       minute_snapshot_id: str | None = None,
                       minute_reconciliation: tuple = (),
                       minute_source_evidence: dict | None = None,
                       runtime_evidence: Mapping[str, str] | None = None,
                       market_sessions: tuple | None = None,
                       calendar_evidence: dict | None = None,
                       corporate=None) -> BacktestResultBundle:
    from ashare_lab.application.result_views import (
        RESULT_HASH_SCHEMA_VERSION,
        calculate_result_bundle_hash,
    )
    plan = strategy.trading_plan
    independent = strategy.independent_plans
    assert plan is not None or independent is not None
    from ashare_lab.domain.strategy import ComposedExecutionPolicy
    if isinstance(strategy.execution, ComposedExecutionPolicy):
        complete = (bool(minute_source_evidence) and (
            minute_source_evidence.get('independentPlans') == independent.model_dump(mode='json')
            if independent is not None else 'dailySignals' in minute_source_evidence))
        if minute_result is None or not complete:
            raise GridSpecificationError(
                "组合策略缺少完整执行结果或双方规则来源，不能回退成只运行价格计划。")
    if minute_result is not None:
        from ashare_lab.application.minute_result import minute_result_bundle
        if not minute_snapshot_id:
            raise ValueError("minute result requires pinned source identity")
        return minute_result_bundle(run_id=str(run_id), result=minute_result,
                                    initial_cash=Decimal(strategy.backtest.initial_cash_cny),
                                    snapshot_id=minute_snapshot_id, reconciliation=minute_reconciliation,
                                    source_evidence=minute_source_evidence,
                                    strategy=strategy, runtime_evidence=runtime_evidence)
    if isinstance(plan, ScheduledPlan):
        raise GridSpecificationError("定时计划尚未接入独立交易日历执行器，已保留计划，不能回退为其他策略。")
    result = (run_grid_backtest if isinstance(plan, GridPlan) else run_conditional_backtest)(
        params=plan.parameters, history=history,
        start=strategy.backtest.start, end=strategy.backtest.end,
        execution_config=config,
        market_sessions=market_sessions,
        corporate_actions=corporate.applier if corporate is not None else None,
        price_rebases=corporate.price_rebases if corporate is not None else (),
    )
    if calendar_evidence is not None:
        result["provenance"]["market_calendar"] = calendar_evidence
    if corporate is not None:
        result["provenance"]["corporate_actions"] = corporate.evidence
    initial = Decimal(result["summary"]["initial_equity_cny"])
    opening = result["opening_portfolio"]
    peak = initial
    # The price-plan benchmark mirrors its audited initial-build fills. Plans
    # without an explicit initial build use their first actual buy fill. It
    # then freezes cash and shares, isolating later trading without inventing
    # an all-in allocation the user never selected.
    filled_buys = [
        order for order in result["orders"]
        if order["side"] == "buy" and order["filled_quantity"]
    ]
    initial_build = [order for order in filled_buys if order["initial"]]
    benchmark_entries = [] if opening["shares"] else initial_build or filled_buys[:1]
    initial_fills_by_date: dict[object, list[dict[str, object]]] = {}
    for order in benchmark_entries:
        initial_fills_by_date.setdefault(order["date"], []).append(order)
    benchmark_cash = Decimal(opening["cash_cny"])
    benchmark_shares = int(opening["shares"])
    benchmark_filled = bool(benchmark_shares)
    benchmark_values: dict[object, Decimal] = {}
    raw_close_by_date = {
        row.session_date.isoformat(): row.raw_close for row in history.rows
    }
    for point in result["series"]:
        for order in initial_fills_by_date.get(point["date"], []):
            quantity = int(order["filled_quantity"])
            benchmark_cash -= Decimal(order["price"]) * quantity + Decimal(order["fees_cny"])
            benchmark_shares += quantity
            benchmark_filled = True
        benchmark_values[point["date"]] = (
            benchmark_cash + benchmark_shares * raw_close_by_date[point["date"]]
        )
    if corporate is not None:
        benchmark_filled = result["benchmark_entered"]
        benchmark_values = {point["date"]: point["benchmark_equity_cny"] for point in result["series"]}

    # The common report includes one initial equity point before observed sessions.
    curve = [{"date": strategy.backtest.start - timedelta(days=1), "equity": 100,
              "benchmark": 100 if benchmark_filled else None, "drawdown": 0}]
    for point in result["series"]:
        equity = point["equity_cny"]
        peak = max(peak, equity)
        curve.append({"date": point["date"], "equity": equity / initial * 100,
                      "benchmark": (benchmark_values[point["date"]] / initial * 100
                                    if benchmark_filled else None),
                      "drawdown": equity / peak - 1})
    activities = []
    closed = 0
    for index, order in enumerate(result["orders"]):
        chain_id = f"{run_id}:price:{index}"
        signal_date = datetime.fromisoformat(order["signal_date"]).date()
        execution_date = datetime.fromisoformat(order["date"]).date()
        execution_at = datetime.combine(execution_date, time(9, 30), ZoneInfo("Asia/Shanghai"))
        signal_at = datetime.combine(signal_date, time(9, 30) if signal_date == execution_date
                                     else time(15), ZoneInfo("Asia/Shanghai"))
        if order.get("signal_at"):
            signal_at = datetime.fromisoformat(order["signal_at"])
        if order.get("effective_at"):
            execution_at = datetime.fromisoformat(order["effective_at"])
        title = "初始建仓" if order["initial"] else (
            "网格跨格触发" if isinstance(plan, GridPlan) else f"条件 {order['rule_id']} 触发"
        )
        activities.extend([
            {"id": f"{chain_id}:signal", "chainId": chain_id, "kind": "signal",
             "occurredAt": signal_at, "side": order["side"], "title": title,
             "status": "confirmed", "reason": title, "price": order["trigger_price"],
             "timeQuality": "daily_bar_open_proxy" if signal_at.time() == time(9, 30)
             else "daily_bar_close_confirmed"},
            {"id": f"{chain_id}:order", "chainId": chain_id, "orderId": chain_id,
             "parentId": f"{chain_id}:signal", "kind": "order", "occurredAt": execution_at,
             "side": order["side"], "title": "模拟委托", "status": "submitted",
             "reason": "开盘起挂限价，按当日O/H/L检查" if order["limit_price"] is not None else "日线开盘代理尝试成交", "price": order["limit_price"],
             "quantity": order["requested_quantity"] or None, "timeQuality": "daily_bar_open_proxy"},
        ])
        quantity = order["filled_quantity"]
        fill_at = datetime.fromisoformat(order["filled_at"]) if order.get("filled_at") else execution_at
        partial = 0 < quantity < order["requested_quantity"]
        disposition = order.get("remainder_disposition")
        day_expired = disposition == "expired_day"
        terminal_at = datetime.combine(execution_date, time(15), ZoneInfo("Asia/Shanghai")) if day_expired else execution_at
        if quantity and order["side"] == "sell" and order["shares"] == 0 and not order.get("pending_share_delta", 0):
            closed += 1
        activities.append({
            "id": f"{chain_id}:outcome", "chainId": chain_id, "orderId": chain_id,
            "parentId": f"{chain_id}:order",
            "kind": "partial_fill" if partial else "fill" if quantity else "expired" if day_expired else "unfilled",
            "occurredAt": fill_at if quantity else terminal_at,
            "side": order["side"], "title": "模拟成交" if quantity else "限价委托当日失效" if day_expired else "委托未成交",
            "price": order["price"], "quantity": quantity or None,
            "notionalCny": order["price"] * quantity if quantity else None,
            "status": "partially_filled" if partial else "filled" if quantity else "cancelled" if disposition == "ended" else "expired",
            "reason": ("限价当日未成交，收盘撤销，不再沿用该触发跨日等待。" if day_expired and not quantity else
                       _UNFILLED_REASONS.get(order["reason"], order["reason"])),
            "timeQuality": "daily_bar_close_confirmed" if day_expired and not quantity else order.get("time_quality", "daily_bar_proxy" if order["limit_price"] is not None else "daily_bar_open_proxy"),
            "timeSemantics": "当日收盘撤销未成交限价委托，无实际成交" if day_expired and not quantity else "日线限价撮合，具体成交时刻未知，非券商真实成交" if order["limit_price"] is not None else "日线开盘价模拟，非券商真实成交",
            "executionDetails": json.loads(json.dumps(order, default=str)),
        })
        if partial and disposition in {"expired_day", "ended"}:
            activities.append({
                "id": f"{chain_id}:remainder", "chainId": chain_id, "orderId": chain_id,
                "parentId": f"{chain_id}:order", "kind": "expired", "occurredAt": terminal_at,
                "side": order["side"], "title": "未成交余量已结束",
                "quantity": order["requested_quantity"] - quantity,
                "status": "expired" if day_expired else "cancelled",
                "reason": "未成交余量收盘撤销。" if day_expired else "普通委托剩余数量不再重试；已成交部分保留。",
                "timeQuality": "daily_bar_close_confirmed" if day_expired else "daily_bar_open_proxy",
                "executionDetails": {"remainder_disposition": disposition},
            })
    condition_state = result.get("condition_state", {})
    if condition_state.get("unfinished_exit_quantity", 0):
        result["warnings"].append(f"期末仍有{condition_state['unfinished_exit_quantity']}股退出意图未完成，剩余委托已撤销，未强制平仓。")
    end_cancellation = condition_state.get("end_cancellation")
    if end_cancellation is not None:
        end_chain = f"{run_id}:price:end"
        activities.append({
            "id": f"{end_chain}:cancel", "chainId": end_chain, "kind": "expired",
            "occurredAt": datetime.combine(datetime.fromisoformat(end_cancellation["date"]).date(), time(15), ZoneInfo("Asia/Shanghai")),
            "side": end_cancellation["side"], "title": "回测结束撤销剩余委托",
            "quantity": end_cancellation["quantity"] or None, "status": "cancelled",
            "reason": "仅撤销未成交委托，剩余持仓按期末收盘价估值，不作强制平仓。",
            "timeQuality": "daily_bar_close_confirmed",
            "executionDetails": json.loads(json.dumps(end_cancellation, default=str)),
        })
    summary = result["summary"]
    benchmark_return = (
        benchmark_values[result["series"][-1]["date"]] / initial - 1
        if benchmark_filled else None
    )
    run_evidence = None
    if runtime_evidence is not None:
        # Daily price plans must pin their inputs just like minute plans. Do not
        # substitute a minute snapshot or claim release identity for legacy callers.
        source = dict(history=asdict(history), marketSessions=market_sessions,
                      calendar=calendar_evidence,
                      corporateActions=corporate.evidence if corporate is not None else None,
                      priceRebases=[asdict(item) for item in corporate.price_rebases]
                      if corporate is not None else [])
        source_json = json.dumps(source, default=str, sort_keys=True, separators=(",", ":"))
        checksum = sha256(source_json.encode()).hexdigest()
        run_evidence = dict(
            strategyHash=canonical_hash(strategy.model_dump(mode="json")),
            catalogHash=runtime_evidence["catalog_hash"],
            codeRevision=runtime_evidence["code_revision"],
            engineVersion=runtime_evidence["engine_version"],
            dataSnapshotId=f"daily-price-plan:{checksum}",
            dataSnapshotChecksum=f"sha256:{checksum}",
            dataSchemaVersion="daily-price-plan-input.v1",
            executionAssumptions=dict(
                timeQuality="daily_bar_proxy", execution=result["provenance"]["execution"],
                corporateActions=result["corporate_action_policy"] or "not_supplied",
                config=json.dumps(asdict(config or BacktestRunConfig()),
                                  default=str, sort_keys=True),
            ),
        )
        result["provenance"]["input_snapshot"] = json.loads(source_json)
    bundle = BacktestResultBundle.model_validate({
        "summary": {
            "runId": str(run_id), "totalReturn": summary["total_return"],
            "benchmarkReturn": benchmark_return,
            "benchmarkComparisonStatus": (
                "comparable" if benchmark_filled else "strategy_entry_not_filled"
            ),
            "annualizedReturn": None, "maxDrawdown": summary["max_drawdown"],
            "sharpeRatio": None, "winRate": None, "tradeCount": closed,
            "tradeCountSemantics": "closed_position_cycles",
            "initialCashCny": summary["initial_cash_cny"], "initialEquityCny": initial,
            "finalEquityCny": summary["final_equity_cny"],
            "interpretation": (
                "交易计划按实际股数、可卖底仓与费用逐笔模拟。"
                "同期持有基准优先复用期初已有持仓与剩余现金；没有期初持仓时，"
                "复用实际初始建仓（无初始建仓时取首笔买入）的日期、股数、价格、费用和剩余现金，"
                "此后不再交易；完整交易数按清仓周期计。"
            ),
            "dataRange": {"start": strategy.backtest.start, "end": strategy.backtest.end,
                          "sessions": len(result["series"])},
            "warnings": result["warnings"],
            "runEvidence": run_evidence,
            "executionNote": zero_fill_execution_note(activities),
            "dataProvenance": {
                "provider": history.provider, "instrumentId": history.instrument_id,
                "priceBasis": "unadjusted", "retrievedAt": history.retrieved_at,
                "historyStart": history.rows[0].session_date,
                "historyEnd": history.rows[-1].session_date,
                "historyRows": len(history.rows), "indicatorSeries": 0, "indicatorPoints": 0,
                "historyCacheStatus": history.cache_status or "unknown",
                "queries": [item.query for item in history.query_evidence],
            },
        },
        "series": curve, "activities": activities,
        "audit": {"hashSchemaVersion": RESULT_HASH_SCHEMA_VERSION,
                  "pricePlanLedger": json.dumps(result, ensure_ascii=False, default=str),
                  "openPositionShares": summary["final_shares"],
                  "openPositionNotionalCny": (summary["final_equity_cny"]
                                              - result["series"][-1]["cash_cny"]
                                              - result["series"][-1].get("dividend_receivable_cny", 0)),
                  "openDividendReceivableCny": result["series"][-1].get("dividend_receivable_cny", 0)},
    })
    bundle.audit.result_hash = calculate_result_bundle_hash(
        bundle.model_dump(mode="json", by_alias=True),
    )
    return bundle
