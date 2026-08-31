"""Durable and process-local implementations of the backtest run store port."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from threading import RLock

from sqlalchemy import (
    URL,
    CheckConstraint,
    DateTime,
    Engine,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    select,
    update,
)
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.pool import StaticPool

from ashare_lab.domain.shared import RunId, require_aware
from ashare_lab.ports.backtest_runs import (
    BacktestJobState,
    BacktestResultIntegrityPolicy,
    BacktestRunRecord,
    CreateRunResult,
)


class BacktestRunPersistenceError(RuntimeError):
    """Base class for stable persistence-boundary failures."""


class BacktestRunNotFoundError(BacktestRunPersistenceError, LookupError):
    """Raised when a requested run identity does not exist."""


class BacktestRunConflictError(BacktestRunPersistenceError):
    """Raised when a compare-and-swap expectation no longer holds."""


class IllegalBacktestRunTransitionError(BacktestRunPersistenceError, ValueError):
    """Raised when the requested state edge is not part of the run lifecycle."""


_ACTIVE_STATES = {
    BacktestJobState.QUEUED,
    BacktestJobState.RUNNING_DATA,
    BacktestJobState.RUNNING_SIGNAL,
    BacktestJobState.RUNNING_EXECUTION,
    BacktestJobState.RUNNING_REPORT,
}

_ALLOWED_TRANSITIONS: dict[BacktestJobState, frozenset[BacktestJobState]] = {
    BacktestJobState.QUEUED: frozenset(
        {
            BacktestJobState.QUEUED,
            BacktestJobState.RUNNING_DATA,
            BacktestJobState.CANCEL_REQUESTED,
            BacktestJobState.FAILED,
        }
    ),
    BacktestJobState.RUNNING_DATA: frozenset(
        {
            BacktestJobState.RUNNING_DATA,
            BacktestJobState.RUNNING_SIGNAL,
            BacktestJobState.CANCEL_REQUESTED,
            BacktestJobState.FAILED,
        }
    ),
    BacktestJobState.RUNNING_SIGNAL: frozenset(
        {
            BacktestJobState.RUNNING_SIGNAL,
            BacktestJobState.RUNNING_EXECUTION,
            BacktestJobState.CANCEL_REQUESTED,
            BacktestJobState.FAILED,
        }
    ),
    BacktestJobState.RUNNING_EXECUTION: frozenset(
        {
            BacktestJobState.RUNNING_EXECUTION,
            BacktestJobState.RUNNING_REPORT,
            BacktestJobState.CANCEL_REQUESTED,
            BacktestJobState.FAILED,
        }
    ),
    BacktestJobState.RUNNING_REPORT: frozenset(
        {
            BacktestJobState.RUNNING_REPORT,
            BacktestJobState.CANCEL_REQUESTED,
            BacktestJobState.SUCCEEDED,
            BacktestJobState.FAILED,
        }
    ),
    BacktestJobState.CANCEL_REQUESTED: frozenset(
        {
            BacktestJobState.CANCEL_REQUESTED,
            BacktestJobState.CANCELLED,
            BacktestJobState.FAILED,
        }
    ),
    BacktestJobState.SUCCEEDED: frozenset(),
    BacktestJobState.FAILED: frozenset(),
    BacktestJobState.CANCELLED: frozenset(),
}


class _Base(DeclarativeBase):
    pass


class _BacktestRunRow(_Base):
    __tablename__ = "backtest_runs"
    __table_args__ = (
        UniqueConstraint("fingerprint", name="uq_backtest_runs_fingerprint"),
        CheckConstraint(
            "progress_percent >= 0 AND progress_percent <= 100",
            name="ck_backtest_runs_progress",
        ),
        CheckConstraint("version >= 1", name="ck_backtest_runs_version"),
        CheckConstraint(
            "result_integrity_policy IN ('bundle_hash_v1', 'legacy_unverified')",
            name="ck_backtest_runs_result_integrity_policy",
        ),
    )

    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(71), nullable=False, index=True)
    strategy_json: Mapped[str] = mapped_column(Text, nullable=False)
    manifest_json: Mapped[str] = mapped_column(Text, nullable=False)
    config_json: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    progress_percent: Mapped[int] = mapped_column(Integer, nullable=False)
    progress_label: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_integrity_policy: Mapped[str] = mapped_column(String(32), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)


BACKTEST_RUN_METADATA: MetaData = _Base.metadata


def create_backtest_run_engine(database_url: str | URL) -> Engine:
    """Create a portable SQLAlchemy engine for SQLite or PostgreSQL.

    In-memory SQLite uses one shared connection so that separate sessions and
    worker threads observe the same schema. File SQLite permits cross-thread
    access and waits for a concurrent writer instead of failing immediately.
    """

    parsed = database_url if isinstance(database_url, URL) else make_url(database_url)
    backend = parsed.get_backend_name()
    database = parsed.database

    if backend != "sqlite":
        return create_engine(database_url, pool_pre_ping=True)

    connect_args = {"check_same_thread": False, "timeout": 30}
    if database in {None, "", ":memory:"}:
        return create_engine(
            database_url,
            connect_args=connect_args,
            poolclass=StaticPool,
        )
    return create_engine(database_url, connect_args=connect_args)


def create_backtest_run_schema(database: str | URL | Engine) -> Engine:
    """Create the run-store table for local/test bootstrap.

    Production deployments should call the equivalent Alembic migration; this
    helper deliberately does not drop or rewrite existing tables.
    """

    engine = database if isinstance(database, Engine) else create_backtest_run_engine(database)
    BACKTEST_RUN_METADATA.create_all(engine)
    return engine


def create_schema(database: str | URL | Engine) -> Engine:
    """Short, discoverable alias for local schema bootstrap."""

    return create_backtest_run_schema(database)


class InMemoryBacktestRunStore:
    """Thread-safe reference adapter with the same lifecycle rules as SQL storage."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._clock = clock
        self._by_run_id: dict[RunId, BacktestRunRecord] = {}
        self._run_id_by_fingerprint: dict[str, RunId] = {}
        self._lock = RLock()

    def create_or_get(self, record: BacktestRunRecord) -> CreateRunResult:
        with self._lock:
            existing_id = self._run_id_by_fingerprint.get(record.fingerprint)
            if existing_id is not None:
                return CreateRunResult(self._by_run_id[existing_id], replayed=True)

            existing = self._by_run_id.get(record.run_id)
            if existing is not None:
                if existing.fingerprint == record.fingerprint:
                    return CreateRunResult(existing, replayed=True)
                raise BacktestRunConflictError(
                    f"run id {record.run_id} already belongs to another fingerprint"
                )

            self._by_run_id[record.run_id] = record
            self._run_id_by_fingerprint[record.fingerprint] = record.run_id
            return CreateRunResult(record, replayed=False)

    def get(self, run_id: RunId) -> BacktestRunRecord | None:
        with self._lock:
            return self._by_run_id.get(run_id)

    def transition(
        self,
        run_id: RunId,
        *,
        expected: tuple[BacktestJobState, ...],
        target: BacktestJobState,
        progress_percent: int,
        progress_label: str,
        result_json: str | None = None,
        error_code: str | None = None,
        expected_version: int | None = None,
    ) -> BacktestRunRecord:
        with self._lock:
            current = self._by_run_id.get(run_id)
            if current is None:
                raise BacktestRunNotFoundError(str(run_id))
            updated = _build_transition(
                current,
                expected=expected,
                target=target,
                progress_percent=progress_percent,
                progress_label=progress_label,
                result_json=result_json,
                error_code=error_code,
                expected_version=expected_version,
                occurred_at=self._clock(),
            )
            self._by_run_id[run_id] = updated
            return updated

    def request_cancel(self, run_id: RunId) -> BacktestRunRecord:
        with self._lock:
            current = self._by_run_id.get(run_id)
            if current is None:
                raise BacktestRunNotFoundError(str(run_id))
            if current.state in {
                BacktestJobState.CANCEL_REQUESTED,
                BacktestJobState.CANCELLED,
            }:
                return current
            if current.state not in _ACTIVE_STATES:
                raise IllegalBacktestRunTransitionError(
                    f"cannot cancel a {current.state.value} run"
                )
            updated = _build_transition(
                current,
                expected=(current.state,),
                target=BacktestJobState.CANCEL_REQUESTED,
                progress_percent=current.progress_percent,
                progress_label="cancel_requested",
                result_json=None,
                error_code=None,
                expected_version=current.version,
                occurred_at=self._clock(),
            )
            self._by_run_id[run_id] = updated
            return updated


class SQLAlchemyBacktestRunStore:
    """SQLAlchemy 2.x adapter using atomic state/version compare-and-swap writes."""

    def __init__(
        self,
        database_url: str | URL | Engine,
        *,
        initialize_schema: bool = False,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._engine = (
            database_url
            if isinstance(database_url, Engine)
            else create_backtest_run_engine(database_url)
        )
        self._clock = clock
        if initialize_schema:
            create_backtest_run_schema(self._engine)

    @property
    def engine(self) -> Engine:
        return self._engine

    def create_or_get(self, record: BacktestRunRecord) -> CreateRunResult:
        with Session(self._engine, expire_on_commit=False) as session:
            session.add(_row_from_record(record))
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                replay = session.scalar(
                    select(_BacktestRunRow).where(_BacktestRunRow.fingerprint == record.fingerprint)
                )
                if replay is not None:
                    return CreateRunResult(_record_from_row(replay), replayed=True)
                colliding_id = session.get(_BacktestRunRow, str(record.run_id))
                if colliding_id is not None:
                    raise BacktestRunConflictError(
                        f"run id {record.run_id} already belongs to another fingerprint"
                    ) from None
                raise
        return CreateRunResult(record, replayed=False)

    def get(self, run_id: RunId) -> BacktestRunRecord | None:
        with Session(self._engine) as session:
            row = session.get(_BacktestRunRow, str(run_id))
            return None if row is None else _record_from_row(row)

    def transition(
        self,
        run_id: RunId,
        *,
        expected: tuple[BacktestJobState, ...],
        target: BacktestJobState,
        progress_percent: int,
        progress_label: str,
        result_json: str | None = None,
        error_code: str | None = None,
        expected_version: int | None = None,
    ) -> BacktestRunRecord:
        with Session(self._engine, expire_on_commit=False) as session:
            current_row = session.get(_BacktestRunRow, str(run_id))
            if current_row is None:
                raise BacktestRunNotFoundError(str(run_id))
            current = _record_from_row(current_row)
            updated = _build_transition(
                current,
                expected=expected,
                target=target,
                progress_percent=progress_percent,
                progress_label=progress_label,
                result_json=result_json,
                error_code=error_code,
                expected_version=expected_version,
                occurred_at=self._clock(),
            )
            statement = (
                update(_BacktestRunRow)
                .where(
                    _BacktestRunRow.run_id == str(run_id),
                    _BacktestRunRow.version == current.version,
                    _BacktestRunRow.state == current.state.value,
                )
                .values(**_transition_values(updated))
                .returning(_BacktestRunRow.version)
            )
            changed_version = session.execute(statement).scalar_one_or_none()
            if changed_version is None:
                session.rollback()
                raise BacktestRunConflictError(
                    f"run {run_id} changed while applying version {current.version}"
                )
            session.commit()
            return updated

    def request_cancel(self, run_id: RunId) -> BacktestRunRecord:
        # A retry converts simultaneous cancel requests into an idempotent read.
        for _attempt in range(3):
            current = self.get(run_id)
            if current is None:
                raise BacktestRunNotFoundError(str(run_id))
            if current.state in {
                BacktestJobState.CANCEL_REQUESTED,
                BacktestJobState.CANCELLED,
            }:
                return current
            if current.state not in _ACTIVE_STATES:
                raise IllegalBacktestRunTransitionError(
                    f"cannot cancel a {current.state.value} run"
                )
            try:
                return self.transition(
                    run_id,
                    expected=(current.state,),
                    target=BacktestJobState.CANCEL_REQUESTED,
                    progress_percent=current.progress_percent,
                    progress_label="cancel_requested",
                    expected_version=current.version,
                )
            except BacktestRunConflictError:
                continue
        raise BacktestRunConflictError(f"run {run_id} changed during cancellation")


def _build_transition(
    current: BacktestRunRecord,
    *,
    expected: tuple[BacktestJobState, ...],
    target: BacktestJobState,
    progress_percent: int,
    progress_label: str,
    result_json: str | None,
    error_code: str | None,
    expected_version: int | None,
    occurred_at: datetime,
) -> BacktestRunRecord:
    if not expected:
        raise ValueError("expected states cannot be empty")
    if current.state not in expected:
        expected_values = ", ".join(item.value for item in expected)
        raise BacktestRunConflictError(
            f"run is {current.state.value}; expected one of: {expected_values}"
        )
    if expected_version is not None and current.version != expected_version:
        raise BacktestRunConflictError(
            f"run is version {current.version}; expected version {expected_version}"
        )
    if target not in _ALLOWED_TRANSITIONS[current.state]:
        raise IllegalBacktestRunTransitionError(
            f"cannot transition run from {current.state.value} to {target.value}"
        )
    if progress_percent < current.progress_percent:
        raise IllegalBacktestRunTransitionError("run progress cannot decrease")

    require_aware(occurred_at, "occurred_at")
    next_time = max(_as_utc(current.updated_at), _as_utc(occurred_at))
    return replace(
        current,
        state=target,
        progress_percent=progress_percent,
        progress_label=progress_label,
        updated_at=next_time,
        result_json=result_json,
        error_code=error_code,
        version=current.version + 1,
    )


def _row_from_record(record: BacktestRunRecord) -> _BacktestRunRow:
    return _BacktestRunRow(
        run_id=str(record.run_id),
        fingerprint=record.fingerprint,
        strategy_json=record.strategy_json,
        manifest_json=record.manifest_json,
        config_json=record.config_json,
        state=record.state.value,
        progress_percent=record.progress_percent,
        progress_label=record.progress_label,
        created_at=_as_utc(record.created_at),
        updated_at=_as_utc(record.updated_at),
        result_json=record.result_json,
        result_integrity_policy=record.result_integrity_policy.value,
        error_code=record.error_code,
        version=record.version,
    )


def _record_from_row(row: _BacktestRunRow) -> BacktestRunRecord:
    return BacktestRunRecord(
        run_id=RunId(row.run_id),
        fingerprint=row.fingerprint,
        strategy_json=row.strategy_json,
        manifest_json=row.manifest_json,
        config_json=row.config_json,
        state=BacktestJobState(row.state),
        progress_percent=row.progress_percent,
        progress_label=row.progress_label,
        created_at=_timestamp_from_storage(row.created_at),
        updated_at=_timestamp_from_storage(row.updated_at),
        result_integrity_policy=BacktestResultIntegrityPolicy(row.result_integrity_policy),
        result_json=row.result_json,
        error_code=row.error_code,
        version=row.version,
    )


def _transition_values(record: BacktestRunRecord) -> dict[str, object]:
    return {
        "state": record.state.value,
        "progress_percent": record.progress_percent,
        "progress_label": record.progress_label,
        "updated_at": _as_utc(record.updated_at),
        "result_json": record.result_json,
        "error_code": record.error_code,
        "version": record.version,
    }


def _as_utc(value: datetime) -> datetime:
    require_aware(value, "persistence timestamp")
    return value.astimezone(UTC)


def _timestamp_from_storage(value: datetime) -> datetime:
    # SQLite's DateTime adapter drops offsets. Values are normalized to UTC on
    # every write, so a naive SQLite value can be restored without ambiguity.
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "BACKTEST_RUN_METADATA",
    "BacktestRunConflictError",
    "BacktestRunNotFoundError",
    "BacktestRunPersistenceError",
    "IllegalBacktestRunTransitionError",
    "InMemoryBacktestRunStore",
    "SQLAlchemyBacktestRunStore",
    "create_backtest_run_engine",
    "create_backtest_run_schema",
    "create_schema",
]
