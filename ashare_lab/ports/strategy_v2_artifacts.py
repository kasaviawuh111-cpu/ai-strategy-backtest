"""Durable append-only storage boundary for Strategy v2 audit artifacts."""

from __future__ import annotations

from typing import Protocol

from ashare_lab.domain.runs.models_v2 import (
    DraftRevisionV2,
    ExecutableStrategyPlanRecordV2,
    RunManifestV2,
    StoredRunResultV2,
    StoredValidationReceiptV2,
)


class StrategyV2ArtifactStore(Protocol):
    def append_draft_revision(self, draft: DraftRevisionV2) -> None: ...

    def get_draft_revision(self, draft_id: str, revision: int) -> DraftRevisionV2 | None: ...

    def append_validated_plan(
        self,
        plan: ExecutableStrategyPlanRecordV2,
        receipt: StoredValidationReceiptV2,
    ) -> None: ...

    def get_plan(self, plan_id: str) -> ExecutableStrategyPlanRecordV2 | None: ...

    def get_validation_receipt(
        self,
        receipt_id: str,
    ) -> StoredValidationReceiptV2 | None: ...

    def get_validation_receipt_for_draft_revision(
        self,
        draft_id: str,
        revision: int,
    ) -> StoredValidationReceiptV2 | None: ...

    def append_run_manifest(self, manifest: RunManifestV2) -> None: ...

    def get_run_manifest(self, run_id: str) -> RunManifestV2 | None: ...

    def append_run_result(self, result: StoredRunResultV2) -> None: ...

    def append_completed_run(
        self,
        manifest: RunManifestV2,
        result: StoredRunResultV2,
    ) -> None: ...

    def get_run_result(self, run_id: str) -> StoredRunResultV2 | None: ...


__all__ = ["StrategyV2ArtifactStore"]
