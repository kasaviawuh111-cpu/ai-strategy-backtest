"""Durable v1 dialogue state on the existing SQLAlchemy database engine."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import TypeAdapter
from sqlalchemy import Engine, Integer, String, Text, inspect, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.pool import StaticPool

from ashare_lab.application.compile_strategy import CompileOutcome
from ashare_lab.application.dialogue_state import (
    DialogueState,
    DialogueTurn,
    VerifiedInstrumentMemory,
)
from ashare_lab.ports.candidate_generation import CompileInput

from .backtest_review_schemas import BacktestReviewResponse
from .store import (
    DialogueTurnRecord,
    DraftNotFoundError,
    DraftRevisionStaleError,
    IdempotencyConflictError,
    StoredDraftRevision,
    StoreResult,
)

_REVISION = TypeAdapter(StoredDraftRevision)
_TURNS = TypeAdapter(tuple[DialogueTurnRecord, ...])
_REVIEW = TypeAdapter(BacktestReviewResponse)


class _Base(DeclarativeBase):
    pass


class _DraftHead(_Base):
    __tablename__ = "dialogue_drafts"

    draft_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    latest_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_version: Mapped[int] = mapped_column(Integer, nullable=False)
    turns_json: Mapped[str] = mapped_column(Text, nullable=False)


class _RevisionRow(_Base):
    __tablename__ = "dialogue_draft_revisions"

    draft_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)


class _IdempotencyRow(_Base):
    __tablename__ = "dialogue_idempotency"

    scope: Mapped[str] = mapped_column(String(64), primary_key=True)
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    # A discuss-only response can differ from its unchanged executable revision.
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)


class _ReviewRow(_Base):
    __tablename__ = "dialogue_backtest_reviews"

    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    response_hash: Mapped[str] = mapped_column(String(71), primary_key=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)


DIALOGUE_METADATA = _Base.metadata


class _ConcurrentWrite(RuntimeError):
    pass


def _transaction_retry[T](operation: Callable[[], T]) -> T:
    # A fresh transaction rechecks the public revision guard after a CAS race;
    # a duplicate idempotency insert resolves to the committed first response.
    for attempt in range(3):
        try:
            return operation()
        except (_ConcurrentWrite, IntegrityError):
            if attempt == 2:
                raise DraftRevisionStaleError("draft changed concurrently") from None
    raise AssertionError("unreachable")


def _head(session: Session, draft_id: UUID) -> _DraftHead:
    row = session.get(_DraftHead, str(draft_id))
    if row is None:
        raise DraftNotFoundError(str(draft_id))
    return row


def _latest(session: Session, head: _DraftHead) -> StoredDraftRevision:
    row = session.get(_RevisionRow, (head.draft_id, head.latest_revision))
    if row is None:
        raise DraftNotFoundError(head.draft_id)
    value = _REVISION.validate_json(row.payload_json)
    if str(value.draft_id) != head.draft_id or value.revision != head.latest_revision:
        raise ValueError("stored draft identity does not match its revision")
    return value


def _guard_revision(head: _DraftHead, expected: int | None) -> None:
    if expected is not None and head.latest_revision != expected:
        raise DraftRevisionStaleError(f"expected revision {expected}")


def _compare_and_swap(
    session: Session, head: _DraftHead, *, revision: int | None = None,
    turns: tuple[DialogueTurnRecord, ...] | None = None,
) -> None:
    changed = session.execute(
        update(_DraftHead)
        .where(_DraftHead.draft_id == head.draft_id,
               _DraftHead.storage_version == head.storage_version)
        .values(
            storage_version=head.storage_version + 1,
            latest_revision=head.latest_revision if revision is None else revision,
            turns_json=head.turns_json if turns is None else _TURNS.dump_json(turns[-20:]).decode(),
        )
        .returning(_DraftHead.storage_version)
        .execution_options(synchronize_session=False)
    ).scalar_one_or_none()
    if changed is None:
        raise _ConcurrentWrite


def _replay(
    session: Session, scope: str, key: str | None, request_hash: str,
) -> StoredDraftRevision | None:
    if key is None:
        return None
    row = session.get(_IdempotencyRow, (scope, key))
    if row is None:
        return None
    if row.request_hash != request_hash:
        raise IdempotencyConflictError(key)
    return _REVISION.validate_json(row.payload_json)


def _remember(
    session: Session, scope: str, key: str | None, request_hash: str, value: StoredDraftRevision,
) -> None:
    if key is not None:
        session.add(_IdempotencyRow(
            scope=scope, key=key, request_hash=request_hash,
            payload_json=_REVISION.dump_json(value).decode(),
        ))


def _project(value: StoredDraftRevision, turns: tuple[DialogueTurnRecord, ...]) -> DialogueState:
    return DialogueState.project(
        draft_id=value.draft_id, revision=value.revision, outcome=value.outcome,
        compile_input=value.compile_input, created_at=value.created_at,
        pending_instrument_reuse=value.pending_instrument_reuse,
        recent_turns=tuple(DialogueTurn(
            user_text=turn.user_text, assistant_text=turn.assistant_text, intent=turn.intent,
            revision=turn.revision, created_at=turn.created_at,
            verified_instrument=turn.verified_instrument,
        ) for turn in turns),
    )


class SQLAlchemyDraftStore:
    """Same asynchronous route contract; each operation reads/writes durable state."""

    def __init__(self, engine: Engine, *, initialize_schema: bool = False) -> None:
        # Parallel Sessions cannot share one DBAPI transaction/connection.
        # A file-backed SQLite engine (the local profile) is also restart-safe.
        if isinstance(engine.pool, StaticPool):
            raise ValueError("draft persistence requires a durable, multi-connection database")
        self.engine = engine
        if initialize_schema:
            DIALOGUE_METADATA.create_all(engine)
        else:
            missing = sorted(set(DIALOGUE_METADATA.tables) - set(inspect(engine).get_table_names()))
            if missing:
                raise ValueError(
                    f"draft persistence schema is missing tables: {', '.join(missing)}"
                )

    async def create(
        self, *, outcome: CompileOutcome, compile_input: CompileInput,
        request_hash: str, idempotency_key: str | None,
        parent_draft_id: UUID | None = None, expected_parent_revision: int | None = None,
        pending_instrument_reuse: VerifiedInstrumentMemory | None = None,
        preserve_parent_revision: bool = False,
    ) -> StoreResult:
        def operation() -> StoreResult:
            with Session(self.engine) as session, session.begin():
                parent = None if parent_draft_id is None else _head(session, parent_draft_id)
                if parent is not None:
                    _guard_revision(parent, expected_parent_revision)
                elif expected_parent_revision is not None:
                    raise ValueError("expected_parent_revision requires parent_draft_id")
                replay = _replay(session, "create", idempotency_key, request_hash)
                if replay is not None:
                    return StoreResult(replay, replayed=True)
                if preserve_parent_revision:
                    if parent is None:
                        raise ValueError("preserving a revision requires a parent draft")
                    value = replace(_latest(session, parent), outcome=outcome)
                else:
                    value = StoredDraftRevision(
                        draft_id=uuid4(), revision=1, outcome=outcome, compile_input=compile_input,
                        created_at=datetime.now(UTC), parent_draft_id=parent_draft_id,
                        pending_instrument_reuse=pending_instrument_reuse,
                    )
                    turns = () if parent is None else _TURNS.validate_json(parent.turns_json)
                    session.add(_DraftHead(
                        draft_id=str(value.draft_id), latest_revision=1, storage_version=1,
                        turns_json=_TURNS.dump_json(turns[-20:]).decode(),
                    ))
                    session.add(_RevisionRow(
                        draft_id=str(value.draft_id), revision=1,
                        payload_json=_REVISION.dump_json(value).decode(),
                    ))
                if parent is not None:
                    # Serializes parent validation/history copy against concurrent edits.
                    _compare_and_swap(session, parent)
                _remember(session, "create", idempotency_key, request_hash, value)
                return StoreResult(value, replayed=False)

        return await asyncio.to_thread(_transaction_retry, operation)

    async def revise(
        self, *, draft_id: UUID, outcome: CompileOutcome,
        compile_input: CompileInput | None = None, request_hash: str,
        idempotency_key: str | None, expected_revision: int | None = None,
        pending_instrument_reuse: VerifiedInstrumentMemory | None = None,
    ) -> StoreResult:
        def operation() -> StoreResult:
            with Session(self.engine) as session, session.begin():
                head = _head(session, draft_id)
                _guard_revision(head, expected_revision)
                scope = f"revise:{draft_id}"
                replay = _replay(session, scope, idempotency_key, request_hash)
                if replay is not None:
                    return StoreResult(replay, replayed=True)
                latest = _latest(session, head)
                value = StoredDraftRevision(
                    draft_id=draft_id, revision=head.latest_revision + 1, outcome=outcome,
                    compile_input=compile_input or latest.compile_input,
                    created_at=datetime.now(UTC), parent_draft_id=latest.parent_draft_id,
                    pending_instrument_reuse=pending_instrument_reuse,
                )
                _compare_and_swap(session, head, revision=value.revision)
                session.add(_RevisionRow(
                    draft_id=str(draft_id), revision=value.revision,
                    payload_json=_REVISION.dump_json(value).decode(),
                ))
                _remember(session, scope, idempotency_key, request_hash, value)
                return StoreResult(value, replayed=False)

        return await asyncio.to_thread(_transaction_retry, operation)

    async def latest_for_answer(self, *, draft_id: UUID, revision: int) -> StoredDraftRevision:
        def operation() -> StoredDraftRevision:
            with Session(self.engine) as session, session.begin():
                head = _head(session, draft_id)
                _guard_revision(head, revision)
                return _latest(session, head)

        return await asyncio.to_thread(operation)

    def _load(self, draft_id: UUID, revision: int | None) -> DialogueState:
        with Session(self.engine) as session, session.begin():
            head = _head(session, draft_id)
            _guard_revision(head, revision)
            return _project(_latest(session, head), _TURNS.validate_json(head.turns_json))

    async def load_dialogue_state(self, *, draft_id: UUID, revision: int) -> DialogueState:
        return await asyncio.to_thread(self._load, draft_id, revision)

    async def load_latest_dialogue_state(self, *, draft_id: UUID) -> DialogueState:
        return await asyncio.to_thread(self._load, draft_id, None)

    async def record_dialogue_turn(
        self, *, draft_id: UUID, user_text: str, assistant_text: str, intent: str, revision: int,
        verified_instrument: VerifiedInstrumentMemory | None = None, require_latest: bool = False,
    ) -> DialogueTurnRecord:
        record = DialogueTurnRecord(
            user_text=user_text, assistant_text=assistant_text, intent=intent, revision=revision,
            created_at=datetime.now(UTC), verified_instrument=verified_instrument,
        )

        def operation() -> DialogueTurnRecord:
            with Session(self.engine) as session, session.begin():
                head = _head(session, draft_id)
                if revision > head.latest_revision or (require_latest
                                                       and revision != head.latest_revision):
                    raise DraftRevisionStaleError(f"latest revision is {head.latest_revision}")
                turns = _TURNS.validate_json(head.turns_json)
                _compare_and_swap(session, head, turns=(*turns, record))
                return record

        return await asyncio.to_thread(_transaction_retry, operation)

    async def dialogue_history(
        self, *, draft_id: UUID, limit: int = 20,
    ) -> tuple[DialogueTurnRecord, ...]:
        if not 1 <= limit <= 20:
            raise ValueError("dialogue history limit must be between 1 and 20")

        def operation() -> tuple[DialogueTurnRecord, ...]:
            with Session(self.engine) as session:
                return _TURNS.validate_json(_head(session, draft_id).turns_json)[-limit:]

        return await asyncio.to_thread(operation)

    async def remember_review(self, review: BacktestReviewResponse) -> None:
        def operation() -> None:
            with Session(self.engine) as session, session.begin():
                key = (review.run_id, review.model_provenance.response_hash)
                row = session.get(_ReviewRow, key)
                payload = _REVIEW.dump_json(review).decode()
                if row is not None:
                    saved = _REVIEW.validate_json(row.payload_json)
                    if saved.model_dump(exclude={"generated_at"}) != review.model_dump(
                        exclude={"generated_at"},
                    ):
                        raise ValueError("review identity already contains different content")
                    return
                session.add(_ReviewRow(run_id=key[0], response_hash=key[1], payload_json=payload))

        await asyncio.to_thread(_transaction_retry, operation)

    async def get_review(self, run_id: str, response_hash: str) -> BacktestReviewResponse | None:
        def operation() -> BacktestReviewResponse | None:
            with Session(self.engine) as session:
                row = session.get(_ReviewRow, (run_id, response_hash))
                return None if row is None else _REVIEW.validate_json(row.payload_json)

        return await asyncio.to_thread(operation)
