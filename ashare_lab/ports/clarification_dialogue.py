"""Bounded provider boundary for one clarification answer.

The provider may classify an answer, acknowledge it, rank existing choices,
suggest a display-only creative style, and extract a named security. Security
resolution and executable strategy validation remain separate server gates.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from ashare_lab.ports.current_fact_research import CurrentFactResearchResult

type ClarificationReplyKind = Literal[
    "off_topic",
    "preference",
    "question",
    "unclear",
    "cancelled",
]
type ClarificationAcknowledgementId = Literal[
    "light_redirect",
    "respect_preference",
    "answer_question",
    "ask_rephrase",
    "confirm_cancel",
]


@dataclass(frozen=True, slots=True)
class ClarificationOption:
    id: str
    title: str
    preview: str


@dataclass(frozen=True, slots=True)
class ClarificationDialogueTurn:
    """One server-recorded exchange supplied as bounded dialogue context."""

    user_text: str
    assistant_text: str
    intent: str
    revision: int
    created_at: datetime
    verified_instrument: Mapping[str, str | None] | None = None


@dataclass(frozen=True, slots=True)
class ClarificationDialogueRequest:
    answer: str
    prior_utterance: str
    diagnostic_code: str
    question: str
    context_summary: str
    options: tuple[ClarificationOption, ...]
    recent_turns: tuple[ClarificationDialogueTurn, ...] = ()
    response_only: bool = False
    research: CurrentFactResearchResult | None = None
    allow_data_query: bool = False
    identity_only: bool = False
    verified_instruments: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ClarificationDialogueAssessment:
    reply_kind: ClarificationReplyKind
    acknowledgement_id: ClarificationAcknowledgementId
    natural_reply: str
    recommended_option_ids: tuple[str, ...] = ()
    strategy_inspiration: str | None = None
    instrument_name: str | None = None
    instrument_selected: bool = False
    selected_option_id: str | None = None
    requires_new_data: bool = False
    run_requested: bool | None = None
    run_request_evidence: str | None = None


class ClarificationDialogueRouter(Protocol):
    async def assess(
        self,
        request: ClarificationDialogueRequest,
    ) -> ClarificationDialogueAssessment | None: ...
