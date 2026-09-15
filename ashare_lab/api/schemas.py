"""Stable HTTP schemas for the strategy-compilation API."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ashare_lab.application.compile_strategy import CompileStatus
from ashare_lab.domain.strategy import StrategySpec, canonical_hash
from ashare_lab.domain.strategy.price_plans import GridPlan
from ashare_lab.ports.execution_settings import ExecutionSettingsPatch
from ashare_lab.ports.idea_routing import UnboundIdeaStrategy

from .execution_assessment import ExecutionAssessment


class ApiModel(BaseModel):
    """Strict base model so accidental fields never become public API."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


class ErrorDetail(ApiModel):
    location: str | None = None
    message: str
    type: str | None = None


class ErrorBody(ApiModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,63}$")
    message: str
    details: tuple[ErrorDetail, ...] = ()


class ErrorEnvelope(ApiModel):
    error: ErrorBody
    request_id: str = Field(min_length=1, max_length=64)


class BacktestReviewReference(ApiModel):
    run_id: str = Field(min_length=1, max_length=128)
    response_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class BacktestReviewContextRequest(ApiModel):
    related_run_ids: tuple[Annotated[str, Field(min_length=1, max_length=128)], ...] = Field(
        default=(), max_length=20,
    )
    related_reviews: tuple[BacktestReviewReference, ...] = Field(default=(), max_length=20)


class StrategyDraftRequest(ApiModel):
    utterance: str = Field(max_length=2_000)
    instrument_context: str | None = Field(default=None, max_length=32)
    as_of_date: date
    edit_current_strategy: bool = False
    execution_settings: ExecutionSettingsPatch = Field(default_factory=ExecutionSettingsPatch)
    related_review: BacktestReviewReference | None = None
    related_reviews: tuple[BacktestReviewReference, ...] = Field(default=(), max_length=20)
    related_run_ids: tuple[Annotated[str, Field(min_length=1, max_length=128)], ...] = Field(
        default=(), max_length=20,
    )


class StrategyDraftRevisionRequest(ApiModel):
    strategy: StrategySpec
    execution_settings: ExecutionSettingsPatch = Field(default_factory=ExecutionSettingsPatch)
    utterance: str | None = Field(default=None, max_length=2_000)
    recover_if_missing: bool = False


class ClarificationAnswerRequest(ApiModel):
    answer: str = Field(min_length=1, max_length=2_000)
    execution_settings: ExecutionSettingsPatch = Field(default_factory=ExecutionSettingsPatch)
    related_review: BacktestReviewReference | None = None
    related_reviews: tuple[BacktestReviewReference, ...] = Field(default=(), max_length=20)
    related_run_ids: tuple[Annotated[str, Field(min_length=1, max_length=128)], ...] = Field(
        default=(), max_length=20,
    )


class LiveMarketScreenRequest(ApiModel):
    """A current-data discovery query, never a historical backtest input."""

    query: str = Field(min_length=1, max_length=2_000)
    asset_type: Literal["A股", "ETF", "基金"]


class LiveMarketProvenancePayload(ApiModel):
    response_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    retrieved_at: datetime
    schema_version: str = Field(min_length=1, max_length=128)


class LiveMarketScreenResponse(ApiModel):
    provider: str = Field(min_length=1, max_length=128)
    query: str = Field(min_length=1, max_length=2_000)
    asset_type: str = Field(min_length=1, max_length=32)
    columns: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]
    provenance: LiveMarketProvenancePayload
    provider_metadata: dict[str, Any] = Field(default_factory=dict)


class LiveFinanceQueryRequest(ApiModel):
    """A current-data lookup, never a historical backtest input."""

    query: str = Field(min_length=1, max_length=2_000)
    indicators: str | None = Field(default=None, max_length=2_000)


class LiveFinanceQueryResponse(ApiModel):
    provider: str = Field(min_length=1, max_length=128)
    query: str = Field(min_length=1, max_length=2_000)
    indicators: str | None = Field(default=None, max_length=2_000)
    tables: tuple[dict[str, Any], ...]
    provenance: LiveMarketProvenancePayload


class SkillSeriesDiscoveryRequest(ApiModel):
    """Ask the Skill for a metric, without a catalog-name admission gate."""

    instrument_id: str = Field(pattern=r"^[0-9]{6}\.(SH|SZ|BJ)$")
    metric_query: str = Field(min_length=1, max_length=200)
    start: date
    end: date

    @model_validator(mode="after")
    def ordered_dates(self) -> SkillSeriesDiscoveryRequest:
        if self.start > self.end:
            raise ValueError("start must not exceed end")
        return self


class LiveScreenedFinanceQueryRequest(ApiModel):
    """Screen securities, then query current data for the selected entities."""

    screening_query: str = Field(min_length=1, max_length=2_000)
    asset_type: Literal["A股", "ETF", "基金"]
    indicators: str = Field(min_length=1, max_length=2_000)


class LiveSecurityEntityPayload(ApiModel):
    code: str = Field(pattern=r"^\d{6}(?:\.(?:SH|SZ|BJ))?$")
    name: str | None = Field(default=None, max_length=128)
    asset_type: str = Field(min_length=1, max_length=32)


class LiveScreenedFinanceQueryResponse(ApiModel):
    """Current-only composition; never eligible as a historical PIT snapshot."""

    usage_scope: Literal["current_query_only"] = "current_query_only"
    historical_backtest_eligible: Literal[False] = False
    screen: LiveMarketScreenResponse
    entities: tuple[LiveSecurityEntityPayload, ...]
    batches: tuple[LiveFinanceQueryResponse, ...]


class ClarificationDataPayload(ApiModel):
    """Typed provider result returned without changing the pending strategy."""

    usage_scope: Literal["current_query_only"] = "current_query_only"
    historical_backtest_eligible: Literal[False] = False
    kind: Literal["screen", "finance", "screened_finance"]
    screen: LiveMarketScreenResponse | None = None
    finance: LiveFinanceQueryResponse | None = None
    screened_finance: LiveScreenedFinanceQueryResponse | None = None

    @model_validator(mode="after")
    def payload_matches_kind(self) -> ClarificationDataPayload:
        if self.kind == "screen" and (
            self.screen is None or self.finance is not None or self.screened_finance is not None
        ):
            raise ValueError("screen data must contain only the screen result")
        if self.kind == "finance" and (
            self.finance is None or self.screen is not None or self.screened_finance is not None
        ):
            raise ValueError("finance data must contain only the finance result")
        if self.kind == "screened_finance" and (
            self.screened_finance is None or self.screen is not None or self.finance is not None
        ):
            raise ValueError("screened finance data must contain only the composed result")
        return self


class ProvenanceItem(ApiModel):
    path: str = Field(min_length=1, max_length=256)
    source: str = Field(min_length=1, max_length=128)


class CandidateProvenanceItem(ApiModel):
    """Public, non-secret identity of the bounded interpretation contract."""

    source: Literal["bounded_provider"]
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    prompt_version: str = Field(min_length=1, max_length=64)
    schema_version: str = Field(min_length=1, max_length=64)
    capability_projection_version: str = Field(min_length=1, max_length=64)
    capability_projection_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    upstream_pattern_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_rank: int = Field(ge=1, le=3)


class CandidateRejectionItem(ApiModel):
    candidate_rank: int = Field(ge=1, le=3)
    diagnostic_code: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.:/-]+$",
    )


class CandidateGroundingItem(ApiModel):
    path: str = Field(min_length=1, max_length=128)
    start: int = Field(ge=0, le=2_000)
    end: int = Field(gt=0, le=2_000)
    text: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def span_is_ordered(self) -> CandidateGroundingItem:
        if self.end <= self.start:
            raise ValueError("grounding span end must be greater than start")
        return self


class CandidateGroundingPayload(ApiModel):
    """Opaque-friendly object envelope for exact lexical grounding spans."""

    matched_spans: tuple[str, ...]
    spans: tuple[CandidateGroundingItem, ...]


class CandidateAlternativeItem(ApiModel):
    candidate_rank: int = Field(ge=1, le=3)
    strategy_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class IdeaRouteProvenancePayload(ApiModel):
    source: Literal["bounded_provider"]
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    prompt_version: str = Field(min_length=1, max_length=64)
    schema_version: str = Field(min_length=1, max_length=64)
    capability_projection_version: str = Field(min_length=1, max_length=64)
    capability_projection_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    upstream_pattern_commit: str = Field(pattern=r"^[0-9a-f]{40}$")


class IdeaAssetMappingPayload(ApiModel):
    instrument_symbol: str | None = Field(default=None, pattern=r"^[0-9]{6}\.(SH|SZ|BJ)$")
    relation: Literal["current_page_proxy", "unbound"]
    rationale: str = Field(min_length=1, max_length=512)
    evidence_status: Literal["host_context_only", "instrument_required"]


class IdeaProposalPayload(ApiModel):
    strategy: StrategySpec | None = None
    strategy_template: UnboundIdeaStrategy | None = None
    grid_plan: GridPlan | None = None
    id: str = Field(pattern=r"^idea_[0-9a-f]{12}$")
    title: str = Field(min_length=1, max_length=96)
    hypothesis: str = Field(min_length=1, max_length=512)
    entry_summary: str = Field(min_length=1, max_length=160)
    exit_summary: str = Field(min_length=1, max_length=160)
    suggested_utterance: str = Field(min_length=1, max_length=512)
    # Unbound ideas have no executable DSL yet; capabilities are derived only
    # after the user chooses a stock and the strategy passes compilation.
    capability_ids: tuple[str, ...] = Field(max_length=8)
    assumptions: tuple[str, ...] = Field(max_length=8)
    confidence: float = Field(ge=0.0, le=1.0)
    instrument_symbol: str | None = Field(
        default=None,
        pattern=r"^[0-9]{6}\.(SH|SZ|BJ)$",
    )
    instrument_name: str | None = Field(default=None, max_length=64)
    # Server composes the 240-character inspiration framing with the verified
    # stock rationale; the output contract must admit both without truncation.
    pairing_reason: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def require_capabilities_for_bound_proposal(self) -> IdeaProposalPayload:
        if self.instrument_symbol is not None and not self.capability_ids:
            raise ValueError("bound proposals require validated capabilities")
        return self


class IdeaResearchFactPayload(ApiModel):
    statement: str = Field(min_length=1, max_length=10_000)
    fact_kind: Literal["reported_fact", "inference", "uncertain"]
    source_ids: tuple[str, ...] = Field(max_length=8)
    time_scope: str | None = Field(default=None, max_length=120)


class IdeaResearchSourcePayload(ApiModel):
    source_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=300)
    url: str = Field(min_length=8, max_length=2_048)
    publisher: str = Field(min_length=1, max_length=160)
    published_at: str | None = Field(default=None, max_length=80)


class IdeaResearchPayload(ApiModel):
    """Display-only web evidence; never strategy or historical price input."""

    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    provider_response_id: str = Field(min_length=1, max_length=256)
    query: str = Field(min_length=1, max_length=4_000)
    purpose: Literal["viewpoint", "unknown_entity", "current_fact"]
    as_of: datetime
    summary: str = Field(min_length=1, max_length=500)
    facts: tuple[IdeaResearchFactPayload, ...] = Field(max_length=12)
    sources: tuple[IdeaResearchSourcePayload, ...] = Field(max_length=20)
    unresolved_questions: tuple[str, ...] = Field(max_length=8)
    retrieved_at: datetime
    response_sha256: str = Field(pattern=r"^(?:sha256:)?[0-9a-f]{64}$")
    search_call_count: int = Field(ge=1)
    schema_version: Literal["current-fact-research.v1"]


class IdeaRoutePayload(ApiModel):
    schema_version: Literal["idea-route.v1"]
    # Preflight may summarize up to three verified stock rationales plus status.
    # The model's own generation limits remain separate and unchanged.
    understanding: str = Field(min_length=1, max_length=2_048)
    hypothesis: str = Field(min_length=1, max_length=320)
    asset_mapping: IdeaAssetMappingPayload
    proposals: tuple[IdeaProposalPayload, ...] = Field(max_length=3)
    provenance: IdeaRouteProvenancePayload | None = None
    research: IdeaResearchPayload | None = None


class InstrumentSuggestionPayload(ApiModel):
    symbol: str = Field(pattern=r"^[0-9]{6}\.(SH|SZ|BJ)$")
    name: str | None = None
    source: str
    retrieved_at: datetime
    evidence: str | None = None


class _BacktestReviewApiModel(ApiModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        populate_by_name=True,
        serialize_by_alias=True,
        allow_inf_nan=False,
    )


class BacktestReviewModelProvenance(_BacktestReviewApiModel):
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    prompt_version: str = Field(alias="promptVersion", min_length=1, max_length=128)
    schema_version: str = Field(alias="schemaVersion", min_length=1, max_length=128)
    response_hash: str = Field(alias="responseHash", pattern=r"^sha256:[0-9a-f]{64}$")


class BacktestOptimizationCandidateView(_BacktestReviewApiModel):
    id: str = Field(pattern=r"^model-opt-[1-3]$")
    title: str = Field(min_length=2, max_length=48)
    diagnosis: str = Field(min_length=2, max_length=240)
    change_dimension: Literal[
        "entry",
        "exit",
        "confirmation",
        "risk_control",
    ] = Field(alias="changeDimension")
    expected_effect: str = Field(alias="expectedEffect", min_length=2, max_length=180)
    tradeoff: str = Field(min_length=2, max_length=180)
    suggested_utterance: str = Field(alias="suggestedUtterance", min_length=12, max_length=420)
    strategy: StrategySpec
    strategy_hash: str = Field(alias="strategyHash", pattern=r"^sha256:[0-9a-f]{64}$")
    model_suggested: Literal[True] = Field(default=True, alias="modelSuggested")


class BacktestReviewResponse(_BacktestReviewApiModel):
    run_id: str = Field(alias="runId", min_length=1, max_length=128)
    source_result_hash: str = Field(
        alias="sourceResultHash",
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    generated_at: datetime = Field(alias="generatedAt")
    evidence_grade: Literal["insufficient", "limited", "moderate"] = Field(
        alias="evidenceGrade"
    )
    evidence_reasons: tuple[str, ...] = Field(alias="evidenceReasons", min_length=1)
    analysis: str = Field(min_length=8, max_length=600)
    conclusion: str = Field(min_length=4, max_length=280)
    optimization_candidates: tuple[BacktestOptimizationCandidateView, ...] = Field(
        alias="optimizationCandidates",
        min_length=0,
        max_length=3,
    )
    model_provenance: BacktestReviewModelProvenance = Field(alias="modelProvenance")
    disclaimer: Literal["历史回测与模型建议仅用于研究，不构成投资建议或真实交易指令"] = (
        "历史回测与模型建议仅用于研究，不构成投资建议或真实交易指令"
    )


class StrategyDraftResponse(ApiModel):
    execution_assessment: ExecutionAssessment | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    query_diagnostic_code: str | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    draft_id: UUID
    revision: int = Field(ge=1)
    status: CompileStatus
    run_requested: bool = Field(default=False, exclude_if=lambda value: not value)
    refresh_data: bool = Field(default=False, exclude_if=lambda value: not value)
    is_strategy_edit: bool = Field(default=False, exclude_if=lambda value: not value)
    execution_settings: ExecutionSettingsPatch = Field(default_factory=ExecutionSettingsPatch)
    strategy: StrategySpec | None = None
    strategy_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    clarification: str | None = None
    diagnostic_code: str | None = None
    instrument_suggestion: InstrumentSuggestionPayload | None = None
    instrument_suggestions: tuple[InstrumentSuggestionPayload, ...] = Field(
        default=(), max_length=3, exclude_if=lambda value: not value,
    )
    instrument_candidates: tuple[InstrumentSuggestionPayload, ...] = Field(
        default=(), max_length=3, exclude_if=lambda value: not value,
    )
    verified_instrument: InstrumentSuggestionPayload | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    backtest_review: BacktestReviewResponse | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    provenance: tuple[ProvenanceItem, ...] = ()
    candidate_provenance: CandidateProvenanceItem | None = None
    candidate_grounding: CandidateGroundingPayload | None = None
    candidate_rejections: tuple[CandidateRejectionItem, ...] = ()
    candidate_alternatives: tuple[CandidateAlternativeItem, ...] = ()
    idea_route: IdeaRoutePayload | None = None
    # A compile-only interpretation for a colloquial idea.  It is not the
    # executable ``strategy`` field and the client must send the suggested
    # utterance through the clarification endpoint before it can run.
    suggested_strategy: StrategySpec | None = None
    suggested_strategy_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    suggested_strategy_choice_id: str | None = Field(default=None, min_length=1, max_length=128)
    suggested_strategy_note: str | None = Field(default=None, min_length=1, max_length=240)
    assistant_message: str | None = Field(
        default=None,
        min_length=1,
        max_length=1_000,
        exclude_if=lambda value: value is None,
    )
    data: ClarificationDataPayload | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    created_at: datetime

    @model_validator(mode="after")
    def ready_response_is_complete(self) -> StrategyDraftResponse:
        if self.status is CompileStatus.READY:
            if self.strategy is None or self.strategy_hash is None:
                raise ValueError("ready draft must contain a strategy and strategy_hash")
            if self.idea_route is not None:
                raise ValueError("ready draft cannot contain idea guidance")
        elif self.strategy is not None or self.strategy_hash is not None:
            raise ValueError("non-ready draft cannot contain a strategy or strategy_hash")
        suggested_fields = (
            self.suggested_strategy,
            self.suggested_strategy_hash,
            self.suggested_strategy_choice_id,
            self.suggested_strategy_note,
        )
        if self.diagnostic_code in {
            "semantic_confirmation_required", "execution_prerequisite_required",
        }:
            if (self.status is not CompileStatus.NEEDS_CLARIFICATION
                    or self.suggested_strategy is None
                    or self.suggested_strategy_hash is None
                    or self.suggested_strategy_note is None
                    or self.suggested_strategy_choice_id is not None
                    or self.idea_route is not None
                    or self.run_requested or self.refresh_data):
                raise ValueError("preview clarification must be an isolated non-executable preview")
            if self.suggested_strategy_hash != canonical_hash(self.suggested_strategy):
                raise ValueError("preview clarification hash does not match")
        elif any(item is not None for item in suggested_fields):
            if self.status is not CompileStatus.NEEDS_CLARIFICATION:
                raise ValueError("suggested strategy must require clarification")
            if any(item is None for item in suggested_fields):
                raise ValueError("suggested strategy response is incomplete")
            if self.idea_route is None:
                raise ValueError("suggested strategy requires idea guidance")
            if self.suggested_strategy_choice_id not in {
                item.id for item in self.idea_route.proposals
            }:
                raise ValueError("suggested strategy choice must belong to idea guidance")
        if self.idea_route is not None and self.status is not CompileStatus.NEEDS_CLARIFICATION:
            raise ValueError("idea guidance must require clarification")
        route_codes = {
            "candidate_data_incomplete",
            "backtest_data_not_yet_available",
            "backtest_date_range_invalid",
            "capability_research_fallback",
            "idea_guidance_required",
            "idea_guidance_execution_invalid",
            "entry_rule_not_recognized",
            "exit_rule_not_recognized",
            "strategy_rule_incomplete",
            "no_supported_signal_recognized",
            "ambiguous_obv_direction",
            "ambiguous_volume_direction",
            "ambiguous_boolean_expression",
            "ambiguous_cross_indicator",
            "numeric_threshold_requires_clarification",
            "data_query_only",
        }
        if self.idea_route is not None and self.diagnostic_code not in route_codes:
            raise ValueError("idea route is not allowed for this diagnostic code")
        if self.diagnostic_code == "idea_guidance_required" and self.idea_route is None:
            raise ValueError("idea_guidance_required must include idea_route")
        return self


class ClarificationSuggestionPayload(ApiModel):
    id: str = Field(pattern=r"^idea_[0-9a-f]{12}$")
    title: str = Field(min_length=1, max_length=96)
    preview: str = Field(min_length=1, max_length=512)


class ClarificationAnswerResponse(ApiModel):
    reply_kind: Literal["accepted", "clarification"]
    assistant_message: str = Field(min_length=1, max_length=1_000)
    suggestions: tuple[ClarificationSuggestionPayload, ...] = Field(max_length=3)
    draft: StrategyDraftResponse
    query_diagnostic_code: str | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    data: ClarificationDataPayload | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


class HealthResponse(ApiModel):
    status: Literal["ok"] = "ok"


class ReadinessResponse(ApiModel):
    status: Literal["ready"] = "ready"
    checks: dict[str, Literal["ok"]]


class CatalogRelease(ApiModel):
    catalog_id: str
    release_version: str
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class VersionResponse(ApiModel):
    service: Literal["ashare-strategy-api"] = "ashare-strategy-api"
    service_version: str
    api_version: Literal["v1"] = "v1"
    catalog_snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    catalog_releases: tuple[CatalogRelease, ...]


type CapabilityScalar = str | int | float | bool


class IndicatorTriggerCapability(ApiModel):
    id: str
    display_name: str | None = None
    description: str | None = None
    value_requirement: Literal["required", "forbidden"]
    minimum: float | None = None
    maximum: float | None = None
    unit: str | None = None
    exclusive_minimum: bool = False
    exclusive_maximum: bool = False


class IndicatorParameterCapability(ApiModel):
    name: str
    value_type: Literal["integer", "number", "string", "boolean"]
    required: bool
    default: CapabilityScalar | None = None
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[CapabilityScalar, ...] = ()
    display_name: str | None = None
    unit: str | None = None


class IndicatorCapability(ApiModel):
    indicator_id: str
    definition_version: str
    status: Literal["stable", "experimental", "unavailable"]
    display_name: str
    description: str
    data_source: Literal[
        "provider_indicator", "skill_ohlcv_python", "skill_numeric_history", "unavailable",
    ] | None = None
    formula_summary: str | None = None
    warmup_bars: int = Field(ge=0)
    timeframes: tuple[Literal["1d"], ...]
    evaluation_modes: tuple[Literal["bar_close_confirmed"], ...]
    triggers: tuple[str, ...]
    trigger_definitions: tuple[IndicatorTriggerCapability, ...]
    parameters: tuple[IndicatorParameterCapability, ...]


class EventDocumentTextCapability(ApiModel):
    """Separate DSL publication from concrete full-document data readiness."""

    catalog_available: bool
    backtest_available: bool
    preparation_available: bool
    availability_scope: Literal["pinned_snapshot", "request_preparation", "unavailable"]
    unavailable_reason: (
        Literal[
            "not_catalog_available",
            "snapshot_coverage_unavailable",
            "preparation_required",
        ]
        | None
    ) = None


class EventCapability(ApiModel):
    event_code: str
    definition_version: str
    catalog_status: Literal["stable"] = "stable"
    status: Literal["available", "unavailable"]
    backtest_available: bool
    preparation_available: bool = False
    availability_scope: Literal["pinned_snapshot", "request_preparation", "unavailable"] = (
        "unavailable"
    )
    unavailable_reason: Literal["snapshot_coverage_unavailable", "preparation_required"] | None = (
        None
    )
    document_text: EventDocumentTextCapability
    triggers: tuple[Literal["published"], ...] = ("published",)


class RequestLimits(ApiModel):
    max_body_bytes: int = Field(gt=0)
    max_utterance_characters: Literal[2000] = 2_000
    max_instrument_context_characters: Literal[32] = 32


class SkillDataDiscoveryCapability(ApiModel):
    """An open query entry is distinct from verified executable history."""

    available: bool
    metric_scope: Literal["open_ended"] = "open_ended"
    requires_catalog_indicator: Literal[False] = False
    endpoint: Literal["/api/v1/market/series-discovery"] = "/api/v1/market/series-discovery"
    automatic_backtest_binding: bool = False
    description: str = (
        "可查询目录外指标；返回实际字段、单位、日期及缺失原因。"
        "数据发现不等于已通过历史回测的字段口径与可得时间检查。"
    )


class CapabilitiesResponse(ApiModel):
    available_data_end: date | None = None
    markets: tuple[Literal["CN_A"], ...] = ("CN_A",)
    input_modes: tuple[Literal["natural_language_zh"], ...] = ("natural_language_zh",)
    strategy_scopes: tuple[Literal["single_instrument", "long_only"], ...] = (
        "single_instrument",
        "long_only",
    )
    indicators: tuple[IndicatorCapability, ...]
    events: tuple[EventCapability, ...]
    execution_policies: tuple[Literal["next_tradable_session_open"], ...] = (
        "next_tradable_session_open",
    )
    event_catalog_status: Literal["published"] = "published"
    event_backtest_available: bool
    event_preparation_available: bool = False
    event_availability_scope: Literal["pinned_snapshot", "request_preparation", "unavailable"] = (
        "unavailable"
    )
    backtest_execution_available: bool
    skill_data_discovery: SkillDataDiscoveryCapability | None = None
    limits: RequestLimits


def error_response_docs(*status_codes: int) -> dict[int | str, dict[str, Any]]:
    """Return OpenAPI response declarations using the uniform error envelope."""

    descriptions = {
        404: "Resource not found",
        409: "Idempotency conflict",
        413: "Request body too large",
        422: "Request validation failed",
        500: "Internal server error",
        503: "Service dependency is unavailable",
    }
    return {
        status: {
            "model": ErrorEnvelope,
            "description": descriptions.get(status, "Request failed"),
        }
        for status in status_codes
    }
