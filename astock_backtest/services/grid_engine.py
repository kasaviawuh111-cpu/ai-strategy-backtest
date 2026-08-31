"""网格交易引擎 v1.3

独立于 backtest_engine, 但共享 metrics / board_limits / 费率模型 / benchmark 对齐。

v1.3 相对 v1.2 的核心修正:
- 新增底仓建仓 (base_position_ratio, 默认 0.5)
- Day 1 开盘前按 first_open 填充 slot[first_g..0], 每档用 per_grid_capital
- 累计底仓成本 ≤ initial_capital * ratio, 超限停止填充
- 底仓 entry_date = Day 1, 自动走现有 T+1 保护

v1.2 相对 v1.1 的核心修正:
- 触发与成交价基于"格线穿越"而非"格位变化"
- 每个 slot[i] 买在下线 line_i, 卖在上线 line_{i+1}, 单笔理论盈利 = 格宽
- v1.1 实现把单格穿越用 pos.grid_high 做成交价, 导致买入卖出同价、零费率下 pnl=0

核心规则:
1. N+1 根格线, N 个槽 (slot). slot[i] 代表"买在 line_i, 卖在 line_{i+1}"
2. 底仓: Day 1 按 first_open 填充 slot[first_g] 向下, 每档 per_grid_capital
3. 触发: 每日 close 所在格位变化, 算出穿越的格线集合
   - DOWN cross 线 L (L in [0, N-1]): 买 slot[L], 成交价 = line_L (+滑点/佣金)
   - UP cross 线 L (L in [1, N]): 卖 slot[L-1], 成交价 = line_L (-滑点-佣金-印花税)
4. 单格穿越: 用格线价; 跳空多格: 退化为当日 close 近似并 warn
5. T+1: slot.entry_date == trading_day 则当日不卖
6. 三段式费率 commission + stamp_tax + slippage, 与 backtest_engine._apply_cost 一致
7. 组合级止损: total_value/initial - 1 <= stop_loss → 全平, state.stopped=True,
   之后 equity=cash 横线延伸至 end_date (保证 benchmark 对齐)
8. 涨跌停阈值走 board_limits.limit_threshold
"""

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from astock_backtest.data_loader import DataStore
from astock_backtest.services import metrics
from astock_backtest.services.board_limits import limit_threshold


@dataclass
class GridSlot:
    """slot[i]: 买在 line_i, 卖在 line_{i+1}, 盈利 = 格宽"""
    grid_index: int         # 对外保留 grid_index 命名, 与前端/doc 一致
    entry_line: float       # 买入格线价 (line_i)
    exit_line: float        # 卖出格线价 (line_{i+1})
    entry_price: float = 0.0
    shares: int = 0
    entry_date: Optional[pd.Timestamp] = None


@dataclass
class GridState:
    slots: list[GridSlot]
    cash: float
    initial_capital: float
    stopped: bool = False
    stopped_at: Optional[pd.Timestamp] = None


def init_slots(price_lower: float, price_upper: float, grid_count: int) -> list[GridSlot]:
    grid_width = (price_upper - price_lower) / grid_count
    return [
        GridSlot(
            grid_index=i,
            entry_line=price_lower + i * grid_width,
            exit_line=price_lower + (i + 1) * grid_width,
        )
        for i in range(grid_count)
    ]


def get_grid_index(price: float, price_lower: float, grid_width: float, grid_count: int) -> int:
    if price <= price_lower:
        return -1
    if price >= price_lower + grid_count * grid_width:
        return grid_count
    return int((price - price_lower) / grid_width)


def get_crossed_lines(prev_grid_idx: int, curr_grid_idx: int) -> tuple[list[int], Optional[str]]:
    """返回穿越的格线索引列表 (按穿越顺序) 和方向.

    格线索引: 0 = price_lower, N = price_upper. 共 N+1 根线.
    - DOWN: 从 grid prev 到 grid curr (curr < prev), 穿越线 prev, prev-1, ..., curr+1
      例 prev=4 curr=1: 穿越线 [4,3,2] (grid 4/3 分界, 3/2 分界, 2/1 分界)
    - UP: 从 grid prev 到 grid curr (curr > prev), 穿越线 prev+1, ..., curr
      例 prev=1 curr=4: 穿越线 [2,3,4]
    """
    if prev_grid_idx == curr_grid_idx:
        return [], None
    if curr_grid_idx < prev_grid_idx:
        return list(range(prev_grid_idx, curr_grid_idx, -1)), "down"
    return list(range(prev_grid_idx + 1, curr_grid_idx + 1)), "up"


def _apply_cost(price: float, is_buy: bool, commission: float, stamp_tax: float, slippage: float) -> float:
    """与 backtest_engine._apply_cost 完全一致的三段式费率"""
    if is_buy:
        return price * (1.0 + slippage) * (1.0 + commission)
    return price * (1.0 - slippage) * (1.0 - commission - stamp_tax)


def _parse_date(s: Optional[str]) -> Optional[pd.Timestamp]:
    if not s:
        return None
    s = s.replace("-", "")
    return pd.Timestamp(f"{s[:4]}-{s[4:6]}-{s[6:8]}")


def _pair_trades_by_grid(trade_log: list[dict]) -> list[dict]:
    """按 grid_index 分组配对 BUY→SELL"""
    pairs: list[dict] = []
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
    return pairs


def run_grid_backtest(
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
    # === 参数校验 ===
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

    slots = init_slots(price_lower, price_upper, grid_count)
    state = GridState(
        slots=slots,
        cash=float(initial_capital),
        initial_capital=float(initial_capital),
    )

    equity_dates: list[pd.Timestamp] = []
    equity_values: list[float] = []
    trade_log: list[dict] = []
    warnings: list[str] = []
    out_above_days = 0
    out_below_days = 0
    gap_trade_count = 0
    gap_warning_logged = False

    prev_grid_index = get_grid_index(first_open, price_lower, grid_width, grid_count)

    # === 底仓建仓 (v1.3) ===
    # 按"幻影成本 = L_i"填充 slot[first_g..0], 模拟"回测前就按格线累积的筹码"
    # 累计成本 ≤ initial_capital * ratio, 超限停止
    # entry_date = None (不触发 T+1, 假设回测前已持有)
    # 这样 pnl = (L_{i+1} - L_i) * shares > 0 符合每档固定收益的理论
    if base_position_ratio > 0:
        base_day_str = window.iloc[0]["date"].strftime("%Y-%m-%d")
        max_base_cash = float(initial_capital) * base_position_ratio
        running_base_cost = 0.0
        clamped_g = max(-1, min(prev_grid_index, grid_count - 1))
        for slot_idx in range(clamped_g, -1, -1):
            slot = state.slots[slot_idx]
            line_price = price_lower + slot_idx * grid_width
            phantom_buy_price = _apply_cost(line_price, True, commission, stamp_tax, slippage)
            affordable = int(per_grid_capital // phantom_buy_price)
            affordable = (affordable // 100) * 100
            required_cash = affordable * phantom_buy_price
            if affordable < 100 or state.cash < required_cash:
                break
            if running_base_cost + required_cash > max_base_cash:
                break
            state.cash -= required_cash
            running_base_cost += required_cash
            slot.shares = affordable
            slot.entry_price = phantom_buy_price
            slot.entry_date = None  # 不触发 T+1
            trade_log.append({
                "date": base_day_str,
                "action": "BUY",
                "price": round(phantom_buy_price, 4),
                "shares": affordable,
                "cash_flow": round(-required_cash, 2),
                "pnl": None,
                "reason": "base_position",
                "grid_index": slot_idx,
            })

    for i in range(len(window)):
        row = window.iloc[i]
        day: pd.Timestamp = row["date"]
        close_p = float(row["close"])
        pct_change = row.get("pct_change")
        pct_change = float(pct_change) if pd.notna(pct_change) else 0.0

        # === 0. 组合级止损 ===
        if stop_loss is not None and not state.stopped:
            total_value = state.cash + sum(s.shares * close_p for s in state.slots if s.shares > 0)
            if (total_value - initial_capital) / initial_capital <= stop_loss:
                for s in state.slots:
                    if s.shares > 0:
                        sell_price = _apply_cost(close_p, False, commission, stamp_tax, slippage)
                        revenue = s.shares * sell_price
                        pnl = (sell_price - s.entry_price) * s.shares
                        state.cash += revenue
                        trade_log.append({
                            "date": day.strftime("%Y-%m-%d"),
                            "action": "SELL",
                            "price": round(sell_price, 4),
                            "shares": s.shares,
                            "cash_flow": round(revenue, 2),
                            "pnl": round(pnl, 2),
                            "reason": "stop_loss",
                            "grid_index": s.grid_index,
                        })
                        s.shares = 0
                        s.entry_price = 0.0
                        s.entry_date = None
                state.stopped = True
                state.stopped_at = day

        if state.stopped:
            equity_dates.append(day)
            equity_values.append(state.cash)
            continue

        # === 1. 今日 close 所在格位 ===
        curr_idx = get_grid_index(close_p, price_lower, grid_width, grid_count)
        if curr_idx < 0:
            out_below_days += 1
        elif curr_idx >= grid_count:
            out_above_days += 1

        # === 2. 穿越的格线 ===
        crossed_lines, direction = get_crossed_lines(prev_grid_index, curr_idx)
        is_gap = len(crossed_lines) > 1
        if is_gap and not gap_warning_logged:
            if pct_change >= threshold:
                warnings.append(f"{day.strftime('%Y-%m-%d')} 一字涨停穿越 {len(crossed_lines)} 条线, 成交价用 close 近似")
            elif pct_change <= -threshold:
                warnings.append(f"{day.strftime('%Y-%m-%d')} 一字跌停穿越 {len(crossed_lines)} 条线, 成交价用 close 近似")
            else:
                warnings.append(f"{day.strftime('%Y-%m-%d')} 跳空穿越 {len(crossed_lines)} 条线, 成交价用 close 近似")
            gap_warning_logged = True

        # === 3. 处理每条穿越线 ===
        for line_idx in crossed_lines:
            # 成交原始价: 单线用格线价, 多线用 close 近似
            line_price = price_lower + line_idx * grid_width
            raw_price = close_p if is_gap else line_price

            if direction == "down":
                # DOWN cross 线 L → 买 slot[L] (L 必须在 [0, N-1])
                if not (0 <= line_idx <= grid_count - 1):
                    continue
                slot = state.slots[line_idx]
                if slot.shares != 0:
                    continue
                buy_price = _apply_cost(raw_price, True, commission, stamp_tax, slippage)
                affordable = int(per_grid_capital // buy_price)
                affordable = (affordable // 100) * 100
                required_cash = affordable * buy_price
                if affordable < 100 or state.cash < required_cash:
                    break  # v1.1: 跳空多格现金不够, break 放弃更远的格
                state.cash -= required_cash
                slot.shares = affordable
                slot.entry_price = buy_price
                slot.entry_date = day
                trade_log.append({
                    "date": day.strftime("%Y-%m-%d"),
                    "action": "BUY",
                    "price": round(buy_price, 4),
                    "shares": affordable,
                    "cash_flow": round(-required_cash, 2),
                    "pnl": None,
                    "reason": "signal",
                    "grid_index": line_idx,
                })
                if is_gap:
                    gap_trade_count += 1

            else:  # direction == "up"
                # UP cross 线 L → 卖 slot[L-1] (L 必须在 [1, N])
                if not (1 <= line_idx <= grid_count):
                    continue
                slot = state.slots[line_idx - 1]
                if slot.shares == 0:
                    continue
                if slot.entry_date == day:
                    continue  # T+1
                sell_price = _apply_cost(raw_price, False, commission, stamp_tax, slippage)
                revenue = slot.shares * sell_price
                pnl = (sell_price - slot.entry_price) * slot.shares
                state.cash += revenue
                trade_log.append({
                    "date": day.strftime("%Y-%m-%d"),
                    "action": "SELL",
                    "price": round(sell_price, 4),
                    "shares": slot.shares,
                    "cash_flow": round(revenue, 2),
                    "pnl": round(pnl, 2),
                    "reason": "signal",
                    "grid_index": slot.grid_index,
                })
                slot.shares = 0
                slot.entry_price = 0.0
                slot.entry_date = None
                if is_gap:
                    gap_trade_count += 1

        # === 4. 当日结算 ===
        equity = state.cash + sum(s.shares * close_p for s in state.slots if s.shares > 0)
        equity_dates.append(day)
        equity_values.append(equity)

        prev_grid_index = curr_idx

    # === benchmark 对齐 ===
    equity_series = pd.Series(equity_values, index=pd.DatetimeIndex(equity_dates))
    idx = store.index_daily
    if not idx.empty:
        idx_window = idx[(idx["date"] >= equity_dates[0]) & (idx["date"] <= equity_dates[-1])]
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

    # === 指标 (复用 metrics.py) ===
    tr = metrics.total_return(equity_curve_aligned)
    ar = metrics.annual_return(equity_curve_aligned)
    mdd = metrics.max_drawdown(equity_curve_aligned)
    sr = metrics.sharpe_ratio(equity_curve_aligned)
    br = metrics.benchmark_return(bench_curve) if not bench_curve.empty else None

    # === 交易配对 ===
    pairs = _pair_trades_by_grid(trade_log)
    if pairs:
        wins = [p for p in pairs if p["return"] > 0]
        losses = [p for p in pairs if p["return"] <= 0]
        win_rate: Optional[float] = len(wins) / len(pairs)
        avg_win = float(np.mean([p["return"] for p in wins])) if wins else 0.0
        avg_loss = abs(float(np.mean([p["return"] for p in losses]))) if losses else 0.0
        pl_ratio: Optional[float]
        if avg_loss > 0:
            pl_ratio = avg_win / avg_loss
        elif wins:
            pl_ratio = None
        else:
            pl_ratio = 0.0
        avg_hold: Optional[float] = float(np.mean([p["hold_days"] for p in pairs]))
    else:
        win_rate = None
        pl_ratio = None
        avg_hold = None

    # === grid_summary ===
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
        "grid_count": grid_count,
        "grid_width": round(grid_width, 4),
        "price_upper": price_upper,
        "price_lower": price_lower,
        "per_grid_capital": round(per_grid_capital, 2),
        "grids": grids_list,
        "out_of_range_days": out_of_range_days,
        "out_of_range_direction": out_of_range_direction,
        "gap_cross_events": gap_trade_count,
        "stopped_at": state.stopped_at.strftime("%Y-%m-%d") if state.stopped_at else None,
    }

    # === 结果质量 warnings ===
    total_days = len(window)
    if len(pairs) < 5:
        warnings.append(f"回测期间仅触发 {len(pairs)} 次完整交易, 结果统计意义有限")
    if total_days > 0 and out_of_range_days / total_days > 0.2:
        pct = out_of_range_days / total_days * 100
        warnings.append(f"价格有 {pct:.0f}% 的时间超出网格区间, 建议调整上下限")
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
            "grid_count": grid_count,
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
