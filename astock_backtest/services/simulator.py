"""轻量 trade simulator (Phase A)

给定 (ohlcv, triggers, holding_rule, 可选 reverse_signal_array) 算 list[Trade].
跟 backtest_engine 的关键差异:
  - 无资金管理 / equity_curve / 持仓状态
  - 无 T+1 / 涨跌停 (假定都能成交, 简化)
  - 无加仓减仓 (一次买一次卖)
  - 买卖时点跟 backtest_engine 对齐: 触发日次日 open 买, 持有规则触发日 close 卖

性能: 完全 vectorized signal trigger 找位置 + 简单 Python 循环算 exit (规则层接 batch 优化是 Phase B).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from astock_backtest.holding_rules import HoldingRule


@dataclass(frozen=True)
class Trade:
    buy_idx: int        # 触发日 idx (相对 ohlcv 0-based)
    exit_idx: int       # 卖出日 idx
    buy_price: float    # 实际买入价 (次日 open)
    exit_price: float   # 实际卖出价 (触发日 close)
    return_pct: float   # (exit/buy - 1) * 100, 单位 %
    exit_reason: str    # 卖出原因 (来自 holding_rule)


def simulate_trades(
    ohlcv: pd.DataFrame,
    triggers: np.ndarray,
    holding_rule: HoldingRule,
    *,
    reverse_signal_array: Optional[np.ndarray] = None,
    min_warmup: int = 0,
    return_numpy: bool = False,
) -> list[Trade] | np.ndarray:
    """给定信号 trigger 数组 + 持有规则 → 算所有 trades.

    参数:
        ohlcv: 完整 ohlcv DataFrame, 至少含 open/close 列
        triggers: np.int8, shape=(len(ohlcv),), 1=该日触发买入
        holding_rule: 持有规则实例 (HoldingRule)
        reverse_signal_array: 反向信号 array
        min_warmup: 信号 warmup 之前的 trigger 已被原子层清零, 这里再保险跳过
        return_numpy: True 时返 np.ndarray[float64] 仅含 return_pct (省 ~25x 内存,
                      cache 路径全市场大数据量必须用). False (默认) 返 Trade list (兼容
                      Phase A scan 路径 + signal_detail 路径).

    返回:
        return_numpy=False: list[Trade] (含 buy_idx/exit_idx/buy_price/exit_price/exit_reason)
        return_numpy=True: np.ndarray[float64], shape=(n_trades,), 仅 return_pct (%)
    """
    n = len(ohlcv)
    if len(triggers) != n:
        raise ValueError(f"triggers len={len(triggers)} 不匹配 ohlcv len={n}")

    buy_indices = np.where(triggers == 1)[0]
    if len(buy_indices) == 0:
        return np.empty(0, dtype=np.float64) if return_numpy else []

    open_arr = ohlcv["open"].astype(float).to_numpy()
    close_arr = ohlcv["close"].astype(float).to_numpy()

    if return_numpy:
        # 预分配上限 (可能因 skip 缩短)
        returns_buf = np.empty(len(buy_indices), dtype=np.float64)
        write_idx = 0
        for buy_idx in buy_indices:
            buy_idx = int(buy_idx)
            if buy_idx < min_warmup or buy_idx >= n - 1:
                continue
            buy_price = open_arr[buy_idx + 1]
            if not (buy_price > 0) or np.isnan(buy_price):
                continue
            result = holding_rule.find_exit(
                buy_idx=buy_idx, ohlcv=ohlcv,
                reverse_signal_array=reverse_signal_array,
            )
            if result.exit_idx < buy_idx + 1:
                continue
            exit_price = close_arr[result.exit_idx]
            if not (exit_price > 0) or np.isnan(exit_price):
                continue
            returns_buf[write_idx] = (exit_price / buy_price - 1.0) * 100.0
            write_idx += 1
        return returns_buf[:write_idx]

    # Trade list 路径 (scan / signal_detail 用)
    trades: list[Trade] = []
    for buy_idx in buy_indices:
        buy_idx = int(buy_idx)
        if buy_idx < min_warmup:
            continue
        if buy_idx >= n - 1:
            continue
        buy_price = open_arr[buy_idx + 1]
        if not (buy_price > 0) or np.isnan(buy_price):
            continue

        result = holding_rule.find_exit(
            buy_idx=buy_idx,
            ohlcv=ohlcv,
            reverse_signal_array=reverse_signal_array,
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

    return trades
