"""Performance metrics calculated only from immutable run artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from math import sqrt
from statistics import fmean, stdev

from ashare_lab.domain.shared import DomainValidationError


@dataclass(frozen=True, slots=True)
class EquityPoint:
    session_date: date
    equity: Decimal
    benchmark: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.equity.is_finite() or self.equity <= 0:
            raise DomainValidationError("equity must be a finite positive Decimal")
        if self.benchmark is not None and (not self.benchmark.is_finite() or self.benchmark <= 0):
            raise DomainValidationError("benchmark must be a finite positive Decimal")


@dataclass(frozen=True, slots=True)
class RoundTrip:
    entry_date: date
    exit_date: date
    net_pnl: Decimal

    def __post_init__(self) -> None:
        if self.entry_date > self.exit_date:
            raise DomainValidationError("round-trip entry cannot follow exit")
        if not self.net_pnl.is_finite():
            raise DomainValidationError("round-trip PnL must be finite")

    @property
    def holding_days(self) -> int:
        return (self.exit_date - self.entry_date).days


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    total_return: float
    annualized_return: float | None
    maximum_drawdown: float
    annualized_volatility: float | None
    sharpe_ratio: float | None
    benchmark_return: float | None
    excess_return: float | None
    trade_count: int
    win_rate: float | None
    profit_factor: float | None
    average_holding_days: float | None
    statistical_warning: str | None


def calculate_metrics(
    curve: tuple[EquityPoint, ...],
    trades: tuple[RoundTrip, ...] = (),
) -> BacktestMetrics:
    if not curve:
        raise DomainValidationError("equity curve cannot be empty")
    dates = [point.session_date for point in curve]
    if dates != sorted(dates) or len(dates) != len(set(dates)):
        raise DomainValidationError("equity curve dates must be strictly increasing")

    start = float(curve[0].equity)
    end = float(curve[-1].equity)
    total_return = end / start - 1.0
    elapsed_days = (curve[-1].session_date - curve[0].session_date).days
    annualized_return = None
    if elapsed_days > 0 and start > 0 and end > 0:
        annualized_return = (end / start) ** (365.2425 / elapsed_days) - 1.0

    maximum_drawdown = _maximum_drawdown(curve)
    daily_returns = [
        float(curve[index].equity / curve[index - 1].equity) - 1.0 for index in range(1, len(curve))
    ]
    annualized_volatility = None
    sharpe_ratio = None
    if len(daily_returns) >= 2:
        daily_stdev = stdev(daily_returns)
        annualized_volatility = daily_stdev * sqrt(252)
        if daily_stdev > 0:
            sharpe_ratio = fmean(daily_returns) / daily_stdev * sqrt(252)

    benchmark_return = _benchmark_return(curve)
    excess_return = None if benchmark_return is None else total_return - benchmark_return
    winning = [trade for trade in trades if trade.net_pnl > 0]
    losses = [trade for trade in trades if trade.net_pnl < 0]
    win_rate = len(winning) / len(trades) if trades else None
    gross_profit = sum((trade.net_pnl for trade in winning), Decimal("0"))
    gross_loss = -sum((trade.net_pnl for trade in losses), Decimal("0"))
    profit_factor = None
    if gross_loss > 0:
        profit_factor = float(gross_profit / gross_loss)
    elif gross_profit > 0:
        profit_factor = float("inf")
    average_holding_days = fmean(trade.holding_days for trade in trades) if trades else None

    return BacktestMetrics(
        total_return=total_return,
        annualized_return=annualized_return,
        maximum_drawdown=maximum_drawdown,
        annualized_volatility=annualized_volatility,
        sharpe_ratio=sharpe_ratio,
        benchmark_return=benchmark_return,
        excess_return=excess_return,
        trade_count=len(trades),
        win_rate=win_rate,
        profit_factor=profit_factor,
        average_holding_days=average_holding_days,
        statistical_warning=("sample_too_small_for_strong_inference" if len(trades) < 30 else None),
    )


def _maximum_drawdown(curve: tuple[EquityPoint, ...]) -> float:
    peak = curve[0].equity
    worst = Decimal("0")
    for point in curve:
        peak = max(peak, point.equity)
        drawdown = point.equity / peak - Decimal("1")
        worst = min(worst, drawdown)
    return float(worst)


def _benchmark_return(curve: tuple[EquityPoint, ...]) -> float | None:
    if any(point.benchmark is None for point in curve):
        return None
    first = curve[0].benchmark
    last = curve[-1].benchmark
    if first is None or last is None:
        return None
    return float(last / first - Decimal("1"))
