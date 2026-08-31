"""§4.6 反向信号配对表 (Phase A 范围内)

用于 SellOnReverseSignal: BUY 是穿越/状态类原子时, 自动找到"反向" 原子做 SELL.
没在表里的 BUY 信号 → 引擎跳过这个组合 (限于 Phase A, Phase B 补穿越下穿原子后会扩).

Phase A 限制: 30 BUY 原子里只有部分有反向 (下穿/N 日新低多数还没实现).
"""

from __future__ import annotations

from typing import Optional


# BUY 原子 id → 反向 SELL 原子 id (必须都在 ATOM_REGISTRY 里)
REVERSE_SIGNAL_PAIRS: dict[str, str] = {
    # ── 技术指标 (A 类) ──
    # MACD
    "macd_golden_cross": "macd_death_cross",
    "macd_above_zero": "macd_below_zero",
    "macd_hist_turn_positive": "macd_death_cross",  # 红柱出现的反向: hist 转负 = death_cross
    # RSI
    "rsi_oversold_recover_30": "rsi_overbought_decline_70",
    "rsi_oversold_recover_20": "rsi_overbought_decline_80",
    "rsi_below_30": "rsi_above_70",
    # Bollinger
    "bollinger_lower_touch": "bollinger_upper_touch",
    "bollinger_lower_break": "bollinger_upper_break",
    # A1 均线穿越上穿 ↔ 下穿 (P0 补全)
    "ma_5_cross_up_10": "ma_5_cross_down_10",
    "ma_5_cross_up_20": "ma_5_cross_down_20",
    "ma_10_cross_up_20": "ma_10_cross_down_20",
    "ma_60_cross_up_250": "ma_60_cross_down_250",
    # 5↑60 没有对应下穿 (Phase A 没要), 跳过
    # A2 close 高于 ↔ 低于 均线 (P0 补)
    "close_above_ma_20": "close_below_ma_20",
    "close_above_ma_60": "close_below_ma_60",
    "close_above_ma_250": "close_below_ma_250",
    # ── 价格行为 (B 类) ──
    # B1 N 日新高 ↔ 新低 (P0 补全)
    "new_high_5d": "new_low_5d",
    "new_high_10d": "new_low_10d",
    "new_high_20d": "new_low_20d",
    "new_high_60d": "new_low_60d",
    "new_high_120d": "new_low_120d",
    # B2 涨停 ↔ 跌停, 单日涨 ↔ 跌 (P0 补)
    "limit_up": "limit_down",
    "single_day_up_3": "single_day_down_3",
    "single_day_up_5": "single_day_down_5",
    "single_day_up_7": "single_day_down_7",
    # B3 连涨 ↔ 连跌 (P0 补, 只配对 3 天版本, 因为 down 只有 3)
    "consecutive_up_3": "consecutive_down_3",
    # B4 跳空高开 ↔ 低开 (P0 补)
    "gap_up": "gap_down",
    # ── 没有合理反向的 (引擎跳过 SellOnReverseSignal 组合) ──
    # ma_5_cross_up_60 (没对应下穿) / consecutive_up_2/5 (没对应 down)
    # volume / pe / turnover / 量价配合 等 — 非穿越/状态类无反向
}


def get_reverse_atom_id(buy_atom_id: str) -> Optional[str]:
    """返 BUY 原子的反向 SELL 原子 id. 没有配对时返 None (引擎跳过)."""
    return REVERSE_SIGNAL_PAIRS.get(buy_atom_id)
