"""Natural-language strategy-draft endpoints; no backtest executes here."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status

from ashare_lab.application.compile_strategy import CompileOutcome, CompileStatus, FieldProvenance
from ashare_lab.domain.market_data import AshareInstrumentCodeError, normalize_a_share_instrument
from ashare_lab.domain.strategy import (
    StrategyCatalogError,
    canonical_hash,
    validate_strategy_against_catalog,
)
from ashare_lab.ports.candidate_generation import CompileInput
from ashare_lab.ports.idea_routing import IdeaRoute

from ..container import ApiContainer, get_container
from ..errors import ApiProblem
from ..headers import IdempotencyKey, set_idempotency_replayed, validate_idempotency_key
from ..schemas import (
    CandidateAlternativeItem,
    CandidateGroundingItem,
    CandidateGroundingPayload,
    CandidateProvenanceItem,
    CandidateRejectionItem,
    ClarificationAnswerRequest,
    ClarificationAnswerResponse,
    ClarificationSuggestionPayload,
    IdeaAssetMappingPayload,
    IdeaProposalPayload,
    IdeaRoutePayload,
    IdeaRouteProvenancePayload,
    ProvenanceItem,
    StrategyDraftRequest,
    StrategyDraftResponse,
    StrategyDraftRevisionRequest,
    error_response_docs,
)
from ..store import (
    DraftNotFoundError,
    DraftRevisionStaleError,
    IdempotencyConflictError,
    StoredDraftRevision,
)

router = APIRouter(prefix="/api/v1/strategy-drafts", tags=["strategy-drafts"])
Container = Annotated[ApiContainer, Depends(get_container)]


@router.post(
    "",
    response_model=StrategyDraftResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="createStrategyDraft",
    responses=error_response_docs(409, 413, 422, 500),
)
async def create_strategy_draft(
    body: StrategyDraftRequest,
    response: Response,
    container: Container,
    idempotency_key: IdempotencyKey = None,
) -> StrategyDraftResponse:
    validate_idempotency_key(idempotency_key)
    compile_input = _compile_input(body)
    outcome = await container.compiler.compile(compile_input)
    request_hash = canonical_hash(body.model_dump(mode="json"))
    try:
        result = await container.drafts.create(
            outcome=outcome,
            compile_input=compile_input,
            request_hash=request_hash,
            idempotency_key=idempotency_key,
        )
    except IdempotencyConflictError as exc:
        raise _idempotency_conflict() from exc
    set_idempotency_replayed(response, replayed=result.replayed)
    return _to_response(result.value)


@router.post(
    "/{draft_id}/revisions/{revision}/clarification-answers",
    response_model=ClarificationAnswerResponse,
    status_code=status.HTTP_200_OK,
    operation_id="answerStrategyDraftClarification",
    responses=error_response_docs(404, 409, 413, 422, 500),
)
async def answer_strategy_draft_clarification(
    draft_id: UUID,
    revision: int,
    body: ClarificationAnswerRequest,
    response: Response,
    container: Container,
) -> ClarificationAnswerResponse:
    try:
        prior = await container.drafts.latest_for_answer(
            draft_id=draft_id,
            revision=revision,
        )
    except DraftNotFoundError as exc:
        raise ApiProblem(
            status_code=404,
            code="strategy_draft_not_found",
            message="Strategy draft was not found",
        ) from exc
    except DraftRevisionStaleError as exc:
        raise ApiProblem(
            status_code=409,
            code="strategy_draft_revision_stale",
            message="Clarification answer must target the latest draft revision",
        ) from exc
    try:
        turn = await container.compiler.answer_clarification(
            original_input=prior.compile_input,
            prior_outcome=prior.outcome,
            answer=body.answer,
        )
    except ValueError as exc:
        raise ApiProblem(
            status_code=409,
            code="strategy_draft_not_awaiting_clarification",
            message="Strategy draft is not awaiting clarification",
        ) from exc

    stored = prior
    replayed = False
    if turn.revision_changed:
        request_hash = canonical_hash(
            {
                "draft_id": str(draft_id),
                "revision": revision,
                "answer": body.answer,
            }
        )
        try:
            result = await container.drafts.revise(
                draft_id=draft_id,
                outcome=turn.outcome,
                compile_input=turn.compile_input,
                request_hash=request_hash,
                idempotency_key=None,
                expected_revision=revision,
            )
        except DraftRevisionStaleError as exc:
            raise ApiProblem(
                status_code=409,
                code="strategy_draft_revision_stale",
                message="Clarification answer must target the latest draft revision",
            ) from exc
        except IdempotencyConflictError as exc:
            raise _idempotency_conflict() from exc
        stored = result.value
        replayed = result.replayed
    set_idempotency_replayed(response, replayed=replayed)
    return ClarificationAnswerResponse(
        reply_kind=turn.reply_kind,
        assistant_message=turn.assistant_message,
        suggestions=tuple(
            ClarificationSuggestionPayload(
                id=item.id,
                title=item.title,
                preview=item.preview,
            )
            for item in turn.suggestions
        ),
        draft=_to_response(stored),
    )


@router.post(
    "/{draft_id}/revisions",
    response_model=StrategyDraftResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="reviseStrategyDraft",
    responses=error_response_docs(404, 409, 413, 422, 500),
)
async def revise_strategy_draft(
    draft_id: UUID,
    body: StrategyDraftRevisionRequest,
    response: Response,
    container: Container,
    idempotency_key: IdempotencyKey = None,
) -> StrategyDraftResponse:
    validate_idempotency_key(idempotency_key)
    try:
        normalize_a_share_instrument(body.strategy.instrument.symbol)
        strategy = validate_strategy_against_catalog(body.strategy, container.catalog)
    except (AshareInstrumentCodeError, StrategyCatalogError) as exc:
        raise ApiProblem(
            status_code=422,
            code="strategy_revision_invalid",
            message="Revised strategy is not executable with the active Catalog",
        ) from exc
    outcome = CompileOutcome(
        status=CompileStatus.READY,
        strategy=strategy,
        strategy_hash=canonical_hash(strategy),
        provenance=(FieldProvenance(path="/", source="revision/request.strategy"),),
    )
    request_hash = canonical_hash(body.model_dump(mode="json"))
    try:
        result = await container.drafts.revise(
            draft_id=draft_id,
            outcome=outcome,
            request_hash=request_hash,
            idempotency_key=idempotency_key,
        )
    except DraftNotFoundError as exc:
        raise ApiProblem(
            status_code=404,
            code="strategy_draft_not_found",
            message="Strategy draft was not found",
        ) from exc
    except IdempotencyConflictError as exc:
        raise _idempotency_conflict() from exc
    set_idempotency_replayed(response, replayed=result.replayed)
    return _to_response(result.value)


def _compile_input(body: StrategyDraftRequest) -> CompileInput:
    return CompileInput(
        utterance=body.utterance,
        instrument_context=body.instrument_context,
        as_of_date=body.as_of_date,
    )


def _to_response(stored: StoredDraftRevision) -> StrategyDraftResponse:
    outcome: CompileOutcome = stored.outcome
    candidate_provenance = outcome.candidate_provenance
    idea_route = outcome.idea_route
    grounding_spans = tuple(
        CandidateGroundingItem(
            path=item.path,
            start=item.start,
            end=item.end,
            text=item.text,
        )
        for item in outcome.candidate_grounding
    )
    return StrategyDraftResponse(
        draft_id=stored.draft_id,
        revision=stored.revision,
        status=outcome.status,
        strategy=outcome.strategy,
        strategy_hash=outcome.strategy_hash,
        clarification=outcome.clarification,
        diagnostic_code=outcome.diagnostic_code,
        provenance=tuple(
            ProvenanceItem(path=item.path, source=item.source) for item in outcome.provenance
        ),
        candidate_provenance=(
            None
            if candidate_provenance is None
            else CandidateProvenanceItem(
                source=candidate_provenance.source,
                provider=candidate_provenance.provider,
                model=candidate_provenance.model,
                prompt_version=candidate_provenance.prompt_version,
                schema_version=candidate_provenance.schema_version,
                capability_projection_version=(candidate_provenance.capability_projection_version),
                capability_projection_hash=candidate_provenance.capability_projection_hash,
                upstream_pattern_commit=candidate_provenance.upstream_pattern_commit,
                candidate_rank=candidate_provenance.candidate_rank,
            )
        ),
        candidate_grounding=(
            CandidateGroundingPayload(
                matched_spans=tuple(item.text for item in grounding_spans),
                spans=grounding_spans,
            )
            if grounding_spans
            else None
        ),
        candidate_rejections=tuple(
            CandidateRejectionItem(
                candidate_rank=item.candidate_rank,
                diagnostic_code=item.diagnostic_code,
            )
            for item in outcome.candidate_rejections
        ),
        candidate_alternatives=tuple(
            CandidateAlternativeItem(
                candidate_rank=item.candidate_rank,
                strategy_hash=item.strategy_hash,
            )
            for item in outcome.candidate_alternatives
        ),
        idea_route=None if idea_route is None else _to_idea_route_payload(idea_route),
        created_at=stored.created_at,
    )


def _to_idea_route_payload(idea_route: IdeaRoute) -> IdeaRoutePayload:
    instrument_symbol = idea_route.asset_mapping.instrument_symbol
    return IdeaRoutePayload(
        schema_version=idea_route.schema_version,
        understanding=idea_route.understanding,
        hypothesis=idea_route.hypothesis,
        asset_mapping=IdeaAssetMappingPayload(
            instrument_symbol=instrument_symbol,
            relation=idea_route.asset_mapping.relation,
            rationale=idea_route.asset_mapping.rationale,
            evidence_status=idea_route.asset_mapping.evidence_status,
        ),
        proposals=tuple(
            IdeaProposalPayload(
                id=item.id,
                title=item.title,
                hypothesis=item.hypothesis,
                entry_summary=item.entry_summary,
                exit_summary=item.exit_summary,
                suggested_utterance=item.suggested_utterance,
                capability_ids=item.capability_ids,
                assumptions=item.assumptions,
                confidence=item.confidence,
            )
            for item in idea_route.proposals
        ),
        provenance=(
            None
            if idea_route.provenance is None
            else IdeaRouteProvenancePayload(
                source=idea_route.provenance.source,
                provider=idea_route.provenance.provider,
                model=idea_route.provenance.model,
                prompt_version=idea_route.provenance.prompt_version,
                schema_version=idea_route.provenance.schema_version,
                capability_projection_version=(idea_route.provenance.capability_projection_version),
                capability_projection_hash=(idea_route.provenance.capability_projection_hash),
                upstream_pattern_commit=idea_route.provenance.upstream_pattern_commit,
            )
        ),
    )


def _idempotency_conflict() -> ApiProblem:
    return ApiProblem(
        status_code=409,
        code="idempotency_key_conflict",
        message="Idempotency-Key was already used with a different request",
    )
