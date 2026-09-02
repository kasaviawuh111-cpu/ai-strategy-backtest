from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from ashare_lab.adapters.language.vibe_candidates import (
    CandidateCapabilityMatrix,
    CandidateTransportError,
    CandidateTransportRequest,
    CandidateTransportResponse,
    build_candidate_capability_matrix,
)
from ashare_lab.adapters.language.vibe_clarification import VibeClarificationDialogueRouter
from ashare_lab.domain.catalog import load_catalog_directory, load_coverage_catalog_directory
from ashare_lab.ports.clarification_dialogue import (
    ClarificationDialogueRequest,
    ClarificationOption,
)

ROOT = Path(__file__).parents[4]


class _RecordingTransport:
    def __init__(self, response: object) -> None:
        self.response = response
        self.requests: list[CandidateTransportRequest] = []

    async def generate_json(
        self,
        request: CandidateTransportRequest,
    ) -> CandidateTransportResponse:
        self.requests.append(request)
        if isinstance(self.response, CandidateTransportError):
            raise self.response
        return cast(CandidateTransportResponse, self.response)


@pytest.fixture
def capability_matrix() -> CandidateCapabilityMatrix:
    return build_candidate_capability_matrix(
        load_catalog_directory(ROOT / "catalogs"),
        load_coverage_catalog_directory(ROOT / "catalogs" / "coverage"),
    )


def _request() -> ClarificationDialogueRequest:
    return ClarificationDialogueRequest(
        answer="我是你爸",
        prior_utterance="东方财富MACD金叉买入",
        diagnostic_code="exit_rule_not_recognized",
        question="什么时候卖？",
        context_summary="用户已经说清了进场条件。",
        options=(
            ClarificationOption(id="idea_000000000001", title="动能转弱", preview="候选一"),
            ClarificationOption(id="idea_000000000002", title="持有到期", preview="候选二"),
        ),
    )


@pytest.mark.asyncio
async def test_dialogue_provider_can_only_acknowledge_and_rank_server_option_ids(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    transport = _RecordingTransport(
        {
            "reply_kind": "off_topic",
            "acknowledgement_id": "light_redirect",
            "natural_reply": "我知道你是在开玩笑，这句先不放进策略里。",
            "recommended_option_ids": ["idea_000000000002", "idea_000000000001"],
        }
    )
    router = VibeClarificationDialogueRouter(
        transport,
        capability_matrix=capability_matrix,
    )

    result = await router.assess(_request())

    assert result is not None
    assert result.reply_kind == "off_topic"
    assert result.natural_reply == "我知道你是在开玩笑，这句先不放进策略里"
    assert result.recommended_option_ids == (
        "idea_000000000002",
        "idea_000000000001",
    )
    request = transport.requests[0]
    schema = cast(dict[str, object], request.response_schema)
    properties = cast(dict[str, object], schema["properties"])
    assert request.response_schema_name == "ashare_clarification_dialogue"
    assert schema["additionalProperties"] is False
    assert "strategy" not in properties


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {
            "reply_kind": "off_topic",
            "acknowledgement_id": "light_redirect",
            "natural_reply": "这句先不放进策略里。",
            "acknowledgement": "东方财富300059用PE进场",
            "recommended_option_ids": [],
        },
        {
            "reply_kind": "preference",
            "acknowledgement_id": "respect_preference",
            "natural_reply": "我会保留你的偏好，但不会替你补规则。",
            "recommended_option_ids": ["idea_not_server_owned"],
        },
        {
            "reply_kind": "question",
            "acknowledgement_id": "answer_question",
            "natural_reply": "我先回答你的问题，不把它当成确认。",
            "recommended_option_ids": [],
            "dsl": {"executable": True},
        },
        {
            "reply_kind": "off_topic",
            "acknowledgement_id": "light_redirect",
            "natural_reply": "建议在18元立即买入。",
            "recommended_option_ids": [],
        },
        {
            "reply_kind": "preference",
            "acknowledgement_id": "respect_preference",
            "natural_reply": "我理解你想用RSI超卖。",
            "recommended_option_ids": [],
        },
        {
            "reply_kind": "preference",
            "acknowledgement_id": "respect_preference",
            "natural_reply": "我理解你说的股票是贵州茅台。",
            "recommended_option_ids": [],
        },
        {
            "reply_kind": "preference",
            "acknowledgement_id": "respect_preference",
            "natural_reply": "我理解你的阈值是八个点。",
            "recommended_option_ids": [],
        },
        {
            "reply_kind": "preference",
            "acknowledgement_id": "respect_preference",
            "natural_reply": "我理解你已经确定要持有到期卖出。",
            "recommended_option_ids": [],
        },
        {
            "reply_kind": "unclear",
            "acknowledgement_id": "ask_rephrase",
            "natural_reply": "。。",
            "recommended_option_ids": [],
        },
        {
            "reply_kind": "unclear",
            "acknowledgement_id": "ask_rephrase",
            "natural_reply": "我会保留你已经说清的部分，接下来只差卖出条件。",
            "recommended_option_ids": [],
        },
        {
            "reply_kind": "off_topic",
            "acknowledgement_id": "light_redirect",
            "natural_reply": "我会把这句当作无关消息处理。",
            "recommended_option_ids": [],
        },
        {
            "reply_kind": "unclear",
            "acknowledgement_id": "ask_rephrase",
            "natural_reply": "你可以从下面选，也可以在输入框里告诉我。",
            "recommended_option_ids": [],
        },
        {
            "reply_kind": "preference",
            "acknowledgement_id": "respect_preference",
            "natural_reply": "我理解你想用MACD死叉买入。",
            "recommended_option_ids": [],
        },
    ],
)
async def test_dialogue_provider_strategy_authority_is_rejected(
    capability_matrix: CandidateCapabilityMatrix,
    payload: dict[str, object],
) -> None:
    router = VibeClarificationDialogueRouter(
        _RecordingTransport(payload),
        capability_matrix=capability_matrix,
    )

    assert await router.assess(_request()) is None


@pytest.mark.asyncio
async def test_dialogue_provider_reply_may_reference_only_server_supplied_strategy_facts(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    router = VibeClarificationDialogueRouter(
        _RecordingTransport(
            {
                "reply_kind": "question",
                "acknowledgement_id": "answer_question",
                "natural_reply": "你是在确认MACD是否适合作为卖出条件，我先不把提问当成决定。",
                "recommended_option_ids": [],
            }
        ),
        capability_matrix=capability_matrix,
    )

    result = await router.assess(_request())

    assert result is not None
    assert "MACD" in result.natural_reply


@pytest.mark.asyncio
async def test_dialogue_provider_unavailable_returns_none_for_server_fallback(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    router = VibeClarificationDialogueRouter(
        _RecordingTransport(CandidateTransportError("timeout")),
        capability_matrix=capability_matrix,
    )

    assert await router.assess(_request()) is None
