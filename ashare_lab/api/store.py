"""Process-local draft repository used by the first API vertical slice."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from ashare_lab.application.compile_strategy import CompileOutcome


class DraftNotFoundError(LookupError):
    pass


class IdempotencyConflictError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class StoredDraftRevision:
    draft_id: UUID
    revision: int
    outcome: CompileOutcome
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StoreResult:
    value: StoredDraftRevision
    replayed: bool


@dataclass(frozen=True, slots=True)
class _IdempotencyRecord:
    request_hash: str
    value: StoredDraftRevision


class InMemoryDraftStore:
    """Concurrency-safe MVP store; production adapters can keep the same route contract."""

    def __init__(self) -> None:
        self._drafts: dict[UUID, list[StoredDraftRevision]] = {}
        self._idempotency: dict[tuple[str, str], _IdempotencyRecord] = {}
        self._lock = asyncio.Lock()

    async def create(
        self,
        *,
        outcome: CompileOutcome,
        request_hash: str,
        idempotency_key: str | None,
    ) -> StoreResult:
        async with self._lock:
            replay = self._find_replay("create", idempotency_key, request_hash)
            if replay is not None:
                return StoreResult(value=replay, replayed=True)

            value = StoredDraftRevision(
                draft_id=uuid4(),
                revision=1,
                outcome=outcome,
                created_at=datetime.now(UTC),
            )
            self._drafts[value.draft_id] = [value]
            self._remember("create", idempotency_key, request_hash, value)
            return StoreResult(value=value, replayed=False)

    async def revise(
        self,
        *,
        draft_id: UUID,
        outcome: CompileOutcome,
        request_hash: str,
        idempotency_key: str | None,
    ) -> StoreResult:
        async with self._lock:
            revisions = self._drafts.get(draft_id)
            if revisions is None:
                raise DraftNotFoundError(str(draft_id))

            scope = f"revise:{draft_id}"
            replay = self._find_replay(scope, idempotency_key, request_hash)
            if replay is not None:
                return StoreResult(value=replay, replayed=True)

            value = StoredDraftRevision(
                draft_id=draft_id,
                revision=len(revisions) + 1,
                outcome=outcome,
                created_at=datetime.now(UTC),
            )
            revisions.append(value)
            self._remember(scope, idempotency_key, request_hash, value)
            return StoreResult(value=value, replayed=False)

    def _find_replay(
        self,
        scope: str,
        idempotency_key: str | None,
        request_hash: str,
    ) -> StoredDraftRevision | None:
        if idempotency_key is None:
            return None
        record = self._idempotency.get((scope, idempotency_key))
        if record is None:
            return None
        if record.request_hash != request_hash:
            raise IdempotencyConflictError(idempotency_key)
        return record.value

    def _remember(
        self,
        scope: str,
        idempotency_key: str | None,
        request_hash: str,
        value: StoredDraftRevision,
    ) -> None:
        if idempotency_key is not None:
            self._idempotency[(scope, idempotency_key)] = _IdempotencyRecord(
                request_hash=request_hash,
                value=value,
            )
