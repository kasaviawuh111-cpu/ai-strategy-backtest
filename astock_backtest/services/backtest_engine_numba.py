"""A股回测引擎 — Numba 加速版 (Phase 2 v0.4 改造)

跟 backtest_engine.py 行为完全等价 (回归对账要求 bit-perfect),
只是核心循环用 Numba JIT 编译, 单股从 ~0.5s 降至 ~0.3ms 量级.

原引擎 6 类规则全部保留:
1. 信号与成交分离: 当日 close 生成信号 → 次日 open 成交
2. 止损止盈: 当日 close 近似触发
3. T+1: 当日买入次日才能卖
4. 涨跌停板: 买入遇涨停作废, 卖出遇跌停延后
5. 同日双信号: 止损触发 → 跳过本日所有建仓 (清 pending)
6. Benchmark: 沪深300, 按交易日 union + 前值填充
7. 单股 100% 满仓
"""
import math
from typing import Any, Optional

import numba
import numpy as np
import pandas as pd

from astock_backtest.data_loader import DataStore
from astock_backtest.services import metrics
from astock_backtest.services.board_limits import limit_threshold
from astock_backtest.strategies import get_strategy
from astock_backtest.strategies.base import SIG_BUY_INT, SIG_HOLD_INT, SIG_SELL_INT

# 交易原因编码
REASON_SIGNAL = 0
REASON_STOP_LOSS = 1
REASON_STOP_PROFIT = 2

# 警告类型
WARN_LIMIT_UP = 1
WARN_LIMIT_DOWN = 2


def _parse_date(s: Optional[str]) -> Optional[pd.Timestamp]:
    if not s:
        return None
    s = s.replace("-", "")
    return pd.Timestamp(f"{s[:4]}-{s[4:6]}-{s[6:8]}")


@numba.njit(nogil=True, cache=True)
def _run_backtest_loop_numba(
    open_arr,            # float64[n]
    close_arr,           # float64[n]
    pct_arr,             # float64[n], NaN→0 已处理
    signals,             # int8[n], 0=HOLD/1=BUY/2=SELL
    initial_capital,     # float
    commission,          # float
    stamp_tax,           # float
    slippage,            # float
    stop_loss,           # float, NaN 表示无
    stop_profit,         # float, NaN 表示无
    limit_thresh,        # float, 涨跌停阈值 (例 9.8 / 19.6)
):
    n = len(close_arr)
    max_trades = n * 2  # 最坏每天交易一次

    trade_indices = np.zeros(max_trades, dtype=np.int64)
    trade_actions = np.zeros(max_trades, dtype=np.int8)
    trade_prices = np.zeros(max_trades, dtype=np.float64)
    trade_shares = np.zeros(max_trades, dtype=np.int64)
    trade_cash_flows = np.zeros(max_trades, dtype=np.float64)
    trade_pnls = np.zeros(max_trades, dtype=np.float64)
    trade_pnl_valid = np.zeros(max_trades, dtype=np.int8)  # 0 = pnl is None, 1 = 有效
    trade_reasons = np.zeros(max_trades, dtype=np.int8)
    trade_count = 0

    warning_indices = np.zeros(max_trades, dtype=np.int64)
    warning_types = np.zeros(max_trades, dtype=np.int8)
    warning_count = 0

    equity_arr = np.zeros(n, dtype=np.float64)

    cash = initial_capital
    shares = 0
    entry_price = 0.0
    has_position = False
    bought_today = False
    bought_yesterday = False
    pending_signal = SIG_HOLD_INT

    has_sl = not math.isnan(stop_loss)
    has_sp = not math.isnan(stop_profit)

    for i in range(n):
        open_p = open_arr[i]
        close_p = close_arr[i]
        pct = pct_arr[i]
        is_lu = pct >= limit_thresh
        is_ld = pct <= -limit_thresh

        bought_today = False

        # === 1. 执行 pending 信号 (次日 open 成交) ===
        if pending_signal == SIG_BUY_INT and shares == 0:
            if is_lu:
                warning_indices[warning_count] = i
                warning_types[warning_count] = WARN_LIMIT_UP
                warning_count += 1
                pending_signal = SIG_HOLD_INT
            else:
                exec_price = open_p * (1.0 + slippage) * (1.0 + commission)
                buy_shares_int = int(cash // exec_price)
                if buy_shares_int >= 100:
                    buy_shares_int = (buy_shares_int // 100) * 100
                    cost = buy_shares_int * exec_price
                    cash -= cost
                    shares = buy_shares_int
                    entry_price = exec_price
                    has_position = True
                    bought_today = True
                    trade_indices[trade_count] = i
                    trade_actions[trade_count] = SIG_BUY_INT
                    trade_prices[trade_count] = exec_price
                    trade_shares[trade_count] = buy_shares_int
                    trade_cash_flows[trade_count] = -cost
                    trade_pnl_valid[trade_count] = 0  # BUY pnl=None
                    trade_reasons[trade_count] = REASON_SIGNAL
                    trade_count += 1
                pending_signal = SIG_HOLD_INT
        elif pending_signal == SIG_SELL_INT and shares > 0 and not bought_yesterday:
            if is_ld:
                warning_indices[warning_count] = i
                warning_types[warning_count] = WARN_LIMIT_DOWN
                warning_count += 1
                # 保持 pending = SELL
            else:
                exec_price = open_p * (1.0 - slippage) * (1.0 - commission - stamp_tax)
                proceeds = shares * exec_price
                pnl = (exec_price - entry_price) * shares if has_position else 0.0
                trade_indices[trade_count] = i
                trade_actions[trade_count] = SIG_SELL_INT
                trade_prices[trade_count] = exec_price
                trade_shares[trade_count] = shares
                trade_cash_flows[trade_count] = proceeds
                trade_pnls[trade_count] = pnl
                trade_pnl_valid[trade_count] = 1
                trade_reasons[trade_count] = REASON_SIGNAL
                trade_count += 1
                cash += proceeds
                shares = 0
                entry_price = 0.0
                has_position = False
                pending_signal = SIG_HOLD_INT

        stop_triggered_today = False

        # === 2. 止损止盈 (当日 close 近似触发, T+1 约束) ===
        if shares > 0 and not bought_today and has_position:
            cur_pnl_pct = (close_p - entry_price) / entry_price
            sl_hit = has_sl and cur_pnl_pct <= stop_loss
            sp_hit = has_sp and cur_pnl_pct >= stop_profit
            if (sl_hit or sp_hit) and not is_ld:
                exec_price = close_p * (1.0 - slippage) * (1.0 - commission - stamp_tax)
                proceeds = shares * exec_price
                pnl = (exec_price - entry_price) * shares
                trade_indices[trade_count] = i
                trade_actions[trade_count] = SIG_SELL_INT
                trade_prices[trade_count] = exec_price
                trade_shares[trade_count] = shares
                trade_cash_flows[trade_count] = proceeds
                trade_pnls[trade_count] = pnl
                trade_pnl_valid[trade_count] = 1
                trade_reasons[trade_count] = REASON_STOP_LOSS if sl_hit else REASON_STOP_PROFIT
                trade_count += 1
                cash += proceeds
                shares = 0
                entry_price = 0.0
                has_position = False
                pending_signal = SIG_HOLD_INT  # 止损后清 pending
                stop_triggered_today = True

        # === 3. 当日 close 生成信号 (从 precompute 数组取) ===
        if not stop_triggered_today:
            sig = signals[i]
            if sig == SIG_BUY_INT and shares == 0:
                pending_signal = SIG_BUY_INT
            elif sig == SIG_SELL_INT and shares > 0 and not bought_today:
                pending_signal = SIG_SELL_INT

        # === 4. 当日结算 equity ===
        equity_arr[i] = cash + shares * close_p

        bought_yesterday = bought_today

    return (
        equity_arr,
        trade_indices[:trade_count],
        trade_actions[:trade_count],
        trade_prices[:trade_count],
        trade_shares[:trade_count],
        trade_cash_flows[:trade_count],
        trade_pnls[:trade_count],
        trade_pnl_valid[:trade_count],
        trade_reasons[:trade_count],
        warning_indices[:warning_count],
        warning_types[:warning_count],
    )


def run_backtest_numba(
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
    """Numba 加速版, 输入输出跟老 run_backtest 完全一致."""
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

    all_ohlcv = ohlcv.reset_index(drop=True)

    window_mask = (all_ohlcv["date"] >= start_ts) & (all_ohlcv["date"] <= end_ts)
    window_indices = np.where(window_mask)[0]
    if len(window_indices) == 0:
        raise ValueError("no trading days in range")
    start_idx = int(window_indices[0])
    end_idx = int(window_indices[-1]) + 1
    n = end_idx - start_idx

    window = all_ohlcv.iloc[start_idx:end_idx].reset_index(drop=True)
    open_arr = window["open"].to_numpy().astype(np.float64)
    close_arr = window["close"].to_numpy().astype(np.float64)
    if "pct_change" in window.columns:
        pct_arr = window["pct_change"].fillna(0.0).to_numpy().astype(np.float64)
    else:
        pct_arr = np.zeros(n, dtype=np.float64)

    # 预计算信号 (策略级, 走 precompute_signals 接口)
    signals = strategy.precompute_signals(all_ohlcv, start_idx, end_idx)
    if signals.shape[0] != n:
        raise RuntimeError(
            f"precompute_signals returned shape {signals.shape}, expected ({n},)"
        )
    if signals.dtype != np.int8:
        signals = signals.astype(np.int8)

    # 统一 warmup mask: 跟老引擎 `if len(history) >= warmup` 一致
    # len(history) = global_idx + 1, warmup 内 (即 global_idx + 1 < warmup) 强制 HOLD
    warmup = strategy.min_warmup()
    if warmup > 0:
        warmup_local_end = max(0, min(n, warmup - 1 - start_idx))
        if warmup_local_end > 0:
            signals[:warmup_local_end] = 0

    limit_thresh = limit_threshold(stock_code)
    sl = float(stop_loss) if stop_loss is not None else float("nan")
    sp = float(stop_profit) if stop_profit is not None else float("nan")

    (
        equity_arr,
        ti, ta, tp_, ts_, tcf, tpn, tpv, tre,
        wi, wt,
    ) = _run_backtest_loop_numba(
        open_arr, close_arr, pct_arr, signals,
        float(initial_capital), float(commission), float(stamp_tax), float(slippage),
        sl, sp, float(limit_thresh),
    )

    # === 还原成 dict 输出 (字段跟老版完全一致) ===
    equity_dates = window["date"].tolist()
    equity_series = pd.Series(equity_arr, index=pd.DatetimeIndex(equity_dates))

    trade_log: list[dict] = []
    for k in range(len(ti)):
        idx = int(ti[k])
        action = "BUY" if ta[k] == SIG_BUY_INT else "SELL"
        reason_int = int(tre[k])
        if reason_int == REASON_STOP_LOSS:
            reason = "stop_loss"
        elif reason_int == REASON_STOP_PROFIT:
            reason = "stop_profit"
        else:
            reason = "signal"
        pnl_value = None if tpv[k] == 0 else round(float(tpn[k]), 2)
        trade_log.append({
            "date": equity_dates[idx].strftime("%Y-%m-%d"),
            "action": action,
            "price": round(float(tp_[k]), 4),
            "shares": int(ts_[k]),
            "cash_flow": round(float(tcf[k]), 2),
            "pnl": pnl_value,
            "reason": reason,
        })

    warnings: list[str] = []
    for k in range(len(wi)):
        idx = int(wi[k])
        date_str = equity_dates[idx].strftime("%Y-%m-%d")
        if int(wt[k]) == WARN_LIMIT_UP:
            warnings.append(f"{date_str} 涨停无法买入，信号作废")
        else:
            warnings.append(f"{date_str} 跌停无法卖出，信号延后")

    # === benchmark 对齐 (复刻老版逻辑) ===
    idx_daily = store.index_daily
    if not idx_daily.empty:
        idx_window = idx_daily[
            (idx_daily["date"] >= equity_dates[0])
            & (idx_daily["date"] <= equity_dates[-1])
        ]
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

    tr = metrics.total_return(equity_curve_aligned)
    ar = metrics.annual_return(equity_curve_aligned)
    mdd = metrics.max_drawdown(equity_curve_aligned)
    sr = metrics.sharpe_ratio(equity_curve_aligned)
    ts_metrics = metrics.trade_stats(trade_log)
    br = metrics.benchmark_return(bench_curve) if not bench_curve.empty else None

    if ts_metrics["closed_trades"] < 5:
        warnings.append(f"总交易次数 {ts_metrics['closed_trades']} 偏少，结果可能失真")

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
        "win_rate": ts_metrics["win_rate"],
        "profit_loss_ratio": ts_metrics["profit_loss_ratio"],
        "total_trades": ts_metrics["closed_trades"],
        "avg_holding_days": ts_metrics["avg_holding_days"],
        "benchmark_return": br,
        "equity_curve": equity_curve_out,
        "benchmark_curve": bench_curve_out,
        "trade_log": trade_log,
        "warnings": warnings,
    }
