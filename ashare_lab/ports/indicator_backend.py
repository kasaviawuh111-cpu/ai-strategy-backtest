"""Boundary for causal third-party indicator oracles and shadow providers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

type NumericIndicatorSeries = tuple[Decimal | None, ...]


@dataclass(frozen=True, slots=True)
class MacdIndicatorPoint:
    """Domestic-chart MACD values for one observable bar."""

    dif: Decimal
    dea: Decimal
    histogram: Decimal


type MacdIndicatorSeries = tuple[MacdIndicatorPoint | None, ...]


@runtime_checkable
class IndicatorBackend(Protocol):
    """Provider protocol used to cross-check canonical indicator arithmetic.

    Inputs are ordered closes. Implementations must be pure and causal: output
    position ``i`` may depend only on input positions ``<= i``.  A method being
    present does not make it safe for runtime replacement; only definitions in
    ``adoption_allowlist`` have passed the required semantic comparison.
    """

    @property
    def backend_id(self) -> str: ...

    @property
    def adoption_allowlist(self) -> frozenset[str]: ...

    def ema(
        self,
        closes: Sequence[Decimal],
        *,
        period: int,
    ) -> NumericIndicatorSeries: ...

    def rsi(
        self,
        closes: Sequence[Decimal],
        *,
        period: int = 14,
    ) -> NumericIndicatorSeries: ...

    def macd(
        self,
        closes: Sequence[Decimal],
        *,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
    ) -> MacdIndicatorSeries: ...
