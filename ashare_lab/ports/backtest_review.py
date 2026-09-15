"""Bounded model review of one completed, integrity-checked backtest."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Protocol

from ashare_lab.domain.strategy import StrategySpec

EvidenceGrade = Literal["insufficient", "limited", "moderate"]
ChangeDimension = Literal[
    "entry",
    "exit",
    "confirmation",
    "risk_control",
]


class BacktestReviewContentError(ValueError):
    """The model responded, but its review is not safe to display."""


@dataclass(frozen=True, slots=True)
class BacktestReviewRequest:
    run_id: str
    instrument_symbol: str
    as_of_date: date
    strategy_payload: Mapping[str, object]
    result_facts: Mapping[str, object]
    evidence_grade: EvidenceGrade
    evidence_reasons: tuple[str, ...]
    max_proposals: int = 3
    user_request: str | None = None
    completed_runs: tuple[Mapping[str, object], ...] = ()
    exposed_proposals: tuple[Mapping[str, object], ...] = ()
    report_references: Mapping[str, object] = field(default_factory=lambda: dict[str, object]())


@dataclass(frozen=True, slots=True)
class BacktestOptimizationCandidate:
    title: str
    diagnosis: str
    change_dimension: ChangeDimension
    expected_effect: str
    tradeoff: str
    suggested_utterance: str
    strategy: StrategySpec


@dataclass(frozen=True, slots=True)
class BacktestModelReview:
    analysis: str
    conclusion: str
    proposals: tuple[BacktestOptimizationCandidate, ...]
    provider: str
    model: str
    prompt_version: str
    schema_version: str
    response_hash: str


class BacktestReviewAdvisor(Protocol):
    """Return None only for unavailability; invalid prose raises a content error."""

    async def review(self, request: BacktestReviewRequest) -> BacktestModelReview | None: ...


__all__ = [
    "BacktestModelReview",
    "BacktestOptimizationCandidate",
    "BacktestReviewAdvisor",
    "BacktestReviewContentError",
    "BacktestReviewRequest",
    "ChangeDimension",
    "EvidenceGrade",
]
