"""Process-local draft repository used by the first API vertical slice."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from ashare_lab.application.compile_strategy import CompileOutcome
from ashare_lab.application.dialogue_state import (
    DialogueState,
    DialogueTurn,
    VerifiedInstrumentMemory,
)
from ashare_lab.ports.candidate_generation import CompileInput

from .backtest_review_schemas import BacktestReviewResponse


class DraftNotFoundError(LookupError):
    pass


class IdempotencyConflictError(ValueError):
    pass


class DraftRevisionStaleError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class StoredDraftRevision:
    draft_id: UUID
    revision: int
    outcome: CompileOutcome
    compile_input: CompileInput
    created_at: datetime
    parent_draft_id: UUID | None = None
    pending_instrument_reuse: VerifiedInstrumentMemory | None = None


@dataclass(frozen=True, slots=True)
class DialogueTurnRecord:
    """One bounded, append-only exchange associated with a draft."""

    user_text: str
    assistant_text: str
    intent: str
    revision: int
    created_at: datetime
    verified_instrument: VerifiedInstrumentMemory | None = None


@dataclass(frozen=True, slots=True)
class StoreResult:
    value: StoredDraftRevision
    replayed: bool


@dataclass(frozen=True, slots=True)
class _IdempotencyRecord:
    request_hash: str
    value: StoredDraftRevision


class DraftStore(Protocol):
    """The same draft lifecycle for isolated tests and persistent local sessions."""

    async def get_http_response(
        self, *, scope: str, key: str, request_hash: str,
    ) -> str | None: ...

    async def remember_http_response(
        self, *, scope: str, key: str, request_hash: str, payload_json: str,
    ) -> str: ...

    async def remember_review(self, review: BacktestReviewResponse) -> None: ...

    async def get_review(
        self, run_id: str, response_hash: str,
    ) -> BacktestReviewResponse | None: ...

    async def create(
        self, *, outcome: CompileOutcome, compile_input: CompileInput,
        request_hash: str, idempotency_key: str | None,
        parent_draft_id: UUID | None = None, expected_parent_revision: int | None = None,
        pending_instrument_reuse: VerifiedInstrumentMemory | None = None,
        preserve_parent_revision: bool = False,
    ) -> StoreResult: ...

    async def revise(
        self, *, draft_id: UUID, outcome: CompileOutcome,
        compile_input: CompileInput | None = None, request_hash: str,
        idempotency_key: str | None, expected_revision: int | None = None,
        pending_instrument_reuse: VerifiedInstrumentMemory | None = None,
    ) -> StoreResult: ...

    async def latest_for_answer(
        self, *, draft_id: UUID, revision: int,
    ) -> StoredDraftRevision: ...

    async def load_dialogue_state(
        self, *, draft_id: UUID, revision: int,
    ) -> DialogueState: ...

    async def load_latest_dialogue_state(self, *, draft_id: UUID) -> DialogueState: ...

    async def record_dialogue_turn(
        self, *, draft_id: UUID, user_text: str, assistant_text: str,
        intent: str, revision: int, verified_instrument: VerifiedInstrumentMemory | None = None,
        require_latest: bool = False,
    ) -> DialogueTurnRecord: ...

    async def dialogue_history(
        self, *, draft_id: UUID, limit: int = 20,
    ) -> tuple[DialogueTurnRecord, ...]: ...


class InMemoryDraftStore:
    """Concurrency-safe MVP store; production adapters can keep the same route contract."""

    def __init__(self) -> None:
        self._drafts: dict[UUID, list[StoredDraftRevision]] = {}
        self._dialogue_turns: dict[UUID, list[DialogueTurnRecord]] = {}
        self._idempotency: dict[tuple[str, str], _IdempotencyRecord] = {}
        self._reviews: dict[tuple[str, str], BacktestReviewResponse] = {}
        self._http_responses: dict[tuple[str, str], tuple[str, str]] = {}
        self._lock = asyncio.Lock()

    async def get_http_response(
        self, *, scope: str, key: str, request_hash: str,
    ) -> str | None:
        async with self._lock:
            record = self._http_responses.get((scope, key))
            if record is None:
                return None
            if record[0] != request_hash:
                raise IdempotencyConflictError(key)
            return record[1]

    async def remember_http_response(
        self, *, scope: str, key: str, request_hash: str, payload_json: str,
    ) -> str:
        async with self._lock:
            record = self._http_responses.get((scope, key))
            if record is not None:
                if record[0] != request_hash:
                    raise IdempotencyConflictError(key)
                return record[1]
            self._http_responses[(scope, key)] = (request_hash, payload_json)
            return payload_json

    async def remember_review(self, review: BacktestReviewResponse) -> None:
        async with self._lock:
            key = (review.run_id, review.model_provenance.response_hash)
            self._reviews[key] = review
            while len(self._reviews) > 128:
                self._reviews.pop(next(iter(self._reviews)))

    async def get_review(self, run_id: str, response_hash: str) -> BacktestReviewResponse | None:
        async with self._lock:
            return self._reviews.get((run_id, response_hash))

    async def create(
        self,
        *,
        outcome: CompileOutcome,
        compile_input: CompileInput,
        request_hash: str,
        idempotency_key: str | None,
        parent_draft_id: UUID | None = None,
        expected_parent_revision: int | None = None,
        pending_instrument_reuse: VerifiedInstrumentMemory | None = None,
        preserve_parent_revision: bool = False,
    ) -> StoreResult:
        async with self._lock:
            parent_revisions: list[StoredDraftRevision] | None = None
            if parent_draft_id is not None:
                parent_revisions = self._drafts.get(parent_draft_id)
                if parent_revisions is None:
                    raise DraftNotFoundError(str(parent_draft_id))
                if (
                    expected_parent_revision is not None
                    and parent_revisions[-1].revision != expected_parent_revision
                ):
                    raise DraftRevisionStaleError(
                        f"expected parent revision {expected_parent_revision}"
                    )
            elif expected_parent_revision is not None:
                raise ValueError("expected_parent_revision requires parent_draft_id")

            replay = self._find_replay("create", idempotency_key, request_hash)
            if replay is not None:
                return StoreResult(value=replay, replayed=True)

            if preserve_parent_revision:
                if parent_revisions is None:
                    raise ValueError("preserving a revision requires a parent draft")
                # Cache only the response projection, not a new executable revision.
                value = replace(parent_revisions[-1], outcome=outcome)
                self._remember("create", idempotency_key, request_hash, value)
                return StoreResult(value=value, replayed=False)

            value = StoredDraftRevision(
                draft_id=uuid4(),
                revision=1,
                outcome=outcome,
                compile_input=compile_input,
                created_at=datetime.now(UTC),
                parent_draft_id=parent_draft_id,
                pending_instrument_reuse=pending_instrument_reuse,
            )
            self._drafts[value.draft_id] = [value]
            self._dialogue_turns[value.draft_id] = (
                []
                if parent_draft_id is None
                else list(self._dialogue_turns.get(parent_draft_id, ())[-20:])
            )
            self._remember("create", idempotency_key, request_hash, value)
            return StoreResult(value=value, replayed=False)

    async def revise(
        self,
        *,
        draft_id: UUID,
        outcome: CompileOutcome,
        compile_input: CompileInput | None = None,
        request_hash: str,
        idempotency_key: str | None,
        expected_revision: int | None = None,
        pending_instrument_reuse: VerifiedInstrumentMemory | None = None,
    ) -> StoreResult:
        async with self._lock:
            revisions = self._drafts.get(draft_id)
            if revisions is None:
                raise DraftNotFoundError(str(draft_id))
            if expected_revision is not None and revisions[-1].revision != expected_revision:
                raise DraftRevisionStaleError(f"expected revision {expected_revision}")

            scope = f"revise:{draft_id}"
            replay = self._find_replay(scope, idempotency_key, request_hash)
            if replay is not None:
                return StoreResult(value=replay, replayed=True)

            value = StoredDraftRevision(
                draft_id=draft_id,
                revision=len(revisions) + 1,
                outcome=outcome,
                compile_input=compile_input or revisions[-1].compile_input,
                created_at=datetime.now(UTC),
                parent_draft_id=revisions[-1].parent_draft_id,
                pending_instrument_reuse=pending_instrument_reuse,
            )
            revisions.append(value)
            self._remember(scope, idempotency_key, request_hash, value)
            return StoreResult(value=value, replayed=False)

    async def latest_for_answer(
        self,
        *,
        draft_id: UUID,
        revision: int,
    ) -> StoredDraftRevision:
        """Return the exact latest revision so an answer cannot cross conversations."""

        async with self._lock:
            revisions = self._drafts.get(draft_id)
            if revisions is None:
                raise DraftNotFoundError(str(draft_id))
            latest = revisions[-1]
            if revision != latest.revision:
                raise DraftRevisionStaleError(f"expected revision {latest.revision}")
            return latest

    async def load_dialogue_state(
        self,
        *,
        draft_id: UUID,
        revision: int,
    ) -> DialogueState:
        """Load the latest revision and its bounded history under one lock."""

        async with self._lock:
            revisions = self._drafts.get(draft_id)
            if revisions is None:
                raise DraftNotFoundError(str(draft_id))
            latest = revisions[-1]
            if revision != latest.revision:
                raise DraftRevisionStaleError(f"expected revision {latest.revision}")
            return self._project_dialogue_state(latest)

    async def load_latest_dialogue_state(self, *, draft_id: UUID) -> DialogueState:
        """Load the current state for a server-verified parent draft."""

        async with self._lock:
            revisions = self._drafts.get(draft_id)
            if revisions is None:
                raise DraftNotFoundError(str(draft_id))
            return self._project_dialogue_state(revisions[-1])

    async def record_dialogue_turn(
        self,
        *,
        draft_id: UUID,
        user_text: str,
        assistant_text: str,
        intent: str,
        revision: int,
        verified_instrument: VerifiedInstrumentMemory | None = None,
        require_latest: bool = False,
    ) -> DialogueTurnRecord:
        """Append one exchange and retain only the latest twenty rounds."""

        record = DialogueTurnRecord(
            user_text=user_text,
            assistant_text=assistant_text,
            intent=intent,
            revision=revision,
            created_at=datetime.now(UTC),
            verified_instrument=verified_instrument,
        )
        async with self._lock:
            revisions = self._drafts.get(draft_id)
            if revisions is None:
                raise DraftNotFoundError(str(draft_id))
            if (revision > revisions[-1].revision
                    or (require_latest and revision != revisions[-1].revision)):
                raise DraftRevisionStaleError(f"latest revision is {revisions[-1].revision}")
            turns = self._dialogue_turns.setdefault(draft_id, [])
            turns.append(record)
            del turns[:-20]
        return record

    async def dialogue_history(
        self,
        *,
        draft_id: UUID,
        limit: int = 20,
    ) -> tuple[DialogueTurnRecord, ...]:
        """Return a snapshot of recent exchanges without exposing mutable state."""

        if not 1 <= limit <= 20:
            raise ValueError("dialogue history limit must be between 1 and 20")
        async with self._lock:
            if draft_id not in self._drafts:
                raise DraftNotFoundError(str(draft_id))
            return tuple(self._dialogue_turns.get(draft_id, ())[-limit:])

    def _project_dialogue_state(self, latest: StoredDraftRevision) -> DialogueState:
        turns = tuple(
            DialogueTurn(
                user_text=item.user_text,
                assistant_text=item.assistant_text,
                intent=item.intent,
                revision=item.revision,
                created_at=item.created_at,
                verified_instrument=item.verified_instrument,
            )
            for item in self._dialogue_turns.get(latest.draft_id, ())[-20:]
        )
        return DialogueState.project(
            draft_id=latest.draft_id,
            revision=latest.revision,
            compile_input=latest.compile_input,
            outcome=latest.outcome,
            created_at=latest.created_at,
            recent_turns=turns,
            pending_instrument_reuse=latest.pending_instrument_reuse,
        )

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
