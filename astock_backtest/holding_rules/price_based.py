"""I 类: 价格阈值规则.

StopLossPct(pct): 跌幅 ≥ pct% 时止损 (基于买入日 close 算回撤).
TakeProfitPct(pct): 涨幅 ≥ pct% 时止盈.

注: 触发以"当日 close"判定, 跟 backtest_engine 现有"次日开盘卖"差异由引擎层处理 (Task #3).
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from astock_backtest.holding_rules.base import HoldingRule, HoldingRuleResult, _force_close


class StopLossPct(HoldingRule):
    def __init__(self, pct: float) -> None:
        if pct <= 0:
            raise ValueError(f"StopLossPct pct 必须 > 0, 实际 {pct}")
        self.pct = float(pct)
        self.name = f"stop_loss_{int(pct) if pct == int(pct) else pct}"
        self.description_easy = f"跌幅 ≥ {pct:g}% 止损"

    def find_exit(
        self,
        buy_idx: int,
        ohlcv: pd.DataFrame,
        *,
        reverse_signal_array: Optional[np.ndarray] = None,
    ) -> HoldingRuleResult:
        if buy_idx >= len(ohlcv) - 1:
            return _force_close(ohlcv, "force_close_end_no_holding_period")
        buy_close = float(ohlcv["close"].iloc[buy_idx])
        threshold = buy_close * (1.0 - self.pct / 100.0)
        # 从 buy_idx+1 开始扫
        future_closes = ohlcv["close"].iloc[buy_idx + 1:].to_numpy(dtype=float)
        hits = np.where(future_closes <= threshold)[0]
        if len(hits) == 0:
            return _force_close(ohlcv, "force_close_end_stop_loss_not_hit")
        return HoldingRuleResult(
            exit_idx=buy_idx + 1 + int(hits[0]),
            exit_reason=f"hit_stop_loss_{self.pct:g}pct",
        )


class TakeProfitPct(HoldingRule):
    def __init__(self, pct: float) -> None:
        if pct <= 0:
            raise ValueError(f"TakeProfitPct pct 必须 > 0, 实际 {pct}")
        self.pct = float(pct)
        self.name = f"take_profit_{int(pct) if pct == int(pct) else pct}"
        self.description_easy = f"涨幅 ≥ {pct:g}% 止盈"

    def find_exit(
        self,
        buy_idx: int,
        ohlcv: pd.DataFrame,
        *,
        reverse_signal_array: Optional[np.ndarray] = None,
    ) -> HoldingRuleResult:
        if buy_idx >= len(ohlcv) - 1:
            return _force_close(ohlcv, "force_close_end_no_holding_period")
        buy_close = float(ohlcv["close"].iloc[buy_idx])
        threshold = buy_close * (1.0 + self.pct / 100.0)
        future_closes = ohlcv["close"].iloc[buy_idx + 1:].to_numpy(dtype=float)
        hits = np.where(future_closes >= threshold)[0]
        if len(hits) == 0:
            return _force_close(ohlcv, "force_close_end_take_profit_not_hit")
        return HoldingRuleResult(
            exit_idx=buy_idx + 1 + int(hits[0]),
            exit_reason=f"hit_take_profit_{self.pct:g}pct",
        )
