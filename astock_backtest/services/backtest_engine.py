"""A股回测引擎 — v1.1 规范

核心规则:
1. 信号与成交分离: 当日 close 生成信号 → 次日 open 成交
2. 止损止盈: 当日 close 近似触发 (v1, 未来升级用 low/high + 目标价)
3. T+1: 当日买入次日才能卖
4. 涨跌停板: 买入遇涨停作废, 卖出遇跌停延后
5. 同日双信号: 止损触发 → 跳过本日所有建仓
6. Benchmark: 沪深300, 按交易日 union + 前值填充
7. 单股 100% 满仓
"""
from typing import Any, Optional

import pandas as pd

from astock_backtest.data_loader import DataStore
from astock_backtest.services import metrics
from astock_backtest.services.board_limits import is_limit_down, is_limit_up
from astock_backtest.strategies import get_strategy
from astock_backtest.strategies.base import Signal


def _parse_date(s: Optional[str]) -> Optional[pd.Timestamp]:
    if not s:
        return None
    s = s.replace("-", "")
    return pd.Timestamp(f"{s[:4]}-{s[4:6]}-{s[6:8]}")


def _apply_cost(price: float, is_buy: bool, commission: float, stamp_tax: float, slippage: float) -> float:
    """成交价 × (1 + 滑点 + 手续费)(买) 或 × (1 - 滑点 - 手续费 - 印花税)(卖)"""
    if is_buy:
        return price * (1.0 + slippage) * (1.0 + commission)
    return price * (1.0 - slippage) * (1.0 - commission - stamp_tax)


def run_backtest(
    store: DataStore,
    stock_code: str,
    strategy_id: str,
    params: dict[str, Any],
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    initial_capital: float = 100000.0,
    commission: float = 0.0003,
    stamp_tax: float = 0.001,
    slippage: float = 0.001,
    stop_loss: Optional[float] = None,
    stop_profit: Optional[float] = None,
) -> dict:
    if stock_code not in store.code_name_map:
        raise ValueError(f"stock_code {stock_code} not found")

    stock_name = store.code_name_map[stock_code]
    ohlcv = store.get_ohlcv(stock_code)
    if ohlcv.empty:
        raise ValueError(f"no ohlcv data for {stock_code}")

    start_ts = _parse_date(start_date) or ohlcv["date"].min()
    end_ts = _parse_date(end_date) or ohlcv["date"].max()

    strategy = get_strategy(strategy_id, params)
    strategy.stock_code = stock_code
    warmup = strategy.min_warmup()

    all_ohlcv = ohlcv.reset_index(drop=True)
    window = all_ohlcv[(all_ohlcv["date"] >= start_ts) & (all_ohlcv["date"] <= end_ts)].reset_index(drop=True)
    if window.empty:
        raise ValueError("no trading days in range")

    cash = float(initial_capital)
    shares = 0
    entry_price: Optional[float] = None
    bought_today = False
    bought_yesterday = False
    pending_signal: Optional[Signal] = None

    equity_dates: list[pd.Timestamp] = []
    equity_values: list[float] = []
    trade_log: list[dict] = []
    warnings: list[str] = []

    for i in range(len(window)):
        row = window.iloc[i]
        day = row["date"]
        open_p = float(row["open"])
        close_p = float(row["close"])
        pct_change = row.get("pct_change")
        if pd.notna(pct_change):
            pct_change = float(pct_change)
        else:
            pct_change = 0.0

        bought_today = False

        # === 1. 执行昨日 pending 信号 (次日 open 成交) ===
        if pending_signal == Signal.BUY and shares == 0:
            if is_limit_up(pct_change, stock_code):
                warnings.append(f"{day.strftime('%Y-%m-%d')} 涨停无法买入，信号作废")
                pending_signal = None
            else:
                exec_price = _apply_cost(open_p, True, commission, stamp_tax, slippage)
                buy_shares = int(cash // exec_price)
                if buy_shares >= 100:
                    buy_shares = (buy_shares // 100) * 100
                    cost = buy_shares * exec_price
                    cash -= cost
                    shares = buy_shares
                    entry_price = exec_price
                    bought_today = True
                    trade_log.append({
                        "date": day.strftime("%Y-%m-%d"),
                        "action": "BUY",
                        "price": round(exec_price, 4),
                        "shares": buy_shares,
                        "cash_flow": round(-cost, 2),
                        "pnl": None,
                        "reason": "signal",
                    })
                pending_signal = None
        elif pending_signal == Signal.SELL and shares > 0 and not bought_yesterday:
            if is_limit_down(pct_change, stock_code):
                warnings.append(f"{day.strftime('%Y-%m-%d')} 跌停无法卖出，信号延后")
                # 保持 pending_signal = SELL
            else:
                exec_price = _apply_cost(open_p, False, commission, stamp_tax, slippage)
                proceeds = shares * exec_price
                pnl = (exec_price - entry_price) * shares if entry_price else 0.0
                trade_log.append({
                    "date": day.strftime("%Y-%m-%d"),
                    "action": "SELL",
                    "price": round(exec_price, 4),
                    "shares": shares,
                    "cash_flow": round(proceeds, 2),
                    "pnl": round(pnl, 2),
                    "reason": "signal",
                })
                cash += proceeds
                shares = 0
                entry_price = None
                pending_signal = None

        stop_triggered_today = False

        # === 2. 止损止盈 (用当日 close 近似, T+1 约束)
        # TODO v2: 用 low 判止损触发 / high 判止盈触发, 成交价=目标价
        if shares > 0 and not bought_today and entry_price is not None:
            cur_pnl_pct = (close_p - entry_price) / entry_price
            sl_hit = stop_loss is not None and cur_pnl_pct <= stop_loss
            sp_hit = stop_profit is not None and cur_pnl_pct >= stop_profit
            if (sl_hit or sp_hit) and not is_limit_down(pct_change, stock_code):
                exec_price = _apply_cost(close_p, False, commission, stamp_tax, slippage)
                proceeds = shares * exec_price
                pnl = (exec_price - entry_price) * shares
                reason = "stop_loss" if sl_hit else "stop_profit"
                trade_log.append({
                    "date": day.strftime("%Y-%m-%d"),
                    "action": "SELL",
                    "price": round(exec_price, 4),
                    "shares": shares,
                    "cash_flow": round(proceeds, 2),
                    "pnl": round(pnl, 2),
                    "reason": reason,
                })
                cash += proceeds
                shares = 0
                entry_price = None
                pending_signal = None  # 止损后清 pending (同日双信号规则)
                stop_triggered_today = True

        # === 3. 当日 close 生成信号 (下一日 open 执行) ===
        if not stop_triggered_today:
            history_idx_end = all_ohlcv[all_ohlcv["date"] == day].index
            if len(history_idx_end) > 0:
                end_idx = int(history_idx_end[0]) + 1
                history = all_ohlcv.iloc[:end_idx]
                if len(history) >= warmup:
                    sig = strategy.generate_signal(history)
                    if sig == Signal.BUY and shares == 0:
                        pending_signal = Signal.BUY
                    elif sig == Signal.SELL and shares > 0 and not bought_today:
                        pending_signal = Signal.SELL

        # === 4. 当日结算 equity (用当日 close 估值持仓) ===
        equity = cash + shares * close_p
        equity_dates.append(day)
        equity_values.append(equity)

        bought_yesterday = bought_today

    equity_series = pd.Series(equity_values, index=pd.DatetimeIndex(equity_dates))

    # === benchmark 对齐 ===
    idx = store.index_daily
    if not idx.empty:
        idx_window = idx[(idx["date"] >= equity_dates[0]) & (idx["date"] <= equity_dates[-1])]
        if not idx_window.empty:
            bench_series = pd.Series(
                idx_window["close"].astype(float).values,
                index=pd.DatetimeIndex(idx_window["date"].values),
            )
            union_idx = equity_series.index.union(bench_series.index)
            equity_series_u = equity_series.reindex(union_idx).ffill()
            bench_series_u = bench_series.reindex(union_idx).ffill()
            bench_start = float(bench_series_u.iloc[0])
            bench_curve = bench_series_u / bench_start * float(initial_capital)
            equity_curve_aligned = equity_series_u
        else:
            bench_curve = pd.Series(dtype=float)
            equity_curve_aligned = equity_series
    else:
        bench_curve = pd.Series(dtype=float)
        equity_curve_aligned = equity_series

    # === 指标 ===
    tr = metrics.total_return(equity_curve_aligned)
    ar = metrics.annual_return(equity_curve_aligned)
    mdd = metrics.max_drawdown(equity_curve_aligned)
    sr = metrics.sharpe_ratio(equity_curve_aligned)
    ts = metrics.trade_stats(trade_log)
    br = metrics.benchmark_return(bench_curve) if not bench_curve.empty else None

    if ts["closed_trades"] < 5:
        warnings.append(f"总交易次数 {ts['closed_trades']} 偏少，结果可能失真")

    equity_curve_out = [
        {"date": d.strftime("%Y-%m-%d"), "value": round(float(v), 2)}
        for d, v in equity_curve_aligned.items()
    ]
    bench_curve_out = [
        {"date": d.strftime("%Y-%m-%d"), "value": round(float(v), 2)}
        for d, v in bench_curve.items()
    ] if not bench_curve.empty else []

    return {
        "stock_code": stock_code,
        "stock_name": stock_name,
        "strategy_id": strategy_id,
        "strategy_name": strategy.display_name,
        "params": strategy.params,
        "start_date": equity_dates[0].strftime("%Y-%m-%d"),
        "end_date": equity_dates[-1].strftime("%Y-%m-%d"),
        "initial_capital": float(initial_capital),
        "total_return": float(tr),
        "annual_return": float(ar),
        "max_drawdown": float(mdd),
        "sharpe_ratio": sr,
        "win_rate": ts["win_rate"],
        "profit_loss_ratio": ts["profit_loss_ratio"],
        "total_trades": ts["closed_trades"],
        "avg_holding_days": ts["avg_holding_days"],
        "benchmark_return": br,
        "equity_curve": equity_curve_out,
        "benchmark_curve": bench_curve_out,
        "trade_log": trade_log,
        "warnings": warnings,
    }


# ── Digest 转换 (合规: 不返回 raw 时序+不返回单笔股价) ──

def _normalize_curve_monthly(curve: list[dict], max_points: int = 60) -> list[dict]:
    """[{date,value}] 按月末取样 + 起点归一为 100, 上限 max_points 点."""
    if not curve:
        return []
    df = pd.DataFrame(curve)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")
    df["ym"] = df["date"].dt.to_period("M")
    monthly = df.groupby("ym", as_index=False).last()[["date", "value"]]
    if monthly.empty:
        return []
    if len(monthly) > max_points:
        import numpy as _np
        idx = _np.linspace(0, len(monthly) - 1, max_points).astype(int)
        monthly = monthly.iloc[idx]
    base = float(monthly.iloc[0]["value"])
    if base <= 0:
        return []
    return [
        {"date": row.date.strftime("%Y-%m-%d"),
         "value": round(float(row.value) / base * 100.0, 2)}
        for row in monthly.itertuples(index=False)
    ]


def _summarize_trades(trade_log: list[dict]) -> dict:
    """trade_log -> 统计摘要 (无具体日期/股价/股数)."""
    if not trade_log:
        return {
            "total_buys": 0, "total_sells": 0,
            "profitable_trades": 0, "losing_trades": 0,
            "avg_holding_days": None, "max_holding_days": None,
            "best_trade_pct": None, "worst_trade_pct": None,
            "active_months": 0,
            "stop_loss_count": 0, "stop_profit_count": 0,
        }
    buys = [t for t in trade_log if t.get("action") == "BUY"]
    sells = [t for t in trade_log if t.get("action") == "SELL"]
    sl = sum(1 for t in sells if t.get("reason") == "stop_loss")
    sp = sum(1 for t in sells if t.get("reason") == "stop_profit")

    # 配对 BUY-SELL 算单笔收益% + 持仓天数
    holding_days = []
    pct_returns = []
    pairs = min(len(buys), len(sells))
    for i in range(pairs):
        b, s = buys[i], sells[i]
        try:
            bp = float(b["price"]); sp_ = float(s["price"])
            if bp > 0:
                pct_returns.append((sp_ - bp) / bp)
            d_b = pd.to_datetime(b["date"])
            d_s = pd.to_datetime(s["date"])
            holding_days.append((d_s - d_b).days)
        except Exception:
            pass

    profitable = sum(1 for r in pct_returns if r > 0)
    losing = sum(1 for r in pct_returns if r < 0)

    months_set = {pd.to_datetime(t["date"]).strftime("%Y-%m") for t in trade_log if t.get("date")}

    return {
        "total_buys": len(buys),
        "total_sells": len(sells),
        "profitable_trades": profitable,
        "losing_trades": losing,
        "avg_holding_days": round(sum(holding_days) / len(holding_days), 1) if holding_days else None,
        "max_holding_days": int(max(holding_days)) if holding_days else None,
        "best_trade_pct": round(max(pct_returns), 4) if pct_returns else None,
        "worst_trade_pct": round(min(pct_returns), 4) if pct_returns else None,
        "active_months": len(months_set),
        "stop_loss_count": sl,
        "stop_profit_count": sp,
    }


def to_digest_backtest(full: dict) -> dict:
    """Convert full BacktestResponse dict → digest dict.

    Drops: sharpe_ratio (合规), trade_log (含具体股价), equity_curve/benchmark_curve 每日点
    Adds:  equity_curve_normalized (月度+归一), benchmark_curve_normalized, trade_summary
    """
    return {
        "stock_code": full["stock_code"],
        "stock_name": full["stock_name"],
        "strategy_id": full["strategy_id"],
        "strategy_name": full["strategy_name"],
        "params": full.get("params") or {},
        "start_date": full["start_date"],
        "end_date": full["end_date"],
        "total_return": full["total_return"],
        "annual_return": full["annual_return"],
        "max_drawdown": full["max_drawdown"],
        "win_rate": full.get("win_rate"),
        "profit_loss_ratio": full.get("profit_loss_ratio"),
        "total_trades": full.get("total_trades", 0),
        "avg_holding_days": full.get("avg_holding_days"),
        "benchmark_return": full.get("benchmark_return"),
        "equity_curve_normalized": _normalize_curve_monthly(full.get("equity_curve") or []),
        "benchmark_curve_normalized": _normalize_curve_monthly(full.get("benchmark_curve") or []),
        "trade_summary": _summarize_trades(full.get("trade_log") or []),
        "warnings": full.get("warnings") or [],
    }
