"""§ 单股定制策略 (③) schema — 单股 + 用户自定义 atom 组合 + 持有规则.

跟反向求解 schema 差异:
  - 必填 stock_code (单股, 非全市场)
  - 必填 buy_atoms (1-2 个 atom_id, AND 关系)
  - 持有规则字段跟反向求解扁平 7 字段一致
  - 输出沿用 BacktestResponse (P&L 曲线 + 4 指标 + 逐笔), 不是 top N 列表
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, model_validator


class BacktestCustomRequest(BaseModel):
    # === 必填: 单股 + 信号 ===
    stock_code: str = Field(description="股票代码 (6 位, 例 '002151')")
    buy_atoms: list[str] = Field(
        description="买入信号 atom_id 列表 (1-2 个, AND 关系)",
        min_length=1,
        max_length=2,
    )

    # === 持有规则 (扁平 7 字段, 跟 reverse_search 完全一致) ===
    holding_days: Optional[int] = Field(
        default=None, description="固定持有 N 个交易日", ge=1, le=250
    )
    stop_loss_pct: Optional[float] = Field(
        default=None, description="跌幅 ≥ pct% 止损", gt=0, le=50
    )
    take_profit_pct: Optional[float] = Field(
        default=None, description="涨幅 ≥ pct% 止盈", gt=0, le=200
    )
    trailing_stop_pct: Optional[float] = Field(
        default=None, description="移动止损 (峰值回撤 pct%)", gt=0, le=50
    )
    sell_on_fixed_atom_id: Optional[str] = Field(
        default=None, description="出现指定 atom_id 信号时卖"
    )
    sell_on_reverse_signal: bool = Field(
        default=False,
        description="出现反向信号时卖 (仅在 buy_atoms[0] 是穿越/状态类信号时有效)",
    )
    hold_until: Optional[str] = Field(
        default=None, description="持有到时间周期末 ('month_end'/'quarter_end')"
    )

    # === 回测区间 ===
    start_date: Optional[str] = Field(
        default=None, description="起始日 YYYYMMDD / YYYY-MM-DD (None=全段)"
    )
    end_date: Optional[str] = Field(
        default=None, description="结束日 (None=今日)"
    )

    # === 资金参数 (跟 ① run_backtest 一致) ===
    initial_capital: float = Field(default=100000.0, gt=0)
    commission: float = 0.0003
    stamp_tax: float = 0.001
    slippage: float = 0.001

    # === 5-19 股池字段 (③ 本身是单股, 这些字段保留为占位, 不会用; 但跟 ②④ schema 对齐方便统一处理) ===
    # 注: ③ stock_code 已经定了单股, stock_pool/industry/market_cap 字段无意义, 跳过

    @model_validator(mode="after")
    def _validate_rule(self) -> "BacktestCustomRequest":
        rules_set = [
            self.holding_days,
            self.stop_loss_pct,
            self.take_profit_pct,
            self.trailing_stop_pct,
            self.sell_on_fixed_atom_id,
            self.hold_until,
        ]
        if not any(r is not None for r in rules_set) and not self.sell_on_reverse_signal:
            raise ValueError("至少需要一个持有规则 (holding_days / stop_loss_pct / take_profit_pct / trailing_stop_pct / sell_on_fixed_atom_id / sell_on_reverse_signal / hold_until)")
        # 去重: buy_atoms 不能重复
        if len(self.buy_atoms) != len(set(self.buy_atoms)):
            raise ValueError("buy_atoms 不能重复")
        return self
