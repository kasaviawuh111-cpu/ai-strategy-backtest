"""Boundary between untrusted language interpretation and strategy compilation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal, Protocol

from ashare_lab.domain.strategy.models import JsonScalar


@dataclass(frozen=True, slots=True)
class CompileInput:
    utterance: str
    as_of_date: date
    instrument_context: str | None = None


@dataclass(frozen=True, slots=True)
class IndicatorIntent:
    indicator_id: str
    definition_version: str
    trigger: str
    params: tuple[tuple[str, JsonScalar], ...]
    value: float | None = None

    def params_dict(self) -> dict[str, JsonScalar]:
        return dict(self.params)


@dataclass(frozen=True, slots=True)
class EventIntent:
    event_code: str
    definition_version: str
    attributes: tuple[tuple[str, JsonScalar], ...] = ()
    trigger: Literal["published"] = "published"
    document_text: DocumentTextIntent | None = None

    def attributes_dict(self) -> dict[str, JsonScalar]:
        return dict(self.attributes)


type SignalIntent = IndicatorIntent | EventIntent
type ExitIntent = SignalIntent | HoldingPeriodIntent | PositionReturnIntent | TrailingDrawdownIntent
type ConditionJoin = Literal["all", "any"]


@dataclass(frozen=True, slots=True)
class CandidateProvenance:
    """Non-secret identity for an untrusted bounded candidate source.

    The provider response is never treated as executable evidence by itself;
    the compiler and Catalog still validate every emitted leaf.  This record
    exists so a draft can say exactly which bounded contract produced the
    candidate without persisting credentials, endpoint headers, or prompts.
    """

    source: Literal["bounded_provider"]
    provider: str
    model: str
    prompt_version: str
    schema_version: str
    capability_projection_version: str
    capability_projection_hash: str
    upstream_pattern_commit: str
    candidate_rank: int


@dataclass(frozen=True, slots=True)
class CandidateGroundingEvidence:
    """Exact user-text span supporting one bounded candidate field."""

    path: str
    start: int
    end: int
    text: str


@dataclass(frozen=True, slots=True)
class DocumentTextIntent:
    term: str
    match_mode: Literal["ascii_token", "literal"]
    comparator: Literal["gt", "gte"]
    value: int
    case_sensitive: bool = False


@dataclass(frozen=True, slots=True)
class HoldingPeriodIntent:
    sessions: int


@dataclass(frozen=True, slots=True)
class PositionReturnIntent:
    trigger: Literal["take_profit", "stop_loss"]
    threshold_pct: float


@dataclass(frozen=True, slots=True)
class TrailingDrawdownIntent:
    threshold_pct: float


@dataclass(frozen=True, slots=True)
class CandidateAst:
    instrument_symbol: str | None
    entry: tuple[SignalIntent, ...]
    exit: tuple[ExitIntent, ...]
    confidence: float
    entry_join: ConditionJoin = "all"
    exit_join: ConditionJoin = "any"
    defaulted_fields: tuple[str, ...] = ()
    unsupported_code: str | None = None
    backtest_start: date | None = None
    backtest_end: date | None = None
    backtest_lookback_years: int | None = None
    provenance: CandidateProvenance | None = None
    grounding_evidence: tuple[CandidateGroundingEvidence, ...] = ()


class CandidateGenerator(Protocol):
    async def generate(self, request: CompileInput) -> tuple[CandidateAst, ...]: ...
