import numpy as np
import pandas as pd

from astock_backtest.strategies.base import SIG_BUY_INT, SIG_SELL_INT, Signal, Strategy


class BollingerStrategy(Strategy):
    name = "bollinger"
    display_name = "布林带反转"
    default_params = {
        "period": 20,
        "std_mult": 2.0,
        "direction": "mean_reversion",
    }

    def min_warmup(self) -> int:
        return int(self.params["period"]) + 2

    def generate_signal(self, history: pd.DataFrame) -> Signal:
        period = int(self.params["period"])
        std_mult = float(self.params["std_mult"])
        direction = str(self.params.get("direction") or "mean_reversion")
        if len(history) < period + 2:
            return Signal.HOLD
        closes = history["close"].astype(float)
        ma = closes.rolling(period).mean()
        sd = closes.rolling(period).std(ddof=0)
        upper = ma + std_mult * sd
        lower = ma - std_mult * sd
        prev_c, cur_c = closes.iloc[-2], closes.iloc[-1]
        prev_up, cur_up = upper.iloc[-2], upper.iloc[-1]
        prev_lo, cur_lo = lower.iloc[-2], lower.iloc[-1]
        if any(pd.isna(x) for x in (prev_up, cur_up, prev_lo, cur_lo)):
            return Signal.HOLD

        if direction == "breakout":
            if prev_c <= prev_up and cur_c > cur_up:
                return Signal.BUY
            if prev_c >= prev_lo and cur_c < cur_lo:
                return Signal.SELL
            return Signal.HOLD

        # mean_reversion (default)
        if prev_c > prev_lo and cur_c <= cur_lo:
            return Signal.BUY
        if prev_c < prev_up and cur_c >= cur_up:
            return Signal.SELL
        return Signal.HOLD

    def precompute_signals(
        self,
        all_ohlcv: pd.DataFrame,
        start_idx: int,
        end_idx: int,
    ) -> np.ndarray:
        period = int(self.params["period"])
        std_mult = float(self.params["std_mult"])
        direction = str(self.params.get("direction") or "mean_reversion")

        closes = all_ohlcv["close"].astype(float)
        ma = closes.rolling(period).mean()
        sd = closes.rolling(period).std(ddof=0)
        upper = ma + std_mult * sd
        lower = ma - std_mult * sd

        closes_prev = closes.shift(1)
        upper_prev = upper.shift(1)
        lower_prev = lower.shift(1)

        if direction == "breakout":
            buy_mask = ((closes_prev <= upper_prev) & (closes > upper)).fillna(False)
            sell_mask = ((closes_prev >= lower_prev) & (closes < lower)).fillna(False)
        else:  # mean_reversion (default)
            buy_mask = ((closes_prev > lower_prev) & (closes <= lower)).fillna(False)
            sell_mask = ((closes_prev < upper_prev) & (closes >= upper)).fillna(False)

        n = end_idx - start_idx
        out = np.zeros(n, dtype=np.int8)
        out[buy_mask.values[start_idx:end_idx]] = SIG_BUY_INT
        out[sell_mask.values[start_idx:end_idx]] = SIG_SELL_INT
        return out
