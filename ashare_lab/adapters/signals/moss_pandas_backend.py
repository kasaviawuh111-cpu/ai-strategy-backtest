"""Pandas-backed indicator oracle adapted from the pinned MOSS implementation.

The upstream formulas come from ``scripts/core/indicators.py`` in
``moss-site/moss-trade-bot-skills`` at commit
``1fce09b03151a01ba1dee230466ec64cdd12fda8`` (MIT-0).  This module calls the
same mature pandas primitives instead of maintaining a second handwritten
recurrence.  It is a differential oracle, not the production signal runtime.

The A-share observation adapter only adds two explicit conventions: stability
warmups and the domestic MACD histogram ``2 * (DIF - DEA)``.  Indicators are
eligible to replace the canonical implementation only when listed by
``adoption_allowlist`` after golden-vector comparison.  MOSS RSI deliberately
remains available for comparison but is excluded because its EWM seed differs
from the project's frozen Wilder definition.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from decimal import Decimal
from typing import cast

import pandas as pd

from ashare_lab.ports.indicator_backend import (
    MacdIndicatorPoint,
    MacdIndicatorSeries,
    NumericIndicatorSeries,
)

MOSS_REPOSITORY = "https://github.com/moss-site/moss-trade-bot-skills"
MOSS_COMMIT = "1fce09b03151a01ba1dee230466ec64cdd12fda8"


class MossPandasIndicatorBackend:
    """Thin, causal adapter over the pinned MOSS/pandas indicator formulas."""

    @property
    def backend_id(self) -> str:
        return f"moss.pandas.{MOSS_COMMIT}.a-share-observation.v1"

    @property
    def adoption_allowlist(self) -> frozenset[str]:
        """Definitions proven equivalent enough for a future shadow rollout."""

        return frozenset({"ema", "macd"})

    def ema(
        self,
        closes: Sequence[Decimal],
        *,
        period: int,
    ) -> NumericIndicatorSeries:
        values = _series(closes)
        _require_positive_period(period)
        raw = cast(
            pd.Series,
            values.ewm(span=period, adjust=False).mean(),  # pyright: ignore[reportUnknownMemberType]
        )
        return _decimal_series(raw, first_ready_index=period - 1)

    def rsi(
        self,
        closes: Sequence[Decimal],
        *,
        period: int = 14,
    ) -> NumericIndicatorSeries:
        """Return the pinned MOSS RSI for comparison, never runtime adoption.

        MOSS uses pandas' adjusted EWM seed and replaces a zero average loss
        with NaN.  Those semantics are intentionally preserved here so a
        differential test can prove why this candidate is not interchangeable
        with the frozen A-share Wilder RSI.
        """

        values = _series(closes)
        _require_positive_period(period)
        delta = values.diff()
        gain = delta.where(  # pyright: ignore[reportUnknownMemberType]
            delta > 0,
            0.0,
        )
        loss = -delta.where(  # pyright: ignore[reportUnknownMemberType]
            delta < 0,
            0.0,
        )
        average_gain = cast(
            pd.Series,
            gain.ewm(alpha=1 / period, min_periods=period).mean(),  # pyright: ignore[reportUnknownMemberType]
        )
        average_loss = cast(
            pd.Series,
            loss.ewm(alpha=1 / period, min_periods=period).mean(),  # pyright: ignore[reportUnknownMemberType]
        )
        nonzero_loss = average_loss.replace(  # pyright: ignore[reportUnknownMemberType]
            0,
            float("nan"),
        )
        relative_strength = average_gain / nonzero_loss
        raw = cast(pd.Series, 100 - (100 / (1 + relative_strength)))
        return _decimal_series(raw, first_ready_index=period - 1)

    def macd(
        self,
        closes: Sequence[Decimal],
        *,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
    ) -> MacdIndicatorSeries:
        values = _series(closes)
        for period in (fast, slow, signal):
            _require_positive_period(period)
        if fast >= slow:
            raise ValueError("MACD fast period must be less than slow period")

        fast_ema = cast(
            pd.Series,
            values.ewm(span=fast, adjust=False).mean(),  # pyright: ignore[reportUnknownMemberType]
        )
        slow_ema = cast(
            pd.Series,
            values.ewm(span=slow, adjust=False).mean(),  # pyright: ignore[reportUnknownMemberType]
        )
        dif = cast(pd.Series, fast_ema - slow_ema)
        dea = cast(
            pd.Series,
            dif.ewm(span=signal, adjust=False).mean(),  # pyright: ignore[reportUnknownMemberType]
        )
        first_ready_index = slow + signal - 2

        result: list[MacdIndicatorPoint | None] = [None] * len(values)
        for index in range(first_ready_index, len(values)):
            dif_value = _decimal(cast(float, dif.iloc[index]))
            dea_value = _decimal(cast(float, dea.iloc[index]))
            if dif_value is None or dea_value is None:
                continue
            result[index] = MacdIndicatorPoint(
                dif=dif_value,
                dea=dea_value,
                histogram=Decimal(2) * (dif_value - dea_value),
            )
        return tuple(result)


def _series(closes: Sequence[Decimal]) -> pd.Series:
    values = tuple(closes)
    if any(type(value) is not Decimal for value in values):
        raise TypeError("indicator closes must be Decimal values")
    if any(not value.is_finite() for value in values):
        raise ValueError("indicator closes must be finite")
    floats = tuple(float(value) for value in values)
    if any(not math.isfinite(value) for value in floats):
        raise ValueError("indicator closes exceed the pandas float range")
    return pd.Series(floats, dtype="float64")


def _decimal_series(series: pd.Series, *, first_ready_index: int) -> NumericIndicatorSeries:
    return tuple(
        None if index < first_ready_index else _decimal(cast(float, series.iloc[index]))
        for index in range(len(series))
    )


def _decimal(value: float) -> Decimal | None:
    number = float(value)
    if not math.isfinite(number):
        return None
    return Decimal(str(number))


def _require_positive_period(period: int) -> None:
    if type(period) is not int or period <= 0:
        raise ValueError("indicator period must be a positive integer")
