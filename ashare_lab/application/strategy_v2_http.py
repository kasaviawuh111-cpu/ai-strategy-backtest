"""Narrow application contract exposed by the Strategy v2 HTTP edge.

The edge deliberately receives only immutable user text or a server-owned
``draft_id`` plus ``revision``.  Concrete orchestration may load receipts,
snapshots and source references, but those authority-bearing values are never
part of the public command shapes in this module.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Protocol

from ashare_lab.application.execute_strategy_v2 import (
    ExecuteStrategyV2Error,
)
from ashare_lab.domain.instruments import AssetType, Exchange, InstrumentRef
from ashare_lab.domain.runs import (
    DraftRevisionV2,
    ExecutableStrategyPlanRecordV2,
    RunManifestV2,
    StoredRunResultV2,
    StoredValidationReceiptV2,
)
from ashare_lab.domain.strategy import StrategySpecV2
from ashare_lab.ports.strategy_v2_artifacts import StrategyV2ArtifactStore

type StrategyV2DraftStatus = Literal[
    "ready",
    "needs_clarification",
    "unsupported",
    "invalid",
]
type StrategyV2RunState = Literal["succeeded"]


class StrategyV2HttpServiceError(RuntimeError):
    """Stable public failure raised by one server-owned v2 application service."""

    def __init__(self, code: str, message: str) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_]{1,63}", code) is None:
            raise ValueError("Strategy v2 service error code is invalid")
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class StrategyV2DraftCommand:
    utterance: str
    instrument_context: str | None
    # A browser date is observation context only. Relative backtest windows are
    # anchored by the server-loaded immutable market snapshot, never this field.
    as_of_date: date | None = None


@dataclass(frozen=True, slots=True)
class StrategyV2DraftView:
    draft_id: str
    revision: int
    status: StrategyV2DraftStatus
    provider: str
    created_at: datetime
    strategy: StrategySpecV2 | None = None
    strategy_hash: str | None = None
    clarification: str | None = None
    diagnostic_code: str | None = None


@dataclass(frozen=True, slots=True)
class StrategyV2ValidationView:
    draft_id: str
    revision: int
    status: Literal["validated"]
    plan_id: str
    receipt_id: str
    strategy_hash: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class StrategyV2ExecutionView:
    run_id: str
    state: StrategyV2RunState
    result_hash: str
    manifest_hash: str


@dataclass(frozen=True, slots=True)
class StrategyV2RunView:
    run_id: str
    state: StrategyV2RunState
    result_hash: str
    manifest_hash: str
    manifest: RunManifestV2
    result: dict[str, object]


class StrategyV2HttpService(Protocol):
    """Server-side use cases required by the additive v2 HTTP router."""

    async def create_draft(self, command: StrategyV2DraftCommand) -> StrategyV2DraftView: ...

    def get_draft(self, draft_id: str, revision: int) -> StrategyV2DraftView: ...

    def validate(self, draft_id: str, revision: int) -> StrategyV2ValidationView: ...

    def execute(self, draft_id: str, revision: int) -> StrategyV2ExecutionView: ...

    def get_run(self, run_id: str) -> StrategyV2RunView: ...


class StrategyV2DraftLifecycle(Protocol):
    """Candidate-building boundary missing from the P0-C persistence schema.

    Implementations own candidate generation, candidate persistence and the
    ordered v2 validator.  The HTTP service never receives a candidate, plan,
    receipt or snapshot from its caller.
    """

    async def create_draft(self, command: StrategyV2DraftCommand) -> StrategyV2DraftView: ...

    def get_draft(self, draft_id: str, revision: int) -> StrategyV2DraftView: ...

    def validate(self, draft_id: str, revision: int) -> None: ...


class StrategyV2CompletedExecution(Protocol):
    @property
    def run_id(self) -> str: ...

    @property
    def manifest(self) -> RunManifestV2: ...


class StrategyV2ReceiptExecutor(Protocol):
    def execute(self, receipt_id: str) -> StrategyV2CompletedExecution: ...


class AssetRoutingStrategyV2ReceiptExecutor:
    """Select an attested execution policy from the stored plan identity.

    The client supplies only a receipt id. Asset type and exchange are loaded
    from the server-owned immutable plan, so an ETF can never be charged or
    matched under an equity policy by changing HTTP fields.
    """

    def __init__(
        self,
        *,
        artifacts: StrategyV2ArtifactStore,
        executors: Mapping[tuple[AssetType, Exchange], StrategyV2ReceiptExecutor],
        instrument_executors: Mapping[str, StrategyV2ReceiptExecutor] | None = None,
        executor_factory: (
            Callable[[InstrumentRef, str, str], StrategyV2ReceiptExecutor] | None
        ) = None,
    ) -> None:
        if not executors and not instrument_executors and executor_factory is None:
            raise ValueError("at least one Strategy v2 asset executor is required")
        self._artifacts = artifacts
        self._executors = dict(executors)
        self._instrument_executors = dict(instrument_executors or {})
        self._executor_factory = executor_factory

    def execute(self, receipt_id: str) -> StrategyV2CompletedExecution:
        receipt = self._artifacts.get_validation_receipt(receipt_id)
        if receipt is None:
            raise ExecuteStrategyV2Error(
                "receipt_untrusted",
                "validation receipt was not found in server storage",
            )
        plan = self._artifacts.get_plan(receipt.plan_id)
        if plan is None:
            raise ExecuteStrategyV2Error(
                "receipt_untrusted",
                "validation receipt plan was not found in server storage",
            )
        instrument = plan.strategy.instrument
        executor = self._instrument_executors.get(instrument.symbol)
        if executor is None:
            executor = self._executors.get((instrument.asset_type, instrument.exchange))
        if executor is None and self._executor_factory is not None:
            composite = plan.composite_snapshot
            if composite is None:
                raise ExecuteStrategyV2Error(
                    "capability_unavailable",
                    "validated plan does not bind an outer Composite snapshot",
                )
            executor = self._executor_factory(
                instrument,
                composite.snapshot_id,
                plan.security_master.snapshot_id,
            )
        if executor is None:
            raise ExecuteStrategyV2Error(
                "capability_unavailable",
                "server has no attested execution policy for this asset and exchange",
            )
        return executor.execute(receipt_id)


class PersistedStrategyV2HttpService:
    """HTTP-facing orchestrator that selects all execution authority server-side."""

    def __init__(
        self,
        *,
        drafts: StrategyV2DraftLifecycle,
        artifacts: StrategyV2ArtifactStore,
        executor: StrategyV2ReceiptExecutor,
    ) -> None:
        self._drafts = drafts
        self._artifacts = artifacts
        self._executor = executor

    async def create_draft(self, command: StrategyV2DraftCommand) -> StrategyV2DraftView:
        return await self._drafts.create_draft(command)

    def get_draft(self, draft_id: str, revision: int) -> StrategyV2DraftView:
        return self._drafts.get_draft(draft_id, revision)

    def validate(self, draft_id: str, revision: int) -> StrategyV2ValidationView:
        self._drafts.validate(draft_id, revision)
        draft, plan, receipt = self._load_validation(draft_id, revision)
        return StrategyV2ValidationView(
            draft_id=draft.draft_id,
            revision=draft.revision,
            status="validated",
            plan_id=plan.plan_id,
            receipt_id=receipt.receipt_id,
            strategy_hash=plan.strategy_hash,
            expires_at=receipt.expires_at,
        )

    def execute(self, draft_id: str, revision: int) -> StrategyV2ExecutionView:
        _, _, receipt = self._load_validation(draft_id, revision)
        try:
            completed = self._executor.execute(receipt.receipt_id)
        except ExecuteStrategyV2Error as error:
            raise StrategyV2HttpServiceError(error.code, str(error)) from error
        manifest, result = self._load_completed_run(completed.run_id)
        if completed.manifest != manifest:
            raise StrategyV2HttpServiceError(
                "backtest_artifact_integrity_failed",
                "executor manifest differs from the immutable stored manifest",
            )
        return StrategyV2ExecutionView(
            run_id=manifest.run_id,
            state="succeeded",
            result_hash=result.result_hash,
            manifest_hash=manifest.manifest_hash,
        )

    def get_run(self, run_id: str) -> StrategyV2RunView:
        manifest = self._artifacts.get_run_manifest(run_id)
        if manifest is None:
            raise StrategyV2HttpServiceError(
                "backtest_run_not_found",
                "backtest run was not found in server storage",
            )
        result = self._artifacts.get_run_result(run_id)
        if result is None:
            raise StrategyV2HttpServiceError(
                "backtest_result_not_ready",
                "backtest result is not yet available",
            )
        self._require_result_binding(manifest, result)
        return StrategyV2RunView(
            run_id=manifest.run_id,
            state="succeeded",
            result_hash=result.result_hash,
            manifest_hash=manifest.manifest_hash,
            manifest=manifest,
            result=result.payload,
        )

    def _load_validation(
        self,
        draft_id: str,
        revision: int,
    ) -> tuple[
        DraftRevisionV2,
        ExecutableStrategyPlanRecordV2,
        StoredValidationReceiptV2,
    ]:
        draft = self._artifacts.get_draft_revision(draft_id, revision)
        if draft is None:
            raise StrategyV2HttpServiceError(
                "strategy_draft_not_found",
                "draft revision was not found in server storage",
            )
        receipt = self._artifacts.get_validation_receipt_for_draft_revision(
            draft_id,
            revision,
        )
        if receipt is None:
            raise StrategyV2HttpServiceError(
                "draft_not_ready",
                "draft revision does not have a server-issued validation receipt",
            )
        plan = self._artifacts.get_plan(receipt.plan_id)
        if plan is None or (plan.draft_id, plan.revision) != (draft_id, revision):
            raise StrategyV2HttpServiceError(
                "receipt_untrusted",
                "stored receipt is not bound to this draft revision",
            )
        if receipt.plan_id != plan.plan_id or plan.provider != draft.provider:
            raise StrategyV2HttpServiceError(
                "receipt_untrusted",
                "stored validation artifacts disagree",
            )
        return draft, plan, receipt

    def _load_completed_run(self, run_id: str) -> tuple[RunManifestV2, StoredRunResultV2]:
        manifest = self._artifacts.get_run_manifest(run_id)
        result = self._artifacts.get_run_result(run_id)
        if manifest is None or result is None:
            raise StrategyV2HttpServiceError(
                "backtest_artifact_integrity_failed",
                "completed execution did not persist its immutable artifacts",
            )
        self._require_result_binding(manifest, result)
        return manifest, result

    @staticmethod
    def _require_result_binding(manifest: RunManifestV2, result: StoredRunResultV2) -> None:
        if (
            result.run_id != manifest.run_id
            or result.manifest_hash != manifest.manifest_hash
            or result.engine_result_hash != manifest.engine_result_hash
        ):
            raise StrategyV2HttpServiceError(
                "backtest_artifact_integrity_failed",
                "stored result does not bind the immutable run manifest",
            )


__all__ = [
    "AssetRoutingStrategyV2ReceiptExecutor",
    "PersistedStrategyV2HttpService",
    "StrategyV2CompletedExecution",
    "StrategyV2DraftCommand",
    "StrategyV2DraftLifecycle",
    "StrategyV2DraftStatus",
    "StrategyV2DraftView",
    "StrategyV2ExecutionView",
    "StrategyV2HttpService",
    "StrategyV2HttpServiceError",
    "StrategyV2ReceiptExecutor",
    "StrategyV2RunState",
    "StrategyV2RunView",
    "StrategyV2ValidationView",
]
