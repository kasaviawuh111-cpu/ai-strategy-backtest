"""Review protocol recovery cannot turn a valid rejection into acceptance."""
from datetime import date
from unittest.mock import AsyncMock

import pytest

from ashare_lab.adapters.language.reply_semantic_review import review_display_semantics
from ashare_lab.adapters.language.vibe_candidates import CandidateTransportRequest


@pytest.mark.asyncio
@pytest.mark.parametrize("first,second,accepted,calls", [
    ({"explanation": "wrong schema"}, "supported", True, 2),
    ({"explanation": "wrong schema"}, "unsupported", False, 2),
    ({"facts": "unsupported", "state_and_authority": "supported",
      "user_intent_and_tone": "supported"}, "supported", False, 1),
    ("not json", None, False, 2),
])
async def test_review_format_retry_keeps_same_evidence(first, second, accepted, calls):
    verdict = ({"facts": second, "state_and_authority": "supported",
                "user_intent_and_tone": "supported"} if second else "still invalid")
    transport = AsyncMock()
    transport.generate_json.side_effect = [first, verdict]
    original = CandidateTransportRequest(
        utterance="研究关联业务", instrument_context=None, as_of_date=date(2026, 9, 13),
        max_candidates=1, response_schema={}, capability_matrix={},
        system_contract="fixture",
        capability_projection_version="test", capability_projection_hash="test",
    )
    assert await review_display_semantics(
        transport, original, display_payload={"reason": "真实主营业务"},
        verified_context={"business": "真实主营业务"},
        response_scope="仅审核股票候选理由，非完整聊天回复。",
    ) is accepted
    assert transport.generate_json.await_count == calls
    requests = [call.args[0] for call in transport.generate_json.await_args_list]
    assert "仅审核股票候选理由" in requests[0].system_contract
    if calls == 2:
        assert requests[0].user_payload == requests[1].user_payload
