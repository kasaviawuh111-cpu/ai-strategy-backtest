import numpy as np
import pandas as pd

from astock_backtest.strategies.base import SIG_BUY_INT, SIG_SELL_INT, Signal, Strategy


class MACDStrategy(Strategy):
    name = "macd"
    display_name = "MACD 趋势"
    default_params = {"fast_period": 12, "slow_period": 26, "signal_period": 9}

    def min_warmup(self) -> int:
        return int(self.params["slow_period"]) + int(self.params["signal_period"]) + 5

    def generate_signal(self, history: pd.DataFrame) -> Signal:
        fast = int(self.params["fast_period"])
        slow = int(self.params["slow_period"])
        sig_p = int(self.params["signal_period"])
        need = slow + sig_p + 2
        if len(history) < need:
            return Signal.HOLD
        closes = history["close"].astype(float)
        ema_fast = closes.ewm(span=fast, adjust=False).mean()
        ema_slow = closes.ewm(span=slow, adjust=False).mean()
        dif = ema_fast - ema_slow
        dea = dif.ewm(span=sig_p, adjust=False).mean()
        prev = dif.iloc[-2] - dea.iloc[-2]
        cur = dif.iloc[-1] - dea.iloc[-1]
        if pd.isna(prev) or pd.isna(cur):
            return Signal.HOLD
        if prev <= 0 and cur > 0:
            return Signal.BUY
        if prev >= 0 and cur < 0:
            return Signal.SELL
        return Signal.HOLD

    def precompute_signals(
        self,
        all_ohlcv: pd.DataFrame,
        start_idx: int,
        end_idx: int,
    ) -> np.ndarray:
        fast = int(self.params["fast_period"])
        slow = int(self.params["slow_period"])
        sig_p = int(self.params["signal_period"])

        closes = all_ohlcv["close"].astype(float)
        ema_fast = closes.ewm(span=fast, adjust=False).mean()
        ema_slow = closes.ewm(span=slow, adjust=False).mean()
        dif = ema_fast - ema_slow
        dea = dif.ewm(span=sig_p, adjust=False).mean()
        diff_dea = dif - dea
        diff_dea_prev = diff_dea.shift(1)

        buy_mask = ((diff_dea_prev <= 0) & (diff_dea > 0)).fillna(False)
        sell_mask = ((diff_dea_prev >= 0) & (diff_dea < 0)).fillna(False)

        n = end_idx - start_idx
        out = np.zeros(n, dtype=np.int8)
        out[buy_mask.values[start_idx:end_idx]] = SIG_BUY_INT
        out[sell_mask.values[start_idx:end_idx]] = SIG_SELL_INT
        # 引擎层会统一应用 warmup mask, 这里不再重复
        return out
