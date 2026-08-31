import numpy as np
import pandas as pd

from astock_backtest.strategies.base import SIG_BUY_INT, SIG_SELL_INT, Signal, Strategy


class DualMAStrategy(Strategy):
    name = "dual_ma"
    display_name = "双均线金叉"
    default_params = {"fast_period": 5, "slow_period": 20}

    def min_warmup(self) -> int:
        return int(self.params["slow_period"]) + 2

    def generate_signal(self, history: pd.DataFrame) -> Signal:
        fast = int(self.params["fast_period"])
        slow = int(self.params["slow_period"])
        if len(history) < slow + 2:
            return Signal.HOLD
        closes = history["close"].astype(float)
        fast_ma = closes.rolling(fast).mean()
        slow_ma = closes.rolling(slow).mean()
        prev_diff = fast_ma.iloc[-2] - slow_ma.iloc[-2]
        cur_diff = fast_ma.iloc[-1] - slow_ma.iloc[-1]
        if pd.isna(prev_diff) or pd.isna(cur_diff):
            return Signal.HOLD
        if prev_diff <= 0 and cur_diff > 0:
            return Signal.BUY
        if prev_diff >= 0 and cur_diff < 0:
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
        closes = all_ohlcv["close"].astype(float)
        fast_ma = closes.rolling(fast).mean()
        slow_ma = closes.rolling(slow).mean()
        diff = fast_ma - slow_ma
        diff_prev = diff.shift(1)

        # NaN <= 0 / NaN > 0 都是 False, mask 自然过滤掉 warmup
        buy_mask = ((diff_prev <= 0) & (diff > 0)).fillna(False)
        sell_mask = ((diff_prev >= 0) & (diff < 0)).fillna(False)

        n = end_idx - start_idx
        out = np.zeros(n, dtype=np.int8)
        out[buy_mask.values[start_idx:end_idx]] = SIG_BUY_INT
        out[sell_mask.values[start_idx:end_idx]] = SIG_SELL_INT
        return out
