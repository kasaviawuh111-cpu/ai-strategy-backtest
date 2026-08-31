"""持有规则基础设施 — abstract HoldingRule + EarliestOf 组合.

接口: find_exit(buy_idx, ohlcv, ...) -> HoldingRuleResult (exit_idx + reason).
任何规则都必须给出 exit_idx (兜底返 len(ohlcv)-1 强平), 不允许返 None.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class HoldingRuleResult:
    exit_idx: int            # 卖出日 index (相对 ohlcv 0-based)
    exit_reason: str         # 卖出原因 (例 "hit_take_profit_10pct" / "force_close_end")


class HoldingRule(ABC):
    name: str = "base"
    description_easy: str = "基础规则"

    @abstractmethod
    def find_exit(
        self,
        buy_idx: int,
        ohlcv: pd.DataFrame,
        *,
        reverse_signal_array: Optional[np.ndarray] = None,
    ) -> HoldingRuleResult:
        """从 buy_idx+1 开始扫, 返第一次触发卖出的 idx + 原因.

        若到 ohlcv 末尾仍未触发, 兜底返 (len(ohlcv)-1, "force_close_end").
        """


def _force_close(ohlcv: pd.DataFrame, reason: str = "force_close_end") -> HoldingRuleResult:
    return HoldingRuleResult(exit_idx=len(ohlcv) - 1, exit_reason=reason)


class EarliestOf(HoldingRule):
    """多规则组合: 最早触发任一规则就卖.

    用于 §5.1 schema 同时给多个字段值时 (例 holding_days=10 + stop_loss_pct=5),
    引擎自动用 EarliestOf 包起来.
    """

    def __init__(self, rules: list[HoldingRule]) -> None:
        if not rules:
            raise ValueError("EarliestOf 至少需要 1 个子规则")
        self.rules = rules
        self.name = "earliest_of"
        self.description_easy = "+".join(r.description_easy for r in rules) + " 最早触发卖"

    def find_exit(
        self,
        buy_idx: int,
        ohlcv: pd.DataFrame,
        *,
        reverse_signal_array: Optional[np.ndarray] = None,
    ) -> HoldingRuleResult:
        results = [
            r.find_exit(buy_idx, ohlcv, reverse_signal_array=reverse_signal_array)
            for r in self.rules
        ]
        best = min(results, key=lambda r: r.exit_idx)
        return best
