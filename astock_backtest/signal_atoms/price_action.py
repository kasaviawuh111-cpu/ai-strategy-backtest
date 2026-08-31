"""B 类: 价格行为原子 (Phase A 共 6 个)

B1 N 日新高 (3) + N 日新低 (1):  new_high_20d / new_high_60d / new_high_120d / new_low_20d
B2 单日涨跌 (2): limit_up / single_day_down_5

注: limit_up 用板块涨停阈值 (主板 10% / 创业板科创板 20% / 北交所 30% / ST 5%).
复用 services/board_limits.py 的 get_up_limit_pct(stock_code).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from astock_backtest.services.board_limits import limit_threshold
from astock_backtest.signal_atoms.base import (
    DataDep,
    SignalAtom,
    SignalCategory,
    register,
)


# ============================================================
# B1 N 日新高/新低 (4 个)
# ============================================================


def _new_high_n(ohlcv: pd.DataFrame, *, n: int, **_) -> np.ndarray:
    """突破前 N 日 close 最高: 当日 close > 前 N 日 close 最大 (不含当日)."""
    closes = ohlcv["close"].astype(float)
    rolling_max = closes.shift(1).rolling(n).max()
    mask = (closes > rolling_max).fillna(False)
    return mask.to_numpy(dtype=np.int8)


def _new_low_n(ohlcv: pd.DataFrame, *, n: int, **_) -> np.ndarray:
    closes = ohlcv["close"].astype(float)
    rolling_min = closes.shift(1).rolling(n).min()
    mask = (closes < rolling_min).fillna(False)
    return mask.to_numpy(dtype=np.int8)


_NEW_HIGH_CONFIGS = [
    (5, "突破前 5 日 close 最高", "突破 5 天高点"),
    (10, "突破前 10 日 close 最高", "突破 10 天高点"),
    (20, "突破前 20 日 close 最高", "突破月度高点"),
    (60, "突破前 60 日 close 最高", "突破季度高点"),
    (120, "突破前 120 日 close 最高", "突破半年高点"),
]
for _n, _pro, _lay in _NEW_HIGH_CONFIGS:
    register(
        SignalAtom(
            id=f"new_high_{_n}d",
            professional_name=_pro,
            layman_name=_lay,
            category=SignalCategory.PRICE_ACTION,
            deps=(DataDep.OHLCV,),
            min_warmup=_n + 1,
            compute_fn=_new_high_n,
            params={"n": _n},
        )
    )

# 新低补全 (Phase A 只有 new_low_20d, P0 补 5d/10d/60d/120d)
_NEW_LOW_CONFIGS = [
    (5, "跌破前 5 日 close 最低", "跌破 5 天低点"),
    (10, "跌破前 10 日 close 最低", "跌破 10 天低点"),
    (20, "跌破前 20 日 close 最低", "跌破月度低点"),
    (60, "跌破前 60 日 close 最低", "跌破季度低点"),
    (120, "跌破前 120 日 close 最低", "跌破半年低点"),
]
for _n, _pro, _lay in _NEW_LOW_CONFIGS:
    register(
        SignalAtom(
            id=f"new_low_{_n}d",
            professional_name=_pro,
            layman_name=_lay,
            category=SignalCategory.PRICE_ACTION,
            deps=(DataDep.OHLCV,),
            min_warmup=_n + 1,
            compute_fn=_new_low_n,
            params={"n": _n},
        )
    )


# B1b 历史新高 (2026-07-04): close > 此前全部 close 的累计最高 (不含当日)。
# K 线库起点 2011, 故语义 = 数据起点以来新高 (2011 后上市的即上市以来新高)。
# warmup 250 (约一年) 挡新上市股前几天就"创历史新高"的假信号。
def _new_high_alltime(ohlcv: pd.DataFrame, **_) -> np.ndarray:
    closes = ohlcv["close"].astype(float)
    prev_max = closes.shift(1).cummax()
    mask = (closes > prev_max).fillna(False)
    return mask.to_numpy(dtype=np.int8)


register(
    SignalAtom(
        id="new_high_alltime",
        professional_name="收盘价突破历史最高收盘价 (数据起点 2011)",
        layman_name="收盘价创历史新高",
        category=SignalCategory.PRICE_ACTION,
        deps=(DataDep.OHLCV,),
        min_warmup=250,
        compute_fn=_new_high_alltime,
        params={},
    )
)


# ============================================================
# B2 单日涨跌 (2 个)
# ============================================================


def _limit_up(ohlcv: pd.DataFrame, **_) -> np.ndarray:
    """涨停: pct_change ≥ 板块涨停阈值 (board_limits.limit_threshold 已含 0.2% 容差).

    注: ohlcv 行内已带 stock_code 列 (DataStore 切片时保留). 阈值返百分数 (9.8 表示 9.8%).
    """
    if "stock_code" not in ohlcv.columns or ohlcv.empty:
        return np.zeros(len(ohlcv), dtype=np.int8)
    code = str(ohlcv["stock_code"].iloc[0])
    threshold_pct = limit_threshold(code)  # 9.8 / 19.6 / 29.4
    closes = ohlcv["close"].astype(float)
    prev = closes.shift(1)
    pct_change = (closes / prev - 1.0) * 100.0
    mask = (pct_change >= threshold_pct).fillna(False)
    return mask.to_numpy(dtype=np.int8)


def _limit_down(ohlcv: pd.DataFrame, **_) -> np.ndarray:
    """跌停: pct_change ≤ -板块涨停阈值."""
    if "stock_code" not in ohlcv.columns or ohlcv.empty:
        return np.zeros(len(ohlcv), dtype=np.int8)
    code = str(ohlcv["stock_code"].iloc[0])
    threshold_pct = limit_threshold(code)
    closes = ohlcv["close"].astype(float)
    prev = closes.shift(1)
    pct_change = (closes / prev - 1.0) * 100.0
    mask = (pct_change <= -threshold_pct).fillna(False)
    return mask.to_numpy(dtype=np.int8)


def _single_day_up_pct(ohlcv: pd.DataFrame, *, pct: float, **_) -> np.ndarray:
    """单日涨幅 ≥ pct%."""
    closes = ohlcv["close"].astype(float)
    prev = closes.shift(1)
    chg = (closes / prev - 1.0) * 100.0
    mask = (chg >= pct).fillna(False)
    return mask.to_numpy(dtype=np.int8)


def _single_day_down_pct(ohlcv: pd.DataFrame, *, pct: float, **_) -> np.ndarray:
    """单日跌幅 ≤ -pct%."""
    closes = ohlcv["close"].astype(float)
    prev = closes.shift(1)
    chg = (closes / prev - 1.0) * 100.0
    mask = (chg <= -pct).fillna(False)
    return mask.to_numpy(dtype=np.int8)


register(
    SignalAtom(
        id="limit_up",
        professional_name="涨停 (按板块阈值)",
        layman_name="涨停",
        category=SignalCategory.PRICE_ACTION,
        deps=(DataDep.OHLCV,),
        min_warmup=2,
        compute_fn=_limit_up,
    )
)
register(
    SignalAtom(
        id="limit_down",
        professional_name="跌停 (按板块阈值)",
        layman_name="跌停",
        category=SignalCategory.PRICE_ACTION,
        deps=(DataDep.OHLCV,),
        min_warmup=2,
        compute_fn=_limit_down,
    )
)

# B2 单日涨跌阈值补全 (P0 补 up_3/5/7, down_3/7; Phase A 已有 down_5)
_SINGLE_DAY_UP_CONFIGS = [(3, "单日大涨 3% 以上"), (5, "单日大涨 5% 以上"), (7, "单日大涨 7% 以上")]
for _pct, _lay in _SINGLE_DAY_UP_CONFIGS:
    register(
        SignalAtom(
            id=f"single_day_up_{_pct}",
            professional_name=f"单日涨幅 ≥ {_pct}%",
            layman_name=_lay,
            category=SignalCategory.PRICE_ACTION,
            deps=(DataDep.OHLCV,),
            min_warmup=2,
            compute_fn=_single_day_up_pct,
            params={"pct": float(_pct)},
        )
    )

register(
    SignalAtom(
        id="single_day_down_3",
        professional_name="单日跌幅 ≤ -3%",
        layman_name="单日下跌 3% 以上",
        category=SignalCategory.PRICE_ACTION,
        deps=(DataDep.OHLCV,),
        min_warmup=2,
        compute_fn=_single_day_down_pct,
        params={"pct": 3.0},
    )
)
register(
    SignalAtom(
        id="single_day_down_5",
        professional_name="单日跌幅 ≤ -5%",
        layman_name="单日大跌 5% 以上",
        category=SignalCategory.PRICE_ACTION,
        deps=(DataDep.OHLCV,),
        min_warmup=2,
        compute_fn=_single_day_down_pct,
        params={"pct": 5.0},
    )
)
register(
    SignalAtom(
        id="single_day_down_7",
        professional_name="单日跌幅 ≤ -7%",
        layman_name="单日大跌 7% 以上",
        category=SignalCategory.PRICE_ACTION,
        deps=(DataDep.OHLCV,),
        min_warmup=2,
        compute_fn=_single_day_down_pct,
        params={"pct": 7.0},
    )
)


# ============================================================
# B3 连续涨跌 (P0 补 4 个)
# ============================================================


def _consecutive_up(ohlcv: pd.DataFrame, *, n: int, **_) -> np.ndarray:
    """连涨 N 天: 连续 N 天 close > prev_close."""
    closes = ohlcv["close"].astype(float)
    up = (closes > closes.shift(1)).fillna(False)
    # rolling N 天 all True
    rolling_all = up.rolling(n).sum() >= n
    return rolling_all.fillna(False).to_numpy(dtype=np.int8)


def _consecutive_down(ohlcv: pd.DataFrame, *, n: int, **_) -> np.ndarray:
    closes = ohlcv["close"].astype(float)
    down = (closes < closes.shift(1)).fillna(False)
    rolling_all = down.rolling(n).sum() >= n
    return rolling_all.fillna(False).to_numpy(dtype=np.int8)


_CONSEC_UP_CONFIGS = [
    (2, "连涨 2 天", "连续上涨 2 天"),
    (3, "连涨 3 天", "连续上涨 3 天"),
    (5, "连涨 5 天", "连续上涨 5 天 (强势)"),
]
for _n, _pro, _lay in _CONSEC_UP_CONFIGS:
    register(
        SignalAtom(
            id=f"consecutive_up_{_n}",
            professional_name=_pro,
            layman_name=_lay,
            category=SignalCategory.PRICE_ACTION,
            deps=(DataDep.OHLCV,),
            min_warmup=_n + 1,
            compute_fn=_consecutive_up,
            params={"n": _n},
        )
    )

register(
    SignalAtom(
        id="consecutive_down_3",
        professional_name="连跌 3 天",
        layman_name="连续下跌 3 天",
        category=SignalCategory.PRICE_ACTION,
        deps=(DataDep.OHLCV,),
        min_warmup=4,
        compute_fn=_consecutive_down,
        params={"n": 3},
    )
)


# ============================================================
# B4 跳空 (P0 补 2 个)
# ============================================================


def _gap_up(ohlcv: pd.DataFrame, **_) -> np.ndarray:
    """跳空高开: open > 昨日 high."""
    open_arr = ohlcv["open"].astype(float)
    prev_high = ohlcv["high"].astype(float).shift(1)
    mask = (open_arr > prev_high).fillna(False)
    return mask.to_numpy(dtype=np.int8)


def _gap_down(ohlcv: pd.DataFrame, **_) -> np.ndarray:
    """跳空低开: open < 昨日 low."""
    open_arr = ohlcv["open"].astype(float)
    prev_low = ohlcv["low"].astype(float).shift(1)
    mask = (open_arr < prev_low).fillna(False)
    return mask.to_numpy(dtype=np.int8)


register(
    SignalAtom(
        id="gap_up",
        professional_name="跳空高开",
        layman_name="开盘价高于昨日最高价",
        category=SignalCategory.PRICE_ACTION,
        deps=(DataDep.OHLCV,),
        min_warmup=2,
        compute_fn=_gap_up,
    )
)
register(
    SignalAtom(
        id="gap_down",
        professional_name="跳空低开",
        layman_name="开盘价低于昨日最低价",
        category=SignalCategory.PRICE_ACTION,
        deps=(DataDep.OHLCV,),
        min_warmup=2,
        compute_fn=_gap_down,
    )
)


# ============================================================
# B5 K 线形态 (6 个) — 单根 K 线形态
# ============================================================
# 实体 body = |close-open|;  全幅 rng = high-low
# 上影 upper = high - max(open,close);  下影 lower = min(open,close) - low


def _candle_parts(ohlcv: pd.DataFrame):
    """返 (body, rng, upper_shadow, lower_shadow) 四个 Series."""
    o = ohlcv["open"].astype(float)
    h = ohlcv["high"].astype(float)
    low_ = ohlcv["low"].astype(float)
    c = ohlcv["close"].astype(float)
    body = (c - o).abs()
    rng = h - low_
    upper = h - o.combine(c, max)
    lower = o.combine(c, min) - low_
    return body, rng, upper, lower


def _doji(ohlcv: pd.DataFrame, **_) -> np.ndarray:
    """十字星: 实体/全幅 < 0.1 (开收价接近, 多空胶着). 全幅为 0 不算."""
    body, rng, _, _ = _candle_parts(ohlcv)
    mask = ((rng > 0) & (body / rng.replace(0, np.nan) < 0.1)).fillna(False)
    return mask.to_numpy(dtype=np.int8)


def _hammer(ohlcv: pd.DataFrame, **_) -> np.ndarray:
    """锤子线: 下影 ≥ 2×实体 且 上影 ≤ 实体 (长下影小实体, 可能止跌)."""
    body, rng, upper, lower = _candle_parts(ohlcv)
    mask = ((rng > 0) & (lower >= 2 * body) & (upper <= body)).fillna(False)
    return mask.to_numpy(dtype=np.int8)


def _inverted_hammer(ohlcv: pd.DataFrame, **_) -> np.ndarray:
    """倒锤子: 上影 ≥ 2×实体 且 下影 ≤ 实体 (长上影小实体)."""
    body, rng, upper, lower = _candle_parts(ohlcv)
    mask = ((rng > 0) & (upper >= 2 * body) & (lower <= body)).fillna(False)
    return mask.to_numpy(dtype=np.int8)


def _long_upper_shadow(ohlcv: pd.DataFrame, **_) -> np.ndarray:
    """长上影: 上影 ≥ 2×实体 且 实体 > 0 (冲高回落)."""
    body, _, upper, _ = _candle_parts(ohlcv)
    mask = ((body > 0) & (upper >= 2 * body)).fillna(False)
    return mask.to_numpy(dtype=np.int8)


def _long_lower_shadow(ohlcv: pd.DataFrame, **_) -> np.ndarray:
    """长下影: 下影 ≥ 2×实体 且 实体 > 0 (杀跌回升)."""
    body, _, _, lower = _candle_parts(ohlcv)
    mask = ((body > 0) & (lower >= 2 * body)).fillna(False)
    return mask.to_numpy(dtype=np.int8)


def _one_word_limit(ohlcv: pd.DataFrame, **_) -> np.ndarray:
    """一字涨停: 全幅/close < 0.5% (几乎无波动) 且 当日涨停 (按板块阈值)."""
    if "stock_code" not in ohlcv.columns or ohlcv.empty:
        return np.zeros(len(ohlcv), dtype=np.int8)
    code = str(ohlcv["stock_code"].iloc[0])
    threshold_pct = limit_threshold(code)
    h = ohlcv["high"].astype(float)
    low_ = ohlcv["low"].astype(float)
    c = ohlcv["close"].astype(float)
    prev = c.shift(1)
    pct_change = (c / prev - 1.0) * 100.0
    flat = (h - low_) / c.replace(0, np.nan) < 0.005
    mask = (flat & (pct_change >= threshold_pct)).fillna(False)
    return mask.to_numpy(dtype=np.int8)


_B5_CONFIGS = [
    ("doji", "十字星", "开盘收盘价接近 (多空胶着)", _doji, 1),
    ("hammer", "锤子线", "长下影小实体 (可能止跌)", _hammer, 1),
    ("inverted_hammer", "倒锤子", "长上影小实体", _inverted_hammer, 1),
    ("long_upper_shadow", "长上影", "股价冲高回落", _long_upper_shadow, 1),
    ("long_lower_shadow", "长下影", "股价杀跌回升", _long_lower_shadow, 1),
    ("one_word_limit", "一字涨停", "全天封死涨停板", _one_word_limit, 2),
]
for _id, _pro, _lay, _fn, _warmup in _B5_CONFIGS:
    register(
        SignalAtom(
            id=_id,
            professional_name=_pro,
            layman_name=_lay,
            category=SignalCategory.PRICE_ACTION,
            deps=(DataDep.OHLCV,),
            min_warmup=_warmup,
            compute_fn=_fn,
        )
    )
