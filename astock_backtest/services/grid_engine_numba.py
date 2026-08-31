"""网格交易引擎 — Numba 加速版 (Phase 2 v0.4 改造)

跟 grid_engine.py 行为完全等价 (回归对账要求 bit-perfect),
只是核心循环 + 底仓建仓用 Numba JIT, slot 状态用平行 numpy 数组替代 dataclass.

跟 grid_engine.py 一致的 8 类规则:
1. N+1 根格线, N 个槽 (slot[i] = 买在 line_i, 卖在 line_{i+1})
2. 底仓建仓 (Day 1 按 first_open 填 slot[first_g..0], entry_date_idx = -1 不触发 T+1)
3. 触发: 每日 close 所在格位变化, 算穿越线
   - DOWN cross 线 L (L in [0, N-1]): 买 slot[L]
   - UP cross 线 L (L in [1, N]): 卖 slot[L-1]
4. 单格穿越: 用格线价; 跳空多格: 用 close 近似 + warn (只记第一次)
5. T+1: slot.entry_date_idx == i 则当日不卖
6. 三段式费率 (commission + stamp_tax + slippage)
7. 组合级止损: total_value/initial - 1 <= stop_loss → 全平 cash 横线延伸
8. 涨跌停阈值走 board_limits.limit_threshold (传入内核作为标量)
"""
import math
from typing import Optional

import numba
import numpy as np
import pandas as pd

from astock_backtest.data_loader import DataStore
from astock_backtest.services import metrics
from astock_backtest.services.board_limits import limit_threshold

# action 编码
ACTION_BUY = 0
ACTION_SELL = 1

# reason 编码
REASON_SIGNAL = 0
REASON_BASE = 1
REASON_STOP_LOSS = 2


def _parse_date(s: Optional[str]) -> Optional[pd.Timestamp]:
    if not s:
        return None
    s = s.replace("-", "")
    return pd.Timestamp(f"{s[:4]}-{s[4:6]}-{s[6:8]}")


@numba.njit(nogil=True, cache=True)
def _run_grid_loop_numba(
    open_arr,            # float64[n]
    close_arr,           # float64[n]
    pct_arr,             # float64[n], NaN→0 已处理
    initial_capital,     # float
    commission,          # float
    stamp_tax,           # float
    slippage,            # float
    stop_loss,           # float, NaN 表示无
    base_position_ratio, # float
    price_lower,         # float
    grid_width,          # float
    grid_count,          # int
    limit_thresh,        # float, 涨跌停阈值
):
    n = len(close_arr)
    has_stop_loss = not math.isnan(stop_loss)
    per_grid_capital = initial_capital / grid_count

    # slot 平行数组
    slot_shares = np.zeros(grid_count, dtype=np.int64)
    slot_entry_price = np.zeros(grid_count, dtype=np.float64)
    slot_entry_date_idx = np.full(grid_count, -1, dtype=np.int64)  # -1 = None (底仓或空)

    cash = initial_capital
    stopped = False
    stopped_at_idx = -1

    # 预分配 trade arrays
    max_trades = n * grid_count + grid_count
    trade_day_idx = np.zeros(max_trades, dtype=np.int64)
    trade_action = np.zeros(max_trades, dtype=np.int8)
    trade_price = np.zeros(max_trades, dtype=np.float64)
    trade_shares = np.zeros(max_trades, dtype=np.int64)
    trade_cash_flow = np.zeros(max_trades, dtype=np.float64)
    trade_pnl = np.zeros(max_trades, dtype=np.float64)
    trade_pnl_valid = np.zeros(max_trades, dtype=np.int8)
    trade_reason = np.zeros(max_trades, dtype=np.int8)
    trade_grid_index = np.zeros(max_trades, dtype=np.int64)
    trade_count = 0

    equity_arr = np.zeros(n, dtype=np.float64)

    # 统计
    out_above_days = 0
    out_below_days = 0
    gap_trade_count = 0
    gap_first_day_idx = -1
    gap_first_pct = 0.0
    gap_first_n_lines = 0

    # === 计算初始格位 ===
    first_open = open_arr[0]
    upper_bound = price_lower + grid_count * grid_width
    if first_open <= price_lower:
        prev_grid_index = -1
    elif first_open >= upper_bound:
        prev_grid_index = grid_count
    else:
        prev_grid_index = int((first_open - price_lower) / grid_width)

    # === 底仓建仓 (Day 1, entry_date_idx=-1) ===
    if base_position_ratio > 0:
        max_base_cash = initial_capital * base_position_ratio
        running_base_cost = 0.0
        clamped_g = max(-1, min(prev_grid_index, grid_count - 1))
        slot_idx = clamped_g
        while slot_idx >= 0:
            line_price = price_lower + slot_idx * grid_width
            phantom_buy_price = line_price * (1.0 + slippage) * (1.0 + commission)
            affordable = int(per_grid_capital // phantom_buy_price)
            affordable = (affordable // 100) * 100
            required_cash = affordable * phantom_buy_price
            if affordable < 100 or cash < required_cash:
                break
            if running_base_cost + required_cash > max_base_cash:
                break
            cash -= required_cash
            running_base_cost += required_cash
            slot_shares[slot_idx] = affordable
            slot_entry_price[slot_idx] = phantom_buy_price
            slot_entry_date_idx[slot_idx] = -1  # 不触发 T+1
            trade_day_idx[trade_count] = 0
            trade_action[trade_count] = ACTION_BUY
            trade_price[trade_count] = phantom_buy_price
            trade_shares[trade_count] = affordable
            trade_cash_flow[trade_count] = -required_cash
            trade_pnl_valid[trade_count] = 0  # None
            trade_reason[trade_count] = REASON_BASE
            trade_grid_index[trade_count] = slot_idx
            trade_count += 1
            slot_idx -= 1

    # === 主循环 ===
    for i in range(n):
        close_p = close_arr[i]
        pct = pct_arr[i]

        # 0. 组合级止损
        if has_stop_loss and not stopped:
            total_value = cash
            for j in range(grid_count):
                if slot_shares[j] > 0:
                    total_value += slot_shares[j] * close_p
            if (total_value - initial_capital) / initial_capital <= stop_loss:
                for j in range(grid_count):
                    if slot_shares[j] > 0:
                        sell_price = close_p * (1.0 - slippage) * (1.0 - commission - stamp_tax)
                        revenue = slot_shares[j] * sell_price
                        pnl = (sell_price - slot_entry_price[j]) * slot_shares[j]
                        cash += revenue
                        trade_day_idx[trade_count] = i
                        trade_action[trade_count] = ACTION_SELL
                        trade_price[trade_count] = sell_price
                        trade_shares[trade_count] = slot_shares[j]
                        trade_cash_flow[trade_count] = revenue
                        trade_pnl[trade_count] = pnl
                        trade_pnl_valid[trade_count] = 1
                        trade_reason[trade_count] = REASON_STOP_LOSS
                        trade_grid_index[trade_count] = j
                        trade_count += 1
                        slot_shares[j] = 0
                        slot_entry_price[j] = 0.0
                        slot_entry_date_idx[j] = -1
                stopped = True
                stopped_at_idx = i

        if stopped:
            equity_arr[i] = cash
            continue

        # 1. 当日格位
        if close_p <= price_lower:
            curr_idx = -1
            out_below_days += 1
        elif close_p >= upper_bound:
            curr_idx = grid_count
            out_above_days += 1
        else:
            curr_idx = int((close_p - price_lower) / grid_width)

        # 2. 穿越的格线
        if curr_idx != prev_grid_index:
            if curr_idx < prev_grid_index:
                n_crossed = prev_grid_index - curr_idx
                direction_down = True
            else:
                n_crossed = curr_idx - prev_grid_index
                direction_down = False

            is_gap = n_crossed > 1
            if is_gap and gap_first_day_idx < 0:
                gap_first_day_idx = i
                gap_first_pct = pct
                gap_first_n_lines = n_crossed

            # 处理每条穿越线
            for k in range(n_crossed):
                if direction_down:
                    line_idx = prev_grid_index - k
                else:
                    line_idx = prev_grid_index + 1 + k

                line_price = price_lower + line_idx * grid_width
                raw_price = close_p if is_gap else line_price

                if direction_down:
                    if line_idx < 0 or line_idx > grid_count - 1:
                        continue
                    if slot_shares[line_idx] != 0:
                        continue
                    buy_price = raw_price * (1.0 + slippage) * (1.0 + commission)
                    affordable = int(per_grid_capital // buy_price)
                    affordable = (affordable // 100) * 100
                    required_cash = affordable * buy_price
                    if affordable < 100 or cash < required_cash:
                        break  # 跳空多格现金不够, break 放弃更远的格
                    cash -= required_cash
                    slot_shares[line_idx] = affordable
                    slot_entry_price[line_idx] = buy_price
                    slot_entry_date_idx[line_idx] = i
                    trade_day_idx[trade_count] = i
                    trade_action[trade_count] = ACTION_BUY
                    trade_price[trade_count] = buy_price
                    trade_shares[trade_count] = affordable
                    trade_cash_flow[trade_count] = -required_cash
                    trade_pnl_valid[trade_count] = 0  # None
                    trade_reason[trade_count] = REASON_SIGNAL
                    trade_grid_index[trade_count] = line_idx
                    trade_count += 1
                    if is_gap:
                        gap_trade_count += 1
                else:  # UP
                    if line_idx < 1 or line_idx > grid_count:
                        continue
                    sl_idx = line_idx - 1
                    if slot_shares[sl_idx] == 0:
                        continue
                    if slot_entry_date_idx[sl_idx] == i:
                        continue  # T+1
                    sell_price = raw_price * (1.0 - slippage) * (1.0 - commission - stamp_tax)
                    revenue = slot_shares[sl_idx] * sell_price
                    pnl = (sell_price - slot_entry_price[sl_idx]) * slot_shares[sl_idx]
                    cash += revenue
                    trade_day_idx[trade_count] = i
                    trade_action[trade_count] = ACTION_SELL
                    trade_price[trade_count] = sell_price
                    trade_shares[trade_count] = slot_shares[sl_idx]
                    trade_cash_flow[trade_count] = revenue
                    trade_pnl[trade_count] = pnl
                    trade_pnl_valid[trade_count] = 1
                    trade_reason[trade_count] = REASON_SIGNAL
                    trade_grid_index[trade_count] = sl_idx
                    trade_count += 1
                    slot_shares[sl_idx] = 0
                    slot_entry_price[sl_idx] = 0.0
                    slot_entry_date_idx[sl_idx] = -1
                    if is_gap:
                        gap_trade_count += 1

        # 4. 当日结算 equity
        equity = cash
        for j in range(grid_count):
            if slot_shares[j] > 0:
                equity += slot_shares[j] * close_p
        equity_arr[i] = equity

        prev_grid_index = curr_idx

    return (
        equity_arr,
        trade_day_idx[:trade_count],
        trade_action[:trade_count],
        trade_price[:trade_count],
        trade_shares[:trade_count],
        trade_cash_flow[:trade_count],
        trade_pnl[:trade_count],
        trade_pnl_valid[:trade_count],
        trade_reason[:trade_count],
        trade_grid_index[:trade_count],
        out_above_days,
        out_below_days,
        gap_trade_count,
        gap_first_day_idx,
        gap_first_pct,
        gap_first_n_lines,
        stopped,
        stopped_at_idx,
    )


def run_grid_backtest_numba(
    store: DataStore,
    stock_code: str,
    price_lower: float,
    price_upper: float,
    grid_count: int = 10,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    initial_capital: float = 100000.0,
    commission: float = 0.0003,
    stamp_tax: float = 0.001,
    slippage: float = 0.001,
    stop_loss: Optional[float] = None,
    base_position_ratio: float = 0.5,
) -> dict:
    """Numba 加速版 run_grid_backtest, 输入输出跟老版完全一致 (回归对账 bit-perfect)."""
    # === 参数校验 (跟老版一致) ===
    if price_upper <= price_lower:
        raise ValueError("价格上限必须大于下限")
    if price_lower <= 0:
        raise ValueError("价格下限必须大于 0")
    if grid_count < 2 or grid_count > 50:
        raise ValueError("网格数量必须在 2-50 之间")
    if stop_loss is not None and (stop_loss >= 0 or stop_loss <= -1):
        raise ValueError("stop_loss 必须为负数且 > -1")
    if commission < 0 or stamp_tax < 0 or slippage < 0:
        raise ValueError("费率不能为负数")
    if base_position_ratio < 0 or base_position_ratio > 1:
        raise ValueError("base_position_ratio 必须在 [0, 1] 之间")
    if stock_code not in store.code_name_map:
        raise ValueError(f"stock_code {stock_code} not found")

    stock_name = store.code_name_map[stock_code]
    ohlcv = store.get_ohlcv(stock_code)
    if ohlcv.empty:
        raise ValueError(f"no ohlcv data for {stock_code}")

    start_ts = _parse_date(start_date) or ohlcv["date"].min()
    end_ts = _parse_date(end_date) or ohlcv["date"].max()
    window = ohlcv[(ohlcv["date"] >= start_ts) & (ohlcv["date"] <= end_ts)].reset_index(drop=True)
    if window.empty:
        raise ValueError("no trading days in range")

    grid_width = (price_upper - price_lower) / grid_count
    per_grid_capital = float(initial_capital) / grid_count

    first_open = float(window.iloc[0]["open"])
    min_required = first_open * 100 * (1.0 + commission) * (1.0 + slippage)
    if per_grid_capital < min_required:
        raise ValueError(
            f"资金不足: 每格 {per_grid_capital:.0f} 元, 最低需要 {min_required:.0f} 元才能买入 100 股"
        )

    threshold = limit_threshold(stock_code)

    # 准备 numpy 数组
    open_arr = window["open"].to_numpy().astype(np.float64)
    close_arr = window["close"].to_numpy().astype(np.float64)
    if "pct_change" in window.columns:
        pct_arr = window["pct_change"].fillna(0.0).to_numpy().astype(np.float64)
    else:
        pct_arr = np.zeros(len(window), dtype=np.float64)

    sl = float(stop_loss) if stop_loss is not None else float("nan")

    (
        equity_arr,
        ti, ta, tp_, ts_, tcf, tpn, tpv, tre, tgi,
        out_above_days, out_below_days,
        gap_trade_count, gap_first_day_idx, gap_first_pct, gap_first_n_lines,
        stopped, stopped_at_idx,
    ) = _run_grid_loop_numba(
        open_arr, close_arr, pct_arr,
        float(initial_capital), float(commission), float(stamp_tax), float(slippage),
        sl, float(base_position_ratio),
        float(price_lower), float(grid_width), int(grid_count),
        float(threshold),
    )

    # === 还原 dict ===
    equity_dates = window["date"].tolist()

    # trade_log
    trade_log: list[dict] = []
    for k in range(len(ti)):
        idx = int(ti[k])
        action = "BUY" if ta[k] == ACTION_BUY else "SELL"
        reason_int = int(tre[k])
        if reason_int == REASON_BASE:
            reason = "base_position"
        elif reason_int == REASON_STOP_LOSS:
            reason = "stop_loss"
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
            "grid_index": int(tgi[k]),
        })

    # warnings
    warnings: list[str] = []
    if gap_first_day_idx >= 0:
        date_str = equity_dates[int(gap_first_day_idx)].strftime("%Y-%m-%d")
        n_lines = int(gap_first_n_lines)
        if gap_first_pct >= threshold:
            warnings.append(f"{date_str} 一字涨停穿越 {n_lines} 条线, 成交价用 close 近似")
        elif gap_first_pct <= -threshold:
            warnings.append(f"{date_str} 一字跌停穿越 {n_lines} 条线, 成交价用 close 近似")
        else:
            warnings.append(f"{date_str} 跳空穿越 {n_lines} 条线, 成交价用 close 近似")

    # benchmark 对齐
    equity_series = pd.Series(equity_arr, index=pd.DatetimeIndex(equity_dates))
    idx_daily = store.index_daily
    if not idx_daily.empty:
        idx_window = idx_daily[(idx_daily["date"] >= equity_dates[0]) & (idx_daily["date"] <= equity_dates[-1])]
        if not idx_window.empty:
            bench_series = pd.Series(
                idx_window["close"].astype(float).values,
                index=pd.DatetimeIndex(idx_window["date"].values),
            )
            union_idx = equity_series.index.union(bench_series.index)
            equity_curve_aligned = equity_series.reindex(union_idx).ffill()
            bench_series_u = bench_series.reindex(union_idx).ffill()
            bench_start = float(bench_series_u.iloc[0])
            bench_curve = bench_series_u / bench_start * float(initial_capital)
        else:
            bench_curve = pd.Series(dtype=float)
            equity_curve_aligned = equity_series
    else:
        bench_curve = pd.Series(dtype=float)
        equity_curve_aligned = equity_series

    # 指标
    tr = metrics.total_return(equity_curve_aligned)
    ar = metrics.annual_return(equity_curve_aligned)
    mdd = metrics.max_drawdown(equity_curve_aligned)
    sr = metrics.sharpe_ratio(equity_curve_aligned)
    br = metrics.benchmark_return(bench_curve) if not bench_curve.empty else None

    # === 配对 (跟老版 _pair_trades_by_grid 一致) ===
    pairs = []
    open_entries: dict[int, dict] = {}
    for t in trade_log:
        gi = t["grid_index"]
        if t["action"] == "BUY":
            open_entries[gi] = t
        elif t["action"] == "SELL" and gi in open_entries:
            entry = open_entries.pop(gi)
            hold_days = (pd.Timestamp(t["date"]) - pd.Timestamp(entry["date"])).days
            ret = (t["price"] - entry["price"]) / entry["price"]
            pairs.append({
                "pnl": (t["price"] - entry["price"]) * t["shares"],
                "hold_days": hold_days,
                "return": ret,
            })

    if pairs:
        wins = [p for p in pairs if p["return"] > 0]
        losses = [p for p in pairs if p["return"] <= 0]
        win_rate: Optional[float] = len(wins) / len(pairs)
        avg_win = float(np.mean([p["return"] for p in wins])) if wins else 0.0
        avg_loss = abs(float(np.mean([p["return"] for p in losses]))) if losses else 0.0
        if avg_loss > 0:
            pl_ratio: Optional[float] = avg_win / avg_loss
        elif wins:
            pl_ratio = None
        else:
            pl_ratio = 0.0
        avg_hold: Optional[float] = float(np.mean([p["hold_days"] for p in pairs]))
    else:
        win_rate = None
        pl_ratio = None
        avg_hold = None

    # grid_summary
    per_grid_stats = {i: {"trades": 0, "total_pnl": 0.0} for i in range(grid_count)}
    for t in trade_log:
        gi = t["grid_index"]
        if 0 <= gi < grid_count:
            per_grid_stats[gi]["trades"] += 1
            if t["pnl"] is not None:
                per_grid_stats[gi]["total_pnl"] += t["pnl"]

    grids_list = [
        {
            "index": i,
            "range": f"{price_lower + i * grid_width:.2f} - {price_lower + (i + 1) * grid_width:.2f}",
            "trades": per_grid_stats[i]["trades"],
            "total_pnl": round(per_grid_stats[i]["total_pnl"], 2),
        }
        for i in range(grid_count)
    ]

    out_of_range_days = out_above_days + out_below_days
    out_of_range_direction: Optional[str] = None
    if out_above_days > 0 and out_below_days == 0:
        out_of_range_direction = "above"
    elif out_below_days > 0 and out_above_days == 0:
        out_of_range_direction = "below"
    elif out_above_days > 0 and out_below_days > 0:
        out_of_range_direction = "both"

    grid_summary = {
        "grid_count": int(grid_count),
        "grid_width": round(grid_width, 4),
        "price_upper": price_upper,
        "price_lower": price_lower,
        "per_grid_capital": round(per_grid_capital, 2),
        "grids": grids_list,
        "out_of_range_days": int(out_of_range_days),
        "out_of_range_direction": out_of_range_direction,
        "gap_cross_events": int(gap_trade_count),
        "stopped_at": equity_dates[int(stopped_at_idx)].strftime("%Y-%m-%d") if stopped_at_idx >= 0 else None,
    }

    # 结果质量 warnings
    total_days = len(window)
    if len(pairs) < 5:
        warnings.append(f"回测期间仅触发 {len(pairs)} 次完整交易, 结果统计意义有限")
    if total_days > 0 and out_of_range_days / total_days > 0.2:
        pct_out = out_of_range_days / total_days * 100
        warnings.append(f"价格有 {pct_out:.0f}% 的时间超出网格区间, 建议调整上下限")
    if grid_width < first_open * 0.01:
        warnings.append(f"格宽仅占股价的 {grid_width/first_open*100:.2f}%, 费率可能吃掉大部分利润")
    if trade_log and gap_trade_count / len(trade_log) > 0.3:
        warnings.append(
            f"有 {gap_trade_count/len(trade_log)*100:.0f}% 的交易因跳空多格采用 close 近似, 结果可能偏离实盘"
        )

    equity_curve_out = [
        {"date": d.strftime("%Y-%m-%d"), "value": round(float(v), 2)}
        for d, v in equity_curve_aligned.items()
    ]
    bench_curve_out = (
        [{"date": d.strftime("%Y-%m-%d"), "value": round(float(v), 2)} for d, v in bench_curve.items()]
        if not bench_curve.empty
        else []
    )

    return {
        "stock_code": stock_code,
        "stock_name": stock_name,
        "strategy_id": "grid",
        "strategy_name": "网格交易",
        "params": {
            "price_lower": price_lower,
            "price_upper": price_upper,
            "grid_count": int(grid_count),
        },
        "start_date": equity_dates[0].strftime("%Y-%m-%d"),
        "end_date": equity_dates[-1].strftime("%Y-%m-%d"),
        "initial_capital": float(initial_capital),
        "total_return": float(tr),
        "annual_return": float(ar),
        "max_drawdown": float(mdd),
        "sharpe_ratio": sr,
        "win_rate": win_rate,
        "profit_loss_ratio": pl_ratio,
        "total_trades": len(pairs),
        "avg_holding_days": avg_hold,
        "benchmark_return": br,
        "equity_curve": equity_curve_out,
        "benchmark_curve": bench_curve_out,
        "trade_log": trade_log,
        "warnings": warnings,
        "grid_summary": grid_summary,
    }
