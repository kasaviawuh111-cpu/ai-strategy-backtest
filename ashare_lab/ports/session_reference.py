"""Boundary for historically versioned instrument-session reference facts."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from ashare_lab.domain.market_data import DailyBar, InstrumentSession


class SessionReferenceProvider(Protocol):
    """Resolve tradability and price-limit facts for the supplied bars."""

    @property
    def version(self) -> str: ...

    def sessions_for(self, bars: Sequence[DailyBar]) -> Sequence[InstrumentSession]: ...
