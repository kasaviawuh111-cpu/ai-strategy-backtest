"""Non-executable boundary for narrating one verified portfolio highlight.

The caller owns every performance fact.  Implementations may research public
context for a likely explanation, but cannot alter the supplied evidence or
turn the narrative into investment advice or a trading signal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol


def _required_text(value: str, *, field_name: str, max_length: int) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} cannot be empty")
    if len(normalized) > max_length:
        raise ValueError(f"{field_name} is too long")
    return normalized


class DriverConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True, slots=True)
class VerifiedPerformanceEvidence:
    """A display-safe fact already reconciled against the account ledger."""

    evidence_id: str
    statement: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "evidence_id",
            _required_text(self.evidence_id, field_name="evidence_id", max_length=128),
        )
        object.__setattr__(
            self,
            "statement",
            _required_text(self.statement, field_name="evidence statement", max_length=500),
        )


@dataclass(frozen=True, slots=True)
class VerifiedPortfolioHighlight:
    """One historical account action whose performance evidence is verified."""

    symbol: str
    name: str
    market: str
    action: str
    occurred_at: datetime
    performance_evidence: tuple[VerifiedPerformanceEvidence, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "symbol",
            _required_text(self.symbol, field_name="symbol", max_length=32),
        )
        object.__setattr__(
            self,
            "name",
            _required_text(self.name, field_name="name", max_length=160),
        )
        object.__setattr__(
            self,
            "market",
            _required_text(self.market, field_name="market", max_length=32),
        )
        object.__setattr__(
            self,
            "action",
            _required_text(self.action, field_name="action", max_length=80),
        )
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must include an explicit timezone")
        if not self.performance_evidence:
            raise ValueError("verified performance evidence cannot be empty")
        evidence_ids = [item.evidence_id for item in self.performance_evidence]
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("performance evidence ids must be unique")


@dataclass(frozen=True, slots=True)
class PortfolioNarrativeSource:
    source_id: str
    title: str
    url: str
    publisher: str
    published_at: str | None


@dataclass(frozen=True, slots=True)
class PortfolioLikelyDriver:
    reason: str
    confidence: DriverConfidence
    source_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PortfolioHighlightNarrative:
    """Model-owned context only; account facts are rendered by the caller.

    Headline, action, date, performance and monetary copy are deliberately not
    part of this contract.  They must come from ``VerifiedPortfolioHighlight``
    after the narrator returns.
    """

    likely_drivers: tuple[PortfolioLikelyDriver, ...]
    sources: tuple[PortfolioNarrativeSource, ...]
    unresolved: tuple[str, ...]

    @property
    def investment_advice(self) -> None:
        return None

    @property
    def signal_records(self) -> tuple[object, ...]:
        return ()


class PortfolioHighlightNarrator(Protocol):
    async def narrate(
        self,
        highlight: VerifiedPortfolioHighlight,
    ) -> PortfolioHighlightNarrative: ...


__all__ = [
    "DriverConfidence",
    "PortfolioHighlightNarrative",
    "PortfolioHighlightNarrator",
    "PortfolioLikelyDriver",
    "PortfolioNarrativeSource",
    "VerifiedPerformanceEvidence",
    "VerifiedPortfolioHighlight",
]
