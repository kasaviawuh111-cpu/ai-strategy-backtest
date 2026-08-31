"""单股定制策略引擎 (③ backtest_custom)

输入: 单股 + buy_atoms (1-2 AND) + 持有规则 (扁平 7 字段)
输出: 跟 ① run_backtest 一致的 BacktestResponse (P&L 曲线 + 4 指标 + 逐笔)

复用:
  - ATOM_REGISTRY: 算 buy_atoms triggers
  - holding_rules: HoldNDays / StopLossPct / TakeProfitPct / TrailingStop /
                   SellOnFixedAtom / SellOnReverseSignal / HoldUntilTimeUnit + EarliestOf
  - reverse_search.simulator.simulate_trades: 触发 → trade list
  - reverse_pairs.get_reverse_atom_id: sell_on_reverse_signal 用
  - metrics: 各类指标

跟反向求解 simulator 行为对齐 (无 T+1 / 无涨跌停板, 简化):
  - 买: 触发日次日 open 价 (扣 slippage + commission)
  - 卖: 持有规则触发日 close 价 (扣 slippage + commission + stamp_tax)
"""
from __future__ import annotations

from typing import Any, Optional

import numpy as np
import pandas as pd

from astock_backtest.data_loader import DataStore
from astock_backtest.holding_rules import (
    EarliestOf,
    HoldingRule,
    HoldNDays,
    HoldUntilTimeUnit,
    SellOnFixedAtom,
    SellOnReverseSignal,
    StopLossPct,
    TakeProfitPct,
    TrailingStop,
)
from astock_backtest.models.backtest_custom import BacktestCustomRequest
from astock_backtest.services import metrics
from astock_backtest.services.simulator import Trade
from astock_backtest.signal_atoms import ATOM_REGISTRY, DataDep
from astock_backtest.signal_atoms.reverse_pairs import get_reverse_atom_id


def _parse_date(s: Optional[str]) -> Optional[pd.Timestamp]:
    if not s:
        return None
    s = s.replace("-", "")
    return pd.Timestamp(f"{s[:4]}-{s[4:6]}-{s[6:8]}")


def _build_holding_rule(req: BacktestCustomRequest) -> HoldingRule:
    """跟 reverse_search._build_holding_rule 同语义."""
    rules: list[HoldingRule] = []
    if req.holding_days is not None:
        rules.append(HoldNDays(n=req.holding_days))
    if req.stop_loss_pct is not None:
        rules.append(StopLossPct(pct=req.stop_loss_pct))
    if req.take_profit_pct is not None:
        rules.append(TakeProfitPct(pct=req.take_profit_pct))
    if req.trailing_stop_pct is not None:
        rules.append(TrailingStop(pct=req.trailing_stop_pct))
    if req.hold_until:
        rules.append(HoldUntilTimeUnit(unit=req.hold_until))
    if req.sell_on_fixed_atom_id:
        rules.append(SellOnFixedAtom(sell_atom_id=req.sell_on_fixed_atom_id))
    elif req.sell_on_reverse_signal:
        rules.append(SellOnReverseSignal())
    if not rules:
        raise ValueError("至少需要一个持有规则")
    if len(rules) == 1:
        return rules[0]
    return EarliestOf(rules)


def _rule_needs_reverse(rule: HoldingRule) -> bool:
    if isinstance(rule, SellOnReverseSignal):
        return True
    if isinstance(rule, EarliestOf):
        return any(_rule_needs_reverse(r) for r in rule.rules)
    return False


def _compute_atom_triggers(
    aid: str, ohlcv: pd.DataFrame, store: DataStore, code: str
) -> np.ndarray:
    """算单个 atom 的 triggers (np.int8). 不在 registry 抛 ValueError."""
    atom = ATOM_REGISTRY.get(aid)
    if atom is None:
        raise ValueError(f"atom_id 不存在: {aid}")
    deps_data: dict[str, Any] = {}
    if DataDep.VALUATION in atom.deps:
        deps_data["valuation"] = store.valuation_by_code.get(code)
    if DataDep.FINANCIAL in atom.deps:
        deps_data["financial"] = (
            store.financial_by_code.get(code) if hasattr(store, "financial_by_code") else None
        )
    if DataDep.DIVIDEND in atom.deps:
        deps_data["dividend"] = (
            store.dividend_by_code.get(code) if hasattr(store, "dividend_by_code") else None
        )
    if DataDep.INDEX in atom.deps:
        deps_data["index"] = store.index_daily if hasattr(store, "index_daily") else None
    return atom.compute(ohlcv=ohlcv, **deps_data)


def run_backtest_custom(req: BacktestCustomRequest, store: DataStore) -> dict:
    """主入口. 输入 schema → 输出 BacktestResponse dict."""
    code = req.stock_code
    if code not in store.code_name_map:
        raise ValueError(f"stock_code {code} 不存在")

    stock_name = store.code_name_map[code]
    ohlcv = store.get_ohlcv(code)
    if ohlcv.empty:
        raise ValueError(f"{code} 无 ohlcv 数据")
    ohlcv = ohlcv.reset_index(drop=True)
    n = len(ohlcv)

    # === 算 buy_atoms triggers + AND ===
    atom_triggers_list = []
    max_warmup = 0
    for aid in req.buy_atoms:
        if aid not in ATOM_REGISTRY:
            raise ValueError(f"atom_id 不存在: {aid}")
        atom_triggers_list.append(_compute_atom_triggers(aid, ohlcv, store, code))
        max_warmup = max(max_warmup, ATOM_REGISTRY[aid].min_warmup)
    if len(atom_triggers_list) == 1:
        triggers = atom_triggers_list[0]
    else:
        # AND 合并
        combined = atom_triggers_list[0].astype(bool)
        for arr in atom_triggers_list[1:]:
            combined &= arr.astype(bool)
        triggers = combined.astype(np.int8)

    # === 区间过滤 (区间外 triggers 置 0) ===
    start_ts = _parse_date(req.start_date)
    end_ts = _parse_date(req.end_date)
    full_start = ohlcv["date"].min()
    full_end = ohlcv["date"].max()
    eff_start = max(start_ts, full_start) if start_ts else full_start
    eff_end = min(end_ts, full_end) if end_ts else full_end

    date_arr = ohlcv["date"].values
    in_range_mask = (date_arr >= np.datetime64(eff_start)) & (date_arr <= np.datetime64(eff_end))
    triggers = triggers.copy()
    triggers[~in_range_mask] = 0

    # === 构持有规则 ===
    rule = _build_holding_rule(req)

    # === reverse_signal_array (sell_on_fixed_atom_id 优先, 否则 reverse) ===
    # 注: SellOnFixedAtom 复用 reverse_signal_array 参数槽 (源码 fixed_signal.py 注释)
    reverse_array = None
    if req.sell_on_fixed_atom_id:
        fixed_id = req.sell_on_fixed_atom_id
        if fixed_id not in ATOM_REGISTRY:
            raise ValueError(f"sell_on_fixed_atom_id 不存在: {fixed_id}")
        reverse_array = _compute_atom_triggers(fixed_id, ohlcv, store, code)
    elif _rule_needs_reverse(rule):
        primary_buy = req.buy_atoms[0]
        rev_id = get_reverse_atom_id(primary_buy)
        if rev_id is None:
            raise ValueError(
                f"buy_atom {primary_buy} 没有反向配对, 无法使用 sell_on_reverse_signal"
            )
        reverse_array = _compute_atom_triggers(rev_id, ohlcv, store, code)

    # === 跑 trades (串行, 有持仓时跳过新 BUY trigger) ===
    # 跟 reverse_search.simulator 关键差异: ③ 是真实单股回测, 必须强制非重叠
    # (同一时刻只能 1 仓). simulator 是统计样本用的, 不维护持仓约束.
    open_arr = ohlcv["open"].astype(float).to_numpy()
    close_arr = ohlcv["close"].astype(float).to_numpy()
    buy_indices_all = np.where(triggers == 1)[0]
    trades: list[Trade] = []
    cur_exit_idx = -1
    for raw_idx in buy_indices_all:
        buy_idx = int(raw_idx)
        if buy_idx <= cur_exit_idx:
            continue  # 持仓中, 跳过
        if buy_idx < max_warmup or buy_idx >= n - 1:
            continue
        buy_price = open_arr[buy_idx + 1]
        if not (buy_price > 0) or np.isnan(buy_price):
            continue
        result = rule.find_exit(
            buy_idx=buy_idx, ohlcv=ohlcv, reverse_signal_array=reverse_array
        )
        exit_idx = result.exit_idx
        if exit_idx < buy_idx + 1:
            continue
        exit_price = close_arr[exit_idx]
        if not (exit_price > 0) or np.isnan(exit_price):
            continue
        return_pct = (exit_price / buy_price - 1.0) * 100.0
        trades.append(Trade(
            buy_idx=buy_idx,
            exit_idx=exit_idx,
            buy_price=float(buy_price),
            exit_price=float(exit_price),
            return_pct=float(return_pct),
            exit_reason=result.exit_reason,
        ))
        cur_exit_idx = exit_idx

    # === 重建 equity_curve / trade_log (区间内) ===
    in_range_idx = np.where(in_range_mask)[0]
    if len(in_range_idx) == 0:
        raise ValueError("区间内无交易日")
    range_start_idx = int(in_range_idx[0])
    range_end_idx = int(in_range_idx[-1])
    window_dates = ohlcv["date"].iloc[range_start_idx : range_end_idx + 1].reset_index(drop=True)
    window_close = ohlcv["close"].iloc[range_start_idx : range_end_idx + 1].to_numpy().astype(float)
    win_n = len(window_dates)

    # 把全局 idx 转窗口 idx
    def _to_win(idx: int) -> int:
        return idx - range_start_idx

    initial_capital = float(req.initial_capital)
    commission = float(req.commission)
    stamp_tax = float(req.stamp_tax)
    slippage = float(req.slippage)

    cash = initial_capital
    shares = 0
    entry_price_eff = 0.0  # 含费效成本价
    equity_arr = np.zeros(win_n, dtype=np.float64)

    # 按时序处理 trades, 把每笔的 buy/sell 转 trade_log
    trade_log: list[dict] = []
    # buy day in simulator = buy_idx + 1 (next-day open). simulator 已用 open_arr[buy_idx+1]
    # 排序: simulator 的 trades 已经按 buy_idx 升序
    sorted_trades = sorted(trades, key=lambda t: t.buy_idx)

    # 预存交易事件: (win_idx, action, price_raw, trade_obj)
    events: list[tuple[int, str, float, Any]] = []
    for t in sorted_trades:
        buy_win_idx = _to_win(t.buy_idx + 1)  # simulator 用 buy_idx+1 的 open 成交
        exit_win_idx = _to_win(t.exit_idx)
        if buy_win_idx < 0 or buy_win_idx >= win_n:
            continue
        if exit_win_idx < buy_win_idx:
            continue
        if exit_win_idx >= win_n:
            exit_win_idx = win_n - 1
        events.append((buy_win_idx, "BUY", t.buy_price, t))
        events.append((exit_win_idx, "SELL", t.exit_price, t))

    event_iter = iter(sorted(events, key=lambda e: (e[0], 0 if e[1] == "SELL" else 1)))
    next_event = next(event_iter, None)

    for i in range(win_n):
        # 处理当日所有事件 (先 SELL 后 BUY, 避免同日双跨)
        while next_event is not None and next_event[0] == i:
            win_idx, action, price_raw, trade_obj = next_event
            date_str = window_dates.iloc[i].strftime("%Y-%m-%d")
            if action == "BUY" and shares == 0 and price_raw > 0:
                exec_price = price_raw * (1.0 + slippage) * (1.0 + commission)
                lots = int(cash // exec_price)
                if lots >= 100:
                    buy_shares = (lots // 100) * 100
                    cost = buy_shares * exec_price
                    cash -= cost
                    shares = buy_shares
                    entry_price_eff = exec_price
                    trade_log.append({
                        "date": date_str,
                        "action": "BUY",
                        "price": round(float(exec_price), 4),
                        "shares": int(buy_shares),
                        "cash_flow": round(-float(cost), 2),
                        "pnl": None,
                        "reason": "signal",
                    })
            elif action == "SELL" and shares > 0 and price_raw > 0:
                exec_price = price_raw * (1.0 - slippage) * (1.0 - commission - stamp_tax)
                proceeds = shares * exec_price
                pnl = (exec_price - entry_price_eff) * shares
                trade_log.append({
                    "date": date_str,
                    "action": "SELL",
                    "price": round(float(exec_price), 4),
                    "shares": int(shares),
                    "cash_flow": round(float(proceeds), 2),
                    "pnl": round(float(pnl), 2),
                    "reason": getattr(trade_obj, "exit_reason", "signal") or "signal",
                })
                cash += proceeds
                shares = 0
                entry_price_eff = 0.0
            next_event = next(event_iter, None)

        equity_arr[i] = cash + shares * window_close[i]

    # === 算指标 ===
    equity_series = pd.Series(equity_arr, index=pd.DatetimeIndex(window_dates))

    # benchmark (沪深300)
    idx_daily = store.index_daily
    bench_curve = pd.Series(dtype=float)
    equity_curve_aligned = equity_series
    if idx_daily is not None and not idx_daily.empty:
        idx_window = idx_daily[
            (idx_daily["date"] >= window_dates.iloc[0])
            & (idx_daily["date"] <= window_dates.iloc[-1])
        ]
        if not idx_window.empty:
            bench_raw = pd.Series(
                idx_window["close"].astype(float).values,
                index=pd.DatetimeIndex(idx_window["date"].values),
            )
            union_idx = equity_series.index.union(bench_raw.index)
            eq_u = equity_series.reindex(union_idx).ffill()
            bc_u = bench_raw.reindex(union_idx).ffill()
            bench_start = float(bc_u.iloc[0])
            bench_curve = bc_u / bench_start * initial_capital
            equity_curve_aligned = eq_u

    tr = metrics.total_return(equity_curve_aligned)
    ar = metrics.annual_return(equity_curve_aligned)
    mdd = metrics.max_drawdown(equity_curve_aligned)
    sr = metrics.sharpe_ratio(equity_curve_aligned)
    ts_stats = metrics.trade_stats(trade_log)
    br = metrics.benchmark_return(bench_curve) if not bench_curve.empty else None

    warnings: list[str] = []
    if ts_stats["closed_trades"] < 5:
        warnings.append(f"总交易次数 {ts_stats['closed_trades']} 偏少，结果可能失真")

    equity_out = [
        {"date": d.strftime("%Y-%m-%d"), "value": round(float(v), 2)}
        for d, v in equity_curve_aligned.items()
    ]
    bench_out = [
        {"date": d.strftime("%Y-%m-%d"), "value": round(float(v), 2)}
        for d, v in bench_curve.items()
    ] if not bench_curve.empty else []

    # strategy_id / strategy_name 用 atom 组合描述
    atoms_layman = []
    for aid in req.buy_atoms:
        atoms_layman.append(ATOM_REGISTRY[aid].layman_name)
    strategy_id = "custom:" + "+".join(req.buy_atoms)
    strategy_name = "自定义策略: " + " 且 ".join(atoms_layman)

    rule_summary: dict[str, Any] = {}
    if req.holding_days is not None:
        rule_summary["holding_days"] = req.holding_days
    if req.stop_loss_pct is not None:
        rule_summary["stop_loss_pct"] = req.stop_loss_pct
    if req.take_profit_pct is not None:
        rule_summary["take_profit_pct"] = req.take_profit_pct
    if req.trailing_stop_pct is not None:
        rule_summary["trailing_stop_pct"] = req.trailing_stop_pct
    if req.sell_on_fixed_atom_id:
        rule_summary["sell_on_fixed_atom_id"] = req.sell_on_fixed_atom_id
    if req.sell_on_reverse_signal:
        rule_summary["sell_on_reverse_signal"] = True
    if req.hold_until:
        rule_summary["hold_until"] = req.hold_until

    return {
        "stock_code": code,
        "stock_name": stock_name,
        "strategy_id": strategy_id,
        "strategy_name": strategy_name,
        "params": {"buy_atoms": req.buy_atoms, "rule": rule_summary},
        "start_date": window_dates.iloc[0].strftime("%Y-%m-%d"),
        "end_date": window_dates.iloc[-1].strftime("%Y-%m-%d"),
        "initial_capital": initial_capital,
        "total_return": float(tr),
        "annual_return": float(ar),
        "max_drawdown": float(mdd),
        "sharpe_ratio": sr,
        "win_rate": ts_stats["win_rate"],
        "profit_loss_ratio": ts_stats["profit_loss_ratio"],
        "total_trades": ts_stats["closed_trades"],
        "avg_holding_days": ts_stats["avg_holding_days"],
        "benchmark_return": br,
        "equity_curve": equity_out,
        "benchmark_curve": bench_out,
        "trade_log": trade_log,
        "warnings": warnings,
    }
