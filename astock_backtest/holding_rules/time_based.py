"""G 类: 时间持有规则.

HoldNDays(n): 买入后第 n 个交易日卖.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from astock_backtest.holding_rules.base import HoldingRule, HoldingRuleResult, _force_close


class HoldNDays(HoldingRule):
    """固定持有 N 个交易日 (买后第 N 日卖).

    例 n=10: buy_idx=100 → exit_idx=110.
    若 buy_idx+n >= len(ohlcv), 兜底返末日强平.
    """

    def __init__(self, n: int) -> None:
        if n < 1:
            raise ValueError(f"HoldNDays n 必须 >= 1, 实际 {n}")
        self.n = n
        self.name = f"hold_n_days_{n}"
        self.description_easy = f"买入后固定持有 {n} 天"

    def find_exit(
        self,
        buy_idx: int,
        ohlcv: pd.DataFrame,
        *,
        reverse_signal_array: Optional[np.ndarray] = None,
    ) -> HoldingRuleResult:
        target = buy_idx + self.n
        if target >= len(ohlcv):
            return _force_close(ohlcv, "force_close_end_hold_n_days_overflow")
        return HoldingRuleResult(exit_idx=target, exit_reason=f"hit_hold_n_days_{self.n}")
