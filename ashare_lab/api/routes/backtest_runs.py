"""Asynchronous backtest submission, lifecycle, and result read endpoints."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Path, Response, status
from pydantic import ValidationError

from ashare_lab.adapters.market_data import MarketDataCapabilityError, SnapshotScopeError
from ashare_lab.adapters.market_data.on_demand_snapshot import (
    SnapshotPreparationDocumentTextIncompleteError,
    SnapshotPreparationError,
    SnapshotPreparationIncompleteError,
    SnapshotPreparationUnsupportedError,
)
from ashare_lab.application.backtest_submission import EventDataUnavailableError
from ashare_lab.application.result_views import (
    RESULT_HASH_SCHEMA_VERSION,
    calculate_result_bundle_hash,
)
from ashare_lab.domain.shared import DomainValidationError, RunId
from ashare_lab.domain.strategy import (
    StrategyCatalogError,
    iter_event_conditions,
    strategy_requires_events,
    validate_strategy_against_catalog,
)
from ashare_lab.ports.backtest_runs import (
    BacktestJobState,
    BacktestResultIntegrityPolicy,
    BacktestRunRecord,
    BacktestRunStore,
)

from ..backtest_schemas import (
    BacktestCancelResponse,
    BacktestRunCreatedResponse,
    BacktestRunRequest,
    BacktestRunStatusResponse,
)
from ..container import ApiContainer, BacktestSubmitter, get_container
from ..errors import ApiProblem
from ..headers import IdempotencyKey, set_idempotency_replayed, validate_idempotency_key
from ..result_schemas import (
    BacktestActivity,
    BacktestResultBundle,
    BacktestSeriesPoint,
    BacktestSummaryView,
)
from ..schemas import error_response_docs

router = APIRouter(prefix="/api/v1/backtest-runs", tags=["backtest-runs"])
Container = Annotated[ApiContainer, Depends(get_container)]
RunIdPath = Annotated[
    str,
    Path(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    ),
]


@router.post(
    "",
    response_model=BacktestRunCreatedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="createBacktestRun",
    responses=error_response_docs(413, 422, 500, 503),
)
def create_backtest_run(
    body: BacktestRunRequest,
    response: Response,
    container: Container,
    idempotency_key: IdempotencyKey = None,
) -> BacktestRunCreatedResponse:
    validate_idempotency_key(idempotency_key)
    submitter, _store = _require_runtime(container)
    event_conditions = tuple(iter_event_conditions(body.strategy))
    needs_document_text = any(condition.document_text is not None for condition in event_conditions)
    try:
        validate_strategy_against_catalog(body.strategy, container.catalog)
        if strategy_requires_events(body.strategy):
            required_event_codes = frozenset(condition.event_code for condition in event_conditions)
            if not (
                container.event_codes_are_available(required_event_codes)
                or container.event_codes_are_preparable(required_event_codes)
            ):
                raise EventDataUnavailableError(
                    "event strategy requires either code-level acquisition coverage "
                    "in a pinned snapshot or an explicit request-preparation capability"
                )
        result = submitter.submit(body.strategy, body.config.to_application_config())
    except EventDataUnavailableError as exc:
        raise _event_data_unavailable() from exc
    except SnapshotPreparationUnsupportedError as exc:
        raise _backtest_data_request_unsupported() from exc
    except SnapshotPreparationDocumentTextIncompleteError as exc:
        if needs_document_text:
            raise _event_document_text_data_unavailable() from exc
        raise _backtest_data_temporarily_unavailable() from exc
    except SnapshotPreparationIncompleteError as exc:
        raise _backtest_data_temporarily_unavailable() from exc
    except SnapshotPreparationError as exc:
        raise _backtest_data_temporarily_unavailable() from exc
    except SnapshotScopeError as exc:
        raise _backtest_data_request_unsupported() from exc
    except MarketDataCapabilityError as exc:
        if needs_document_text:
            raise _event_document_text_data_unavailable() from exc
        raise _backtest_data_request_unsupported() from exc
    except (DomainValidationError, StrategyCatalogError) as exc:
        raise ApiProblem(
            status_code=422,
            code="backtest_submission_invalid",
            message="Backtest request violates an execution constraint",
        ) from exc

    if result.record.result_json is not None:
        _validate_result_bundle(result.record)
    set_idempotency_replayed(response, replayed=result.replayed)
    payload = BacktestRunStatusResponse.from_record(result.record).model_dump()
    return BacktestRunCreatedResponse.model_validate({**payload, "replayed": result.replayed})


@router.get(
    "/{run_id}",
    response_model=BacktestRunStatusResponse,
    operation_id="getBacktestRun",
    responses=error_response_docs(404, 422, 500, 503),
)
def get_backtest_run(run_id: RunIdPath, container: Container) -> BacktestRunStatusResponse:
    record = _get_record(container, run_id)
    if record.result_json is not None:
        _validate_result_bundle(record)
    return BacktestRunStatusResponse.from_record(record)


@router.post(
    "/{run_id}/cancel",
    response_model=BacktestCancelResponse,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="cancelBacktestRun",
    responses=error_response_docs(404, 409, 422, 500, 503),
)
def cancel_backtest_run(run_id: RunIdPath, container: Container) -> BacktestCancelResponse:
    store = _require_store(container)
    current = _get_record_from_store(store, run_id)
    if current.state in {BacktestJobState.SUCCEEDED, BacktestJobState.FAILED}:
        raise ApiProblem(
            status_code=409,
            code="backtest_run_not_cancellable",
            message=f"A {current.state.value} backtest run cannot be cancelled",
        )
    updated = (
        current
        if current.state in {BacktestJobState.CANCEL_REQUESTED, BacktestJobState.CANCELLED}
        else store.request_cancel(current.run_id)
    )
    payload = BacktestRunStatusResponse.from_record(updated).model_dump()
    return BacktestCancelResponse.model_validate(payload)


@router.get(
    "/{run_id}/summary",
    response_model=BacktestSummaryView,
    operation_id="getBacktestSummary",
    responses=error_response_docs(404, 409, 422, 500, 503),
)
def get_backtest_summary(run_id: RunIdPath, container: Container) -> BacktestSummaryView:
    return _get_result_bundle(container, run_id).summary


@router.get(
    "/{run_id}/series",
    response_model=tuple[BacktestSeriesPoint, ...],
    operation_id="getBacktestSeries",
    responses=error_response_docs(404, 409, 422, 500, 503),
)
def get_backtest_series(
    run_id: RunIdPath,
    container: Container,
) -> tuple[BacktestSeriesPoint, ...]:
    return _get_result_bundle(container, run_id).series


@router.get(
    "/{run_id}/trades",
    response_model=tuple[BacktestActivity, ...],
    operation_id="getBacktestTrades",
    responses=error_response_docs(404, 409, 422, 500, 503),
)
def get_backtest_trades(
    run_id: RunIdPath,
    container: Container,
) -> tuple[BacktestActivity, ...]:
    """Return the stable activity timeline used by the H5 backtest report."""

    return _get_result_bundle(container, run_id).activities


def _require_runtime(container: ApiContainer) -> tuple[BacktestSubmitter, BacktestRunStore]:
    if container.backtest_submission is None or container.run_store is None:
        raise _service_unavailable()
    return container.backtest_submission, container.run_store


def _require_store(container: ApiContainer) -> BacktestRunStore:
    if container.run_store is None:
        raise _service_unavailable()
    return container.run_store


def _service_unavailable() -> ApiProblem:
    return ApiProblem(
        status_code=503,
        code="backtest_service_unavailable",
        message="Backtest submission and run storage are not configured",
    )


def _event_data_unavailable() -> ApiProblem:
    return ApiProblem(
        status_code=422,
        code="event_data_unavailable",
        message=(
            "Event backtesting requires EVENT_DATA_REQUIRED=true and an available "
            "events.parquet dataset"
        ),
    )


def _backtest_data_request_unsupported() -> ApiProblem:
    return ApiProblem(
        status_code=422,
        code="backtest_data_request_unsupported",
        message=(
            "The requested instrument, date range, or data capability is not supported "
            "by the configured backtest data sources"
        ),
    )


def _backtest_data_temporarily_unavailable() -> ApiProblem:
    return ApiProblem(
        status_code=503,
        code="backtest_data_temporarily_unavailable",
        message="Historical backtest data is temporarily unavailable; retry later",
    )


def _event_document_text_data_unavailable() -> ApiProblem:
    return ApiProblem(
        status_code=422,
        code="event_document_text_data_unavailable",
        message=(
            "The requested report text is not available as a complete frozen document "
            "for this backtest"
        ),
    )


def _get_record(container: ApiContainer, value: str) -> BacktestRunRecord:
    return _get_record_from_store(_require_store(container), value)


def _get_record_from_store(store: BacktestRunStore, value: str) -> BacktestRunRecord:
    run_id = RunId(value)
    record = store.get(run_id)
    if record is None:
        raise ApiProblem(
            status_code=404,
            code="backtest_run_not_found",
            message="Backtest run was not found",
        )
    return record


def _get_result_bundle(container: ApiContainer, value: str) -> BacktestResultBundle:
    record = _get_record(container, value)
    if record.state is not BacktestJobState.SUCCEEDED:
        raise ApiProblem(
            status_code=409,
            code="backtest_result_not_ready",
            message=f"Backtest result is not available while state is {record.state.value}",
        )
    return _validate_result_bundle(record)


def _validate_result_bundle(record: BacktestRunRecord) -> BacktestResultBundle:
    """Parse and verify one persisted result before any API field is exposed."""

    assert record.result_json is not None
    try:
        decoded: object = json.loads(record.result_json)
        bundle = BacktestResultBundle.model_validate(decoded)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ApiProblem(
            status_code=500,
            code="backtest_result_invalid",
            message="Stored backtest result does not match the result contract",
        ) from exc
    if bundle.summary.run_id != str(record.run_id):
        raise ApiProblem(
            status_code=500,
            code="backtest_result_identity_mismatch",
            message="Stored backtest result belongs to a different run",
        )
    hash_schema_version = bundle.audit.hash_schema_version
    stored_result_hash = bundle.audit.result_hash
    if record.result_integrity_policy is BacktestResultIntegrityPolicy.BUNDLE_HASH_V1:
        if hash_schema_version != RESULT_HASH_SCHEMA_VERSION or stored_result_hash is None:
            raise _result_integrity_mismatch()
    elif record.result_integrity_policy is not BacktestResultIntegrityPolicy.LEGACY_UNVERIFIED:
        raise _result_integrity_mismatch()
    if hash_schema_version is None:
        if stored_result_hash is not None:
            raise _result_integrity_mismatch()
        return bundle
    if not isinstance(decoded, Mapping):
        raise AssertionError("validated result bundle must be a mapping")
    typed_bundle = cast(Mapping[str, object], decoded)
    if (
        hash_schema_version != RESULT_HASH_SCHEMA_VERSION
        or stored_result_hash is None
        or calculate_result_bundle_hash(typed_bundle) != stored_result_hash
    ):
        raise _result_integrity_mismatch()
    return bundle


def _result_integrity_mismatch() -> ApiProblem:
    return ApiProblem(
        status_code=500,
        code="backtest_result_integrity_mismatch",
        message="Stored backtest result failed its content-integrity check",
    )
