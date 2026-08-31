import numpy as np
import pandas as pd

from astock_backtest.strategies.base import SIG_BUY_INT, SIG_SELL_INT, Signal, Strategy


class TrendOversoldStrategy(Strategy):
    name = "trend_oversold"
    display_name = "趋势+超卖组合"
    default_params = {
        "trend_ma_period": 60,
        "rsi_period": 14,
        "oversold": 40,
        "overbought": 70,
    }

    def min_warmup(self) -> int:
        return max(int(self.params["trend_ma_period"]), int(self.params["rsi_period"])) + 5

    def generate_signal(self, history: pd.DataFrame) -> Signal:
        tma_p = int(self.params["trend_ma_period"])
        rsi_p = int(self.params["rsi_period"])
        os_v = float(self.params["oversold"])
        ob_v = float(self.params["overbought"])
        need = max(tma_p, rsi_p) + 2
        if len(history) < need:
            return Signal.HOLD

        closes = history["close"].astype(float)
        ma = closes.rolling(tma_p).mean()

        delta = closes.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.rolling(rsi_p).mean()
        avg_loss = loss.rolling(rsi_p).mean()
        rs = avg_gain / avg_loss.replace(0, 1e-9)
        rsi = 100 - 100 / (1 + rs)

        cur_c = closes.iloc[-1]
        cur_ma = ma.iloc[-1]
        prev_rsi = rsi.iloc[-2]
        cur_rsi = rsi.iloc[-1]
        if any(pd.isna(x) for x in (cur_ma, prev_rsi, cur_rsi)):
            return Signal.HOLD

        # 趋势向上 + RSI 从超卖区回升 → 买
        if cur_c > cur_ma and prev_rsi < os_v and cur_rsi >= os_v:
            return Signal.BUY
        # RSI 从超买区回落 → 卖
        if prev_rsi > ob_v and cur_rsi <= ob_v:
            return Signal.SELL
        return Signal.HOLD

    def precompute_signals(
        self,
        all_ohlcv: pd.DataFrame,
        start_idx: int,
        end_idx: int,
    ) -> np.ndarray:
        tma_p = int(self.params["trend_ma_period"])
        rsi_p = int(self.params["rsi_period"])
        os_v = float(self.params["oversold"])
        ob_v = float(self.params["overbought"])

        closes = all_ohlcv["close"].astype(float)
        ma = closes.rolling(tma_p).mean()

        delta = closes.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.rolling(rsi_p).mean()
        avg_loss = loss.rolling(rsi_p).mean()
        rs = avg_gain / avg_loss.replace(0, 1e-9)
        rsi = 100 - 100 / (1 + rs)
        rsi_prev = rsi.shift(1)

        # BUY: cur_c > cur_ma AND prev_rsi < os AND cur_rsi >= os
        buy_mask = ((closes > ma) & (rsi_prev < os_v) & (rsi >= os_v)).fillna(False)
        # SELL: prev_rsi > ob AND cur_rsi <= ob
        sell_mask = ((rsi_prev > ob_v) & (rsi <= ob_v)).fillna(False)

        n = end_idx - start_idx
        out = np.zeros(n, dtype=np.int8)
        sub_buy = buy_mask.values[start_idx:end_idx]
        sub_sell = sell_mask.values[start_idx:end_idx]
        out[sub_sell] = SIG_SELL_INT
        out[sub_buy] = SIG_BUY_INT
        return out
