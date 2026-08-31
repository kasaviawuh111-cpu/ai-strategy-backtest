"""K 类: 时间限制 (HoldUntilTimeUnit) - Phase A P0 补.

HoldUntilTimeUnit(unit): 持有到 unit 的末尾交易日 (月末/季末).
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from astock_backtest.holding_rules.base import HoldingRule, HoldingRuleResult, _force_close


_VALID_UNITS = {"month_end", "quarter_end"}


def _is_period_end(date: pd.Timestamp, unit: str) -> bool:
    if unit == "month_end":
        return date.is_month_end or _is_last_trading_day_of_month(date)
    if unit == "quarter_end":
        return date.is_quarter_end or _is_last_trading_day_of_quarter(date)
    return False


def _is_last_trading_day_of_month(d: pd.Timestamp) -> bool:
    """date 是否所属月份的最后一个交易日 (近似: 距离月末 <= 3 天即视为月末).

    严格的"最后一个交易日"要全交易日历, 这里近似用日期: 月末倒数 3 天里最后一个交易日.
    实际由引擎层用 ohlcv 中相邻交易日 + 月份变化判断更精确, 这是 fallback.
    """
    next_day = d + pd.Timedelta(days=1)
    return next_day.month != d.month and (d.month != (d + pd.Timedelta(days=2)).month)


def _is_last_trading_day_of_quarter(d: pd.Timestamp) -> bool:
    next_day = d + pd.Timedelta(days=1)
    return next_day.quarter != d.quarter and (d.quarter != (d + pd.Timedelta(days=2)).quarter)


class HoldUntilTimeUnit(HoldingRule):
    """持有到时间周期末尾 (month_end / quarter_end).

    实现: 从 buy_idx+1 开始扫 ohlcv["date"], 找第一个"该月/季的最后一个交易日".
    通过对相邻两行日期对比: 如果 date[i+1].month != date[i].month, 则 i 是该月末.
    """

    def __init__(self, unit: str) -> None:
        if unit not in _VALID_UNITS:
            raise ValueError(f"unit 必须是 {_VALID_UNITS}, 实际 {unit}")
        self.unit = unit
        self.name = f"hold_until_{unit}"
        self.description_easy = "持有到月末" if unit == "month_end" else "持有到季末"

    def find_exit(
        self,
        buy_idx: int,
        ohlcv: pd.DataFrame,
        *,
        reverse_signal_array: Optional[np.ndarray] = None,
    ) -> HoldingRuleResult:
        if buy_idx >= len(ohlcv) - 1:
            return _force_close(ohlcv, "force_close_end_no_holding_period")
        dates = pd.to_datetime(ohlcv["date"]).to_numpy()
        n = len(dates)
        # 从 buy_idx+1 开始扫, 找首个 date[i] 后一交易日跨越月/季
        for i in range(buy_idx + 1, n - 1):
            cur = pd.Timestamp(dates[i])
            nxt = pd.Timestamp(dates[i + 1])
            if self.unit == "month_end":
                if cur.month != nxt.month or cur.year != nxt.year:
                    return HoldingRuleResult(
                        exit_idx=i,
                        exit_reason=f"hit_hold_until_month_end",
                    )
            elif self.unit == "quarter_end":
                if cur.quarter != nxt.quarter or cur.year != nxt.year:
                    return HoldingRuleResult(
                        exit_idx=i,
                        exit_reason=f"hit_hold_until_quarter_end",
                    )
        # 末尾兜底
        return _force_close(ohlcv, f"force_close_end_no_{self.unit}_seen")
