import numpy as np
import pandas as pd

from astock_backtest.strategies.base import SIG_BUY_INT, SIG_SELL_INT, Signal, Strategy


class VolumeBreakoutStrategy(Strategy):
    name = "volume_breakout"
    display_name = "放量突破均线"
    default_params = {
        "ma_period": 20,
        "vol_ma_period": 20,
        "vol_mult": 1.5,
    }

    def min_warmup(self) -> int:
        return max(int(self.params["ma_period"]), int(self.params["vol_ma_period"])) + 2

    def generate_signal(self, history: pd.DataFrame) -> Signal:
        ma_p = int(self.params["ma_period"])
        vma_p = int(self.params["vol_ma_period"])
        mult = float(self.params["vol_mult"])
        need = max(ma_p, vma_p) + 2
        if len(history) < need:
            return Signal.HOLD
        closes = history["close"].astype(float)
        vols = history["volume"].astype(float)
        ma = closes.rolling(ma_p).mean()
        vma = vols.rolling(vma_p).mean()
        prev_c, cur_c = closes.iloc[-2], closes.iloc[-1]
        prev_ma, cur_ma = ma.iloc[-2], ma.iloc[-1]
        cur_v = vols.iloc[-1]
        cur_vma = vma.iloc[-1]
        if any(pd.isna(x) for x in (prev_ma, cur_ma, cur_vma)):
            return Signal.HOLD

        breakout = prev_c <= prev_ma and cur_c > cur_ma
        vol_ok = cur_vma > 0 and cur_v >= cur_vma * mult
        if breakout and vol_ok:
            return Signal.BUY
        if prev_c >= prev_ma and cur_c < cur_ma:
            return Signal.SELL
        return Signal.HOLD

    def precompute_signals(
        self,
        all_ohlcv: pd.DataFrame,
        start_idx: int,
        end_idx: int,
    ) -> np.ndarray:
        ma_p = int(self.params["ma_period"])
        vma_p = int(self.params["vol_ma_period"])
        mult = float(self.params["vol_mult"])

        closes = all_ohlcv["close"].astype(float)
        vols = all_ohlcv["volume"].astype(float)
        ma = closes.rolling(ma_p).mean()
        vma = vols.rolling(vma_p).mean()
        closes_prev = closes.shift(1)
        ma_prev = ma.shift(1)

        breakout = (closes_prev <= ma_prev) & (closes > ma)
        vol_ok = (vma > 0) & (vols >= vma * mult)
        buy_mask = (breakout & vol_ok).fillna(False)
        sell_mask = ((closes_prev >= ma_prev) & (closes < ma)).fillna(False)

        n = end_idx - start_idx
        out = np.zeros(n, dtype=np.int8)
        out[buy_mask.values[start_idx:end_idx]] = SIG_BUY_INT
        out[sell_mask.values[start_idx:end_idx]] = SIG_SELL_INT
        return out
