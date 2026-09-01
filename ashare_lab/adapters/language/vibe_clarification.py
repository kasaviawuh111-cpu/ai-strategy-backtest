"""Constrained dialogue turn for unresolved strategy clarifications."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date
from typing import cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ashare_lab.ports.clarification_dialogue import (
    ClarificationAcknowledgementId,
    ClarificationDialogueAssessment,
    ClarificationDialogueRequest,
    ClarificationReplyKind,
)

from .vibe_candidates import (
    CandidateCapabilityMatrix,
    CandidateJsonTransport,
    CandidateTransportError,
    CandidateTransportRequest,
)


class _ProviderAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    reply_kind: str = Field(pattern=r"^(off_topic|preference|question|unclear|cancelled)$")
    acknowledgement_id: str = Field(
        pattern=r"^(light_redirect|respect_preference|answer_question|ask_rephrase|confirm_cancel)$"
    )
    recommended_option_ids: tuple[str, ...] = Field(default=(), max_length=3)

    @model_validator(mode="after")
    def acknowledgement_matches_kind(self) -> _ProviderAssessment:
        expected = {
            "off_topic": "light_redirect",
            "preference": "respect_preference",
            "question": "answer_question",
            "unclear": "ask_rephrase",
            "cancelled": "confirm_cancel",
        }[self.reply_kind]
        if self.acknowledgement_id != expected:
            raise ValueError("acknowledgement id does not match reply kind")
        return self

    @field_validator("recommended_option_ids")
    @classmethod
    def option_ids_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("recommended option ids must be unique")
        return value


class VibeClarificationDialogueRouter:
    """Let a model converse without letting it write any strategy content."""

    def __init__(
        self,
        transport: CandidateJsonTransport,
        *,
        capability_matrix: CandidateCapabilityMatrix,
    ) -> None:
        self._transport = transport
        self._capability_matrix = capability_matrix

    async def assess(
        self,
        request: ClarificationDialogueRequest,
    ) -> ClarificationDialogueAssessment | None:
        allowed_ids = tuple(item.id for item in request.options)
        transport_request = CandidateTransportRequest(
            utterance=request.answer,
            instrument_context=None,
            as_of_date=_FIXED_CONTRACT_DATE,
            max_candidates=max(1, min(3, len(allowed_ids) or 1)),
            response_schema=_response_schema(allowed_ids),
            capability_matrix=cast(
                Mapping[str, object],
                self._capability_matrix.model_dump(mode="json"),
            ),
            capability_projection_version=self._capability_matrix.schema_version,
            capability_projection_hash=self._capability_matrix.content_hash,
            system_contract=(
                "你只处理一次策略澄清对话。判断用户回答属于偏好、提问、跑题、"
                "不清楚或取消，并用一句自然中文承接。不得写股票、代码、指标、"
                "买卖条件、DSL、信号、数据事实、自由文案或执行结论。只能返回有限"
                " acknowledgement_id，并从 allowedOptionIds"
                " 中排序推荐 id；没有合适选项就返回空数组。"
            ),
            response_schema_name="ashare_clarification_dialogue",
            user_payload={
                "answer": request.answer,
                "diagnosticCode": request.diagnostic_code,
                "question": request.question,
                "contextSummary": request.context_summary,
                "allowedOptions": [
                    {"id": item.id, "title": item.title, "preview": item.preview}
                    for item in request.options
                ],
                "allowedOptionIds": list(allowed_ids),
            },
            json_object_contract=(
                " Return exactly one JSON object matching responseSchema. "
                "Never add strategy, instrument, signal, DSL, executable, reasoning, "
                "or generated-option fields. recommended_option_ids may contain only "
                "ids listed in allowedOptionIds."
            ),
            system_footer="Return one classification object, never a strategy candidate.",
        )
        try:
            payload = await self._transport.generate_json(transport_request)
            assessment = _parse(payload)
        except (CandidateTransportError, TypeError, ValueError, ValidationError):
            return None
        allowed = frozenset(allowed_ids)
        if any(item not in allowed for item in assessment.recommended_option_ids):
            return None
        return ClarificationDialogueAssessment(
            reply_kind=cast(ClarificationReplyKind, assessment.reply_kind),
            acknowledgement_id=cast(
                ClarificationAcknowledgementId,
                assessment.acknowledgement_id,
            ),
            recommended_option_ids=assessment.recommended_option_ids,
        )


_FIXED_CONTRACT_DATE = date(2000, 1, 1)


def _parse(payload: str | bytes | Mapping[str, object]) -> _ProviderAssessment:
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    if isinstance(payload, str):
        payload = json.loads(payload)
    return _ProviderAssessment.model_validate(payload)


def _response_schema(allowed_ids: tuple[str, ...]) -> Mapping[str, object]:
    id_schema: dict[str, object] = {"type": "string"}
    if allowed_ids:
        id_schema["enum"] = list(allowed_ids)
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["reply_kind", "acknowledgement_id", "recommended_option_ids"],
        "properties": {
            "reply_kind": {
                "type": "string",
                "enum": ["off_topic", "preference", "question", "unclear", "cancelled"],
            },
            "acknowledgement_id": {
                "type": "string",
                "enum": [
                    "light_redirect",
                    "respect_preference",
                    "answer_question",
                    "ask_rephrase",
                    "confirm_cancel",
                ],
            },
            "recommended_option_ids": {
                "type": "array",
                "maxItems": 3,
                "uniqueItems": True,
                "items": id_schema,
            },
        },
    }
