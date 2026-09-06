"""Operational and discoverability endpoints."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated

from fastapi import APIRouter, Depends

from ashare_lab.domain.events import EXECUTABLE_EVENT_DEFINITIONS
from ashare_lab.domain.events.catalog import DOCUMENT_TEXT_EVENT_CODES

from ..container import ApiContainer, get_container
from ..errors import ApiProblem
from ..schemas import (
    CapabilitiesResponse,
    CatalogRelease,
    ErrorDetail,
    EventCapability,
    EventDocumentTextCapability,
    HealthResponse,
    IndicatorCapability,
    IndicatorParameterCapability,
    IndicatorTriggerCapability,
    ReadinessResponse,
    RequestLimits,
    VersionResponse,
    error_response_docs,
)

router = APIRouter(prefix="/api/v1", tags=["system"])
Container = Annotated[ApiContainer, Depends(get_container)]


@router.get(
    "/health",
    response_model=HealthResponse,
    operation_id="getHealth",
    responses=error_response_docs(500),
)
async def health() -> HealthResponse:
    return HealthResponse()


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    operation_id="getReadiness",
    responses=error_response_docs(503),
)
async def readiness(container: Container) -> ReadinessResponse:
    if container.readiness_probe is None:
        raise _not_ready(("runtime",))
    checks = dict(container.readiness_probe())
    failed = tuple(sorted(name for name, ready in checks.items() if not ready))
    if not checks or failed:
        reasons = (
            dict(container.readiness_reasons_probe())
            if container.readiness_reasons_probe is not None
            else {}
        )
        raise _not_ready(failed or ("runtime",), reasons=reasons)
    return ReadinessResponse(checks={name: "ok" for name in sorted(checks)})


@router.get(
    "/version",
    response_model=VersionResponse,
    operation_id="getVersion",
    responses=error_response_docs(500),
)
async def version(container: Container) -> VersionResponse:
    releases = tuple(
        CatalogRelease(
            catalog_id=manifest.catalog_id,
            release_version=manifest.release_version,
            content_hash=manifest.content_hash,
        )
        for manifest in container.catalog.manifests
    )
    return VersionResponse(
        service_version=container.service_version,
        catalog_snapshot_hash=container.catalog.content_hash,
        catalog_releases=releases,
    )


@router.get(
    "/capabilities",
    response_model=CapabilitiesResponse,
    operation_id="getCapabilities",
    responses=error_response_docs(500),
)
async def capabilities(container: Container) -> CapabilitiesResponse:
    indicators = _indicator_capabilities(container)
    runnable_event_codes = container.event_backtest_codes
    preparable_event_codes = container.event_preparable_codes
    runnable_document_text_codes = container.event_document_text_backtest_codes
    preparable_document_text_codes = container.event_document_text_preparable_codes
    events = tuple(
        EventCapability(
            event_code=definition.event_code,
            definition_version=definition.definition_version,
            status=(
                "available" if definition.event_code in runnable_event_codes else "unavailable"
            ),
            backtest_available=definition.event_code in runnable_event_codes,
            preparation_available=definition.event_code in preparable_event_codes,
            availability_scope=(
                "pinned_snapshot"
                if definition.event_code in runnable_event_codes
                else (
                    "request_preparation"
                    if definition.event_code in preparable_event_codes
                    else "unavailable"
                )
            ),
            unavailable_reason=(
                None
                if definition.event_code in runnable_event_codes
                else (
                    "preparation_required"
                    if definition.event_code in preparable_event_codes
                    else "snapshot_coverage_unavailable"
                )
            ),
            document_text=_event_document_text_capability(
                definition.event_code,
                runnable_codes=runnable_document_text_codes,
                preparable_codes=preparable_document_text_codes,
            ),
        )
        for definition in sorted(
            EXECUTABLE_EVENT_DEFINITIONS.values(),
            key=lambda item: item.event_code,
        )
    )
    return CapabilitiesResponse(
        indicators=indicators,
        events=events,
        backtest_execution_available=container.backtest_execution_available,
        event_backtest_available=container.event_backtest_available,
        event_preparation_available=container.event_preparation_available,
        event_availability_scope=container.event_availability_scope,
        limits=RequestLimits(max_body_bytes=container.max_body_bytes),
    )


def _event_document_text_capability(
    event_code: str,
    *,
    runnable_codes: frozenset[str],
    preparable_codes: frozenset[str],
) -> EventDocumentTextCapability:
    catalog_available = event_code in DOCUMENT_TEXT_EVENT_CODES
    backtest_available = catalog_available and event_code in runnable_codes
    preparation_available = catalog_available and event_code in preparable_codes
    if backtest_available:
        availability_scope = "pinned_snapshot"
        unavailable_reason = None
    elif preparation_available:
        availability_scope = "request_preparation"
        unavailable_reason = "preparation_required"
    else:
        availability_scope = "unavailable"
        unavailable_reason = (
            "snapshot_coverage_unavailable" if catalog_available else "not_catalog_available"
        )
    return EventDocumentTextCapability(
        catalog_available=catalog_available,
        backtest_available=backtest_available,
        preparation_available=preparation_available,
        availability_scope=availability_scope,
        unavailable_reason=unavailable_reason,
    )


def _indicator_capabilities(container: ApiContainer) -> tuple[IndicatorCapability, ...]:
    executable = {
        definition.id: definition
        for definition in container.catalog.indicators
        if definition.status == "stable"
    }
    coverage = {
        definition.id: definition
        for definition in container.coverage_catalog.metrics
        if definition.status == "stable"
    }
    if set(executable) != set(coverage):
        raise _indicator_catalog_mismatch("stable indicator identifiers differ")

    capabilities: list[IndicatorCapability] = []
    unavailable_reasons = getattr(
        container.backtest_submission, "indicator_unavailable_reasons", {}
    )
    for indicator_id in sorted(executable):
        definition = executable[indicator_id]
        metadata = coverage[indicator_id]
        if {item.name for item in definition.parameters} != set(metadata.parameters):
            raise _indicator_catalog_mismatch(f"parameter definitions drifted for {indicator_id}")
        if {item.id for item in definition.triggers} != set(metadata.triggers):
            raise _indicator_catalog_mismatch(f"trigger definitions drifted for {indicator_id}")
        capabilities.append(
            IndicatorCapability(
                indicator_id=definition.id,
                definition_version=definition.version,
                status="unavailable" if indicator_id in unavailable_reasons else definition.status,
                display_name=metadata.name_zh,
                description=(
                    f"东方财富查数 Skill 的{metadata.name_zh}历史数据暂不可用于回测："
                    f"{unavailable_reasons[indicator_id]}"
                    if indicator_id in unavailable_reasons else metadata.description
                ),
                warmup_bars=definition.warmup_bars,
                timeframes=definition.timeframes,
                evaluation_modes=definition.evaluation_modes,
                triggers=tuple(trigger.id for trigger in definition.triggers),
                trigger_definitions=tuple(
                    IndicatorTriggerCapability(
                        id=trigger.id,
                        value_requirement=trigger.value_requirement,
                        minimum=trigger.minimum,
                        maximum=trigger.maximum,
                        exclusive_minimum=trigger.exclusive_minimum,
                        exclusive_maximum=trigger.exclusive_maximum,
                    )
                    for trigger in definition.triggers
                ),
                parameters=tuple(
                    IndicatorParameterCapability(
                        name=parameter.name,
                        value_type=parameter.value_type,
                        required=parameter.required,
                        default=parameter.default,
                        minimum=parameter.minimum,
                        maximum=parameter.maximum,
                        choices=parameter.choices,
                        unit=None,
                    )
                    for parameter in definition.parameters
                ),
            )
        )
    return tuple(capabilities)


def _indicator_catalog_mismatch(reason: str) -> ApiProblem:
    return ApiProblem(
        status_code=500,
        code="indicator_catalog_mismatch",
        message=f"Indicator capability catalogs are inconsistent: {reason}",
    )


def _not_ready(
    failed: tuple[str, ...],
    *,
    reasons: Mapping[str, str] | None = None,
) -> ApiProblem:
    reason_map = reasons or {}
    return ApiProblem(
        status_code=503,
        code="service_not_ready",
        message="Required runtime checks failed: " + ", ".join(failed),
        details=tuple(
            ErrorDetail(
                location=name,
                message=reason_map[name],
                type="readiness_check_failed",
            )
            for name in failed
            if name in reason_map
        ),
    )
