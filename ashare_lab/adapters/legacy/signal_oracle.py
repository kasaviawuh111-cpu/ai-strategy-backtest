"""Shadow-only bridge to selected legacy ``astock_backtest`` signal atoms.

The old project exposes useful, causal pandas signal atoms, but its public
surface is a boolean signal registry rather than the canonical Decimal-valued
indicator API.  This adapter deliberately preserves that narrow boundary: it
can compare four frozen legacy atoms with canonical boolean observations, but
it cannot place orders, size positions, mutate a ledger, or provide values to
the production runtime.

Two definitions are suitable for post-warmup differential comparison:

* ``ma_5_cross_up_20`` uses inclusive simple moving averages in both engines.
* ``macd_golden_cross`` uses the same first-value-seeded EMA recurrence.  The
  legacy histogram is ``DIF - DEA`` whereas the canonical A-share histogram is
  ``2 * (DIF - DEA)``; the positive-cross event is nevertheless identical.

The other definitions are characterization-only by design:

* legacy RSI uses rolling simple gain/loss averages, not canonical Wilder RSI;
* legacy relative volume includes today (and zero-volume rows) in its MA20,
  while the canonical baseline excludes today and skips suspended observations.

All four legacy implementations operate on float-backed pandas series.  Their
outputs must therefore never enter the Decimal cash ledger.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

import pandas as pd

from astock_backtest.signal_atoms import ATOM_REGISTRY, SignalAtom

LEGACY_ASTOCK_VERSION = "1.0.0"

_SUPPORTED_SIGNAL_IDS = frozenset(
    {
        "ma_5_cross_up_20",
        "macd_golden_cross",
        "rsi_below_30",
        "volume_2x_ma20",
    }
)
_EQUIVALENCE_SIGNAL_IDS = frozenset({"ma_5_cross_up_20", "macd_golden_cross"})
_COMMON_READY_INDEX = {
    "ma_5_cross_up_20": 21,
    "macd_golden_cross": 37,
    "rsi_below_30": 16,
    "volume_2x_ma20": 21,
}
_SEMANTIC_NOTES = {
    "ma_5_cross_up_20": ("inclusive SMA definitions match after the conservative legacy warmup",),
    "macd_golden_cross": (
        "legacy histogram is DIF-DEA; canonical histogram is 2*(DIF-DEA)",
        "the positive-cross event is scale invariant after the common warmup",
    ),
    "rsi_below_30": (
        "characterization only: legacy RSI uses rolling simple averages",
        "canonical RSI uses a Wilder seed and recurrence",
    ),
    "volume_2x_ma20": (
        "characterization only: legacy MA20 includes today's volume",
        "canonical relative-volume baseline excludes today and skips zero-volume rows",
    ),
}


@dataclass(frozen=True, slots=True)
class LegacyDifferentialResult:
    """One aligned shadow comparison without any production adoption claim."""

    signal_id: str
    legacy_values: tuple[bool, ...]
    canonical_values: tuple[bool, ...]
    comparison_start: int
    mismatch_indices: tuple[int, ...]
    values_equal_after_start: bool
    semantically_eligible: bool
    contains_zero_volume_rows: bool
    notes: tuple[str, ...]


class LegacyAstockSignalOracle:
    """Call a frozen subset of the old public signal-atom registry.

    The class is intentionally absent from application bootstrap.  Callers
    must opt in from tests or an offline shadow tool.
    """

    @property
    def backend_id(self) -> str:
        return f"astock_backtest.signal_atoms.{LEGACY_ASTOCK_VERSION}.shadow.v1"

    @property
    def supported_signal_ids(self) -> frozenset[str]:
        return _SUPPORTED_SIGNAL_IDS

    @property
    def differential_equivalence_ids(self) -> frozenset[str]:
        """Signals whose event definitions can be compared after warmup.

        This is not a production adoption allowlist.  Even these signals remain
        float-backed shadow observations.
        """

        return _EQUIVALENCE_SIGNAL_IDS

    def compute(
        self,
        signal_id: str,
        *,
        closes: Sequence[Decimal],
        volumes: Sequence[int] | None = None,
    ) -> tuple[bool, ...]:
        """Compute one selected legacy boolean signal on ordered daily rows."""

        frame = _legacy_frame(closes, volumes)
        atom = _selected_atom(signal_id)
        raw = atom.compute(frame)
        return tuple(bool(value) for value in raw.tolist())

    def compare(
        self,
        signal_id: str,
        *,
        closes: Sequence[Decimal],
        canonical_values: Sequence[bool],
        volumes: Sequence[int] | None = None,
    ) -> LegacyDifferentialResult:
        """Return aligned mismatch evidence for one canonical signal series."""

        close_values = tuple(closes)
        volume_values = _validated_volumes(volumes, len(close_values))
        canonical = _validated_boolean_series(canonical_values, len(close_values))
        legacy = self.compute(signal_id, closes=close_values, volumes=volume_values)
        comparison_start = _COMMON_READY_INDEX[_selected_signal_id(signal_id)]
        mismatches = tuple(
            index
            for index in range(comparison_start, len(legacy))
            if legacy[index] != canonical[index]
        )
        contains_zero_volume_rows = any(volume == 0 for volume in volume_values)
        semantically_eligible = (
            signal_id in _EQUIVALENCE_SIGNAL_IDS and not contains_zero_volume_rows
        )
        notes = list(_SEMANTIC_NOTES[signal_id])
        if contains_zero_volume_rows:
            notes.append(
                "zero-volume rows have no legacy trading-status semantics; equivalence is withheld"
            )
        return LegacyDifferentialResult(
            signal_id=signal_id,
            legacy_values=legacy,
            canonical_values=canonical,
            comparison_start=comparison_start,
            mismatch_indices=mismatches,
            values_equal_after_start=not mismatches,
            semantically_eligible=semantically_eligible,
            contains_zero_volume_rows=contains_zero_volume_rows,
            notes=tuple(notes),
        )


def _selected_atom(signal_id: str) -> SignalAtom:
    selected = _selected_signal_id(signal_id)
    atom = ATOM_REGISTRY.get(selected)
    if atom is None:  # fail closed if the retired registry drifts
        raise RuntimeError(f"legacy astock atom is unavailable: {selected}")
    return atom


def _selected_signal_id(signal_id: str) -> str:
    if signal_id not in _SUPPORTED_SIGNAL_IDS:
        supported = ", ".join(sorted(_SUPPORTED_SIGNAL_IDS))
        raise ValueError(
            f"unsupported legacy shadow signal {signal_id!r}; expected one of {supported}"
        )
    return signal_id


def _legacy_frame(
    closes: Sequence[Decimal],
    volumes: Sequence[int] | None,
) -> pd.DataFrame:
    close_values = tuple(closes)
    if any(type(value) is not Decimal for value in close_values):
        raise TypeError("legacy shadow closes must contain Decimal values")
    if any(not value.is_finite() for value in close_values):
        raise ValueError("legacy shadow closes must be finite")
    float_closes = tuple(float(value) for value in close_values)
    if any(not math.isfinite(value) for value in float_closes):
        raise ValueError("legacy shadow closes exceed the pandas float range")
    volume_values = _validated_volumes(volumes, len(close_values))
    return pd.DataFrame(
        {
            "open": float_closes,
            "high": float_closes,
            "low": float_closes,
            "close": float_closes,
            "volume": volume_values,
        }
    )


def _validated_volumes(volumes: Sequence[int] | None, length: int) -> tuple[int, ...]:
    values = tuple(1 for _ in range(length)) if volumes is None else tuple(volumes)
    if len(values) != length:
        raise ValueError("legacy shadow volumes must align with closes")
    if any(type(value) is not int or value < 0 for value in values):
        raise ValueError("legacy shadow volumes must be non-negative integers")
    return values


def _validated_boolean_series(values: Sequence[bool], length: int) -> tuple[bool, ...]:
    result = tuple(values)
    if len(result) != length:
        raise ValueError("canonical shadow values must align with closes")
    if any(type(value) is not bool for value in result):
        raise TypeError("canonical shadow values must contain booleans")
    return result
