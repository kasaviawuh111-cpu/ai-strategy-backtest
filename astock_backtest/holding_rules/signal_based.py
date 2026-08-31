"""H 类: 信号触发卖出规则 (Phase A 仅 SellOnReverseSignal).

反向信号 array 由 reverse_search 引擎在调用时传入 (从对应 BUY 信号原子查反向配对表得到).
原子层不内置反向配对逻辑 — 那个由引擎/router 层做, 这里只负责"给定 reverse_array 找首次卖点".
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from astock_backtest.holding_rules.base import HoldingRule, HoldingRuleResult, _force_close


class SellOnReverseSignal(HoldingRule):
    """买入后出现反向信号 (reverse_signal_array[i]==1) 第一天卖.

    reverse_signal_array 必须由 find_exit 的 kwarg 传入, 长度 == len(ohlcv).
    若没传 (None) → 兜底强平 (语义: 这个规则不适用此 BUY 信号, 详 v0.3 §4.6).
    """

    def __init__(self) -> None:
        self.name = "sell_on_reverse_signal"
        self.description_easy = "买入后出现反向信号时卖"

    def find_exit(
        self,
        buy_idx: int,
        ohlcv: pd.DataFrame,
        *,
        reverse_signal_array: Optional[np.ndarray] = None,
    ) -> HoldingRuleResult:
        if reverse_signal_array is None:
            return _force_close(ohlcv, "force_close_end_no_reverse_signal_configured")
        if len(reverse_signal_array) != len(ohlcv):
            raise ValueError(
                f"reverse_signal_array len={len(reverse_signal_array)} 不匹配 ohlcv len={len(ohlcv)}"
            )
        if buy_idx >= len(ohlcv) - 1:
            return _force_close(ohlcv, "force_close_end_no_holding_period")
        # 从 buy_idx+1 开始扫反向信号
        future = reverse_signal_array[buy_idx + 1:]
        hits = np.where(future == 1)[0]
        if len(hits) == 0:
            return _force_close(ohlcv, "force_close_end_reverse_signal_not_seen")
        return HoldingRuleResult(
            exit_idx=buy_idx + 1 + int(hits[0]),
            exit_reason="hit_reverse_signal",
        )
