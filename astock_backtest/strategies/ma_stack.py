import numpy as np
import pandas as pd

from astock_backtest.strategies.base import SIG_BUY_INT, SIG_SELL_INT, Signal, Strategy


class MAStackStrategy(Strategy):
    name = "ma_stack"
    display_name = "均线多头排列"
    default_params = {
        "short_period": 5,
        "mid_period": 20,
        "long_period": 60,
        "extra_period": 10,
    }

    def min_warmup(self) -> int:
        return int(self.params["long_period"]) + 2

    def generate_signal(self, history: pd.DataFrame) -> Signal:
        s = int(self.params["short_period"])
        m = int(self.params["mid_period"])
        l = int(self.params["long_period"])
        extra = int(self.params.get("extra_period") or 0)
        if not (s < m < l):
            return Signal.HOLD
        if len(history) < l + 2:
            return Signal.HOLD
        closes = history["close"].astype(float)
        ma_s = closes.rolling(s).mean()
        ma_m = closes.rolling(m).mean()
        ma_l = closes.rolling(l).mean()
        ma_e = closes.rolling(extra).mean() if extra > 0 else None

        def stacked(i: int) -> bool:
            a, b, c = ma_s.iloc[i], ma_m.iloc[i], ma_l.iloc[i]
            if pd.isna(a) or pd.isna(b) or pd.isna(c):
                return False
            if not (a > b > c):
                return False
            if ma_e is not None:
                e = ma_e.iloc[i]
                if pd.isna(e) or not (a > e > b):
                    return False
            return True

        prev_ok = stacked(-2)
        cur_ok = stacked(-1)
        if not prev_ok and cur_ok:
            return Signal.BUY
        if prev_ok and not cur_ok:
            return Signal.SELL
        return Signal.HOLD

    def precompute_signals(
        self,
        all_ohlcv: pd.DataFrame,
        start_idx: int,
        end_idx: int,
    ) -> np.ndarray:
        s = int(self.params["short_period"])
        m = int(self.params["mid_period"])
        l = int(self.params["long_period"])
        extra = int(self.params.get("extra_period") or 0)

        n = end_idx - start_idx
        out = np.zeros(n, dtype=np.int8)
        if not (s < m < l):
            return out  # 参数不合法, 全 HOLD (跟老引擎一致)

        closes = all_ohlcv["close"].astype(float)
        ma_s = closes.rolling(s).mean()
        ma_m = closes.rolling(m).mean()
        ma_l = closes.rolling(l).mean()

        # stacked = a > b > c  (含 NaN 时 False)
        stacked = (ma_s > ma_m) & (ma_m > ma_l)
        if extra > 0:
            ma_e = closes.rolling(extra).mean()
            stacked = stacked & (ma_s > ma_e) & (ma_e > ma_m)

        # NaN 比较返 False, 自然 mask
        stacked = stacked.fillna(False)
        stacked_prev = stacked.shift(1).fillna(False)

        buy_mask = (~stacked_prev) & stacked
        sell_mask = stacked_prev & (~stacked)

        out[buy_mask.values[start_idx:end_idx]] = SIG_BUY_INT
        out[sell_mask.values[start_idx:end_idx]] = SIG_SELL_INT
        return out
