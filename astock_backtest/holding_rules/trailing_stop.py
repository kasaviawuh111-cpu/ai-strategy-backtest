"""J 类: 移动止损规则 (TrailingStop) - Phase A P0 补.

TrailingStop(pct): 维护买入后峰值, 当 close 跌过峰值 × (1 - pct%) 时卖出.
跟固定 StopLossPct 区别: 不锁定买入价, 而是动态跟随峰值上移.

例: pct=5, buy_close=100, 涨到 120 → 新止损=114; 跌到 113 → 触发卖.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from astock_backtest.holding_rules.base import HoldingRule, HoldingRuleResult, _force_close


class TrailingStop(HoldingRule):
    """移动止损 (峰值回撤 pct%).

    实现: 从 buy_idx+1 开始扫, 维护当前 close 峰值 peak,
    第一个 close <= peak × (1 - pct/100) 的位置即卖.
    """

    def __init__(self, pct: float) -> None:
        if pct <= 0:
            raise ValueError(f"TrailingStop pct 必须 > 0, 实际 {pct}")
        self.pct = float(pct)
        self.name = f"trailing_stop_{int(pct) if pct == int(pct) else pct}"
        self.description_easy = f"移动止损 (峰值回撤 {pct:g}%)"

    def find_exit(
        self,
        buy_idx: int,
        ohlcv: pd.DataFrame,
        *,
        reverse_signal_array: Optional[np.ndarray] = None,
    ) -> HoldingRuleResult:
        if buy_idx >= len(ohlcv) - 1:
            return _force_close(ohlcv, "force_close_end_no_holding_period")
        future_closes = ohlcv["close"].iloc[buy_idx + 1:].to_numpy(dtype=float)
        if len(future_closes) == 0:
            return _force_close(ohlcv, "force_close_end_no_holding_period")

        # 向量化: cummax 找每个位置历史峰值, 算回撤阈值, 找首个跌破
        peak = np.maximum.accumulate(future_closes)
        threshold = peak * (1.0 - self.pct / 100.0)
        triggered = future_closes <= threshold
        # 第一个 trigger 之前 peak 必须更新过 (跟买入价比, 否则第一日就有可能误触)
        # 简化: 用 future_closes 的 cummax 已确保 peak >= 当日值, 不会瞬间触发
        # (因为 close <= peak 总成立, 要看是不是 <= peak * 0.95)
        hits = np.where(triggered)[0]
        if len(hits) == 0:
            return _force_close(ohlcv, "force_close_end_trailing_stop_not_hit")
        return HoldingRuleResult(
            exit_idx=buy_idx + 1 + int(hits[0]),
            exit_reason=f"hit_trailing_stop_{self.pct:g}pct",
        )
