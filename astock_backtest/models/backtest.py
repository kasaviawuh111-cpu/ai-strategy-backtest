from typing import Any, Optional

from pydantic import BaseModel, Field


class BacktestRequest(BaseModel):
    stock_code: str
    strategy_id: str
    params: dict[str, Any] = Field(default_factory=dict)
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    initial_capital: float = 100000.0
    commission: float = 0.0003
    stamp_tax: float = 0.001
    slippage: float = 0.001
    stop_loss: Optional[float] = None
    stop_profit: Optional[float] = None


class EquityPoint(BaseModel):
    date: str
    value: float


class TradeRecord(BaseModel):
    date: str
    action: str
    price: float
    shares: int
    cash_flow: float
    pnl: Optional[float] = None
    reason: Optional[str] = None


class BacktestResponse(BaseModel):
    stock_code: str
    stock_name: str
    strategy_id: str
    strategy_name: str
    params: dict[str, Any]
    start_date: str
    end_date: str
    initial_capital: float

    total_return: float
    annual_return: float
    max_drawdown: float
    sharpe_ratio: Optional[float]
    win_rate: Optional[float]
    profit_loss_ratio: Optional[float]
    total_trades: int
    avg_holding_days: Optional[float]
    benchmark_return: Optional[float]

    equity_curve: list[EquityPoint]
    benchmark_curve: list[EquityPoint]
    trade_log: list[TradeRecord]
    warnings: list[str]


# ── Digest schema (compliance: no raw trade prices, monthly normalized curves) ──

class NormalizedEquityPoint(BaseModel):
    date: str          # 月末 ISO 日期
    value: float       # 相对初始资金 100 的归一化净值


class TradeSummary(BaseModel):
    total_buys: int
    total_sells: int
    profitable_trades: int
    losing_trades: int
    avg_holding_days: Optional[float]
    max_holding_days: Optional[int]
    best_trade_pct: Optional[float]    # 单笔最佳收益率
    worst_trade_pct: Optional[float]   # 单笔最差收益率
    active_months: int                  # 有交易的月数
    stop_loss_count: int = 0
    stop_profit_count: int = 0


class BacktestDigestResponse(BaseModel):
    stock_code: str
    stock_name: str
    strategy_id: str
    strategy_name: str
    params: dict[str, Any]
    start_date: str
    end_date: str

    total_return: float
    annual_return: float
    max_drawdown: float
    win_rate: Optional[float]
    profit_loss_ratio: Optional[float]
    total_trades: int
    avg_holding_days: Optional[float]
    benchmark_return: Optional[float]

    equity_curve_normalized: list[NormalizedEquityPoint]
    benchmark_curve_normalized: list[NormalizedEquityPoint]
    trade_summary: TradeSummary
    warnings: list[str]
