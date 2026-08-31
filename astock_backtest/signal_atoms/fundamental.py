"""D 类: 估值/基本面原子 (Phase A 共 2 个)

D1 估值历史分位 (2): pe_below_history_10pct / pe_below_history_30pct

按 §3.7 量化定义:
  回溯 5 年 (1250 个交易日) PE 序列, 当前 pe_ttm < X% 分位即触发.
  PE 为 NaN 或 ≤ 0 (亏损) 时不触发.

数据对齐: valuation_daily 的日期序列可能跟 ohlcv 不完全一致 (停牌等),
按 ohlcv["date"] 轴 reindex + ffill, 保证输出 shape == len(ohlcv).
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from astock_backtest.signal_atoms.base import (
    DataDep,
    SignalAtom,
    SignalCategory,
    register,
)


def _pe_below_history_pct(
    ohlcv: pd.DataFrame,
    *,
    percentile: float,
    valuation: Optional[pd.DataFrame] = None,
    lookback: int = 1250,
    **_,
) -> np.ndarray:
    """当前 pe_ttm 在 lookback 日窗口内 < percentile 分位."""
    n = len(ohlcv)
    out = np.zeros(n, dtype=np.int8)
    if valuation is None or valuation.empty:
        return out

    # 把 valuation 按 ohlcv 日期轴对齐 + ffill
    val = valuation[["date", "pe_ttm"]].drop_duplicates("date").set_index("date").sort_index()
    pe_aligned = val["pe_ttm"].reindex(pd.Index(ohlcv["date"])).ffill()
    pe_arr = pe_aligned.to_numpy(dtype=float)

    # 滚动分位 (min_periods=lookback//2 防早期数据少时全 NaN)
    pe_series = pd.Series(pe_arr)
    threshold = pe_series.rolling(lookback, min_periods=lookback // 2).quantile(
        percentile / 100.0
    )

    valid = (pe_arr > 0) & ~np.isnan(pe_arr)
    threshold_arr = threshold.to_numpy(dtype=float)
    valid_thresh = ~np.isnan(threshold_arr)

    mask = valid & valid_thresh & (pe_arr < threshold_arr)
    out[mask] = 1
    return out


register(
    SignalAtom(
        id="pe_below_history_10pct",
        professional_name="当前 PE < 历史 10% 分位 (回溯 5 年)",
        layman_name="股价比过去 90% 时间都便宜",
        category=SignalCategory.FUNDAMENTAL,
        deps=(DataDep.OHLCV, DataDep.VALUATION),
        min_warmup=625,  # lookback // 2, 保证 rolling 至少有 min_periods 数据
        compute_fn=_pe_below_history_pct,
        params={"percentile": 10.0, "lookback": 1250},
    )
)
register(
    SignalAtom(
        id="pe_below_history_30pct",
        professional_name="当前 PE < 历史 30% 分位 (回溯 5 年)",
        layman_name="股价比过去 70% 时间都便宜",
        category=SignalCategory.FUNDAMENTAL,
        deps=(DataDep.OHLCV, DataDep.VALUATION),
        min_warmup=625,
        compute_fn=_pe_below_history_pct,
        params={"percentile": 30.0, "lookback": 1250},
    )
)
