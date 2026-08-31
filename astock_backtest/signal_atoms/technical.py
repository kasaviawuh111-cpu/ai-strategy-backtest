"""A 类: 技术指标原子

A1 均线穿越:   ma_*_cross_up_* (9) + ma_*_cross_down_* (5)
A2 价格相对均线: close_above/below_ma_{20,60,250} (6)
A3 MACD (5):   macd_golden_cross / macd_death_cross / macd_above_zero / macd_below_zero / macd_hist_turn_positive
A4 RSI (6):    rsi_oversold_recover_{30,20} / rsi_overbought_decline_{70,80} / rsi_below_30 / rsi_above_70
A5 KDJ (4):    kdj_golden_cross_low / kdj_death_cross_high / kdj_j_above_100 / kdj_j_below_0
A6 布林带 (5): bollinger_lower/upper_touch / bollinger_lower/upper_break / bollinger_squeeze
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


# ============================================================
# A1 均线穿越 (5 个)
# ============================================================


def _ma_cross_up(ohlcv: pd.DataFrame, *, fast: int, slow: int, **_) -> np.ndarray:
    """fast 均线上穿 slow 均线: 当日 fast>slow 且昨日 fast<=slow."""
    closes = ohlcv["close"].astype(float)
    fast_ma = closes.rolling(fast).mean()
    slow_ma = closes.rolling(slow).mean()
    diff = fast_ma - slow_ma
    diff_prev = diff.shift(1)
    mask = ((diff_prev <= 0) & (diff > 0)).fillna(False)
    return mask.to_numpy(dtype=np.int8)


_MA_PAIRS = [
    (5, 10), (5, 20), (5, 60), (10, 20),
    (10, 60), (20, 60), (20, 120), (60, 120),  # P0 补全 (母清单 A1 漏的 4 对)
    (60, 250),
]
_LAYMAN_MA = {
    (5, 10): "短期均价突破 10 日均价",
    (5, 20): "短期均价突破月均价",
    (5, 60): "短期均价突破季均价",
    (10, 20): "10 日均价突破月均价",
    (10, 60): "10 日均价突破季均价",
    (20, 60): "月均价突破季均价",
    (20, 120): "月均价突破半年均价",
    (60, 120): "季均价突破半年均价",
    (60, 250): "季均价突破年均价 (大趋势转向)",
}

for _f, _s in _MA_PAIRS:
    register(
        SignalAtom(
            id=f"ma_{_f}_cross_up_{_s}",
            professional_name=f"MA{_f} 上穿 MA{_s}",
            layman_name=_LAYMAN_MA[(_f, _s)],
            category=SignalCategory.TECHNICAL,
            deps=(DataDep.OHLCV,),
            min_warmup=_s + 1,
            compute_fn=_ma_cross_up,
            params={"fast": _f, "slow": _s},
        )
    )


# A1 下穿 (P0 补 5 个)


def _ma_cross_down(ohlcv: pd.DataFrame, *, fast: int, slow: int, **_) -> np.ndarray:
    """fast 均线下穿 slow 均线: 当日 fast<slow 且昨日 fast>=slow."""
    closes = ohlcv["close"].astype(float)
    fast_ma = closes.rolling(fast).mean()
    slow_ma = closes.rolling(slow).mean()
    diff = fast_ma - slow_ma
    diff_prev = diff.shift(1)
    mask = ((diff_prev >= 0) & (diff < 0)).fillna(False)
    return mask.to_numpy(dtype=np.int8)


_MA_CROSS_DOWN_PAIRS = [(5, 10), (5, 20), (10, 20), (20, 60), (60, 250)]
_LAYMAN_MA_DOWN = {
    (5, 10): "短期均价跌破 10 日均价",
    (5, 20): "短期均价跌破月均价",
    (10, 20): "10 日均价跌破月均价",
    (20, 60): "月均价跌破季均价",
    (60, 250): "季均价跌破年均价 (大趋势转弱)",
}

for _f, _s in _MA_CROSS_DOWN_PAIRS:
    register(
        SignalAtom(
            id=f"ma_{_f}_cross_down_{_s}",
            professional_name=f"MA{_f} 下穿 MA{_s}",
            layman_name=_LAYMAN_MA_DOWN[(_f, _s)],
            category=SignalCategory.TECHNICAL,
            deps=(DataDep.OHLCV,),
            min_warmup=_s + 1,
            compute_fn=_ma_cross_down,
            params={"fast": _f, "slow": _s},
        )
    )


# A2 价格相对均线 (P0 补 6 个 - 状态原子)


def _close_above_ma(ohlcv: pd.DataFrame, *, period: int, **_) -> np.ndarray:
    """close > MA_period (状态原子, 每天判断)."""
    closes = ohlcv["close"].astype(float)
    ma = closes.rolling(period).mean()
    mask = (closes > ma).fillna(False)
    return mask.to_numpy(dtype=np.int8)


def _close_below_ma(ohlcv: pd.DataFrame, *, period: int, **_) -> np.ndarray:
    closes = ohlcv["close"].astype(float)
    ma = closes.rolling(period).mean()
    mask = (closes < ma).fillna(False)
    return mask.to_numpy(dtype=np.int8)


_A2_CONFIGS = [
    (20, "close_above_ma_20", "close > MA20", "股价高于月均价", _close_above_ma),
    (60, "close_above_ma_60", "close > MA60", "股价高于季均价", _close_above_ma),
    (250, "close_above_ma_250", "close > MA250", "股价高于年均价 (长期向好)", _close_above_ma),
    (20, "close_below_ma_20", "close < MA20", "股价低于月均价", _close_below_ma),
    (60, "close_below_ma_60", "close < MA60", "股价低于季均价", _close_below_ma),
    (250, "close_below_ma_250", "close < MA250", "股价低于年均价 (长期偏弱)", _close_below_ma),
]

for _p, _id, _pro, _lay, _fn in _A2_CONFIGS:
    register(
        SignalAtom(
            id=_id,
            professional_name=_pro,
            layman_name=_lay,
            category=SignalCategory.TECHNICAL,
            deps=(DataDep.OHLCV,),
            min_warmup=_p + 1,
            compute_fn=_fn,
            params={"period": _p},
        )
    )


# ============================================================
# A3 MACD (5 个) — 标准 (12, 26, 9)
# ============================================================


def _macd_components(closes: pd.Series, fast: int = 12, slow: int = 26, sig: int = 9):
    ema_fast = closes.ewm(span=fast, adjust=False).mean()
    ema_slow = closes.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=sig, adjust=False).mean()
    hist = dif - dea
    return dif, dea, hist


def _macd_golden_cross(ohlcv: pd.DataFrame, **_) -> np.ndarray:
    """DIF 上穿 DEA (hist 由负转正)."""
    closes = ohlcv["close"].astype(float)
    _, _, hist = _macd_components(closes)
    hist_prev = hist.shift(1)
    mask = ((hist_prev <= 0) & (hist > 0)).fillna(False)
    return mask.to_numpy(dtype=np.int8)


def _macd_death_cross(ohlcv: pd.DataFrame, **_) -> np.ndarray:
    closes = ohlcv["close"].astype(float)
    _, _, hist = _macd_components(closes)
    hist_prev = hist.shift(1)
    mask = ((hist_prev >= 0) & (hist < 0)).fillna(False)
    return mask.to_numpy(dtype=np.int8)


def _macd_above_zero(ohlcv: pd.DataFrame, **_) -> np.ndarray:
    """DIF 上穿 0 轴: 昨日 DIF<=0, 今日 DIF>0."""
    closes = ohlcv["close"].astype(float)
    dif, _, _ = _macd_components(closes)
    dif_prev = dif.shift(1)
    mask = ((dif_prev <= 0) & (dif > 0)).fillna(False)
    return mask.to_numpy(dtype=np.int8)


def _macd_below_zero(ohlcv: pd.DataFrame, **_) -> np.ndarray:
    closes = ohlcv["close"].astype(float)
    dif, _, _ = _macd_components(closes)
    dif_prev = dif.shift(1)
    mask = ((dif_prev >= 0) & (dif < 0)).fillna(False)
    return mask.to_numpy(dtype=np.int8)


def _macd_hist_turn_positive(ohlcv: pd.DataFrame, **_) -> np.ndarray:
    """红柱出现: hist 由负转正 (跟金叉等价, 但严格按 §3 列单独保留, 用作"动能由弱转强"语义)."""
    closes = ohlcv["close"].astype(float)
    _, _, hist = _macd_components(closes)
    hist_prev = hist.shift(1)
    mask = ((hist_prev < 0) & (hist > 0)).fillna(False)
    return mask.to_numpy(dtype=np.int8)


_MACD_WARMUP = 26 + 9 + 2  # slow + signal + 安全边距

register(
    SignalAtom(
        id="macd_golden_cross",
        professional_name="MACD 金叉",
        layman_name="短期力量开始超过长期力量",
        category=SignalCategory.TECHNICAL,
        deps=(DataDep.OHLCV,),
        min_warmup=_MACD_WARMUP,
        compute_fn=_macd_golden_cross,
    )
)
register(
    SignalAtom(
        id="macd_death_cross",
        professional_name="MACD 死叉",
        layman_name="短期力量被长期力量压住",
        category=SignalCategory.TECHNICAL,
        deps=(DataDep.OHLCV,),
        min_warmup=_MACD_WARMUP,
        compute_fn=_macd_death_cross,
    )
)
register(
    SignalAtom(
        id="macd_above_zero",
        professional_name="DIF 上穿 0 轴",
        layman_name="趋势进入多头区",
        category=SignalCategory.TECHNICAL,
        deps=(DataDep.OHLCV,),
        min_warmup=_MACD_WARMUP,
        compute_fn=_macd_above_zero,
    )
)
register(
    SignalAtom(
        id="macd_below_zero",
        professional_name="DIF 下穿 0 轴",
        layman_name="趋势进入空头区",
        category=SignalCategory.TECHNICAL,
        deps=(DataDep.OHLCV,),
        min_warmup=_MACD_WARMUP,
        compute_fn=_macd_below_zero,
    )
)
register(
    SignalAtom(
        id="macd_hist_turn_positive",
        professional_name="红柱出现",
        layman_name="短期动能由弱转强",
        category=SignalCategory.TECHNICAL,
        deps=(DataDep.OHLCV,),
        min_warmup=_MACD_WARMUP,
        compute_fn=_macd_hist_turn_positive,
    )
)


# ============================================================
# A4 RSI (6 个) — 标准 14 周期
# ============================================================


def _rsi_series(closes: pd.Series, period: int = 14) -> pd.Series:
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-9)
    return 100 - 100 / (1 + rs)


def _rsi_oversold_recover(ohlcv: pd.DataFrame, *, threshold: float, **_) -> np.ndarray:
    """RSI 从 < threshold 回升至 >= threshold (反弹起点)."""
    closes = ohlcv["close"].astype(float)
    rsi = _rsi_series(closes)
    rsi_prev = rsi.shift(1)
    mask = ((rsi_prev < threshold) & (rsi >= threshold)).fillna(False)
    return mask.to_numpy(dtype=np.int8)


def _rsi_overbought_decline(ohlcv: pd.DataFrame, *, threshold: float, **_) -> np.ndarray:
    closes = ohlcv["close"].astype(float)
    rsi = _rsi_series(closes)
    rsi_prev = rsi.shift(1)
    mask = ((rsi_prev > threshold) & (rsi <= threshold)).fillna(False)
    return mask.to_numpy(dtype=np.int8)


def _rsi_below(ohlcv: pd.DataFrame, *, threshold: float, **_) -> np.ndarray:
    """当前 RSI < threshold (状态原子, 不是事件)."""
    closes = ohlcv["close"].astype(float)
    rsi = _rsi_series(closes)
    return (rsi < threshold).fillna(False).to_numpy(dtype=np.int8)


def _rsi_above(ohlcv: pd.DataFrame, *, threshold: float, **_) -> np.ndarray:
    closes = ohlcv["close"].astype(float)
    rsi = _rsi_series(closes)
    return (rsi > threshold).fillna(False).to_numpy(dtype=np.int8)


_RSI_WARMUP = 14 + 2

register(
    SignalAtom(
        id="rsi_oversold_recover_30",
        professional_name="RSI 从 < 30 回升至 ≥ 30",
        layman_name="跌过头开始反弹",
        category=SignalCategory.TECHNICAL,
        deps=(DataDep.OHLCV,),
        min_warmup=_RSI_WARMUP,
        compute_fn=_rsi_oversold_recover,
        params={"threshold": 30.0},
    )
)
register(
    SignalAtom(
        id="rsi_oversold_recover_20",
        professional_name="RSI 从 < 20 回升至 ≥ 20",
        layman_name="深度超跌反弹",
        category=SignalCategory.TECHNICAL,
        deps=(DataDep.OHLCV,),
        min_warmup=_RSI_WARMUP,
        compute_fn=_rsi_oversold_recover,
        params={"threshold": 20.0},
    )
)
register(
    SignalAtom(
        id="rsi_overbought_decline_70",
        professional_name="RSI 从 > 70 回落至 ≤ 70",
        layman_name="涨过头开始回落",
        category=SignalCategory.TECHNICAL,
        deps=(DataDep.OHLCV,),
        min_warmup=_RSI_WARMUP,
        compute_fn=_rsi_overbought_decline,
        params={"threshold": 70.0},
    )
)
register(
    SignalAtom(
        id="rsi_overbought_decline_80",
        professional_name="RSI 从 > 80 回落至 ≤ 80",
        layman_name="严重涨过头开始回落",
        category=SignalCategory.TECHNICAL,
        deps=(DataDep.OHLCV,),
        min_warmup=_RSI_WARMUP,
        compute_fn=_rsi_overbought_decline,
        params={"threshold": 80.0},
    )
)
register(
    SignalAtom(
        id="rsi_below_30",
        professional_name="RSI < 30",
        layman_name="当前处于超跌区",
        category=SignalCategory.TECHNICAL,
        deps=(DataDep.OHLCV,),
        min_warmup=_RSI_WARMUP,
        compute_fn=_rsi_below,
        params={"threshold": 30.0},
    )
)
register(
    SignalAtom(
        id="rsi_above_70",
        professional_name="RSI > 70",
        layman_name="当前处于超买区",
        category=SignalCategory.TECHNICAL,
        deps=(DataDep.OHLCV,),
        min_warmup=_RSI_WARMUP,
        compute_fn=_rsi_above,
        params={"threshold": 70.0},
    )
)


# ============================================================
# A5 KDJ (4 个) — 标准 (9, 3, 3)
# ============================================================


def _kdj_components(ohlcv: pd.DataFrame, n: int = 9):
    """算 KDJ 三线 (标准 9/3/3).

    RSV = (close - low_n) / (high_n - low_n) × 100
    K   = 2/3·K_prev + 1/3·RSV   (= ewm com=2, 等价通达信 SMA(RSV,3,1))
    D   = 2/3·D_prev + 1/3·K
    J   = 3K - 2D

    一字板等 high_n==low_n 时分母 0: RSV 取前值 (ffill), 起始无前值取 50 (中性).
    K/D 用 ewm(adjust=False) 起始值=首个 RSV (非经典 50 初值), 但
    min_warmup 已留 15 行缓冲, ewm com=2 的初值权重 (2/3)^15≈0.002 可忽略.
    """
    high = ohlcv["high"].astype(float)
    low = ohlcv["low"].astype(float)
    close = ohlcv["close"].astype(float)
    low_n = low.rolling(n).min()
    high_n = high.rolling(n).max()
    denom = high_n - low_n
    rsv = (close - low_n) / denom * 100.0
    rsv = rsv.where(denom != 0)  # 分母 0 → NaN
    rsv = rsv.ffill().fillna(50.0)
    k = rsv.ewm(com=2, adjust=False).mean()
    d = k.ewm(com=2, adjust=False).mean()
    j = 3.0 * k - 2.0 * d
    return k, d, j


def _kdj_golden_cross_low(ohlcv: pd.DataFrame, **_) -> np.ndarray:
    """低位金叉: K 上穿 D 且当日 K < 30."""
    k, d, _ = _kdj_components(ohlcv)
    diff = k - d
    diff_prev = diff.shift(1)
    mask = ((diff_prev <= 0) & (diff > 0) & (k < 30)).fillna(False)
    return mask.to_numpy(dtype=np.int8)


def _kdj_death_cross_high(ohlcv: pd.DataFrame, **_) -> np.ndarray:
    """高位死叉: K 下穿 D 且当日 K > 70."""
    k, d, _ = _kdj_components(ohlcv)
    diff = k - d
    diff_prev = diff.shift(1)
    mask = ((diff_prev >= 0) & (diff < 0) & (k > 70)).fillna(False)
    return mask.to_numpy(dtype=np.int8)


def _kdj_j_above_100(ohlcv: pd.DataFrame, **_) -> np.ndarray:
    """J > 100 (状态原子, 极度超买)."""
    _, _, j = _kdj_components(ohlcv)
    return (j > 100).fillna(False).to_numpy(dtype=np.int8)


def _kdj_j_below_0(ohlcv: pd.DataFrame, **_) -> np.ndarray:
    """J < 0 (状态原子, 极度超跌)."""
    _, _, j = _kdj_components(ohlcv)
    return (j < 0).fillna(False).to_numpy(dtype=np.int8)


_KDJ_WARMUP = 9 + 15  # RSV 窗口 9 + ewm 平滑收敛缓冲

register(
    SignalAtom(
        id="kdj_golden_cross_low",
        professional_name="K 上穿 D 且 K < 30",
        layman_name="低点附近的回升信号",
        category=SignalCategory.TECHNICAL,
        deps=(DataDep.OHLCV,),
        min_warmup=_KDJ_WARMUP,
        compute_fn=_kdj_golden_cross_low,
    )
)
register(
    SignalAtom(
        id="kdj_death_cross_high",
        professional_name="K 下穿 D 且 K > 70",
        layman_name="高点附近的回落信号",
        category=SignalCategory.TECHNICAL,
        deps=(DataDep.OHLCV,),
        min_warmup=_KDJ_WARMUP,
        compute_fn=_kdj_death_cross_high,
    )
)
register(
    SignalAtom(
        id="kdj_j_above_100",
        professional_name="J 值 > 100",
        layman_name="极度超买",
        category=SignalCategory.TECHNICAL,
        deps=(DataDep.OHLCV,),
        min_warmup=_KDJ_WARMUP,
        compute_fn=_kdj_j_above_100,
    )
)
register(
    SignalAtom(
        id="kdj_j_below_0",
        professional_name="J 值 < 0",
        layman_name="极度超跌",
        category=SignalCategory.TECHNICAL,
        deps=(DataDep.OHLCV,),
        min_warmup=_KDJ_WARMUP,
        compute_fn=_kdj_j_below_0,
    )
)


# ============================================================
# A6 布林带 (4 个) — 标准 (20 周期, ±2σ), squeeze 留 Phase B
# ============================================================


def _bollinger_bands(closes: pd.Series, period: int = 20, num_std: float = 2.0):
    mid = closes.rolling(period).mean()
    std = closes.rolling(period).std(ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    return upper, mid, lower


_BOLL_TOL = 1e-9  # touch 用 close <= lower / >= upper, break 用严格 < / >


def _bollinger_lower_touch(ohlcv: pd.DataFrame, **_) -> np.ndarray:
    closes = ohlcv["close"].astype(float)
    _, _, lower = _bollinger_bands(closes)
    mask = (closes <= lower + _BOLL_TOL).fillna(False)
    return mask.to_numpy(dtype=np.int8)


def _bollinger_upper_touch(ohlcv: pd.DataFrame, **_) -> np.ndarray:
    closes = ohlcv["close"].astype(float)
    upper, _, _ = _bollinger_bands(closes)
    mask = (closes >= upper - _BOLL_TOL).fillna(False)
    return mask.to_numpy(dtype=np.int8)


def _bollinger_lower_break(ohlcv: pd.DataFrame, **_) -> np.ndarray:
    """跌破下轨: close < lower (严格小于, 不含触及)."""
    closes = ohlcv["close"].astype(float)
    _, _, lower = _bollinger_bands(closes)
    mask = (closes < lower).fillna(False)
    return mask.to_numpy(dtype=np.int8)


def _bollinger_upper_break(ohlcv: pd.DataFrame, **_) -> np.ndarray:
    closes = ohlcv["close"].astype(float)
    upper, _, _ = _bollinger_bands(closes)
    mask = (closes > upper).fillna(False)
    return mask.to_numpy(dtype=np.int8)


_BOLL_SQUEEZE_WINDOW = 120  # 带宽回看窗口 (近半年)


def _bollinger_squeeze(ohlcv: pd.DataFrame, **_) -> np.ndarray:
    """通道收口: 带宽 (upper-lower)/mid 处于近 120 日最低 (通道最窄, 可能变盘)."""
    closes = ohlcv["close"].astype(float)
    upper, mid, lower = _bollinger_bands(closes)
    bandwidth = (upper - lower) / mid.replace(0, np.nan)
    roll_min = bandwidth.rolling(_BOLL_SQUEEZE_WINDOW).min()
    # rolling min 含当日, 故 bandwidth <= roll_min 仅当当日是窗口内最低
    mask = (bandwidth <= roll_min).fillna(False)
    return mask.to_numpy(dtype=np.int8)


_BOLL_WARMUP = 20 + 1
_BOLL_SQUEEZE_WARMUP = 20 + _BOLL_SQUEEZE_WINDOW  # 布林 20 + 带宽窗口 120

register(
    SignalAtom(
        id="bollinger_lower_touch",
        professional_name="close 触碰下轨",
        layman_name="股价跌到通道底部",
        category=SignalCategory.TECHNICAL,
        deps=(DataDep.OHLCV,),
        min_warmup=_BOLL_WARMUP,
        compute_fn=_bollinger_lower_touch,
    )
)
register(
    SignalAtom(
        id="bollinger_upper_touch",
        professional_name="close 触碰上轨",
        layman_name="股价涨到通道顶部",
        category=SignalCategory.TECHNICAL,
        deps=(DataDep.OHLCV,),
        min_warmup=_BOLL_WARMUP,
        compute_fn=_bollinger_upper_touch,
    )
)
register(
    SignalAtom(
        id="bollinger_lower_break",
        professional_name="close 跌破下轨",
        layman_name="股价跌穿通道底部",
        category=SignalCategory.TECHNICAL,
        deps=(DataDep.OHLCV,),
        min_warmup=_BOLL_WARMUP,
        compute_fn=_bollinger_lower_break,
    )
)
register(
    SignalAtom(
        id="bollinger_upper_break",
        professional_name="close 突破上轨",
        layman_name="股价突破通道顶部",
        category=SignalCategory.TECHNICAL,
        deps=(DataDep.OHLCV,),
        min_warmup=_BOLL_WARMUP,
        compute_fn=_bollinger_upper_break,
    )
)
register(
    SignalAtom(
        id="bollinger_squeeze",
        professional_name="上下轨距离收口",
        layman_name="通道收窄 (可能要变盘)",
        category=SignalCategory.TECHNICAL,
        deps=(DataDep.OHLCV,),
        min_warmup=_BOLL_SQUEEZE_WARMUP,
        compute_fn=_bollinger_squeeze,
    )
)
