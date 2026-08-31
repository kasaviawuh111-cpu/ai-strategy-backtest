from astock_backtest.strategies.base import Signal, Strategy
from astock_backtest.strategies.always_hold import AlwaysHoldStrategy
from astock_backtest.strategies.bollinger import BollingerStrategy
from astock_backtest.strategies.dual_confirm import DualConfirmStrategy
from astock_backtest.strategies.dual_ma import DualMAStrategy
from astock_backtest.strategies.kdj import KDJStrategy
from astock_backtest.strategies.limit_board import LimitBoardStrategy
from astock_backtest.strategies.ma_stack import MAStackStrategy
from astock_backtest.strategies.macd import MACDStrategy
from astock_backtest.strategies.mean_reversion import MeanReversionStrategy
from astock_backtest.strategies.n_day_high import NDayHighStrategy
from astock_backtest.strategies.rsi import RSIStrategy
from astock_backtest.strategies.trend_oversold import TrendOversoldStrategy
from astock_backtest.strategies.volume_breakout import VolumeBreakoutStrategy

STRATEGY_REGISTRY: dict[str, type[Strategy]] = {
    "dual_ma": DualMAStrategy,
    "ma_stack": MAStackStrategy,
    "macd": MACDStrategy,
    "dual_confirm": DualConfirmStrategy,
    "rsi": RSIStrategy,
    "kdj": KDJStrategy,
    "bollinger": BollingerStrategy,
    "mean_reversion": MeanReversionStrategy,
    "trend_oversold": TrendOversoldStrategy,
    "n_day_high": NDayHighStrategy,
    "volume_breakout": VolumeBreakoutStrategy,
    "limit_board": LimitBoardStrategy,
    "always_hold": AlwaysHoldStrategy,
}


def get_strategy(strategy_id: str, params: dict) -> Strategy:
    cls = STRATEGY_REGISTRY.get(strategy_id)
    if cls is None:
        raise ValueError(f"unknown strategy_id: {strategy_id}")
    return cls(**params)


__all__ = [
    "Signal",
    "Strategy",
    "DualMAStrategy",
    "MAStackStrategy",
    "MACDStrategy",
    "DualConfirmStrategy",
    "RSIStrategy",
    "KDJStrategy",
    "BollingerStrategy",
    "MeanReversionStrategy",
    "TrendOversoldStrategy",
    "NDayHighStrategy",
    "VolumeBreakoutStrategy",
    "LimitBoardStrategy",
    "AlwaysHoldStrategy",
    "STRATEGY_REGISTRY",
    "get_strategy",
]
