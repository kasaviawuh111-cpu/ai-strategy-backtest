"""Strict request and response models for the additive Strategy v2 API."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from typing import Literal, cast

from pydantic import Field, model_validator

from ashare_lab.application.result_views import calculate_result_bundle_hash
from ashare_lab.application.strategy_v2_http import (
    StrategyV2DraftView,
    StrategyV2ExecutionView,
    StrategyV2RunView,
    StrategyV2ValidationView,
)
from ashare_lab.domain.runs import RunManifestV2
from ashare_lab.domain.strategy import StrategySpecV2
from ashare_lab.domain.strategy.canonical import canonical_hash

from .result_schemas import BacktestResultBundle
from .schemas import ApiModel


class StrategyV2DraftRequest(ApiModel):
    utterance: str = Field(min_length=1, max_length=2_000)
    instrument_context: str | None = Field(default=None, max_length=128)
    # Kept as an optional compatibility hint; the server never uses it to
    # choose or extend trusted market-data coverage.
    as_of_date: date | None = None


class StrategyV2DraftRefRequest(ApiModel):
    """The complete public authority for validation and execution."""

    draft_id: str = Field(pattern=r"^draft:[A-Za-z0-9_.-]{1,128}$")
    revision: int = Field(ge=1, le=1_000_000)


class StrategyV2DraftResponse(ApiModel):
    draft_id: str = Field(pattern=r"^draft:[A-Za-z0-9_.-]{1,128}$")
    revision: int = Field(ge=1, le=1_000_000)
    status: Literal["ready", "needs_clarification", "unsupported", "invalid"]
    strategy: StrategySpecV2 | None = None
    strategy_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    provider: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    clarification: str | None = Field(default=None, max_length=2_000)
    diagnostic_code: str | None = Field(default=None, max_length=128)
    created_at: datetime

    @model_validator(mode="after")
    def ready_state_is_complete(self) -> StrategyV2DraftResponse:
        if self.status == "ready":
            if self.strategy is None or self.strategy_hash is None:
                raise ValueError("ready Strategy v2 draft requires strategy and hash")
        elif self.strategy is not None or self.strategy_hash is not None:
            raise ValueError("non-ready Strategy v2 draft cannot expose executable DSL")
        return self

    @classmethod
    def from_view(cls, value: StrategyV2DraftView) -> StrategyV2DraftResponse:
        return cls(
            draft_id=value.draft_id,
            revision=value.revision,
            status=value.status,
            strategy=value.strategy,
            strategy_hash=value.strategy_hash,
            provider=value.provider,
            clarification=value.clarification,
            diagnostic_code=value.diagnostic_code,
            created_at=value.created_at,
        )


class StrategyV2ValidationResponse(ApiModel):
    draft_id: str = Field(pattern=r"^draft:[A-Za-z0-9_.-]{1,128}$")
    revision: int = Field(ge=1, le=1_000_000)
    status: Literal["validated"]
    plan_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    receipt_id: str = Field(pattern=r"^receipt:[0-9a-f]{64}$")
    strategy_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    expires_at: datetime

    @classmethod
    def from_view(cls, value: StrategyV2ValidationView) -> StrategyV2ValidationResponse:
        return cls(
            draft_id=value.draft_id,
            revision=value.revision,
            status=value.status,
            plan_id=value.plan_id,
            receipt_id=value.receipt_id,
            strategy_hash=value.strategy_hash,
            expires_at=value.expires_at,
        )


class StrategyV2ExecutionResponse(ApiModel):
    run_id: str = Field(min_length=1, max_length=128)
    state: Literal["succeeded"]
    result_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    manifest_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @classmethod
    def from_view(cls, value: StrategyV2ExecutionView) -> StrategyV2ExecutionResponse:
        return cls(
            run_id=value.run_id,
            state=value.state,
            result_hash=value.result_hash,
            manifest_hash=value.manifest_hash,
        )


class StrategyV2RunResponse(ApiModel):
    run_id: str = Field(min_length=1, max_length=128)
    state: Literal["succeeded"]
    result_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    manifest_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    manifest: RunManifestV2
    result: BacktestResultBundle

    @model_validator(mode="after")
    def result_is_bound_to_manifest(self) -> StrategyV2RunResponse:
        if self.run_id != self.manifest.run_id or self.result.summary.run_id != self.run_id:
            raise ValueError("Strategy v2 result run identity is inconsistent")
        if self.manifest_hash != self.manifest.manifest_hash:
            raise ValueError("Strategy v2 manifest hash is inconsistent")
        if self.result.audit.engine_result_hash != self.manifest.engine_result_hash:
            raise ValueError("Strategy v2 result does not bind the engine result")
        if self.result.audit.result_hash is None:
            raise ValueError("Strategy v2 result bundle hash is inconsistent")
        evidence = self.result.summary.run_evidence
        producer = self.manifest.composite_snapshot
        if evidence is None or producer is None:
            raise ValueError("Strategy v2 result requires complete run evidence")
        if (
            evidence.strategy_hash != self.manifest.strategy_hash
            or evidence.catalog_hash != self.manifest.catalog_hash
            or evidence.data_snapshot_id != self.manifest.market_data.snapshot_id
            or evidence.data_snapshot_checksum != self.manifest.market_data.content_hash
            or evidence.data_schema_version != self.manifest.market_data.schema_version
            or evidence.producer_snapshot_id != producer.snapshot_id
            or evidence.producer_snapshot_schema_version != producer.schema_version
            or evidence.code_revision != self.manifest.code_revision
        ):
            raise ValueError("Strategy v2 result evidence differs from the run manifest")
        return self

    @classmethod
    def from_view(cls, value: StrategyV2RunView) -> StrategyV2RunResponse:
        # Hash the exact canonical object restored from ``StoredRunResultV2``.
        # Re-serializing through the presentation model can normalize numeric
        # values and must not become a second, incompatible storage identity.
        if canonical_hash(value.result) != value.result_hash:
            raise ValueError("Strategy v2 stored-result hash is inconsistent")
        raw_audit = value.result.get("audit")
        if not isinstance(raw_audit, Mapping):
            raise ValueError("Strategy v2 result bundle audit is missing")
        audit = cast(Mapping[object, object], raw_audit)
        result_bundle_hash = audit.get("resultHash")
        if (
            not isinstance(result_bundle_hash, str)
            or calculate_result_bundle_hash(value.result) != result_bundle_hash
        ):
            raise ValueError("Strategy v2 result bundle hash is inconsistent")
        return cls(
            run_id=value.run_id,
            state=value.state,
            result_hash=value.result_hash,
            manifest_hash=value.manifest_hash,
            manifest=value.manifest,
            result=BacktestResultBundle.model_validate(value.result),
        )


__all__ = [
    "StrategyV2DraftRefRequest",
    "StrategyV2DraftRequest",
    "StrategyV2DraftResponse",
    "StrategyV2ExecutionResponse",
    "StrategyV2RunResponse",
    "StrategyV2ValidationResponse",
]
