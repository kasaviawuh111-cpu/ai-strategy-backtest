from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

from ashare_lab.application.compile_strategy import CompileOutcome, CompileStatus
from ashare_lab.application.dialogue_state import (
    DialogueState,
    DialogueTurn,
    append_dialogue_turn,
)
from ashare_lab.ports.candidate_generation import CompileInput
from ashare_lab.ports.idea_routing import (
    IdeaAssetMapping,
    IdeaProposal,
    IdeaRoute,
)


def _proposal(proposal_id: str) -> IdeaProposal:
    return IdeaProposal(
        id=proposal_id,
        title=f"方案 {proposal_id}",
        hypothesis="验证一个明确假设",
        entry_summary="MACD 金叉买入",
        exit_summary="MACD 死叉卖出",
        suggested_utterance="东方财富 MACD 金叉买入，死叉卖出",
        capability_ids=("technical.macd",),
        assumptions=(),
        confidence=0.8,
    )


def test_state_projects_only_existing_compiler_facts() -> None:
    compile_input = CompileInput(
        utterance="东方财富后面会涨",
        instrument_context="300059.SZ",
        as_of_date=date(2026, 8, 20),
    )
    outcome = CompileOutcome(
        status=CompileStatus.NEEDS_CLARIFICATION,
        diagnostic_code="exit_rule_not_recognized",
        idea_route=IdeaRoute(
            understanding="用户看好东方财富",
            hypothesis="需要把观点变成可验证规则",
            asset_mapping=IdeaAssetMapping(instrument_symbol="300059.SZ"),
            proposals=(_proposal("trend"), _proposal("reversal")),
        ),
    )

    state = DialogueState.project(
        draft_id=uuid4(),
        revision=3,
        compile_input=compile_input,
        outcome=outcome,
        created_at=datetime(2026, 8, 20, tzinfo=UTC),
        recent_turns=(),
    )

    assert state.verified_instrument_context == "300059.SZ"
    assert state.pending_slot == "exit_rule_not_recognized"
    assert state.available_option_ids == ("trend", "reversal")


def test_ready_state_has_no_pending_slot() -> None:
    state = DialogueState.project(
        draft_id=uuid4(),
        revision=1,
        compile_input=CompileInput(
            utterance="东方财富 MACD 金叉买入，死叉卖出",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 20),
        ),
        outcome=CompileOutcome(
            status=CompileStatus.READY,
            diagnostic_code="stale_diagnostic_must_not_become_a_slot",
        ),
        created_at=datetime(2026, 8, 20, tzinfo=UTC),
        recent_turns=(),
    )

    assert state.pending_slot is None


def test_append_transition_is_immutable_and_bounded_to_twenty_turns() -> None:
    base = DialogueState.project(
        draft_id=uuid4(),
        revision=1,
        compile_input=CompileInput(
            utterance="MACD 金叉买入",
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 20),
        ),
        outcome=CompileOutcome(
            status=CompileStatus.NEEDS_CLARIFICATION,
            diagnostic_code="exit_rule_not_recognized",
        ),
        created_at=datetime(2026, 8, 20, tzinfo=UTC),
        recent_turns=(),
    )
    now = datetime(2026, 8, 20, tzinfo=UTC)

    current = base
    for index in range(25):
        current = append_dialogue_turn(
            current,
            DialogueTurn(
                user_text=f"user-{index}",
                assistant_text=f"assistant-{index}",
                intent="casual",
                revision=1,
                created_at=now + timedelta(seconds=index),
            ),
        )

    assert base.recent_turns == ()
    assert len(current.recent_turns) == 20
    assert current.recent_turns[0].user_text == "user-5"
    assert current.recent_turns[-1].user_text == "user-24"
