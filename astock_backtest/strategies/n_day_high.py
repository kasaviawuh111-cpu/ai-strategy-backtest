import numpy as np
import pandas as pd

from astock_backtest.strategies.base import SIG_BUY_INT, SIG_SELL_INT, Signal, Strategy


class NDayHighStrategy(Strategy):
    name = "n_day_high"
    display_name = "N 日新高突破"
    default_params = {"buy_period": 20, "sell_period": 10}

    def min_warmup(self) -> int:
        return max(int(self.params["buy_period"]), int(self.params["sell_period"])) + 2

    def generate_signal(self, history: pd.DataFrame) -> Signal:
        buy_p = int(self.params["buy_period"])
        sell_p = int(self.params["sell_period"])
        need = max(buy_p, sell_p) + 2
        if len(history) < need:
            return Signal.HOLD
        closes = history["close"].astype(float)
        today = closes.iloc[-1]
        window_high = closes.iloc[-(buy_p + 1):-1].max()
        window_low = closes.iloc[-(sell_p + 1):-1].min()
        if pd.isna(window_high) or pd.isna(window_low):
            return Signal.HOLD
        if today > window_high:
            return Signal.BUY
        if today < window_low:
            return Signal.SELL
        return Signal.HOLD

    def precompute_signals(
        self,
        all_ohlcv: pd.DataFrame,
        start_idx: int,
        end_idx: int,
    ) -> np.ndarray:
        buy_p = int(self.params["buy_period"])
        sell_p = int(self.params["sell_period"])

        closes = all_ohlcv["close"].astype(float)
        # shift(1).rolling(N).max(): 当天前 N 个交易日 (不含当天) 的最大值
        prev_max = closes.shift(1).rolling(buy_p).max()
        prev_min = closes.shift(1).rolling(sell_p).min()

        # NaN 比较自然返 False, warmup 内 mask 掉 (引擎层还会再强制一次)
        buy_mask = (closes > prev_max).fillna(False)
        sell_mask = (closes < prev_min).fillna(False)

        n = end_idx - start_idx
        out = np.zeros(n, dtype=np.int8)
        out[buy_mask.values[start_idx:end_idx]] = SIG_BUY_INT
        out[sell_mask.values[start_idx:end_idx]] = SIG_SELL_INT
        return out
