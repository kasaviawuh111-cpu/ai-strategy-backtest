"""Non-executable strategy suggestions grounded in verified current facts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol, runtime_checkable

from ashare_lab.ports.idea_routing import IdeaProposal
from ashare_lab.ports.live_market_data import LiveFinanceDataResult, LiveMarketDataResult


@dataclass(frozen=True, slots=True)
class VerifiedFactStrategyAdviceRequest:
    original_utterance: str
    instrument_symbol: str
    as_of_date: date
    verified_facts: tuple[str, ...]
    max_proposals: int = 3


@dataclass(frozen=True, slots=True)
class StrategyAdviceCandidate:
    title: str
    hypothesis: str
    entry_summary: str
    exit_summary: str
    suggested_utterance: str


@dataclass(frozen=True, slots=True)
class VerifiedFactStrategyAdvice:
    analysis: str
    hypothesis: str
    proposals: tuple[StrategyAdviceCandidate, ...]
    provider: str
    model: str
    prompt_version: str
    schema_version: str


class VerifiedFactStrategyAdvisor(Protocol):
    async def advise(
        self,
        request: VerifiedFactStrategyAdviceRequest,
    ) -> VerifiedFactStrategyAdvice | None: ...


@dataclass(frozen=True, slots=True)
class StockRecommendation:
    symbol: str
    name: str
    reason: str
    source: str = ""
    retrieved_at: datetime | None = None


@runtime_checkable
class StockRecommendationAdvisor(Protocol):
    async def recommend_stocks(
        self,
        query: str,
        result: LiveMarketDataResult,
    ) -> tuple[StockRecommendation, ...] | None: ...


@dataclass(frozen=True, slots=True)
class QueryDataReview:
    """A model assessment of returned data, not executable tool instructions."""

    satisfied: bool
    evidence: tuple[str, ...]
    retry_query: str | None
    message: str


@runtime_checkable
class QueryDataReviewAdvisor(Protocol):
    async def review_query_result(
        self,
        *,
        question: str,
        data_snapshot: Mapping[str, object],
        previous_queries: tuple[str, ...] = (),
        remaining_data_rounds: int = 1,
    ) -> QueryDataReview | None: ...


@dataclass(frozen=True, slots=True)
class StockStrategyPair:
    proposal_id: str
    symbol: str
    name: str
    reason: str


@dataclass(frozen=True, slots=True)
class StockStrategyDataRequest:
    """Model-selected fields to look up on existing, verified candidates."""

    symbols: tuple[str, ...]
    fields: tuple[str, ...]
    message: str


@dataclass(frozen=True, slots=True)
class StockStrategyPairing:
    introduction: str
    pairs: tuple[StockStrategyPair, ...]
    data_request: StockStrategyDataRequest | None = None


@runtime_checkable
class StockStrategyPairingAdvisor(Protocol):
    async def pair_stock_strategies(
        self,
        utterance: str,
        result: LiveMarketDataResult,
        proposals: tuple[IdeaProposal, ...],
        understanding: str = "",
        *,
        supplemental_results: tuple[LiveFinanceDataResult, ...] = (),
        previous_requests: tuple[StockStrategyDataRequest, ...] = (),
        remaining_data_rounds: int = 2,
        data_feedback: tuple[str, ...] = (),
    ) -> StockStrategyPairing | None: ...


__all__ = [
    "QueryDataReview",
    "QueryDataReviewAdvisor",
    "StockRecommendation",
    "StockRecommendationAdvisor",
    "StockStrategyDataRequest",
    "StockStrategyPair",
    "StockStrategyPairing",
    "StockStrategyPairingAdvisor",
    "StrategyAdviceCandidate",
    "VerifiedFactStrategyAdvice",
    "VerifiedFactStrategyAdviceRequest",
    "VerifiedFactStrategyAdvisor",
]
