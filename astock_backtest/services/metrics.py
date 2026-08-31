from typing import Optional

import numpy as np
import pandas as pd

TRADING_DAYS_PER_YEAR = 252


def total_return(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    start = float(equity.iloc[0])
    end = float(equity.iloc[-1])
    if start <= 0:
        return 0.0
    return end / start - 1.0


def annual_return(equity: pd.Series) -> float:
    if len(equity) < 2:
        return 0.0
    tr = total_return(equity)
    n_days = len(equity)
    years = n_days / TRADING_DAYS_PER_YEAR
    if years <= 0:
        return 0.0
    base = 1.0 + tr
    if base <= 0:
        return -1.0
    return base ** (1.0 / years) - 1.0


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    cummax = equity.cummax()
    dd = equity / cummax - 1.0
    return float(dd.min())


def sharpe_ratio(equity: pd.Series, risk_free_rate: float = 0.025) -> Optional[float]:
    if len(equity) < 2:
        return None
    returns = equity.pct_change().dropna()
    if returns.empty:
        return None
    std = returns.std()
    if std == 0 or pd.isna(std):
        return None
    mean_daily = returns.mean()
    rf_daily = risk_free_rate / TRADING_DAYS_PER_YEAR
    sr = (mean_daily - rf_daily) / std * np.sqrt(TRADING_DAYS_PER_YEAR)
    return float(sr)


def trade_stats(trade_log: list[dict]) -> dict:
    pairs = []
    entry: Optional[dict] = None
    for t in trade_log:
        if t["action"] == "BUY":
            entry = t
        elif t["action"] == "SELL" and entry is not None:
            pnl = (t["price"] - entry["price"]) * t["shares"]
            hold_days = (pd.Timestamp(t["date"]) - pd.Timestamp(entry["date"])).days
            pairs.append({"pnl": pnl, "hold_days": hold_days, "return": (t["price"] - entry["price"]) / entry["price"]})
            entry = None

    if not pairs:
        return {
            "win_rate": None,
            "profit_loss_ratio": None,
            "avg_holding_days": None,
            "closed_trades": 0,
        }

    wins = [p for p in pairs if p["return"] > 0]
    losses = [p for p in pairs if p["return"] <= 0]
    win_rate = len(wins) / len(pairs)
    avg_win = np.mean([p["return"] for p in wins]) if wins else 0.0
    avg_loss = abs(np.mean([p["return"] for p in losses])) if losses else 0.0
    pl_ratio: Optional[float]
    if avg_loss > 0:
        pl_ratio = float(avg_win / avg_loss)
    elif wins:
        pl_ratio = None
    else:
        pl_ratio = 0.0
    avg_hold = float(np.mean([p["hold_days"] for p in pairs]))
    return {
        "win_rate": float(win_rate),
        "profit_loss_ratio": pl_ratio,
        "avg_holding_days": avg_hold,
        "closed_trades": len(pairs),
    }


def benchmark_return(benchmark_curve: pd.Series) -> Optional[float]:
    if benchmark_curve.empty:
        return None
    start = float(benchmark_curve.iloc[0])
    end = float(benchmark_curve.iloc[-1])
    if start <= 0:
        return None
    return end / start - 1.0
