from enum import Enum
from typing import Any, Optional

import numpy as np
import pandas as pd


class Signal(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


# Numba 内核用的整数编码 (跟 Signal 对应)
SIG_HOLD_INT: int = 0
SIG_BUY_INT: int = 1
SIG_SELL_INT: int = 2


class Strategy:
    name: str = "base"
    display_name: str = "基础策略"
    default_params: dict[str, Any] = {}
    requires_stop_loss: bool = False

    def __init__(self, **params: Any) -> None:
        merged = dict(self.default_params)
        merged.update({k: v for k, v in params.items() if v is not None})
        self.params = merged
        self.stock_code: Optional[str] = None

    def min_warmup(self) -> int:
        return 0

    def generate_signal(self, history: pd.DataFrame) -> Signal:
        raise NotImplementedError

    def precompute_signals(
        self,
        all_ohlcv: pd.DataFrame,
        start_idx: int,
        end_idx: int,
    ) -> np.ndarray:
        """全窗口信号预计算 (Numba 引擎用)

        参数:
          all_ohlcv: 含 warmup 的完整 ohlcv (DataStore.get_ohlcv 返回)
          start_idx: window 在 all_ohlcv 中的起始 index (含)
          end_idx: window 在 all_ohlcv 中的结束 index (不含)

        返回:
          np.int8 数组, shape=(end_idx-start_idx,)
          值: 0=HOLD, 1=BUY, 2=SELL

        默认实现: 用 generate_signal 逐日调一遍 (兼容尚未改造的策略).
        Numba 化的策略应该重写此方法, 用 pandas/numpy 向量化算一次.
        """
        n = end_idx - start_idx
        warmup = self.min_warmup()
        out = np.zeros(n, dtype=np.int8)
        for i in range(n):
            global_idx = start_idx + i
            history = all_ohlcv.iloc[: global_idx + 1]
            if len(history) < warmup:
                continue
            sig = self.generate_signal(history)
            if sig == Signal.BUY:
                out[i] = SIG_BUY_INT
            elif sig == Signal.SELL:
                out[i] = SIG_SELL_INT
        return out
