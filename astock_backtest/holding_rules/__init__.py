"""持有规则原子库 (Phase A: 4 叶子规则 + EarliestOf 组合)

按 docs/phase3-reverse-search-v0.3.md §5.1 schema 暴露的 4 字段对应:
  HoldNDays(n)          ↔ schema.holding_days
  StopLossPct(pct)      ↔ schema.stop_loss_pct
  TakeProfitPct(pct)    ↔ schema.take_profit_pct
  SellOnReverseSignal() ↔ schema.sell_on_reverse_signal
  EarliestOf([rules])     多个 schema 字段同时给值时引擎内部组合

Phase B/C 加新 schema 字段时再实现 TrailingStop / SellOnFixedAtom / HoldUntilTimeUnit.
"""

from astock_backtest.holding_rules.base import (
    EarliestOf,
    HoldingRule,
    HoldingRuleResult,
)
from astock_backtest.holding_rules.fixed_signal import SellOnFixedAtom
from astock_backtest.holding_rules.price_based import StopLossPct, TakeProfitPct
from astock_backtest.holding_rules.signal_based import SellOnReverseSignal
from astock_backtest.holding_rules.time_based import HoldNDays
from astock_backtest.holding_rules.time_unit import HoldUntilTimeUnit
from astock_backtest.holding_rules.trailing_stop import TrailingStop

__all__ = [
    "EarliestOf",
    "HoldNDays",
    "HoldUntilTimeUnit",
    "HoldingRule",
    "HoldingRuleResult",
    "SellOnFixedAtom",
    "SellOnReverseSignal",
    "StopLossPct",
    "TakeProfitPct",
    "TrailingStop",
]
