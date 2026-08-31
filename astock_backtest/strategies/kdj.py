import numpy as np
import pandas as pd

from astock_backtest.strategies.base import SIG_BUY_INT, SIG_SELL_INT, Signal, Strategy


class KDJStrategy(Strategy):
    name = "kdj"
    display_name = "KDJ 低位金叉"
    default_params = {
        "n_period": 9,
        "m1": 3,
        "m2": 3,
        "low_threshold": 30,
        "high_threshold": 70,
    }

    def min_warmup(self) -> int:
        return int(self.params["n_period"]) + int(self.params["m1"]) + int(self.params["m2"]) + 5

    def generate_signal(self, history: pd.DataFrame) -> Signal:
        n = int(self.params["n_period"])
        m1 = int(self.params["m1"])
        m2 = int(self.params["m2"])
        low_thr = float(self.params["low_threshold"])
        high_thr = float(self.params["high_threshold"])
        if len(history) < n + m1 + m2 + 2:
            return Signal.HOLD

        closes = history["close"].astype(float)
        highs = history["high"].astype(float)
        lows = history["low"].astype(float)

        ll = lows.rolling(n).min()
        hh = highs.rolling(n).max()
        rsv = (closes - ll) / (hh - ll).replace(0, 1e-9) * 100.0

        k_vals: list[float] = []
        d_vals: list[float] = []
        prev_k = 50.0
        prev_d = 50.0
        for r in rsv:
            if pd.isna(r):
                k_vals.append(float("nan"))
                d_vals.append(float("nan"))
                continue
            k = (m1 - 1) / m1 * prev_k + r / m1
            d = (m2 - 1) / m2 * prev_d + k / m2
            k_vals.append(k)
            d_vals.append(d)
            prev_k, prev_d = k, d

        if len(k_vals) < 2:
            return Signal.HOLD
        k_cur, k_prev = k_vals[-1], k_vals[-2]
        d_cur, d_prev = d_vals[-1], d_vals[-2]
        if any(pd.isna(x) for x in (k_cur, k_prev, d_cur, d_prev)):
            return Signal.HOLD

        prev_diff = k_prev - d_prev
        cur_diff = k_cur - d_cur
        if prev_diff <= 0 and cur_diff > 0 and k_cur < low_thr:
            return Signal.BUY
        if prev_diff >= 0 and cur_diff < 0 and k_cur > high_thr:
            return Signal.SELL
        return Signal.HOLD

    def precompute_signals(
        self,
        all_ohlcv: pd.DataFrame,
        start_idx: int,
        end_idx: int,
    ) -> np.ndarray:
        n_p = int(self.params["n_period"])
        m1 = int(self.params["m1"])
        m2 = int(self.params["m2"])
        low_thr = float(self.params["low_threshold"])
        high_thr = float(self.params["high_threshold"])

        closes = all_ohlcv["close"].astype(float)
        highs = all_ohlcv["high"].astype(float)
        lows = all_ohlcv["low"].astype(float)

        ll = lows.rolling(n_p).min()
        hh = highs.rolling(n_p).max()
        rsv = (closes - ll) / (hh - ll).replace(0, 1e-9) * 100.0

        # 递推 K, D (跟老引擎完全一致: prev_k=50, prev_d=50 起步)
        N = len(rsv)
        k_arr = np.full(N, np.nan, dtype=np.float64)
        d_arr = np.full(N, np.nan, dtype=np.float64)
        prev_k = 50.0
        prev_d = 50.0
        rsv_vals = rsv.values
        for i in range(N):
            r = rsv_vals[i]
            if np.isnan(r):
                continue
            k = (m1 - 1) / m1 * prev_k + r / m1
            d = (m2 - 1) / m2 * prev_d + k / m2
            k_arr[i] = k
            d_arr[i] = d
            prev_k, prev_d = k, d

        diff = k_arr - d_arr
        diff_prev = np.empty_like(diff)
        diff_prev[0] = np.nan
        diff_prev[1:] = diff[:-1]
        k_prev = np.empty_like(k_arr)
        k_prev[0] = np.nan
        k_prev[1:] = k_arr[:-1]

        # NaN 比较返 False, 自然 mask
        with np.errstate(invalid="ignore"):
            buy_mask = (diff_prev <= 0) & (diff > 0) & (k_arr < low_thr)
            sell_mask = (diff_prev >= 0) & (diff < 0) & (k_arr > high_thr)
        # 还要排除任一是 NaN 的情况 (老引擎 if any pd.isna 就 HOLD, 这里 k_prev/d_prev 也参与)
        valid = ~(np.isnan(diff_prev) | np.isnan(diff) | np.isnan(k_prev) | np.isnan(k_arr))
        buy_mask = buy_mask & valid
        sell_mask = sell_mask & valid

        n = end_idx - start_idx
        out = np.zeros(n, dtype=np.int8)
        out[buy_mask[start_idx:end_idx]] = SIG_BUY_INT
        out[sell_mask[start_idx:end_idx]] = SIG_SELL_INT
        return out
