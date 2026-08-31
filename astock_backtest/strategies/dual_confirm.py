import numpy as np
import pandas as pd

from astock_backtest.strategies.base import SIG_BUY_INT, SIG_SELL_INT, Signal, Strategy


class DualConfirmStrategy(Strategy):
    name = "dual_confirm"
    display_name = "双重确认突破 (MACD+均线)"
    default_params = {
        "ma_period": 20,
        "fast_period": 12,
        "slow_period": 26,
        "signal_period": 9,
    }

    def min_warmup(self) -> int:
        return int(self.params["slow_period"]) + int(self.params["signal_period"]) + 5

    def generate_signal(self, history: pd.DataFrame) -> Signal:
        ma_p = int(self.params["ma_period"])
        fast = int(self.params["fast_period"])
        slow = int(self.params["slow_period"])
        sig_p = int(self.params["signal_period"])
        need = max(ma_p, slow + sig_p) + 2
        if len(history) < need:
            return Signal.HOLD
        closes = history["close"].astype(float)
        ma = closes.rolling(ma_p).mean()
        ema_fast = closes.ewm(span=fast, adjust=False).mean()
        ema_slow = closes.ewm(span=slow, adjust=False).mean()
        dif = ema_fast - ema_slow
        dea = dif.ewm(span=sig_p, adjust=False).mean()

        prev_hist = dif.iloc[-2] - dea.iloc[-2]
        cur_hist = dif.iloc[-1] - dea.iloc[-1]
        cur_c = closes.iloc[-1]
        cur_ma = ma.iloc[-1]
        if any(pd.isna(x) for x in (prev_hist, cur_hist, cur_ma)):
            return Signal.HOLD

        macd_cross_up = prev_hist <= 0 and cur_hist > 0
        macd_cross_down = prev_hist >= 0 and cur_hist < 0
        if macd_cross_up and cur_c > cur_ma:
            return Signal.BUY
        if macd_cross_down:
            return Signal.SELL
        return Signal.HOLD

    def precompute_signals(
        self,
        all_ohlcv: pd.DataFrame,
        start_idx: int,
        end_idx: int,
    ) -> np.ndarray:
        ma_p = int(self.params["ma_period"])
        fast = int(self.params["fast_period"])
        slow = int(self.params["slow_period"])
        sig_p = int(self.params["signal_period"])

        closes = all_ohlcv["close"].astype(float)
        ma = closes.rolling(ma_p).mean()
        ema_fast = closes.ewm(span=fast, adjust=False).mean()
        ema_slow = closes.ewm(span=slow, adjust=False).mean()
        dif = ema_fast - ema_slow
        dea = dif.ewm(span=sig_p, adjust=False).mean()
        hist = dif - dea
        hist_prev = hist.shift(1)

        # macd 金叉 + close > ma → BUY
        # macd 死叉 → SELL (不管 ma)
        macd_up = (hist_prev <= 0) & (hist > 0)
        macd_down = (hist_prev >= 0) & (hist < 0)
        buy_mask = (macd_up & (closes > ma)).fillna(False)
        sell_mask = macd_down.fillna(False)

        n = end_idx - start_idx
        out = np.zeros(n, dtype=np.int8)
        sub_buy = buy_mask.values[start_idx:end_idx]
        sub_sell = sell_mask.values[start_idx:end_idx]
        # 老引擎 if BUY return; if SELL return — BUY 优先级高于 SELL
        out[sub_sell] = SIG_SELL_INT
        out[sub_buy] = SIG_BUY_INT  # 后写覆盖
        return out
