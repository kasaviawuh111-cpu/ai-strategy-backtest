"""Fail-closed Strategy v2 draft, validation, execution and result routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, status

from ashare_lab.application.strategy_v2_http import (
    StrategyV2DraftCommand,
    StrategyV2HttpService,
    StrategyV2HttpServiceError,
)

from ..container import ApiContainer, get_container
from ..errors import ApiProblem
from ..schemas import error_response_docs
from ..v2_schemas import (
    StrategyV2DraftRefRequest,
    StrategyV2DraftRequest,
    StrategyV2DraftResponse,
    StrategyV2ExecutionResponse,
    StrategyV2RunResponse,
    StrategyV2ValidationResponse,
)

router = APIRouter(prefix="/api/v2", tags=["strategy-v2"])
Container = Annotated[ApiContainer, Depends(get_container)]
DraftIdPath = Annotated[
    str,
    Path(pattern=r"^draft:[A-Za-z0-9_.-]{1,128}$", max_length=134),
]
RevisionPath = Annotated[int, Path(ge=1, le=1_000_000)]
RunIdPath = Annotated[
    str,
    Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$", max_length=128),
]


@router.post(
    "/strategy-drafts",
    response_model=StrategyV2DraftResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="createStrategyV2Draft",
    responses=error_response_docs(413, 422, 500, 503),
)
async def create_strategy_v2_draft(
    body: StrategyV2DraftRequest,
    container: Container,
) -> StrategyV2DraftResponse:
    service = _require_service(container)
    try:
        view = await service.create_draft(
            StrategyV2DraftCommand(
                utterance=body.utterance,
                instrument_context=body.instrument_context,
                as_of_date=body.as_of_date,
            )
        )
    except StrategyV2HttpServiceError as exc:
        raise _to_problem(exc) from exc
    return StrategyV2DraftResponse.from_view(view)


@router.get(
    "/strategy-drafts/{draft_id}/revisions/{revision}",
    response_model=StrategyV2DraftResponse,
    operation_id="getStrategyV2DraftRevision",
    responses=error_response_docs(404, 422, 500, 503),
)
def get_strategy_v2_draft(
    draft_id: DraftIdPath,
    revision: RevisionPath,
    container: Container,
) -> StrategyV2DraftResponse:
    service = _require_service(container)
    try:
        view = service.get_draft(draft_id, revision)
    except StrategyV2HttpServiceError as exc:
        raise _to_problem(exc) from exc
    return StrategyV2DraftResponse.from_view(view)


@router.post(
    "/strategy-validations",
    response_model=StrategyV2ValidationResponse,
    operation_id="validateStrategyV2Draft",
    responses=error_response_docs(404, 409, 422, 500, 503),
)
def validate_strategy_v2_draft(
    body: StrategyV2DraftRefRequest,
    container: Container,
) -> StrategyV2ValidationResponse:
    service = _require_service(container)
    try:
        view = service.validate(body.draft_id, body.revision)
    except StrategyV2HttpServiceError as exc:
        raise _to_problem(exc) from exc
    return StrategyV2ValidationResponse.from_view(view)


@router.post(
    "/backtest-runs",
    response_model=StrategyV2ExecutionResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="createStrategyV2BacktestRun",
    responses=error_response_docs(404, 409, 422, 500, 503),
)
def create_strategy_v2_backtest_run(
    body: StrategyV2DraftRefRequest,
    container: Container,
) -> StrategyV2ExecutionResponse:
    service = _require_service(container)
    try:
        view = service.execute(body.draft_id, body.revision)
    except StrategyV2HttpServiceError as exc:
        raise _to_problem(exc) from exc
    return StrategyV2ExecutionResponse.from_view(view)


@router.get(
    "/backtest-runs/{run_id}",
    response_model=StrategyV2RunResponse,
    operation_id="getStrategyV2BacktestRun",
    responses=error_response_docs(404, 409, 422, 500, 503),
)
def get_strategy_v2_backtest_run(
    run_id: RunIdPath,
    container: Container,
) -> StrategyV2RunResponse:
    service = _require_service(container)
    try:
        view = service.get_run(run_id)
    except StrategyV2HttpServiceError as exc:
        raise _to_problem(exc) from exc
    return StrategyV2RunResponse.from_view(view)


def _require_service(container: ApiContainer) -> StrategyV2HttpService:
    if container.strategy_v2_service is None:
        raise ApiProblem(
            status_code=503,
            code="strategy_v2_service_unavailable",
            message="Strategy v2 validation and execution are not configured",
        )
    return container.strategy_v2_service


def _to_problem(error: StrategyV2HttpServiceError) -> ApiProblem:
    if error.code in {"strategy_draft_not_found", "backtest_run_not_found"}:
        status_code = 404
        message = "The requested server-owned Strategy v2 record was not found"
    elif error.code in {
        "backtest_result_not_ready",
        "draft_not_ready",
        "receipt_expired",
        "receipt_untrusted",
        "snapshot_binding_changed",
    }:
        status_code = 409
        message = "The stored Strategy v2 validation is no longer executable"
    elif error.code in {
        "data_unavailable",
        "provider_unavailable",
        "snapshot_unavailable",
        "snapshot_integrity_failed",
    }:
        status_code = 503
        message = "Trusted Strategy v2 data is unavailable or failed integrity checks"
    elif error.code in {"backtest_artifact_integrity_failed"}:
        status_code = 500
        message = "Stored Strategy v2 artifacts failed integrity checks"
    else:
        status_code = 422
        message = str(error)
    return ApiProblem(status_code=status_code, code=error.code, message=message)


__all__ = ["router"]
