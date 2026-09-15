"""Read-only projection of the existing strategy-draft conversation.

This module does not parse language or compile strategies.  It only projects
facts already accepted by the existing compiler and keeps a bounded dialogue
window for routing one subsequent turn.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID

from ashare_lab.application.compile_strategy import CompileOutcome, CompileStatus
from ashare_lab.domain.market_data import AshareInstrumentCodeError, normalize_a_share_instrument
from ashare_lab.ports.candidate_generation import CompileInput

_MAX_DIALOGUE_TURNS = 20


@dataclass(frozen=True, slots=True)
class VerifiedInstrumentMemory:
    """One server-verified instrument fact attached to a dialogue turn."""

    symbol: str
    source: str
    verified_at: datetime
    name: str | None = None
    evidence: str | None = None

    def __post_init__(self) -> None:
        try:
            normalized = normalize_a_share_instrument(self.symbol).value
        except AshareInstrumentCodeError as exc:
            raise ValueError("verified instrument must be a canonical A-share symbol") from exc
        if not self.source.strip():
            raise ValueError("verified instrument source must not be empty")
        if self.verified_at.tzinfo is None or self.verified_at.utcoffset() is None:
            raise ValueError("verified instrument timestamp must include a timezone")
        object.__setattr__(self, "symbol", normalized)


@dataclass(frozen=True, slots=True)
class DialogueTurn:
    """One already-recorded user/assistant exchange."""

    user_text: str
    assistant_text: str
    intent: str
    revision: int
    created_at: datetime
    verified_instrument: VerifiedInstrumentMemory | None = None


@dataclass(frozen=True, slots=True)
class DialogueState:
    """Atomic, immutable view of one current draft revision and its history."""

    draft_id: UUID
    revision: int
    compile_input: CompileInput
    outcome: CompileOutcome
    created_at: datetime
    recent_turns: tuple[DialogueTurn, ...]
    pending_instrument_reuse: VerifiedInstrumentMemory | None = None
    # Request-local, server-loaded report facts; never client-supplied metrics.
    backtest_results: tuple[Mapping[str, object], ...] = ()

    @classmethod
    def project(
        cls,
        *,
        draft_id: UUID,
        revision: int,
        compile_input: CompileInput,
        outcome: CompileOutcome,
        created_at: datetime,
        recent_turns: tuple[DialogueTurn, ...],
        pending_instrument_reuse: VerifiedInstrumentMemory | None = None,
    ) -> DialogueState:
        if revision < 1:
            raise ValueError("dialogue revision must be positive")
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("dialogue state timestamp must include a timezone")
        return cls(
            draft_id=draft_id,
            revision=revision,
            compile_input=compile_input,
            outcome=outcome,
            created_at=created_at,
            recent_turns=tuple(recent_turns[-_MAX_DIALOGUE_TURNS:]),
            pending_instrument_reuse=pending_instrument_reuse,
        )

    @property
    def verified_instrument_context(self) -> str | None:
        """Return only an instrument already present in server-owned state."""

        return verified_instrument_symbol(self.compile_input, self.outcome)

    @property
    def last_verified_instrument(self) -> VerifiedInstrumentMemory | None:
        """Return the newest structured instrument fact in the 20-turn window."""

        return next(
            (
                turn.verified_instrument
                for turn in reversed(self.recent_turns)
                if turn.verified_instrument is not None
            ),
            None,
        )

    @property
    def pending_slot(self) -> str | None:
        """Expose the compiler diagnostic as the sole authoritative open slot."""

        if self.outcome.status is not CompileStatus.NEEDS_CLARIFICATION:
            return None
        return self.outcome.diagnostic_code

    @property
    def available_option_ids(self) -> tuple[str, ...]:
        """Return only option IDs actually emitted by the current compiler outcome."""

        if (self.outcome.status is not CompileStatus.NEEDS_CLARIFICATION
                or self.outcome.diagnostic_code == "candidate_data_not_ready"):
            return ()
        option_ids: list[str] = []
        if self.outcome.idea_route is not None:
            option_ids.extend(item.id for item in self.outcome.idea_route.proposals)
        if self.outcome.suggested_strategy_choice_id is not None:
            option_ids.append(self.outcome.suggested_strategy_choice_id)
        return tuple(dict.fromkeys(option_ids))


def append_dialogue_turn(state: DialogueState, turn: DialogueTurn) -> DialogueState:
    """Pure transition used by adapters that need an updated local projection."""

    if turn.revision > state.revision:
        raise ValueError("dialogue turn cannot target a future revision")
    if turn.created_at.tzinfo is None or turn.created_at.utcoffset() is None:
        raise ValueError("dialogue turn timestamp must include a timezone")
    return replace(
        state,
        recent_turns=(*state.recent_turns, turn)[-_MAX_DIALOGUE_TURNS:],
    )


def verified_instrument_symbol(
    compile_input: CompileInput,
    outcome: CompileOutcome,
) -> str | None:
    """Project a symbol only from fields already accepted by server-owned code."""

    candidates: list[str | None] = []
    if outcome.strategy is not None:
        candidates.append(outcome.strategy.instrument.symbol)
    candidates.append(compile_input.instrument_context)
    if outcome.idea_route is not None:
        candidates.append(outcome.idea_route.asset_mapping.instrument_symbol)
    if outcome.suggested_strategy is not None:
        candidates.append(outcome.suggested_strategy.instrument.symbol)
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            return normalize_a_share_instrument(candidate).value
        except AshareInstrumentCodeError:
            continue
    return None
