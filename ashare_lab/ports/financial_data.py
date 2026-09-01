"""Application boundary for immutable point-in-time financial facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from ashare_lab.domain.financials import FinancialFactRecord
from ashare_lab.domain.strategy import StrategySpec

from .market_data import DateRange


@dataclass(frozen=True, slots=True)
class PinnedFinancialFacts:
    """One server-created, content-addressed financial input bundle."""

    snapshot_id: str
    checksum: str
    provider: str
    schema_version: str
    coverage_start: date
    coverage_end: date
    identity_basis: dict[str, object]
    facts: tuple[FinancialFactRecord, ...]


class FinancialFactLoader(Protocol):
    """Acquire and pin facts needed by one already-validated strategy."""

    def load(
        self,
        strategy: StrategySpec,
        period: DateRange,
        *,
        retrieved_at: datetime,
    ) -> PinnedFinancialFacts: ...


__all__ = ["FinancialFactLoader", "PinnedFinancialFacts"]
