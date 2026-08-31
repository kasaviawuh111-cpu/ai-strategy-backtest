"""C 类: 量能原子

C1 量能放大/萎缩 (5): volume_2x_ma20 / volume_3x_ma20 / volume_5x_ma20 / volume_below_half_ma20 / volume_max_60d
C2 量价配合 (4):    vol_up_price_up / vol_up_price_down / vol_down_price_up / vol_down_price_down
C3 换手率 (3):      turnover_above_5 / turnover_above_10 / turnover_below_1
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from astock_backtest.signal_atoms.base import (
    DataDep,
    SignalAtom,
    SignalCategory,
    register,
)


def _volume_above_ma20_x(ohlcv: pd.DataFrame, *, multiplier: float, **_) -> np.ndarray:
    """成交量 ≥ 20 日均量 × multiplier."""
    vol = ohlcv["volume"].astype(float)
    ma20 = vol.rolling(20).mean()
    mask = (vol >= ma20 * multiplier).fillna(False)
    return mask.to_numpy(dtype=np.int8)


def _volume_max_n(ohlcv: pd.DataFrame, *, n: int, **_) -> np.ndarray:
    """成交量 = 近 N 日新高 (含今日, 即 today >= max 前 N-1 日 + today)."""
    vol = ohlcv["volume"].astype(float)
    # 当日 vol >= 前 N-1 日的最大值 (即近 N 日含今日里今日是最大)
    prev_max = vol.shift(1).rolling(n - 1).max()
    mask = (vol >= prev_max).fillna(False)
    return mask.to_numpy(dtype=np.int8)


# C1 量能放大 5 个
_VOLUME_X_CONFIGS = [
    (2.0, "volume_2x_ma20", "成交量 ≥ 20 日均量 × 2", "成交量比平时大 2 倍"),
    (3.0, "volume_3x_ma20", "成交量 ≥ 20 日均量 × 3", "成交量比平时大 3 倍"),
    (5.0, "volume_5x_ma20", "成交量 ≥ 20 日均量 × 5", "成交量爆量 5 倍"),
]
for _m, _id, _pro, _lay in _VOLUME_X_CONFIGS:
    register(
        SignalAtom(
            id=_id,
            professional_name=_pro,
            layman_name=_lay,
            category=SignalCategory.VOLUME,
            deps=(DataDep.OHLCV,),
            min_warmup=21,
            compute_fn=_volume_above_ma20_x,
            params={"multiplier": _m},
        )
    )


def _volume_below_ma20_x(ohlcv: pd.DataFrame, *, multiplier: float, **_) -> np.ndarray:
    """成交量 < 20 日均量 × multiplier (萎缩)."""
    vol = ohlcv["volume"].astype(float)
    ma20 = vol.rolling(20).mean()
    mask = (vol < ma20 * multiplier).fillna(False)
    return mask.to_numpy(dtype=np.int8)


register(
    SignalAtom(
        id="volume_below_half_ma20",
        professional_name="成交量 < 20 日均量 × 0.5",
        layman_name="成交萎缩到平时一半以下",
        category=SignalCategory.VOLUME,
        deps=(DataDep.OHLCV,),
        min_warmup=21,
        compute_fn=_volume_below_ma20_x,
        params={"multiplier": 0.5},
    )
)
register(
    SignalAtom(
        id="volume_max_60d",
        professional_name="成交量 = 近 60 日新高",
        layman_name="成交量创近 3 个月新高",
        category=SignalCategory.VOLUME,
        deps=(DataDep.OHLCV,),
        min_warmup=60,
        compute_fn=_volume_max_n,
        params={"n": 60},
    )
)


# ============================================================
# C2 量价配合 (4 个) - §3.7 量化定义: 量增=>1.2× / 量减=<0.8×
# ============================================================


def _vol_up_price_up(ohlcv: pd.DataFrame, **_) -> np.ndarray:
    """放量上涨: vol > MA20×1.2 且 close > prev_close."""
    vol = ohlcv["volume"].astype(float)
    closes = ohlcv["close"].astype(float)
    ma20 = vol.rolling(20).mean()
    mask = ((vol > ma20 * 1.2) & (closes > closes.shift(1))).fillna(False)
    return mask.to_numpy(dtype=np.int8)


def _vol_up_price_down(ohlcv: pd.DataFrame, **_) -> np.ndarray:
    vol = ohlcv["volume"].astype(float)
    closes = ohlcv["close"].astype(float)
    ma20 = vol.rolling(20).mean()
    mask = ((vol > ma20 * 1.2) & (closes < closes.shift(1))).fillna(False)
    return mask.to_numpy(dtype=np.int8)


def _vol_down_price_up(ohlcv: pd.DataFrame, **_) -> np.ndarray:
    vol = ohlcv["volume"].astype(float)
    closes = ohlcv["close"].astype(float)
    ma20 = vol.rolling(20).mean()
    mask = ((vol < ma20 * 0.8) & (closes > closes.shift(1))).fillna(False)
    return mask.to_numpy(dtype=np.int8)


def _vol_down_price_down(ohlcv: pd.DataFrame, **_) -> np.ndarray:
    vol = ohlcv["volume"].astype(float)
    closes = ohlcv["close"].astype(float)
    ma20 = vol.rolling(20).mean()
    mask = ((vol < ma20 * 0.8) & (closes < closes.shift(1))).fillna(False)
    return mask.to_numpy(dtype=np.int8)


_C2_CONFIGS = [
    ("vol_up_price_up", "量增价涨", "放量上涨 (健康)", _vol_up_price_up),
    ("vol_up_price_down", "量增价跌", "放量下跌 (可能在出货)", _vol_up_price_down),
    ("vol_down_price_up", "量减价涨", "缩量上涨 (惜售)", _vol_down_price_up),
    ("vol_down_price_down", "量缩价跌", "缩量下跌 (抛压衰竭)", _vol_down_price_down),
]
for _id, _pro, _lay, _fn in _C2_CONFIGS:
    register(
        SignalAtom(
            id=_id,
            professional_name=_pro,
            layman_name=_lay,
            category=SignalCategory.VOLUME,
            deps=(DataDep.OHLCV,),
            min_warmup=21,
            compute_fn=_fn,
        )
    )


# ============================================================
# C3 换手率 (3 个)
# 数据源: ohlcv.turnover_rate (单位 %), data_loader.py 已有
# ============================================================


def _turnover_above(ohlcv: pd.DataFrame, *, threshold: float, **_) -> np.ndarray:
    if "turnover_rate" not in ohlcv.columns:
        return np.zeros(len(ohlcv), dtype=np.int8)
    tr = ohlcv["turnover_rate"].astype(float)
    mask = (tr > threshold).fillna(False)
    return mask.to_numpy(dtype=np.int8)


def _turnover_below(ohlcv: pd.DataFrame, *, threshold: float, **_) -> np.ndarray:
    if "turnover_rate" not in ohlcv.columns:
        return np.zeros(len(ohlcv), dtype=np.int8)
    tr = ohlcv["turnover_rate"].astype(float)
    mask = (tr < threshold).fillna(False)
    return mask.to_numpy(dtype=np.int8)


register(
    SignalAtom(
        id="turnover_above_5",
        professional_name="换手率 > 5%",
        layman_name="当日交投活跃",
        category=SignalCategory.VOLUME,
        deps=(DataDep.OHLCV,),
        min_warmup=1,
        compute_fn=_turnover_above,
        params={"threshold": 5.0},
    )
)
register(
    SignalAtom(
        id="turnover_above_10",
        professional_name="换手率 > 10%",
        layman_name="当日高度活跃",
        category=SignalCategory.VOLUME,
        deps=(DataDep.OHLCV,),
        min_warmup=1,
        compute_fn=_turnover_above,
        params={"threshold": 10.0},
    )
)
register(
    SignalAtom(
        id="turnover_below_1",
        professional_name="换手率 < 1%",
        layman_name="当日交投冷清",
        category=SignalCategory.VOLUME,
        deps=(DataDep.OHLCV,),
        min_warmup=1,
        compute_fn=_turnover_below,
        params={"threshold": 1.0},
    )
)
