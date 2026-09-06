"""Untrusted boundary for turning a broad view into strategy guidance.

A provider-authored strategy remains non-executable until the application has
validated its fixed boundary and active Catalog.  The public API exposes only
the guidance card; an optional internal StrategySpec lets a validated choice
avoid a second model interpretation of model-authored text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from ashare_lab.domain.strategy import (
    BacktestConfig,
    CatalogRef,
    DailyExecutionPolicy,
    FirstOfExit,
    Instrument,
    StrategySpec,
)
from ashare_lab.domain.strategy.models import Condition
from ashare_lab.ports.candidate_generation import CompileInput
from ashare_lab.ports.current_fact_research import CurrentFactResearchResult
from ashare_lab.ports.execution_settings import ExecutionSettingsPatch


class IdeaResearchUnavailableError(RuntimeError):
    """A current-affairs idea could not obtain source-backed web research."""


class IdeaGenerationError(RuntimeError):
    """Sanitized stage of an unsuccessful model-authored idea batch."""

    def __init__(
        self, stage: Literal["transport", "schema", "execution"], *, timed_out: bool = False,
    ) -> None:
        super().__init__(stage)
        self.stage = stage
        self.timed_out = timed_out


class UnboundIdeaStrategy(BaseModel):
    """Model-authored rules awaiting a user-selected security, not executable."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    catalog: CatalogRef
    entry: Condition
    exit: FirstOfExit
    execution: DailyExecutionPolicy
    backtest: BacktestConfig

    def bind(self, symbol: str) -> StrategySpec:
        return StrategySpec(
            catalog=self.catalog, instrument=Instrument(symbol=symbol),
            entry=self.entry, exit=self.exit, execution=self.execution, backtest=self.backtest,
        )


@dataclass(frozen=True, slots=True)
class IdeaRouteProvenance:
    """Public identity of the bounded provider contract used for guidance."""

    source: Literal["bounded_provider"]
    provider: str
    model: str
    prompt_version: str
    schema_version: str
    capability_projection_version: str
    capability_projection_hash: str
    upstream_pattern_commit: str


@dataclass(frozen=True, slots=True)
class IdeaAssetMapping:
    """The only asset mapping the first slice is allowed to make."""

    instrument_symbol: str | None
    relation: Literal["current_page_proxy", "unbound"] = "current_page_proxy"
    rationale: str = ""
    evidence_status: Literal["host_context_only", "instrument_required"] = "host_context_only"


@dataclass(frozen=True, slots=True)
class IdeaProposal:
    """A compiler-validated guidance card the user may choose.

    ``capability_ids`` and ``instrument_symbol`` are server-derived.  Provider
    text cannot populate either field authoritatively.  ``strategy`` is an
    internal, Catalog-gated payload and is intentionally omitted by the HTTP
    mapper.
    """

    id: str
    title: str
    hypothesis: str
    entry_summary: str
    exit_summary: str
    suggested_utterance: str
    capability_ids: tuple[str, ...]
    assumptions: tuple[str, ...]
    confidence: float
    # A missing symbol means the user must still choose a concrete A-share.
    instrument_symbol: str | None = None
    strategy: StrategySpec | None = None
    strategy_hash: str | None = None
    strategy_template: UnboundIdeaStrategy | None = None
    instrument_name: str | None = None
    pairing_reason: str | None = None


@dataclass(frozen=True, slots=True)
class IdeaRoute:
    """One compiler-gated guidance response."""

    understanding: str
    hypothesis: str
    asset_mapping: IdeaAssetMapping
    proposals: tuple[IdeaProposal, ...]
    provenance: IdeaRouteProvenance | None = None
    # Current public-fact evidence is display-only.  Keeping the typed result
    # beside the route makes its sources auditable without ever turning it
    # into a StrategySpec or signal timeline.
    research: CurrentFactResearchResult | None = None
    schema_version: Literal["idea-route.v1"] = "idea-route.v1"
    execution_settings: ExecutionSettingsPatch = field(default_factory=ExecutionSettingsPatch)
    instrument_suggestion_declined: bool = False


class IdeaRouter(Protocol):
    """Interpret a non-strategy utterance without making it executable."""

    async def route(self, request: CompileInput) -> IdeaRoute | None: ...
