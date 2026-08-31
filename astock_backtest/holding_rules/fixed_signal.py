"""H 类: 固定信号卖出 (SellOnFixedAtom) - Phase A P0 补.

跟 SellOnReverseSignal 区别: 不查反向配对表, 而是用户显式指定卖出信号 atom_id.
例: 买入是涨停开板, 卖出是 MACD 死叉 (sell_on_fixed_atom_id="macd_death_cross").

接收 atom_id 字符串, 在 find_exit 里用 reverse_signal_array (前置算好) 找首日.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from astock_backtest.holding_rules.base import HoldingRule, HoldingRuleResult, _force_close


class SellOnFixedAtom(HoldingRule):
    """卖在指定 atom_id 触发的第一天.

    引擎层负责: 拿到 atom_id 后 atom.compute(ohlcv) 算出 trigger 数组,
    通过 reverse_signal_array 参数传进来 (复用 SellOnReverseSignal 接口).
    """

    def __init__(self, sell_atom_id: str) -> None:
        if not sell_atom_id:
            raise ValueError("sell_atom_id 必填")
        self.sell_atom_id = sell_atom_id
        self.name = f"sell_on_fixed_atom_{sell_atom_id}"
        self.description_easy = f"出现 [{sell_atom_id}] 信号时卖"

    def find_exit(
        self,
        buy_idx: int,
        ohlcv: pd.DataFrame,
        *,
        reverse_signal_array: Optional[np.ndarray] = None,
    ) -> HoldingRuleResult:
        # 复用 reverse_signal_array 参数 (引擎层把 sell_atom_id 算出来的 trigger 数组塞进去)
        if reverse_signal_array is None:
            return _force_close(ohlcv, "force_close_end_no_sell_signal_configured")
        if len(reverse_signal_array) != len(ohlcv):
            raise ValueError(
                f"sell_signal_array len={len(reverse_signal_array)} 不匹配 ohlcv len={len(ohlcv)}"
            )
        if buy_idx >= len(ohlcv) - 1:
            return _force_close(ohlcv, "force_close_end_no_holding_period")
        future = reverse_signal_array[buy_idx + 1:]
        hits = np.where(future == 1)[0]
        if len(hits) == 0:
            return _force_close(ohlcv, f"force_close_end_{self.sell_atom_id}_not_seen")
        return HoldingRuleResult(
            exit_idx=buy_idx + 1 + int(hits[0]),
            exit_reason=f"hit_sell_on_{self.sell_atom_id}",
        )
