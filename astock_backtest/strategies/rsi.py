import numpy as np
import pandas as pd

from astock_backtest.strategies.base import SIG_BUY_INT, SIG_SELL_INT, Signal, Strategy


class RSIStrategy(Strategy):
    name = "rsi"
    display_name = "RSI 超卖反弹"
    default_params = {"period": 14, "oversold": 30, "overbought": 70}

    def min_warmup(self) -> int:
        return int(self.params["period"]) + 2

    def generate_signal(self, history: pd.DataFrame) -> Signal:
        period = int(self.params["period"])
        os_v = float(self.params["oversold"])
        ob_v = float(self.params["overbought"])
        if len(history) < period + 2:
            return Signal.HOLD
        closes = history["close"].astype(float)
        delta = closes.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.rolling(period).mean()
        avg_loss = loss.rolling(period).mean()
        rs = avg_gain / avg_loss.replace(0, 1e-9)
        rsi = 100 - 100 / (1 + rs)
        prev = rsi.iloc[-2]
        cur = rsi.iloc[-1]
        if pd.isna(prev) or pd.isna(cur):
            return Signal.HOLD
        if prev < os_v and cur >= os_v:
            return Signal.BUY
        if prev > ob_v and cur <= ob_v:
            return Signal.SELL
        return Signal.HOLD

    def precompute_signals(
        self,
        all_ohlcv: pd.DataFrame,
        start_idx: int,
        end_idx: int,
    ) -> np.ndarray:
        period = int(self.params["period"])
        os_v = float(self.params["oversold"])
        ob_v = float(self.params["overbought"])

        closes = all_ohlcv["close"].astype(float)
        delta = closes.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.rolling(period).mean()
        avg_loss = loss.rolling(period).mean()
        rs = avg_gain / avg_loss.replace(0, 1e-9)
        rsi = 100 - 100 / (1 + rs)
        rsi_prev = rsi.shift(1)

        buy_mask = ((rsi_prev < os_v) & (rsi >= os_v)).fillna(False)
        sell_mask = ((rsi_prev > ob_v) & (rsi <= ob_v)).fillna(False)

        n = end_idx - start_idx
        out = np.zeros(n, dtype=np.int8)
        out[buy_mask.values[start_idx:end_idx]] = SIG_BUY_INT
        out[sell_mask.values[start_idx:end_idx]] = SIG_SELL_INT
        return out
