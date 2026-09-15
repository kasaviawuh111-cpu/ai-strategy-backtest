"""Boundary between untrusted language interpretation and strategy compilation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal, Protocol

from ashare_lab.domain.financials import (
    FinancialMetricId,
    FinancialPeriodBasis,
    FinancialReportType,
    FinancialStatementScope,
    FinancialUnit,
)
from ashare_lab.domain.strategy.models import JsonScalar
from ashare_lab.domain.strategy.price_plans import PricePlan
from ashare_lab.ports.execution_settings import ExecutionSettingsPatch
from ashare_lab.ports.instrument_resolution import InstrumentNameCandidate


@dataclass(frozen=True, slots=True)
class CompileInput:
    utterance: str
    as_of_date: date
    instrument_context: str | None = None
    # Model-authored, display-only inspiration; never an execution condition.
    idea_inspiration: str | None = None
    idea_context: tuple[str, ...] = ()
    # Internal model route, not supplied by the public request or execution authority.
    semantic_intent: str | None = None
    # Server-resolved identity for this exact source span, never public/model input.
    resolved_instrument: ResolvedCompileInstrument | None = None


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


@dataclass(frozen=True, slots=True)
class FinancialIntent:
    metric_id: FinancialMetricId
    comparator: Literal["gt", "gte", "lt", "lte", "eq", "ne"]
    value: Decimal
    unit: FinancialUnit
    report_type: FinancialReportType | None = None
    period_basis: FinancialPeriodBasis | None = None
    statement_scope: FinancialStatementScope | None = None
    definition_version: Literal["1.0.0"] = "1.0.0"


type SignalIntent = IndicatorIntent | EventIntent | FinancialIntent
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
class ResolvedCompileInstrument:
    """Bind a verified code to its original name/code mention across recovery."""

    symbol: str
    evidence: CandidateGroundingEvidence

    def matches(self, request: CompileInput) -> bool:
        span = self.evidence
        return (
            request.instrument_context == self.symbol
            and span.path in {"/instrument/name", "/instrument/symbol"}
            and 0 <= span.start < span.end <= len(request.utterance)
            and request.utterance[span.start:span.end] == span.text
        )


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
    # Programmatic/legacy producers stay daily unless they opt into the new
    # phase-one minute contract. The live language boundary defaults new user
    # utterances to minute_bar and persists that choice explicitly.
    observation: Literal["minute_bar", "daily_close"] = "daily_close"


@dataclass(frozen=True, slots=True)
class TrailingDrawdownIntent:
    threshold_pct: float
    observation: Literal["minute_bar", "daily_close"] = "daily_close"


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
    initial_cash_cny: int | None = None
    provenance: CandidateProvenance | None = None
    grounding_evidence: tuple[CandidateGroundingEvidence, ...] = ()
    # Unresolved model-extracted name. Never grants an executable symbol until
    # the server-owned security resolver confirms it.
    instrument_name: str | None = None
    instrument_candidates: tuple[InstrumentNameCandidate, ...] = ()
    execution_settings: ExecutionSettingsPatch = field(default_factory=ExecutionSettingsPatch)
    instrument_suggestion_declined: bool = False
    # Display-only review disagreements; never source evidence or execution approval.
    semantic_review_issues: tuple[str, ...] = ()
    trading_plan: PricePlan | None = None


class CandidateGenerator(Protocol):
    async def generate(self, request: CompileInput) -> tuple[CandidateAst, ...]: ...


type BoundedCandidateBoundary = Literal["schema_bounded_candidate.v1"]


class BoundedCandidateGenerator(Protocol):
    """Server-owned candidate port that preserves the bounded schema boundary."""

    @property
    def boundary(self) -> BoundedCandidateBoundary: ...

    async def generate(self, request: CompileInput) -> tuple[CandidateAst, ...]: ...
