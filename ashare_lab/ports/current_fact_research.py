"""Non-executable boundary for current public-fact research.

Results from this port may explain a viewpoint or resolve an unknown entity,
but they are never historical market data, a strategy candidate, or a signal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class ResearchPurpose(StrEnum):
    VIEWPOINT = "viewpoint"
    UNKNOWN_ENTITY = "unknown_entity"
    CURRENT_FACT = "current_fact"


@dataclass(frozen=True, slots=True)
class CurrentFactResearchRequest:
    query: str
    purpose: ResearchPurpose
    as_of: datetime
    instrument_context: str | None = None

    def __post_init__(self) -> None:
        query = self.query.strip()
        if not query:
            raise ValueError("research query cannot be empty")
        if len(query) > 4_000:
            raise ValueError("research query is too long")
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ValueError("as_of must include an explicit timezone")
        object.__setattr__(self, "query", query)
        if self.instrument_context is not None:
            context = self.instrument_context.strip()
            object.__setattr__(self, "instrument_context", context or None)


@dataclass(frozen=True, slots=True)
class ResearchSource:
    source_id: str
    title: str
    url: str
    publisher: str
    published_at: str | None


@dataclass(frozen=True, slots=True)
class ResearchFact:
    statement: str
    fact_kind: str
    source_ids: tuple[str, ...]
    time_scope: str | None


@dataclass(frozen=True, slots=True)
class CurrentFactResearchResult:
    provider: str
    model: str
    provider_response_id: str
    query: str
    purpose: ResearchPurpose
    as_of: datetime
    summary: str
    facts: tuple[ResearchFact, ...]
    sources: tuple[ResearchSource, ...]
    unresolved_questions: tuple[str, ...]
    retrieved_at: datetime
    response_sha256: str
    search_call_count: int
    schema_version: str = "current-fact-research.v1"

    @property
    def executable_strategy(self) -> None:
        """Make the non-executable boundary explicit to callers."""

        return None

    @property
    def signal_records(self) -> tuple[object, ...]:
        """Research output can never carry model-authored trading signals."""

        return ()


class CurrentFactResearcher(Protocol):
    async def research(
        self,
        request: CurrentFactResearchRequest,
    ) -> CurrentFactResearchResult: ...


__all__ = [
    "CurrentFactResearchRequest",
    "CurrentFactResearchResult",
    "CurrentFactResearcher",
    "ResearchFact",
    "ResearchPurpose",
    "ResearchSource",
]
