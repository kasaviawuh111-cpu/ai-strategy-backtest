"""Stable HTTP schemas for the strategy-compilation API."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ashare_lab.application.compile_strategy import CompileStatus
from ashare_lab.domain.strategy import StrategySpec


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


class StrategyDraftRequest(ApiModel):
    utterance: str = Field(max_length=2_000)
    instrument_context: str | None = Field(default=None, max_length=32)
    as_of_date: date


class StrategyDraftRevisionRequest(ApiModel):
    strategy: StrategySpec
    utterance: str | None = Field(default=None, max_length=2_000)


class ClarificationAnswerRequest(ApiModel):
    answer: str = Field(min_length=1, max_length=2_000)


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
    id: str = Field(pattern=r"^idea_[0-9a-f]{12}$")
    title: str = Field(min_length=1, max_length=96)
    hypothesis: str = Field(min_length=1, max_length=512)
    entry_summary: str = Field(min_length=1, max_length=160)
    exit_summary: str = Field(min_length=1, max_length=160)
    suggested_utterance: str = Field(min_length=1, max_length=512)
    capability_ids: tuple[str, ...] = Field(min_length=1, max_length=8)
    assumptions: tuple[str, ...] = Field(max_length=8)
    confidence: float = Field(ge=0.0, le=1.0)


class IdeaRoutePayload(ApiModel):
    schema_version: Literal["idea-route.v1"]
    understanding: str = Field(min_length=1, max_length=240)
    hypothesis: str = Field(min_length=1, max_length=320)
    asset_mapping: IdeaAssetMappingPayload
    proposals: tuple[IdeaProposalPayload, ...] = Field(min_length=2, max_length=3)
    provenance: IdeaRouteProvenancePayload | None = None


class StrategyDraftResponse(ApiModel):
    draft_id: UUID
    revision: int = Field(ge=1)
    status: CompileStatus
    strategy: StrategySpec | None = None
    strategy_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    clarification: str | None = None
    diagnostic_code: str | None = None
    provenance: tuple[ProvenanceItem, ...] = ()
    candidate_provenance: CandidateProvenanceItem | None = None
    candidate_grounding: CandidateGroundingPayload | None = None
    candidate_rejections: tuple[CandidateRejectionItem, ...] = ()
    candidate_alternatives: tuple[CandidateAlternativeItem, ...] = ()
    idea_route: IdeaRoutePayload | None = None
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
        if self.idea_route is not None and self.status is not CompileStatus.NEEDS_CLARIFICATION:
            raise ValueError("idea guidance must require clarification")
        route_codes = {
            "idea_guidance_required",
            "entry_rule_not_recognized",
            "exit_rule_not_recognized",
            "strategy_rule_incomplete",
            "no_supported_signal_recognized",
            "ambiguous_obv_direction",
            "ambiguous_volume_direction",
            "ambiguous_boolean_expression",
            "ambiguous_cross_indicator",
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


class CapabilitiesResponse(ApiModel):
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
