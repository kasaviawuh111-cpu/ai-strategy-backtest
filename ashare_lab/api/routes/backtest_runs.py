"""Asynchronous backtest submission, lifecycle, and result read endpoints."""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Path, Response, status
from pydantic import ValidationError

from ashare_lab.adapters.market_data import MarketDataCapabilityError, SnapshotScopeError
from ashare_lab.adapters.market_data.mx_indicator_contract import UnsupportedSkillIndicatorError
from ashare_lab.adapters.market_data.on_demand_snapshot import (
    SnapshotPreparationDocumentTextIncompleteError,
    SnapshotPreparationError,
    SnapshotPreparationIncompleteError,
    SnapshotPreparationUnsupportedError,
)
from ashare_lab.application.backtest_submission import (
    BacktestDataNotYetAvailableError,
    BacktestDateRangeError,
    EventDataUnavailableError,
    FinancialDataUnavailableError,
    validate_a_share_backtest_range,
)
from ashare_lab.application.result_views import (
    RESULT_HASH_SCHEMA_VERSION,
    calculate_result_bundle_hash,
)
from ashare_lab.domain.market_data import AshareInstrumentCodeError, normalize_a_share_instrument
from ashare_lab.domain.shared import DomainValidationError, RunId
from ashare_lab.domain.strategy import (
    StrategyCatalogError,
    StrategySpec,
    canonical_hash,
    iter_event_conditions,
    strategy_requires_events,
    validate_strategy_against_catalog,
)
from ashare_lab.ports.backtest_review import BacktestReviewRequest, EvidenceGrade
from ashare_lab.ports.backtest_runs import (
    BacktestJobState,
    BacktestResultIntegrityPolicy,
    BacktestRunRecord,
    BacktestRunStore,
)

from ..backtest_review_schemas import BacktestReviewResponse
from ..backtest_schemas import (
    BacktestCancelResponse,
    BacktestRunCreatedResponse,
    BacktestRunRequest,
    BacktestRunStatusResponse,
)
from ..container import (
    ApiContainer,
    BacktestPreparationStatus,
    BacktestSubmitter,
    get_container,
)
from ..errors import ApiProblem
from ..headers import IdempotencyKey, set_idempotency_replayed, validate_idempotency_key
from ..result_schemas import (
    BacktestActivity,
    BacktestResultBundle,
    BacktestSeriesPoint,
    BacktestSummaryView,
)
from ..schemas import BacktestReviewReference, error_response_docs

router = APIRouter(prefix="/api/v1/backtest-runs", tags=["backtest-runs"])
_LOGGER = logging.getLogger(__name__)
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
    # Import here: the service also consumes API result schemas at module load.
    from ashare_lab.application.skill_backtest_service import SkillBacktestService

    if body.config.refresh_data and not isinstance(submitter, SkillBacktestService):
        raise ApiProblem(
            status_code=422, code="data_refresh_unavailable",
            message="当前回测数据通道不支持强制重新取数，本次未执行。",
        )
    event_conditions = tuple(iter_event_conditions(body.strategy))
    document_text_event_codes = frozenset(
        condition.event_code
        for condition in event_conditions
        if condition.document_text is not None
    )
    needs_document_text = bool(document_text_event_codes)
    try:
        validate_a_share_backtest_range(body.strategy.backtest.start, body.strategy.backtest.end)
        normalize_a_share_instrument(body.strategy.instrument.symbol)
        validate_strategy_against_catalog(body.strategy, container.catalog)
        if document_text_event_codes and not (
            document_text_event_codes.issubset(container.event_document_text_backtest_codes)
            or document_text_event_codes.issubset(container.event_document_text_preparable_codes)
        ):
            raise _event_document_text_data_unavailable()
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
    except BacktestDateRangeError as exc:
        raise ApiProblem(
            status_code=422,
            code="backtest_date_range_invalid",
            message=(
                "回测起始日期无效，请选择 1990 年及以后的日期，且不能晚于结束日期。"
                "本次尚未取数或启动回测。"
            ),
        ) from exc
    except UnsupportedSkillIndicatorError as exc:
        raise ApiProblem(
            status_code=422,
            code="skill_indicator_unavailable",
            message=f"东方财富查数 Skill 的历史指标数据暂不满足本次回测要求：{exc.reason}",
        ) from exc
    except EventDataUnavailableError as exc:
        raise _event_data_unavailable() from exc
    except FinancialDataUnavailableError as exc:
        raise _financial_data_unavailable(str(exc)) from exc
    except BacktestDataNotYetAvailableError as exc:
        raise _backtest_data_not_yet_available() from exc
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
    except (AshareInstrumentCodeError, DomainValidationError, StrategyCatalogError) as exc:
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
    current = _get_record(container, run_id)
    if current.state in {BacktestJobState.SUCCEEDED, BacktestJobState.FAILED}:
        raise ApiProblem(
            status_code=409,
            code="backtest_run_not_cancellable",
            message=f"A {current.state.value} backtest run cannot be cancelled",
        )
    if store.get(current.run_id) is None:
        submitter = container.backtest_submission
        updated = (
            submitter.request_cancel_preparation(RunId(run_id))
            if isinstance(submitter, BacktestPreparationStatus)
            else None
        )
        if updated is None:
            raise _run_not_found()
    else:
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


@router.post(
    "/{run_id}/review",
    response_model=BacktestReviewResponse,
    operation_id="reviewBacktestRun",
    responses=error_response_docs(404, 409, 422, 500, 503),
)
async def review_backtest_run(
    run_id: RunIdPath,
    container: Container,
) -> BacktestReviewResponse:
    """Keep the public report-review endpoint independent of dialogue input."""
    return await build_backtest_review(run_id, container)


async def build_backtest_review(
    run_id: str, container: ApiContainer, *, user_request: str | None = None,
) -> BacktestReviewResponse:
    """Use the configured deep model to review one verified completed run.

    The model receives only server-owned strategy/result facts. Each model-authored
    strategy is Catalog-gated without another model reinterpreting its prose.
    """

    record = _get_record(container, run_id)
    bundle = _validate_completed_result(record)
    advisor = container.backtest_review_advisor
    if advisor is None:
        raise _backtest_review_model_unavailable()
    result_hash = bundle.audit.result_hash
    if result_hash is None:
        raise ApiProblem(
            status_code=422,
            code="backtest_review_requires_verified_result",
            message="Model review requires a completed result with verified bundle integrity",
        )
    try:
        strategy = StrategySpec.model_validate_json(record.strategy_json)
    except ValidationError as exc:
        raise ApiProblem(
            status_code=500,
            code="backtest_review_strategy_invalid",
            message="Stored backtest strategy does not match the strategy contract",
        ) from exc
    evidence_grade, evidence_reasons = _review_evidence_gate(bundle)
    model_review = await advisor.review(
        BacktestReviewRequest(
            run_id=run_id,
            instrument_symbol=strategy.instrument.symbol,
            as_of_date=strategy.backtest.end,
            strategy_payload=cast(
                Mapping[str, object],
                strategy.model_dump(mode="json"),
            ),
            result_facts=_verified_review_facts(record, bundle),
            evidence_grade=evidence_grade,
            evidence_reasons=evidence_reasons,
            user_request=user_request,
        )
    )
    if model_review is None:
        raise _backtest_review_model_unavailable()

    candidates: list[dict[str, object]] = []
    seen_hashes: set[str] = {canonical_hash(strategy)}
    for proposal in model_review.proposals:
        revision = proposal.strategy
        try:
            validate_strategy_against_catalog(revision, container.catalog)
        except StrategyCatalogError:
            _LOGGER.warning("backtest_review_candidate_rejected reason=catalog")
            continue
        revision_hash = canonical_hash(revision)
        changes_entry = proposal.change_dimension in {"entry", "confirmation"}
        if ((changes_entry and revision.exit != strategy.exit)
                or (not changes_entry and revision.entry != strategy.entry)):
            _LOGGER.warning("backtest_review_candidate_rejected reason=changed_other_dimension")
            continue
        if (
            revision.instrument != strategy.instrument
            or revision.backtest != strategy.backtest
            or revision.execution != strategy.execution
            or revision.catalog != strategy.catalog
            or revision_hash in seen_hashes
        ):
            _LOGGER.warning("backtest_review_candidate_rejected reason=fixed_boundary_or_duplicate")
            continue
        seen_hashes.add(revision_hash)
        candidates.append(
            {
                "id": f"model-opt-{len(candidates) + 1}",
                "title": proposal.title,
                "diagnosis": proposal.diagnosis,
                "changeDimension": proposal.change_dimension,
                "expectedEffect": proposal.expected_effect,
                "tradeoff": proposal.tradeoff,
                "suggestedUtterance": proposal.suggested_utterance,
                "strategy": revision,
                "strategyHash": revision_hash,
                "modelSuggested": True,
            }
        )
    if len(candidates) < 2:
        raise ApiProblem(
            status_code=503,
            code="backtest_review_candidates_unavailable",
            message="AI 分析的优化方案未通过校验，请重试；本次回测结果已保留。",
        )
    response = BacktestReviewResponse.model_validate(
        {
            "runId": run_id,
            "sourceResultHash": result_hash,
            "generatedAt": datetime.now(UTC),
            "evidenceGrade": evidence_grade,
            "evidenceReasons": evidence_reasons,
            "analysis": model_review.analysis,
            "conclusion": model_review.conclusion,
            "optimizationCandidates": candidates,
            "modelProvenance": {
                "provider": model_review.provider,
                "model": model_review.model,
                "promptVersion": model_review.prompt_version,
                "schemaVersion": model_review.schema_version,
                "responseHash": model_review.response_hash,
            },
        }
    )
    await container.drafts.remember_review(response)
    return response


async def load_backtest_dialogue_results(
    container: ApiContainer, run_ids: tuple[str, ...],
    review_reference: BacktestReviewReference | None = None,
) -> tuple[Mapping[str, object], ...]:
    """Resolve explicit conversation references using the existing report boundary."""
    review = None
    if review_reference is not None:
        if review_reference.run_id in run_ids:
            review = await container.drafts.get_review(
                review_reference.run_id, review_reference.response_hash,
            )
        if review is None:
            raise ApiProblem(
                status_code=409, code="backtest_review_context_unavailable",
                message="这版优化方案已无法读取，请重新生成 AI 分析后再选择；尚未执行新回测。",
            )
    results: list[Mapping[str, object]] = []
    for run_id in dict.fromkeys(run_ids):
        record = _get_record(container, run_id)
        bundle = _validate_completed_result(record)
        strategy = StrategySpec.model_validate_json(record.strategy_json)
        grade, reasons = _review_evidence_gate(bundle)
        facts: dict[str, object] = {
            "runId": run_id,
            "strategy": strategy.model_dump(mode="json"),
            "summary": bundle.summary.model_dump(
                mode="json", by_alias=True, exclude={"run_evidence", "data_provenance"},
            ),
            "evidenceGrade": grade, "evidenceReasons": reasons,
            "executionCosts": _execution_cost_facts(record.config_json),
            "dataSource": _data_source_facts(bundle),
        }
        if review is not None and review.run_id == run_id:
            if review.source_result_hash != bundle.audit.result_hash:
                raise ApiProblem(status_code=409, code="backtest_review_result_changed",
                                 message="回测结果已变化，请重新生成优化方案。")
            facts["review"] = review.model_dump(mode="json", by_alias=True,
                                               exclude={"model_provenance", "disclaimer"})
        results.append(facts)
    return tuple(results)


def _validate_completed_result(record: BacktestRunRecord) -> BacktestResultBundle:
    if record.state is not BacktestJobState.SUCCEEDED:
        raise ApiProblem(
            status_code=409,
            code="backtest_result_not_ready",
            message=f"Backtest result is not available while state is {record.state.value}",
        )
    return _validate_result_bundle(record)


def _review_evidence_gate(
    bundle: BacktestResultBundle,
) -> tuple[EvidenceGrade, tuple[str, ...]]:
    trade_count = bundle.summary.trade_count
    if trade_count < 10:
        grade: EvidenceGrade = "insufficient"
        reasons = [f"完整交易仅 {trade_count} 次，无法稳定评价胜率或夏普"]
    elif trade_count < 30:
        grade = "limited"
        reasons = [f"完整交易仅 {trade_count} 次，结论仍容易受少数交易影响"]
    else:
        grade = "moderate"
        reasons = ["完整交易达到 30 次，但仍只是一只标的一段历史样本"]
    if bundle.summary.benchmark_comparison_status != "comparable":
        reasons.append("策略与基准当前不可直接比较")
        grade = "insufficient"
    if bundle.audit.open_position_shares > 0 or (bundle.audit.open_position_notional_cny or 0) > 0:
        reasons.append("回测结束时仍有未平仓持仓，完整交易统计未包含该笔")
        if grade == "moderate":
            grade = "limited"
    if bundle.robustness is None:
        reasons.append("本次结果没有执行成本/容量稳健性场景")
        if grade == "moderate":
            grade = "limited"
    return grade, tuple(reasons)


def _verified_review_facts(
    record: BacktestRunRecord,
    bundle: BacktestResultBundle,
) -> Mapping[str, object]:
    activity_counts = Counter(item.kind for item in bundle.activities)
    status_counts = Counter(item.status for item in bundle.activities)
    facts: dict[str, object] = {
        "runFingerprint": record.fingerprint,
        "summary": bundle.summary.model_dump(mode="json", by_alias=True),
        "audit": bundle.audit.model_dump(mode="json", by_alias=True),
        "activityCounts": dict(sorted(activity_counts.items())),
        "activityStatusCounts": dict(sorted(status_counts.items())),
        "executionCosts": _execution_cost_facts(record.config_json),
        "dataSource": _data_source_facts(bundle),
        "robustness": (
            None
            if bundle.robustness is None
            else bundle.robustness.model_dump(mode="json", by_alias=True)
        ),
    }
    provider = _provider_indicator_fact(record.config_json)
    if provider is not None:
        facts["providerIndicatorEvidence"] = provider
    elif bundle.summary.data_provenance is not None:
        provenance = bundle.summary.data_provenance
        facts["benchmarkDefinition"] = {
            "type": "same_instrument_buy_and_hold",
            "instrumentId": provenance.instrument_id,
            "description": "同一只股票同期买入并持有，不是股票指数。",
        }
        facts["providerIndicatorEvidence"] = {
            "providers": [provenance.provider],
            "seriesCount": provenance.indicator_series,
            "pointCount": provenance.indicator_points,
        }
    return facts


def _execution_cost_facts(config_json: str) -> Mapping[str, str | None]:
    """Project only recorded costs; absent legacy fields must not acquire current defaults."""
    try:
        raw: object = json.loads(config_json)
    except json.JSONDecodeError:
        raw = None
    config: Mapping[str, object] = (
        cast(Mapping[str, object], raw) if isinstance(raw, Mapping) else {}
    )
    costs: dict[str, str | None] = {}
    for stored, public in (
        ("slippage_bps", "slippageBps"),
        ("commission_rate", "commissionRate"),
        ("minimum_commission_cny", "minimumCommissionCny"),
    ):
        costs[public] = None
        value = config.get(stored)
        if isinstance(value, bool) or not isinstance(value, str | int | float):
            continue
        try:
            amount = Decimal(str(value))
        except InvalidOperation:
            continue
        if amount.is_finite() and amount >= 0:
            costs[public] = str(amount)
    return costs


def _data_source_facts(bundle: BacktestResultBundle) -> Mapping[str, object]:
    """Retain source identity without raw indicator data or an inferred cache hit."""
    source = bundle.summary.data_provenance
    return {
        "provider": source.provider if source is not None else None,
        "instrumentId": source.instrument_id if source is not None else None,
        "priceBasis": source.price_basis if source is not None else None,
        "retrievedAt": source.retrieved_at.isoformat() if source is not None else None,
        "historyRange": ({
            "start": source.history_start.isoformat(),
            "end": source.history_end.isoformat(),
            "rows": source.history_rows,
        } if source is not None else None),
        "cacheStatus": source.history_cache_status if source is not None else "unknown",
        "indicatorCacheStatuses": list(source.indicator_cache_statuses) if source else [],
        "refreshRequested": source.refresh_requested if source is not None else False,
    }


def _provider_indicator_fact(config_json: str) -> Mapping[str, object] | None:
    try:
        decoded: object = json.loads(config_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, Mapping):
        return None
    config = cast(Mapping[str, object], decoded)
    raw = config.get("provider_indicator_series")
    if not isinstance(raw, Mapping):
        return None
    provider_payload = cast(Mapping[str, object], raw)
    identity = provider_payload.get("identity_basis")
    if not isinstance(identity, Mapping):
        return None
    identity_payload = cast(Mapping[str, object], identity)
    providers: set[str] = set()
    response_hashes: set[str] = set()
    series_count = 0
    point_count = 0
    for side in ("entry", "exit"):
        items = identity_payload.get(side)
        if not isinstance(items, list):
            continue
        for item in cast(list[object], items):
            if not isinstance(item, Mapping):
                continue
            series = cast(Mapping[str, object], item).get("series")
            if not isinstance(series, Mapping):
                continue
            series_payload = cast(Mapping[str, object], series)
            series_count += 1
            provider = series_payload.get("provider")
            if isinstance(provider, str):
                providers.add(provider)
            response_hash = series_payload.get("response_sha256")
            if isinstance(response_hash, str):
                response_hashes.add(response_hash)
            points = series_payload.get("points")
            if isinstance(points, list):
                point_count += len(cast(list[object], points))
    return {
        "schemaVersion": provider_payload.get("schema_version"),
        "snapshotId": provider_payload.get("snapshot_id"),
        "providers": sorted(providers),
        "responseHashes": sorted(response_hashes),
        "seriesCount": series_count,
        "pointCount": point_count,
    }


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


def _backtest_review_model_unavailable() -> ApiProblem:
    return ApiProblem(
        status_code=503,
        code="backtest_review_model_unavailable",
        message="AI 分析暂未完成，请重试；已完成的回测结果不受影响。",
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


def _financial_data_unavailable(reason: str) -> ApiProblem:
    return ApiProblem(
        status_code=422,
        code="financial_data_unavailable",
        message=(
            "The requested direct financial metric is not available with "
            f"revision-safe history: {reason[:160]}"
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


def _backtest_data_not_yet_available() -> ApiProblem:
    return ApiProblem(
        status_code=422,
        code="backtest_data_not_yet_available",
        message="Backtest end exceeds the latest stable completed A-share daily data date",
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
    run_id = RunId(value)
    record = _require_store(container).get(run_id)
    if record is None and isinstance(container.backtest_submission, BacktestPreparationStatus):
        record = container.backtest_submission.get_preparation(run_id)
    if record is None:
        raise _run_not_found()
    return record


def _run_not_found() -> ApiProblem:
    return ApiProblem(
        status_code=404,
        code="backtest_run_not_found",
        message="Backtest run was not found",
    )


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
