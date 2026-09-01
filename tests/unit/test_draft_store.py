from __future__ import annotations

import asyncio
from datetime import date

import pytest

from ashare_lab.api.store import DraftRevisionStaleError, InMemoryDraftStore, StoreResult
from ashare_lab.application.compile_strategy import CompileOutcome, CompileStatus
from ashare_lab.ports.candidate_generation import CompileInput


@pytest.mark.asyncio
async def test_two_answers_to_the_same_revision_cannot_both_append() -> None:
    store = InMemoryDraftStore()
    compile_input = CompileInput(
        utterance="MACD金叉买入",
        instrument_context="300059.SZ",
        as_of_date=date(2026, 8, 20),
    )
    created = await store.create(
        outcome=CompileOutcome(
            status=CompileStatus.NEEDS_CLARIFICATION,
            diagnostic_code="exit_rule_not_recognized",
        ),
        compile_input=compile_input,
        request_hash="sha256:create",
        idempotency_key=None,
    )

    async def append(answer: str) -> StoreResult:
        return await store.revise(
            draft_id=created.value.draft_id,
            outcome=CompileOutcome(status=CompileStatus.READY),
            compile_input=CompileInput(
                utterance=f"MACD金叉买入，{answer}",
                instrument_context="300059.SZ",
                as_of_date=compile_input.as_of_date,
            ),
            request_hash=f"sha256:{answer}",
            idempotency_key=None,
            expected_revision=1,
        )

    results = await asyncio.gather(
        append("MACD死叉卖出"),
        append("持有5个交易日卖出"),
        return_exceptions=True,
    )

    assert len([item for item in results if isinstance(item, StoreResult)]) == 1
    assert len([item for item in results if isinstance(item, DraftRevisionStaleError)]) == 1
