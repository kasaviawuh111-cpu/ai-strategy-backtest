"""信号原子库 (Phase A: 30 个高频原子)

每个原子是 SignalAtom 实例, 注册到 ATOM_REGISTRY (id -> instance).
"""

from astock_backtest.signal_atoms.base import (
    ATOM_REGISTRY,
    DataDep,
    SignalAtom,
    SignalCategory,
    register,
)

# 触发各模块注册
from astock_backtest.signal_atoms import technical  # noqa: F401, E402
from astock_backtest.signal_atoms import price_action  # noqa: F401, E402
from astock_backtest.signal_atoms import volume  # noqa: F401, E402
from astock_backtest.signal_atoms import fundamental  # noqa: F401, E402

__all__ = [
    "ATOM_REGISTRY",
    "DataDep",
    "SignalAtom",
    "SignalCategory",
    "register",
]
