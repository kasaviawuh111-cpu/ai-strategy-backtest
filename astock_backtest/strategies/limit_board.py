import numpy as np
import pandas as pd

from astock_backtest.services.board_limits import limit_threshold
from astock_backtest.strategies.base import SIG_BUY_INT, Signal, Strategy


class LimitBoardStrategy(Strategy):
    """涨停板策略 (A股特有)

    逻辑: N 日连续涨停 (close pct_change >= 板块阈值) 后,
    首次出现非涨停日 → 视为"开板",发出 BUY 信号。
    退出依赖全局止损 (requires_stop_loss=True)。

    简化版: 不实现"开板后持有 N 日"退出,那需要引擎 hold_days 支持, 留 Phase 3.7。
    """

    name = "limit_board"
    display_name = "涨停板首次开板 (A股)"
    default_params = {"consecutive_days": 2}
    requires_stop_loss = True

    def min_warmup(self) -> int:
        return int(self.params["consecutive_days"]) + 3

    def generate_signal(self, history: pd.DataFrame) -> Signal:
        n = int(self.params["consecutive_days"])
        if len(history) < n + 2:
            return Signal.HOLD

        threshold = limit_threshold(self.stock_code or "")
        closes = history["close"].astype(float)
        pct = closes.pct_change() * 100.0

        today_pct = pct.iloc[-1]
        if pd.isna(today_pct):
            return Signal.HOLD
        # 今日仍涨停 → 还没开板
        if today_pct >= threshold:
            return Signal.HOLD

        # 过去 N 日(不含今日)全部涨停
        prev_n = pct.iloc[-(n + 1):-1]
        if len(prev_n) < n or prev_n.isna().any():
            return Signal.HOLD
        if (prev_n >= threshold).all():
            return Signal.BUY
        return Signal.HOLD

    def precompute_signals(
        self,
        all_ohlcv: pd.DataFrame,
        start_idx: int,
        end_idx: int,
    ) -> np.ndarray:
        n_consec = int(self.params["consecutive_days"])
        threshold = limit_threshold(self.stock_code or "")

        closes = all_ohlcv["close"].astype(float)
        pct = closes.pct_change() * 100.0

        is_lu = pct >= threshold
        # 前 n 日 (不含今日) 全涨停 = is_lu.shift(1).rolling(n).sum() == n
        # 但 .sum() 把 NaN 当 0, 老引擎要求 prev_n.isna().any() == False, 用 min 等价
        prev_n_lu_count = is_lu.shift(1).rolling(n_consec).sum()
        # 但 NaN 在 is_lu 里是 False, sum 不计入 NaN, 跟老引擎"prev_n.isna().any() → HOLD"不等
        # 严格: prev_n 必须有 n 个非 NaN 值, 用 pct.shift(1).rolling(n).count() == n 检查
        prev_n_valid_count = pct.shift(1).rolling(n_consec).count()
        prev_n_all_lu = (prev_n_lu_count >= n_consec) & (prev_n_valid_count >= n_consec)

        # 今日不涨停 + 今日 pct 不是 NaN
        today_not_lu = (pct < threshold) & pct.notna()

        buy_mask = (today_not_lu & prev_n_all_lu).fillna(False)

        n = end_idx - start_idx
        out = np.zeros(n, dtype=np.int8)
        out[buy_mask.values[start_idx:end_idx]] = SIG_BUY_INT
        # 没有 SELL (依赖 requires_stop_loss)
        return out
