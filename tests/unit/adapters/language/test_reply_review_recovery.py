"""Review protocol recovery cannot turn a valid rejection into acceptance."""
from datetime import date
from unittest.mock import AsyncMock

import pytest

from ashare_lab.adapters.language.reply_semantic_review import (
    TABLE_ENCODING_CONTRACT,
    review_display_semantics,
)
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


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict,accepted", [("supported", True), ("unsupported", False)])
async def test_review_keeps_each_encoded_tables_own_column_order_and_rejection(verdict, accepted):
    # Provider display columns may differ from the order encoded for each table.
    # Nulls and separate field dates are evidence, not data to fill or reorder.
    evidence = {
        "columns": ["代码", "PE(倍) 2026-09-15"],
        "verifiedRows": {
            "encoding": "columnar.v1",
            "columns": ["PE(倍) 2026-09-15", "代码"],
            "rows": [[2.38, "600841"], [None, "300059"]],
        },
        "supplementalData": [{
            "encoding": "columnar.v1",
            "columns": ["代码", "ROE(%) 2026-06-30"],
            "rows": [["300059", 9.0], ["600841", None]],
        }],
    }
    transport = AsyncMock()
    transport.generate_json.return_value = {
        "facts": verdict, "state_and_authority": "supported",
        "user_intent_and_tone": "supported",
    }
    request = CandidateTransportRequest(
        utterance="低PE股票研究", instrument_context=None, as_of_date=date(2026, 9, 15),
        max_candidates=1, response_schema={}, capability_matrix={},
        system_contract="fixture", capability_projection_version="test",
        capability_projection_hash="test",
    )
    assert await review_display_semantics(
        transport, request, display_payload={"reason": "市盈率为2.38倍，仅作为待测候选。"},
        verified_context=evidence,
        response_scope="按实际返回指标核对研究候选，不证明历史回测能力。",
    ) is accepted
    transport.generate_json.assert_awaited_once()
    sent = transport.generate_json.await_args.args[0]
    assert TABLE_ENCODING_CONTRACT in sent.system_contract
    assert "不能套用外层或其他表的列顺序" in sent.system_contract
    for key, value in evidence.items():
        assert sent.user_payload[key] == value
