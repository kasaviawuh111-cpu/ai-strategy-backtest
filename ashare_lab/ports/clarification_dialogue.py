"""Bounded provider boundary for one clarification answer.

The provider may classify an answer, write one display-only acknowledgement,
and rank server-owned choice ids.  The server still owns the missing question,
strategy text, securities, signals, and every execution decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

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
class ClarificationDialogueRequest:
    answer: str
    prior_utterance: str
    diagnostic_code: str
    question: str
    context_summary: str
    options: tuple[ClarificationOption, ...]


@dataclass(frozen=True, slots=True)
class ClarificationDialogueAssessment:
    reply_kind: ClarificationReplyKind
    acknowledgement_id: ClarificationAcknowledgementId
    natural_reply: str
    recommended_option_ids: tuple[str, ...] = ()


class ClarificationDialogueRouter(Protocol):
    async def assess(
        self,
        request: ClarificationDialogueRequest,
    ) -> ClarificationDialogueAssessment | None: ...
