"""Incomplete strategy rules must not erase a separately verified stock."""
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from ashare_lab.application.compile_strategy import CompileOutcome, CompileStatus
from ashare_lab.application.dialogue_state import DialogueState, VerifiedInstrumentMemory
from ashare_lab.application.dialogue_turn import DialogueTurnOrchestrator
from ashare_lab.application.turn_intent import TurnIntent
from ashare_lab.ports.candidate_generation import CandidateGroundingEvidence, CompileInput
from tests.unit.application.test_dialogue_turn import _compiler


@pytest.mark.asyncio
@pytest.mark.parametrize("name,symbol", [("东方财富", "300059.SZ"), ("同花顺", "300033.SZ")])
@pytest.mark.parametrize("diagnostic", ["strategy_rule_incomplete", "exit_rule_not_recognized"])
async def test_explicit_stock_survives_incomplete_fresh_rule(name, symbol, diagnostic):
    compiler = _compiler()
    original = CompileInput(utterance="东方财富MACD金叉买入，死叉卖出",
                            instrument_context="300059.SZ", as_of_date=date(2026, 9, 14))
    prior = await compiler.compile(original)
    state = DialogueState.project(
        draft_id=uuid4(), revision=1, compile_input=original, outcome=prior,
        created_at=datetime.now(UTC), recent_turns=(),
        pending_instrument_reuse=VerifiedInstrumentMemory(
            symbol="300059.SZ", name="东方财富", source="test",
            verified_at=datetime.now(UTC), evidence="东方财富"),
    )
    answer = f"{name}每月月初定投，然后MACD死叉卖出"
    incomplete = CompileOutcome(status=CompileStatus.NEEDS_CLARIFICATION,
                                diagnostic_code=diagnostic, clarification="请确认每次投入金额。")
    compiler.compile = AsyncMock(return_value=incomplete)
    grounding = CandidateGroundingEvidence(path="/instrument/symbol", start=0,
                                           end=len(name), text=name)
    compiler.resolve_unsupported_instrument = AsyncMock(return_value=(symbol, grounding))
    compiler.compose_dialogue_response = AsyncMock(return_value="请确认每次投入金额。")
    plan = await DialogueTurnOrchestrator(compiler)._plan_fresh_strategy(
        state=state, answer=answer, intent=TurnIntent.NEW_STRATEGY,
    )
    assert plan is not None and plan.clarification_turn is not None
    turn = plan.clarification_turn
    assert turn.compile_input.instrument_context == symbol
    assert turn.compile_input.utterance == answer
    assert turn.outcome.diagnostic_code == diagnostic
    assert plan.pending_instrument_reuse is None
    assert plan.verified_instrument.symbol == symbol
    assert grounding in turn.outcome.candidate_grounding
    compiler.resolve_unsupported_instrument.assert_awaited_once()
    compiler.compile.assert_awaited_once()  # no pointless retry for a non-identity defect


@pytest.mark.asyncio
async def test_missing_stock_recovery_does_not_invent_an_identity():
    compiler = _compiler()
    request = CompileInput(utterance="每月月初定投，MACD死叉卖出", as_of_date=date(2026, 9, 14))
    outcome = CompileOutcome(status=CompileStatus.NEEDS_CLARIFICATION,
                             diagnostic_code="strategy_rule_incomplete")
    compiler.resolve_unsupported_instrument = AsyncMock(return_value=None)
    assert await compiler.recover_unsupported_identity(request, outcome) == (request, outcome)
    compiler.resolve_unsupported_instrument.assert_awaited_once()


@pytest.mark.asyncio
async def test_existing_verified_identity_is_not_resolved_or_replaced():
    compiler = _compiler()
    request = CompileInput(utterance="每次改成100股", instrument_context="300033.SZ",
                           as_of_date=date(2026, 9, 14))
    outcome = CompileOutcome(status=CompileStatus.NEEDS_CLARIFICATION,
                             diagnostic_code="strategy_rule_incomplete")
    compiler.resolve_unsupported_instrument = AsyncMock()
    assert await compiler.recover_unsupported_identity(request, outcome) == (request, outcome)
    compiler.resolve_unsupported_instrument.assert_not_awaited()
