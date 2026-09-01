"""SQLAlchemy adapter for immutable Strategy v2 audit artifacts."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    Engine,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    String,
    Text,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from ashare_lab.domain.runs.models_v2 import (
    DraftRevisionV2,
    ExecutableStrategyPlanRecordV2,
    RunManifestV2,
    StoredRunResultV2,
    StoredValidationReceiptV2,
)
from ashare_lab.domain.strategy.canonical import canonical_json
from ashare_lab.ports.strategy_v2_artifacts import StrategyV2ArtifactStore

from .immutability import install_artifact_immutability_guards


class StrategyV2ArtifactPersistenceError(RuntimeError):
    """Base error for persistent Strategy v2 artifacts."""


class ImmutableArtifactConflictError(StrategyV2ArtifactPersistenceError):
    """An append-only identity already contains different bytes."""


class MissingArtifactDependencyError(StrategyV2ArtifactPersistenceError):
    """A plan or manifest references a missing server-owned artifact."""


class _ArtifactBase(DeclarativeBase):
    pass


class _DraftRevisionRow(_ArtifactBase):
    __tablename__ = "strategy_draft_revisions_v2"

    draft_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    original_input: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    artifact_json: Mapped[str] = mapped_column(Text, nullable=False)


class _ExecutablePlanRow(_ArtifactBase):
    __tablename__ = "strategy_executable_plans_v2"
    __table_args__ = (
        ForeignKeyConstraint(
            ["draft_id", "revision"],
            ["strategy_draft_revisions_v2.draft_id", "strategy_draft_revisions_v2.revision"],
            name="fk_strategy_plans_v2_draft_revision",
        ),
    )

    plan_id: Mapped[str] = mapped_column(String(71), primary_key=True)
    draft_id: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    strategy_hash: Mapped[str] = mapped_column(String(71), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    artifact_json: Mapped[str] = mapped_column(Text, nullable=False)


class _ValidationReceiptRow(_ArtifactBase):
    __tablename__ = "strategy_validation_receipts_v2"

    receipt_id: Mapped[str] = mapped_column(String(72), primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        String(71),
        ForeignKey("strategy_executable_plans_v2.plan_id"),
        nullable=False,
        index=True,
    )
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    artifact_json: Mapped[str] = mapped_column(Text, nullable=False)


class _RunManifestV2Row(_ArtifactBase):
    __tablename__ = "backtest_run_manifests_v2"

    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    draft_id: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    receipt_id: Mapped[str] = mapped_column(
        String(72),
        ForeignKey("strategy_validation_receipts_v2.receipt_id"),
        nullable=False,
        index=True,
    )
    manifest_hash: Mapped[str] = mapped_column(String(71), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    artifact_json: Mapped[str] = mapped_column(Text, nullable=False)


class _RunResultV2Row(_ArtifactBase):
    __tablename__ = "backtest_run_results_v2"

    run_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("backtest_run_manifests_v2.run_id"),
        primary_key=True,
    )
    manifest_hash: Mapped[str] = mapped_column(String(71), nullable=False, index=True)
    result_hash: Mapped[str] = mapped_column(String(71), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    artifact_json: Mapped[str] = mapped_column(Text, nullable=False)


def _require_manifest_dependencies(session: Session, manifest: RunManifestV2) -> None:
    draft = session.get(_DraftRevisionRow, (manifest.draft_id, manifest.revision))
    receipt = session.get(_ValidationReceiptRow, manifest.validation_receipt_id)
    if draft is None or receipt is None:
        raise MissingArtifactDependencyError(
            "manifest requires an existing draft revision and validation receipt"
        )
    server_draft = DraftRevisionV2.model_validate_json(draft.artifact_json)
    server_receipt = StoredValidationReceiptV2.model_validate_json(receipt.artifact_json)
    plan = session.get(_ExecutablePlanRow, server_receipt.plan_id)
    if plan is None:
        raise MissingArtifactDependencyError("manifest validation plan is missing")
    server_plan = ExecutableStrategyPlanRecordV2.model_validate_json(plan.artifact_json)
    if (
        (manifest.draft_id, manifest.revision) != (server_draft.draft_id, server_draft.revision)
        or manifest.original_input != server_draft.original_input
        or manifest.provider != server_draft.provider
        or (server_plan.draft_id, server_plan.revision)
        != (server_draft.draft_id, server_draft.revision)
        or manifest.final_strategy_json != server_plan.strategy_json
        or manifest.plan_id != server_plan.plan_id
        or manifest.strategy_hash != server_plan.strategy_hash
        or manifest.validation_receipt_sha256 != server_receipt.token_sha256
        or manifest.catalog_hash != server_plan.catalog_hash
        or manifest.security_master != server_plan.security_master
        or manifest.trading_calendar != server_plan.trading_calendar
        or manifest.market_data != server_plan.market_data
        or manifest.composite_snapshot != server_plan.composite_snapshot
        or manifest.producer_children != server_plan.producer_children
        or manifest.code_revision != server_plan.code_revision
    ):
        raise MissingArtifactDependencyError(
            "manifest must be derived from server-owned draft, plan and receipt"
        )


def _require_result_binding(manifest: RunManifestV2, result: StoredRunResultV2) -> None:
    if (
        result.run_id != manifest.run_id
        or result.manifest_hash != manifest.manifest_hash
        or result.engine_result_hash != manifest.engine_result_hash
    ):
        raise MissingArtifactDependencyError(
            "run result must bind the server-owned manifest and engine result"
        )


def _manifest_row(manifest: RunManifestV2, payload: str) -> _RunManifestV2Row:
    return _RunManifestV2Row(
        run_id=manifest.run_id,
        draft_id=manifest.draft_id,
        revision=manifest.revision,
        receipt_id=manifest.validation_receipt_id,
        manifest_hash=manifest.manifest_hash,
        created_at=manifest.created_at,
        artifact_json=payload,
    )


def _result_row(result: StoredRunResultV2, payload: str) -> _RunResultV2Row:
    return _RunResultV2Row(
        run_id=result.run_id,
        manifest_hash=result.manifest_hash,
        result_hash=result.result_hash,
        created_at=result.created_at,
        artifact_json=payload,
    )


def _same_plan_identity(
    row: _ExecutablePlanRow,
    plan: ExecutableStrategyPlanRecordV2,
) -> bool:
    persisted = ExecutableStrategyPlanRecordV2.model_validate_json(row.artifact_json)
    # ``created_at`` is issuance audit metadata and is deliberately excluded
    # from the validator's deterministic plan_id.  A later full revalidation
    # reuses the immutable first plan row and appends only a new signed receipt.
    return persisted.model_dump(exclude={"created_at"}) == plan.model_dump(exclude={"created_at"})


STRATEGY_V2_ARTIFACT_METADATA: MetaData = _ArtifactBase.metadata


def create_strategy_v2_artifact_schema(engine: Engine) -> Engine:
    """Local/test bootstrap; production uses the equivalent Alembic revision."""

    STRATEGY_V2_ARTIFACT_METADATA.create_all(engine)
    install_artifact_immutability_guards(engine)
    return engine


class SQLAlchemyStrategyV2ArtifactStore(StrategyV2ArtifactStore):
    """Append-only store; exact retries are idempotent and rewrites fail closed."""

    def __init__(self, engine: Engine, *, initialize_schema: bool = False) -> None:
        self._engine = engine
        if initialize_schema:
            create_strategy_v2_artifact_schema(engine)

    @property
    def engine(self) -> Engine:
        return self._engine

    def append_draft_revision(self, draft: DraftRevisionV2) -> None:
        draft = DraftRevisionV2.model_validate(draft.model_dump())
        payload = canonical_json(draft)
        with Session(self._engine) as session:
            session.add(
                _DraftRevisionRow(
                    draft_id=draft.draft_id,
                    revision=draft.revision,
                    original_input=draft.original_input,
                    provider=draft.provider,
                    created_at=draft.created_at,
                    artifact_json=payload,
                )
            )
            try:
                session.commit()
                return
            except IntegrityError:
                session.rollback()
                existing = session.get(_DraftRevisionRow, (draft.draft_id, draft.revision))
                if existing is not None and existing.artifact_json == payload:
                    return
                raise ImmutableArtifactConflictError(
                    f"draft revision {draft.draft_id}/{draft.revision} is immutable"
                ) from None

    def get_draft_revision(self, draft_id: str, revision: int) -> DraftRevisionV2 | None:
        with Session(self._engine) as session:
            row = session.get(_DraftRevisionRow, (draft_id, revision))
            return None if row is None else DraftRevisionV2.model_validate_json(row.artifact_json)

    def append_validated_plan(
        self,
        plan: ExecutableStrategyPlanRecordV2,
        receipt: StoredValidationReceiptV2,
    ) -> None:
        plan = ExecutableStrategyPlanRecordV2.model_validate(plan.model_dump())
        receipt = StoredValidationReceiptV2.model_validate(receipt.model_dump())
        if receipt.plan_id != plan.plan_id:
            raise MissingArtifactDependencyError("receipt does not belong to the plan")
        plan_json = canonical_json(plan)
        receipt_json = canonical_json(receipt)
        # A deterministic plan may be fully revalidated after its old receipt
        # expires.  The plan row remains byte-identical while each newly signed
        # receipt is appended under its own content identity.
        for attempt in range(2):
            with Session(self._engine) as session:
                draft = session.get(_DraftRevisionRow, (plan.draft_id, plan.revision))
                if draft is None:
                    raise MissingArtifactDependencyError("server draft revision is missing")
                parsed_draft = DraftRevisionV2.model_validate_json(draft.artifact_json)
                if parsed_draft.provider != plan.provider:
                    raise MissingArtifactDependencyError("plan provider differs from server draft")

                existing_plan = session.get(_ExecutablePlanRow, plan.plan_id)
                if existing_plan is not None and not _same_plan_identity(existing_plan, plan):
                    raise ImmutableArtifactConflictError(
                        f"validated plan {plan.plan_id} is immutable"
                    )
                existing_receipt = session.get(_ValidationReceiptRow, receipt.receipt_id)
                if existing_receipt is not None:
                    if existing_receipt.artifact_json == receipt_json:
                        return
                    raise ImmutableArtifactConflictError(
                        f"validation receipt {receipt.receipt_id} is immutable"
                    )
                if existing_plan is None:
                    session.add(
                        _ExecutablePlanRow(
                            plan_id=plan.plan_id,
                            draft_id=plan.draft_id,
                            revision=plan.revision,
                            strategy_hash=plan.strategy_hash,
                            created_at=plan.created_at,
                            artifact_json=plan_json,
                        )
                    )
                session.add(
                    _ValidationReceiptRow(
                        receipt_id=receipt.receipt_id,
                        plan_id=receipt.plan_id,
                        issued_at=receipt.issued_at,
                        expires_at=receipt.expires_at,
                        artifact_json=receipt_json,
                    )
                )
                try:
                    session.commit()
                    return
                except IntegrityError:
                    session.rollback()
                    if attempt == 0:
                        # Another writer may have appended the same plan between
                        # our read and commit.  Re-read once and append only the
                        # still-missing receipt.
                        continue
                    existing_plan = session.get(_ExecutablePlanRow, plan.plan_id)
                    existing_receipt = session.get(_ValidationReceiptRow, receipt.receipt_id)
                    if (
                        existing_plan is not None
                        and _same_plan_identity(existing_plan, plan)
                        and existing_receipt is not None
                        and existing_receipt.artifact_json == receipt_json
                    ):
                        return
                    raise ImmutableArtifactConflictError(
                        f"validated plan {plan.plan_id} or receipt {receipt.receipt_id} conflicts"
                    ) from None

    def get_plan(self, plan_id: str) -> ExecutableStrategyPlanRecordV2 | None:
        with Session(self._engine) as session:
            row = session.get(_ExecutablePlanRow, plan_id)
            return (
                None
                if row is None
                else ExecutableStrategyPlanRecordV2.model_validate_json(row.artifact_json)
            )

    def get_validation_receipt(
        self,
        receipt_id: str,
    ) -> StoredValidationReceiptV2 | None:
        with Session(self._engine) as session:
            row = session.get(_ValidationReceiptRow, receipt_id)
            return (
                None
                if row is None
                else StoredValidationReceiptV2.model_validate_json(row.artifact_json)
            )

    def get_validation_receipt_for_draft_revision(
        self,
        draft_id: str,
        revision: int,
    ) -> StoredValidationReceiptV2 | None:
        with Session(self._engine) as session:
            row = session.scalar(
                select(_ValidationReceiptRow)
                .join(
                    _ExecutablePlanRow,
                    _ExecutablePlanRow.plan_id == _ValidationReceiptRow.plan_id,
                )
                .where(
                    _ExecutablePlanRow.draft_id == draft_id,
                    _ExecutablePlanRow.revision == revision,
                )
                .order_by(_ValidationReceiptRow.issued_at.desc())
                .limit(1)
            )
            return (
                None
                if row is None
                else StoredValidationReceiptV2.model_validate_json(row.artifact_json)
            )

    def append_run_manifest(self, manifest: RunManifestV2) -> None:
        manifest = RunManifestV2.model_validate(manifest.model_dump())
        payload = canonical_json(manifest)
        with Session(self._engine) as session:
            existing = session.get(_RunManifestV2Row, manifest.run_id)
            if existing is not None:
                if existing.artifact_json == payload:
                    return
                raise ImmutableArtifactConflictError(f"run manifest {manifest.run_id} is immutable")
            _require_manifest_dependencies(session, manifest)
            session.add(_manifest_row(manifest, payload))
            try:
                session.commit()
                return
            except IntegrityError:
                session.rollback()
                raced = session.get(_RunManifestV2Row, manifest.run_id)
                if raced is not None and raced.artifact_json == payload:
                    return
                raise ImmutableArtifactConflictError(
                    f"run manifest {manifest.run_id} is immutable"
                ) from None

    def get_run_manifest(self, run_id: str) -> RunManifestV2 | None:
        with Session(self._engine) as session:
            row = session.get(_RunManifestV2Row, run_id)
            return None if row is None else RunManifestV2.model_validate_json(row.artifact_json)

    def append_run_result(self, result: StoredRunResultV2) -> None:
        result = StoredRunResultV2.model_validate(result.model_dump())
        payload = canonical_json(result)
        with Session(self._engine) as session:
            existing = session.get(_RunResultV2Row, result.run_id)
            if existing is not None:
                if existing.artifact_json == payload:
                    return
                raise ImmutableArtifactConflictError(f"run result {result.run_id} is immutable")
            manifest_row = session.get(_RunManifestV2Row, result.run_id)
            if manifest_row is None:
                raise MissingArtifactDependencyError("run result requires an existing manifest")
            manifest = RunManifestV2.model_validate_json(manifest_row.artifact_json)
            _require_result_binding(manifest, result)
            session.add(_result_row(result, payload))
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                raced = session.get(_RunResultV2Row, result.run_id)
                if raced is not None and raced.artifact_json == payload:
                    return
                raise ImmutableArtifactConflictError(
                    f"run result {result.run_id} is immutable"
                ) from None

    def append_completed_run(
        self,
        manifest: RunManifestV2,
        result: StoredRunResultV2,
    ) -> None:
        """Atomically append the immutable manifest and its completed result."""

        manifest = RunManifestV2.model_validate(manifest.model_dump())
        result = StoredRunResultV2.model_validate(result.model_dump())
        _require_result_binding(manifest, result)
        manifest_json = canonical_json(manifest)
        result_json = canonical_json(result)
        with Session(self._engine) as session:
            existing_manifest = session.get(_RunManifestV2Row, manifest.run_id)
            existing_result = session.get(_RunResultV2Row, result.run_id)
            if existing_manifest is not None and existing_manifest.artifact_json != manifest_json:
                raise ImmutableArtifactConflictError(f"run manifest {manifest.run_id} is immutable")
            if existing_result is not None and existing_result.artifact_json != result_json:
                raise ImmutableArtifactConflictError(f"run result {result.run_id} is immutable")
            if existing_manifest is not None and existing_result is not None:
                return
            if existing_result is not None:
                raise MissingArtifactDependencyError(
                    "stored run result has no server-owned manifest"
                )
            if existing_manifest is None:
                _require_manifest_dependencies(session, manifest)
                session.add(_manifest_row(manifest, manifest_json))
            session.add(_result_row(result, result_json))
            try:
                session.commit()
                return
            except IntegrityError:
                session.rollback()
                persisted_manifest = session.get(_RunManifestV2Row, manifest.run_id)
                persisted_result = session.get(_RunResultV2Row, result.run_id)
                if (
                    persisted_manifest is not None
                    and persisted_manifest.artifact_json == manifest_json
                    and persisted_result is not None
                    and persisted_result.artifact_json == result_json
                ):
                    return
                raise ImmutableArtifactConflictError(
                    f"completed run {manifest.run_id} conflicts with immutable storage"
                ) from None

    def get_run_result(self, run_id: str) -> StoredRunResultV2 | None:
        with Session(self._engine) as session:
            row = session.get(_RunResultV2Row, run_id)
            return None if row is None else StoredRunResultV2.model_validate_json(row.artifact_json)


def copy_strategy_v2_artifact_metadata(target: MetaData) -> None:
    """Copy tables into Alembic's combined metadata without sharing row classes."""

    for table in STRATEGY_V2_ARTIFACT_METADATA.sorted_tables:
        table.to_metadata(target)


__all__ = [
    "STRATEGY_V2_ARTIFACT_METADATA",
    "ImmutableArtifactConflictError",
    "MissingArtifactDependencyError",
    "SQLAlchemyStrategyV2ArtifactStore",
    "StrategyV2ArtifactPersistenceError",
    "copy_strategy_v2_artifact_metadata",
    "create_strategy_v2_artifact_schema",
]
