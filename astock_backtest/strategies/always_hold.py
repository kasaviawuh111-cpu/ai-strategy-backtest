import numpy as np
import pandas as pd

from astock_backtest.strategies.base import SIG_BUY_INT, Signal, Strategy


class AlwaysHoldStrategy(Strategy):
    name = "always_hold"
    display_name = "买入持有（校验用）"
    default_params: dict = {}

    def min_warmup(self) -> int:
        return 0

    def generate_signal(self, history: pd.DataFrame) -> Signal:
        return Signal.BUY

    def precompute_signals(
        self,
        all_ohlcv: pd.DataFrame,
        start_idx: int,
        end_idx: int,
    ) -> np.ndarray:
        # 全部填 BUY, 引擎处理重复 BUY (shares==0 时才执行)
        n = end_idx - start_idx
        return np.full(n, SIG_BUY_INT, dtype=np.int8)
