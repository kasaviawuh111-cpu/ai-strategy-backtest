import numpy as np
import pandas as pd

from astock_backtest.strategies.base import SIG_BUY_INT, SIG_SELL_INT, Signal, Strategy


class MeanReversionStrategy(Strategy):
    name = "mean_reversion"
    display_name = "均值回归"
    default_params = {
        "ma_period": 20,
        "deviation": 0.03,
    }

    def min_warmup(self) -> int:
        return int(self.params["ma_period"]) + 2

    def generate_signal(self, history: pd.DataFrame) -> Signal:
        ma_p = int(self.params["ma_period"])
        dev = float(self.params["deviation"])
        if len(history) < ma_p + 2:
            return Signal.HOLD
        closes = history["close"].astype(float)
        ma = closes.rolling(ma_p).mean()
        cur_c = closes.iloc[-1]
        cur_ma = ma.iloc[-1]
        prev_c = closes.iloc[-2]
        prev_ma = ma.iloc[-2]
        if pd.isna(cur_ma) or pd.isna(prev_ma) or cur_ma <= 0:
            return Signal.HOLD

        # 偏离超阈值向下 → 买
        if (cur_ma - cur_c) / cur_ma >= dev:
            return Signal.BUY
        # 回归到均线上方 → 卖
        if prev_c < prev_ma and cur_c >= cur_ma:
            return Signal.SELL
        return Signal.HOLD

    def precompute_signals(
        self,
        all_ohlcv: pd.DataFrame,
        start_idx: int,
        end_idx: int,
    ) -> np.ndarray:
        ma_p = int(self.params["ma_period"])
        dev = float(self.params["deviation"])

        closes = all_ohlcv["close"].astype(float)
        ma = closes.rolling(ma_p).mean()
        closes_prev = closes.shift(1)
        ma_prev = ma.shift(1)

        # BUY 优先级高于 SELL (老引擎里 BUY 先 return)
        ma_safe = ma.where(ma > 0)  # 避免 ma <= 0 触发 BUY
        buy_mask = (((ma_safe - closes) / ma_safe) >= dev).fillna(False)
        sell_mask = ((closes_prev < ma_prev) & (closes >= ma)).fillna(False)

        n = end_idx - start_idx
        out = np.zeros(n, dtype=np.int8)
        sub_buy = buy_mask.values[start_idx:end_idx]
        sub_sell = sell_mask.values[start_idx:end_idx]
        out[sub_sell] = SIG_SELL_INT
        out[sub_buy] = SIG_BUY_INT  # 后写, BUY 覆盖 SELL (跟老引擎 if/elif 顺序一致)
        return out
