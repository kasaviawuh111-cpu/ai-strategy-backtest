"""Constrained dialogue turn for unresolved strategy clarifications."""

from __future__ import annotations

import json
import re
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
    natural_reply: str = Field(min_length=2, max_length=160)
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

    @field_validator("natural_reply")
    @classmethod
    def natural_reply_is_one_plain_sentence(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("natural reply must stay on one line")
        if any(token in value for token in ("```", "http://", "https://", "{", "}")):
            raise ValueError("natural reply cannot contain code, links or JSON")
        normalized = value.rstrip("。！! ")
        if not normalized:
            raise ValueError("natural reply cannot be empty after punctuation is removed")
        return normalized


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
                "不清楚或取消，并用一句简短、自然、贴合上下文的中文承接。"
                "natural_reply 可以说明你如何理解用户这句话，并明确哪些已说内容会"
                "保留、哪些不会当作确认；只能复述 priorUtterance、answer、question、"
                "contextSummary 或 allowedOptions 已出现的事实。不得新增股票、代码、"
                "指标、阈值、买卖条件、DSL、信号、数据事实、投资建议或执行结论。"
                "如果 priorUtterance 为空，这是首次对话：自然回应当前这句话，不得提"
                "‘之前’、‘刚才’、‘保留’或‘没有需要保留的内容’。"
                "语气要礼貌、像正常对话，不得称用户的内容为无关、跑题、没用或废话；"
                "可以说这句暂时还不能对应到股票或策略条件。"
                "natural_reply 只做承接，不得说‘接下来只差’、‘还差’或‘只缺’，"
                "不得在其中另问问题；不得提‘下面/下方/以下选’、点击"
                "选项或输入框等界面操作。精确的缺失项、下一问题和可选项"
                "都由服务器提供。只能返回"
                "有限 acknowledgement_id，并从 allowedOptionIds 中排序推荐 id；"
                "没有合适选项就返回空数组。"
            ),
            response_schema_name="ashare_clarification_dialogue",
            user_payload={
                "answer": request.answer,
                "priorUtterance": request.prior_utterance,
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
                "natural_reply must be one plain Chinese sentence grounded only in the "
                "supplied text, without a question mark. Never add strategy, instrument, "
                "signal, DSL, executable, reasoning, or generated-option fields. "
                "recommended_option_ids may contain only ids listed in allowedOptionIds."
            ),
            system_footer=(
                "Return one classification and natural acknowledgement object, "
                "never a strategy candidate."
            ),
        )
        try:
            payload = await self._transport.generate_json(transport_request)
            assessment = _parse(payload)
        except (CandidateTransportError, TypeError, ValueError, ValidationError):
            return None
        allowed = frozenset(allowed_ids)
        if any(item not in allowed for item in assessment.recommended_option_ids):
            return None
        if not _natural_reply_is_grounded(
            assessment.natural_reply,
            request,
            capability_matrix=self._capability_matrix,
        ):
            return None
        return ClarificationDialogueAssessment(
            reply_kind=cast(ClarificationReplyKind, assessment.reply_kind),
            acknowledgement_id=cast(
                ClarificationAcknowledgementId,
                assessment.acknowledgement_id,
            ),
            natural_reply=assessment.natural_reply,
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
        "required": [
            "reply_kind",
            "acknowledgement_id",
            "natural_reply",
            "recommended_option_ids",
        ],
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
            "natural_reply": {
                "type": "string",
                "minLength": 2,
                "maxLength": 160,
            },
            "recommended_option_ids": {
                "type": "array",
                "maxItems": 3,
                "uniqueItems": True,
                "items": id_schema,
            },
        },
    }


_FACT_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:\d{6}(?:\.(?:SH|SZ|BJ))?|\d+(?:\.\d+)?%?)(?![A-Za-z0-9])",
    re.I,
)
_UNSAFE_REPLY_RE = re.compile(
    r"(?:稳赚|保证收益|必然上涨|必然下跌|立即下单|马上下单|"
    r"立即买入|马上买入|直接买入|立即卖出|马上卖出|直接卖出|"
    r"已经执行|已执行|已经下单|已下单|开始回测|已经回测|可执行|"
    r"已经确定|已确定|已经选择|已选择|已经决定|已决定|已经采用|已采用|"
    r"改用|换成|换为|切换到|更适合|更合适|"
    r"接下来只差|现在只差|现在只缺|还差|只缺|"
    r"无关消息|无关内容|当作无关|视为无关|跑题|没用|废话|"
    r"(?:从|在)?(?:下面|下方|以下)(?:的)?(?:建议|选项)?(?:中)?(?:选|选择)|"
    r"点击(?:建议|选项)|在输入框)"
)
_NAMED_INSTRUMENT_CLAIM_RE = re.compile(
    r"(?:股票|公司|标的)(?:是|为|叫|名称是)\s*([\u4e00-\u9fff]{2,12})"
)
_CHINESE_QUANTIFIED_FACT_RE = re.compile(
    r"[零一二三四五六七八九十百千万两]+(?:个)?(?:元|块|点|倍|日|天|年|股|成)"
)
_ACTION_TERM_GROUPS = (
    ("买入", "买进", "进场", "开仓", "买"),
    ("卖出", "退出", "离场", "清仓", "卖"),
    ("持有", "持仓"),
    ("止盈",),
    ("止损",),
    ("加仓",),
    ("减仓",),
)


def _natural_reply_is_grounded(
    reply: str,
    request: ClarificationDialogueRequest,
    *,
    capability_matrix: CandidateCapabilityMatrix,
) -> bool:
    """Keep provider prose display-only and reject newly invented hard facts."""

    if "?" in reply or "？" in reply or _UNSAFE_REPLY_RE.search(reply):
        return False
    supplied = " ".join(
        (
            request.answer,
            request.prior_utterance,
            request.question,
            request.context_summary,
        )
    )
    supplied_tokens = {item.upper() for item in _FACT_TOKEN_RE.findall(supplied)}
    reply_tokens = {item.upper() for item in _FACT_TOKEN_RE.findall(reply)}
    if not reply_tokens <= supplied_tokens:
        return False
    if any(match not in supplied for match in _CHINESE_QUANTIFIED_FACT_RE.findall(reply)):
        return False
    if any(match not in supplied for match in _NAMED_INSTRUMENT_CLAIM_RE.findall(reply)):
        return False
    for aliases in _capability_term_groups(capability_matrix):
        if _contains_any(reply, aliases) and not _contains_any(supplied, aliases):
            return False
    for aliases in _ACTION_TERM_GROUPS:
        if _contains_any(reply, aliases, min_length=1) and not _contains_any(
            supplied,
            aliases,
            min_length=1,
        ):
            return False
    return True


def _capability_term_groups(
    matrix: CandidateCapabilityMatrix,
) -> tuple[tuple[str, ...], ...]:
    groups: list[tuple[str, ...]] = []
    for item in matrix.indicators:
        groups.append((item.indicator_id, *item.aliases_zh))
        groups.extend((trigger.id, *trigger.aliases_zh) for trigger in item.triggers)
    groups.extend((item.event_code, *item.aliases_zh) for item in matrix.events)
    return tuple(groups)


def _contains_any(
    text: str,
    aliases: tuple[str, ...],
    *,
    min_length: int = 2,
) -> bool:
    normalized = text.casefold()
    return any(alias.casefold() in normalized for alias in aliases if len(alias) >= min_length)
