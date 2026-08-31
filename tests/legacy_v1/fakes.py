"""Small in-memory fixtures for legacy engine characterization tests."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import ClassVar

import pandas as pd

from astock_backtest.strategies.base import Signal, Strategy

STOCK_CODE = "600000"
STOCK_NAME = "legacy-fixture"


class ScriptedStrategy(Strategy):
    """Return the signal assigned to the current history date."""

    name = "legacy_scripted"
    display_name = "Legacy scripted strategy"
    default_params: ClassVar[dict[str, dict[str, str]]] = {"signals": {}}

    def generate_signal(self, history: pd.DataFrame) -> Signal:
        day = pd.Timestamp(history["date"].iloc[-1]).strftime("%Y-%m-%d")
        raw = self.params["signals"].get(day, Signal.HOLD.value)
        return Signal(raw)


@dataclass
class FakeStore:
    """The subset of DataStore used by the current single-stock engines."""

    ohlcv: pd.DataFrame
    index_daily: pd.DataFrame
    stock_code: str = STOCK_CODE
    stock_name: str = STOCK_NAME

    def __post_init__(self) -> None:
        self.code_name_map = {self.stock_code: self.stock_name}
        self.ohlcv_by_code = {self.stock_code: self.ohlcv}
        self.valuation_by_code: dict[str, pd.DataFrame] = {}
        self.financial_by_code: dict[str, pd.DataFrame] = {}
        self.dividend_by_code: dict[str, pd.DataFrame] = {}

    def get_ohlcv(self, stock_code: str) -> pd.DataFrame:
        if stock_code != self.stock_code:
            return pd.DataFrame()
        return self.ohlcv.copy()


def make_ohlcv(
    *,
    opens: Iterable[float],
    closes: Iterable[float],
    pct_changes: Iterable[float] | None = None,
    start: str = "2024-01-02",
    stock_code: str = STOCK_CODE,
) -> pd.DataFrame:
    open_values = [float(v) for v in opens]
    close_values = [float(v) for v in closes]
    if len(open_values) != len(close_values):
        raise ValueError("opens and closes must have the same length")

    if pct_changes is None:
        pct_values = [0.0] * len(open_values)
    else:
        pct_values = [float(v) for v in pct_changes]
    if len(pct_values) != len(open_values):
        raise ValueError("pct_changes must match opens and closes")

    dates = pd.bdate_range(start, periods=len(open_values))
    highs = [max(o, c) + 0.5 for o, c in zip(open_values, close_values, strict=True)]
    lows = [min(o, c) - 0.5 for o, c in zip(open_values, close_values, strict=True)]
    return pd.DataFrame(
        {
            "stock_code": [stock_code] * len(open_values),
            "date": dates,
            "open": open_values,
            "high": highs,
            "low": lows,
            "close": close_values,
            "volume": [1_000_000.0] * len(open_values),
            "amount": [10_000_000.0] * len(open_values),
            "turnover_rate": [1.0] * len(open_values),
            "pct_change": pct_values,
        }
    )


def make_store(ohlcv: pd.DataFrame) -> FakeStore:
    index_daily = pd.DataFrame(
        {
            "date": ohlcv["date"].copy(),
            "close": [1000.0 + i for i in range(len(ohlcv))],
        }
    )
    return FakeStore(ohlcv=ohlcv, index_daily=index_daily)


def date_label(ohlcv: pd.DataFrame, index: int) -> str:
    return pd.Timestamp(ohlcv["date"].iloc[index]).strftime("%Y-%m-%d")


def signal_plan(ohlcv: pd.DataFrame, signals: Mapping[int, Signal]) -> dict[str, str]:
    return {date_label(ohlcv, index): signal.value for index, signal in signals.items()}
