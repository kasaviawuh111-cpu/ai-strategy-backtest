"""Untrusted boundary for turning a broad view into strategy guidance.

A provider-authored strategy remains non-executable until the application has
validated its fixed boundary and active Catalog.  The public API exposes only
the guidance card; an optional internal StrategySpec lets a validated choice
avoid a second model interpretation of model-authored text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, model_validator

from ashare_lab.domain.strategy import (
    BacktestConfig,
    CatalogRef,
    DailyExecutionPolicy,
    ComposedExecutionPolicy,
    FirstOfExit,
    HybridExecutionPolicy,
    Instrument,
    PricePlanExecutionPolicy,
    StrategySpec,
)
from ashare_lab.domain.strategy.models import Condition
from ashare_lab.domain.strategy.price_plans import PricePlan
from ashare_lab.domain.strategy.independent_plans import IndependentPlanPair
from ashare_lab.ports.candidate_generation import CompileInput
from ashare_lab.ports.current_fact_research import CurrentFactResearchResult
from ashare_lab.ports.execution_settings import ExecutionSettingsPatch
from ashare_lab.ports.live_market_data import LiveMarketDataResult


class IdeaResearchUnavailableError(RuntimeError):
    """A current-affairs idea could not obtain source-backed web research."""


class IdeaStockSelectionUnavailableError(RuntimeError):
    """No verified security was found before strategy generation."""

    def __init__(
        self, message: str, *, stage: Literal["planning", "selection"] = "selection",
        reason: str = "unknown", attempts: int = 1,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.reason = reason
        self.attempts = attempts


@dataclass(frozen=True, slots=True)
class IdeaStockSelection:
    symbol: str
    name: str
    reason: str
    framing: str
    evidence: LiveMarketDataResult | None = None
    alternatives: tuple[IdeaStockSelection, ...] = ()


class IdeaStockSelector(Protocol):
    async def select(
        self, request: CompileInput, research: CurrentFactResearchResult | None,
    ) -> IdeaStockSelection | None: ...


class IdeaGenerationError(RuntimeError):
    """Sanitized stage of an unsuccessful model-authored idea batch."""

    def __init__(
        self, stage: Literal["transport", "schema", "execution", "explanation"],
        *, timed_out: bool = False,
    ) -> None:
        super().__init__(stage)
        self.stage = stage
        self.timed_out = timed_out


class UnboundIdeaStrategy(BaseModel):
    """Model-authored rules awaiting a user-selected security, not executable."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    catalog: CatalogRef
    entry: Condition | None = None
    exit: FirstOfExit | None = None
    trading_plan: PricePlan | None = None
    independent_plans: IndependentPlanPair | None = None
    execution: DailyExecutionPolicy | PricePlanExecutionPolicy | HybridExecutionPolicy | ComposedExecutionPolicy
    backtest: BacktestConfig

    @model_validator(mode="after")
    def complete_rules(self) -> UnboundIdeaStrategy:
        if self.independent_plans is not None:
            if self.trading_plan is not None or self.entry is not None or self.exit is not None:
                raise ValueError('双计划与其他买卖规则不能重复声明所有权')
            if not isinstance(self.execution, ComposedExecutionPolicy):
                raise ValueError('双计划须显式声明组合执行')
            if self.backtest.initial_cash_cny != self.independent_plans.entry_plan.parameters.initial_cash_cny:
                raise ValueError('双计划与回测初始资金必须一致')
            return self
        if self.trading_plan is not None:
            if ((self.entry is not None or self.exit is not None)
                    and not isinstance(self.execution, ComposedExecutionPolicy)):
                raise ValueError("独立买卖组合须保留组合执行声明")
        elif self.entry is None or self.exit is None:
            raise ValueError("待选股票的策略仍须保留完整买卖规则")
        return self

    def bind(self, symbol: str) -> StrategySpec:
        return StrategySpec(
            catalog=self.catalog, instrument=Instrument(symbol=symbol),
            entry=self.entry, exit=self.exit, trading_plan=self.trading_plan,
            independent_plans=self.independent_plans,
            execution=self.execution, backtest=self.backtest,
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
    text cannot populate either field authoritatively. ``strategy`` is a
    Catalog-gated proposal exposed for inspection; selecting it and creating
    a runnable draft still require the normal server-side flow.
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
