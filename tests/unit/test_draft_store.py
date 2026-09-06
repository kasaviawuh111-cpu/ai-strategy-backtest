from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime

import pytest

from ashare_lab.api.store import DraftRevisionStaleError, InMemoryDraftStore, StoreResult
from ashare_lab.application.compile_strategy import CompileOutcome, CompileStatus
from ashare_lab.application.dialogue_state import VerifiedInstrumentMemory
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


@pytest.mark.asyncio
async def test_dialogue_history_is_structured_and_bounded_to_twenty_rounds() -> None:
    store = InMemoryDraftStore()
    compile_input = CompileInput(
        utterance="东方财富MACD金叉买入",
        instrument_context="300059.SZ",
        as_of_date=date(2026, 8, 20),
    )
    created = await store.create(
        outcome=CompileOutcome(
            status=CompileStatus.NEEDS_CLARIFICATION,
            diagnostic_code="exit_rule_not_recognized",
        ),
        compile_input=compile_input,
        request_hash="sha256:create-history",
        idempotency_key=None,
    )

    for index in range(25):
        await store.record_dialogue_turn(
            draft_id=created.value.draft_id,
            user_text=f"user-{index}",
            assistant_text=f"assistant-{index}",
            intent="casual",
            revision=1,
        )

    history = await store.dialogue_history(draft_id=created.value.draft_id)

    assert len(history) == 20
    assert history[0].user_text == "user-5"
    assert history[-1].assistant_text == "assistant-24"
    assert all(item.created_at.tzinfo is not None for item in history)


@pytest.mark.asyncio
async def test_child_draft_inherits_only_the_latest_twenty_conversation_turns() -> None:
    store = InMemoryDraftStore()
    compile_input = CompileInput(
        utterance="300059.SZ MACD金叉买入，死叉卖出",
        instrument_context="300059.SZ",
        as_of_date=date(2026, 8, 20),
    )
    parent = await store.create(
        outcome=CompileOutcome(status=CompileStatus.READY),
        compile_input=compile_input,
        request_hash="sha256:parent",
        idempotency_key=None,
    )
    for index in range(25):
        await store.record_dialogue_turn(
            draft_id=parent.value.draft_id,
            user_text=f"parent-user-{index}",
            assistant_text=f"parent-assistant-{index}",
            intent="new_strategy",
            revision=1,
        )

    child = await store.create(
        outcome=CompileOutcome(
            status=CompileStatus.NEEDS_CLARIFICATION,
            diagnostic_code="instrument_reuse_confirmation",
        ),
        compile_input=CompileInput(
            utterance="RSI低于30买入，高于70卖出",
            instrument_context=None,
            as_of_date=compile_input.as_of_date,
        ),
        request_hash="sha256:child",
        idempotency_key=None,
        parent_draft_id=parent.value.draft_id,
        expected_parent_revision=1,
    )

    inherited = await store.dialogue_history(draft_id=child.value.draft_id)
    assert child.value.parent_draft_id == parent.value.draft_id
    assert len(inherited) == 20
    assert inherited[0].user_text == "parent-user-5"
    assert inherited[-1].user_text == "parent-user-24"

    await store.record_dialogue_turn(
        draft_id=child.value.draft_id,
        user_text="child-user",
        assistant_text="child-assistant",
        intent="new_strategy",
        revision=1,
    )
    bounded = await store.dialogue_history(draft_id=child.value.draft_id)
    assert len(bounded) == 20
    assert bounded[0].user_text == "parent-user-6"
    assert bounded[-1].user_text == "child-user"


@pytest.mark.asyncio
async def test_free_text_cannot_replace_structured_verified_instrument_memory() -> None:
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
        request_hash="sha256:structured-instrument",
        idempotency_key=None,
    )
    verified = VerifiedInstrumentMemory(
        symbol="300059.SZ",
        name="东方财富",
        source="resolver",
        verified_at=datetime(2026, 8, 20, tzinfo=UTC),
        evidence="东方财富",
    )
    await store.record_dialogue_turn(
        draft_id=created.value.draft_id,
        user_text="东方财富",
        assistant_text="已确认标的。",
        intent="change_instrument",
        revision=1,
        verified_instrument=verified,
    )
    await store.record_dialogue_turn(
        draft_id=created.value.draft_id,
        user_text="我讨厌600519.SH",
        assistant_text="这句不作为标的。",
        intent="casual",
        revision=1,
    )

    state = await store.load_dialogue_state(
        draft_id=created.value.draft_id,
        revision=1,
    )

    remembered = state.last_verified_instrument
    assert remembered == verified
    assert remembered is not None
    assert remembered.symbol == "300059.SZ"
    assert remembered.name == "东方财富"
    assert remembered.source == "resolver"
    assert remembered.evidence == "东方财富"


@pytest.mark.asyncio
async def test_load_dialogue_state_returns_one_revision_and_history_snapshot() -> None:
    store = InMemoryDraftStore()
    compile_input = CompileInput(
        utterance="东方财富MACD金叉买入",
        instrument_context="300059.SZ",
        as_of_date=date(2026, 8, 20),
    )
    created = await store.create(
        outcome=CompileOutcome(
            status=CompileStatus.NEEDS_CLARIFICATION,
            diagnostic_code="exit_rule_not_recognized",
        ),
        compile_input=compile_input,
        request_hash="sha256:create-dialogue-state",
        idempotency_key=None,
    )
    await store.record_dialogue_turn(
        draft_id=created.value.draft_id,
        user_text=compile_input.utterance,
        assistant_text="什么时候卖？",
        intent="supplement",
        revision=1,
    )

    state = await store.load_dialogue_state(
        draft_id=created.value.draft_id,
        revision=1,
    )

    assert state.revision == 1
    assert state.compile_input is compile_input
    assert state.outcome is created.value.outcome
    assert state.verified_instrument_context == "300059.SZ"
    assert state.pending_slot == "exit_rule_not_recognized"
    assert tuple(item.user_text for item in state.recent_turns) == ("东方财富MACD金叉买入",)


@pytest.mark.asyncio
async def test_load_dialogue_state_rejects_a_stale_revision() -> None:
    store = InMemoryDraftStore()
    compile_input = CompileInput(
        utterance="东方财富MACD金叉买入",
        instrument_context="300059.SZ",
        as_of_date=date(2026, 8, 20),
    )
    created = await store.create(
        outcome=CompileOutcome(
            status=CompileStatus.NEEDS_CLARIFICATION,
            diagnostic_code="exit_rule_not_recognized",
        ),
        compile_input=compile_input,
        request_hash="sha256:create-stale-dialogue-state",
        idempotency_key=None,
    )
    await store.revise(
        draft_id=created.value.draft_id,
        outcome=CompileOutcome(status=CompileStatus.READY),
        request_hash="sha256:ready",
        idempotency_key=None,
        expected_revision=1,
    )

    with pytest.raises(DraftRevisionStaleError):
        await store.load_dialogue_state(
            draft_id=created.value.draft_id,
            revision=1,
        )
    with pytest.raises(DraftRevisionStaleError):
        await store.record_dialogue_turn(
            draft_id=created.value.draft_id, revision=1,
            user_text="好，我明白了。", assistant_text="好的。", intent="supplement",
            require_latest=True,
        )
    assert await store.dialogue_history(draft_id=created.value.draft_id) == ()
