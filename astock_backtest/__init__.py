"""astock-backtest-engine — 可扩展的 A 股回测框架

数据底座: astock-data-toolkit 下载的 6 张 parquet 表 (环境变量 ASTOCK_DATA_DIR 指向数据目录)。

三个回测入口:
  - run_backtest:        经典策略回测 (13 个内置策略, strategy_id 选择)
  - run_backtest_custom: 信号原子组合回测 (1-2 个 atom AND + 7 种持有规则)
  - run_grid_backtest:   网格交易回测 (A 股 T+1 / 涨跌停 / 三段式费率)

扩展点:
  - register(SignalAtom(...)):  注册自己的信号原子, 立刻可用于 run_backtest_custom
  - Strategy 子类 + STRATEGY_REGISTRY: 添加自己的策略
"""

__version__ = "1.0.0"

from astock_backtest.data_loader import DataStore, get_store
from astock_backtest.models.backtest_custom import BacktestCustomRequest
from astock_backtest.services.backtest_engine import run_backtest, to_digest_backtest
from astock_backtest.services.backtest_custom_engine import run_backtest_custom
from astock_backtest.services.grid_engine import run_grid_backtest
from astock_backtest.signal_atoms import (
    ATOM_REGISTRY,
    DataDep,
    SignalAtom,
    SignalCategory,
    register,
)
from astock_backtest.strategies import STRATEGY_REGISTRY, Strategy, get_strategy

__all__ = [
    "ATOM_REGISTRY",
    "BacktestCustomRequest",
    "DataDep",
    "DataStore",
    "STRATEGY_REGISTRY",
    "SignalAtom",
    "SignalCategory",
    "Strategy",
    "get_store",
    "get_strategy",
    "register",
    "run_backtest",
    "run_backtest_custom",
    "run_grid_backtest",
    "to_digest_backtest",
]
