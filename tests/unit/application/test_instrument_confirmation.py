"""B09 routing regressions; local fixtures are not real model/data acceptance."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from ashare_lab.adapters.language import RuleBasedCandidateGenerator
from ashare_lab.application.compile_strategy import (
    ClarificationTurnOutcome,
    CompileOutcome,
    CompileStatus,
    StrategyCompiler,
)
from ashare_lab.application.dialogue_state import DialogueState, VerifiedInstrumentMemory
from ashare_lab.application.dialogue_turn import DialogueTurnOrchestrator
from ashare_lab.domain.catalog import load_catalog_directory
from ashare_lab.ports.candidate_generation import CompileInput
from ashare_lab.ports.clarification_dialogue import (
    ClarificationDialogueAssessment,
    ClarificationDialogueRequest,
)
from ashare_lab.ports.execution_settings import ExecutionSettingsPatch
from ashare_lab.ports.instrument_resolution import InstrumentNameCandidate

ROOT = Path(__file__).parents[3]
AS_OF = date(2026, 9, 4)
FEES = ExecutionSettingsPatch(
    slippage_bps=Decimal("2"), commission_rate=Decimal("0"), minimum_commission_cny=Decimal("0"),
)


class _Dialogue:
    def __init__(self, assessment: ClarificationDialogueAssessment) -> None:
        self.assessment = assessment
        self.requests: list[ClarificationDialogueRequest] = []

    async def assess(
        self, request: ClarificationDialogueRequest,
    ) -> ClarificationDialogueAssessment:
        self.requests.append(request)
        return self.assessment


def _pending_state(*, pending_run: bool = False) -> DialogueState:
    # The reported failure had no executable draft or idea template to edit.
    return DialogueState.project(
        draft_id=uuid4(), revision=1, created_at=datetime(2026, 9, 4, tzinfo=UTC),
        compile_input=CompileInput(
            utterance=("MACD 金叉买入，死叉卖出，回测近 1 年。"
                       "滑点2基点，佣金0，最低佣金0元。股票等我补充。"),
            as_of_date=AS_OF,
        ),
        outcome=CompileOutcome(
            status=CompileStatus.NEEDS_CLARIFICATION,
            diagnostic_code="instrument_reuse_confirmation",
            clarification="是否用博硕科技，或输入你自己的股票？",
            execution_settings=FEES,
            pending_edit_run_requested=pending_run,
            pending_edit_refresh_data=pending_run,
        ),
        recent_turns=(),
        pending_instrument_reuse=VerifiedInstrumentMemory(
            symbol="300951.SZ", name="博硕科技", source="unit_fixture",
            verified_at=datetime(2026, 9, 4, tzinfo=UTC), evidence="本地单元测试候选",
        ),
    )


def _compiler(dialogue: _Dialogue, resolutions: list[str]) -> StrategyCompiler:
    def resolve(name: str) -> str:
        resolutions.append(name)
        if name == "同花顺":
            return "300033.SZ"
        raise LookupError(name)

    return StrategyCompiler(
        generator=RuleBasedCandidateGenerator(),
        catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01",
        instrument_name_resolver=resolve, clarification_dialogue_router=dialogue,
        trusted_date_provider=lambda: AS_OF, backtest_anchor_date=AS_OF,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("answer", "run_requested", "evidence", "has_recommendation"), [
    ("同花顺，先别跑", False, "先别跑", True),
    ("同花顺按原规则回测", True, "按原规则回测", True),
    ("同花顺，先别跑", False, "先别跑", False),
])
async def test_stock_selection_is_independent_of_run_permission(
    answer: str, run_requested: bool, evidence: str, has_recommendation: bool,
) -> None:
    dialogue = _Dialogue(ClarificationDialogueAssessment(
        reply_kind="preference", acknowledgement_id="respect_preference",
        natural_reply="已选择同花顺。", instrument_name="同花顺", instrument_selected=True,
        run_requested=run_requested, run_request_evidence=evidence,
    ))
    resolutions: list[str] = []
    compiler = _compiler(dialogue, resolutions)
    state = _pending_state(pending_run=not run_requested)
    if not has_recommendation:
        state = replace(
            state, pending_instrument_reuse=None,
            outcome=replace(
                state.outcome, diagnostic_code="instrument_required",
                clarification="你想用哪只股票？", instrument_suggestion_declined=True,
            ),
        )
    assert state.outcome.strategy is state.outcome.revision_base_strategy is None
    assert state.outcome.idea_route is None
    expected = await compiler.compile(replace(state.compile_input, instrument_context="300033.SZ"))
    assert expected.strategy is not None

    plan = await DialogueTurnOrchestrator(compiler).plan(state=state, answer=answer)

    turn = plan.clarification_turn
    assert turn is not None and turn.revision_changed
    assert turn.outcome.status is CompileStatus.READY
    assert turn.outcome.strategy is not None
    assert turn.outcome.strategy.instrument.symbol == "300033.SZ"
    assert turn.outcome.strategy.entry == expected.strategy.entry
    assert turn.outcome.strategy.exit == expected.strategy.exit
    assert turn.outcome.strategy.backtest == expected.strategy.backtest
    assert turn.outcome.strategy.execution == expected.strategy.execution
    assert turn.compile_input.utterance == state.compile_input.utterance
    assert turn.compile_input.as_of_date == AS_OF
    assert turn.compile_input.instrument_context == "300033.SZ"
    assert turn.outcome.execution_settings.slippage_bps == Decimal("2")
    assert turn.outcome.execution_settings.commission_rate == Decimal("0")
    assert turn.outcome.execution_settings.minimum_commission_cny == Decimal("0")
    assert turn.outcome.run_requested is run_requested
    assert not turn.outcome.pending_edit_run_requested
    assert not turn.outcome.pending_edit_refresh_data
    assert plan.pending_instrument_reuse is None
    assert resolutions == ["同花顺"]
    assert len(dialogue.requests) == 1
    assert dialogue.requests[0].answer == answer
    assert not dialogue.requests[0].response_only


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["先别跑", "取消这次换股"])
async def test_pausing_or_cancelling_does_not_bind_the_old_candidate(answer: str) -> None:
    cancelled = answer == "取消这次换股"
    dialogue = _Dialogue(ClarificationDialogueAssessment(
        reply_kind="cancelled" if cancelled else "preference",
        acknowledgement_id="confirm_cancel" if cancelled else "respect_preference",
        natural_reply="好的，先不执行。", instrument_name=None, instrument_selected=False,
        run_requested=False, run_request_evidence=answer,
    ))
    resolutions: list[str] = []
    compiler = _compiler(dialogue, resolutions)
    state = _pending_state(pending_run=True)

    plan = await DialogueTurnOrchestrator(compiler).plan(state=state, answer=answer)

    turn = plan.clarification_turn
    assert turn is not None
    assert turn.outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert turn.outcome.strategy is turn.outcome.revision_base_strategy is None
    assert turn.compile_input == state.compile_input
    assert turn.compile_input.instrument_context is None
    assert turn.outcome.execution_settings == FEES
    assert not turn.outcome.run_requested
    assert not turn.outcome.pending_edit_run_requested
    assert not turn.outcome.pending_edit_refresh_data
    assert plan.verified_instrument is None
    assert resolutions == []
    if not cancelled:
        assert turn.outcome.diagnostic_code == "instrument_reuse_confirmation"
        assert plan.pending_instrument_reuse == state.pending_instrument_reuse
    assert len(dialogue.requests) == 1
    assert dialogue.requests[0].answer == answer
    assert not dialogue.requests[0].response_only


@pytest.mark.asyncio
async def test_persona_inspiration_leaves_the_stock_prompt_and_reuses_the_assessment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer = "我是秦始皇"
    assessment = ClarificationDialogueAssessment(
        reply_kind="preference", acknowledgement_id="respect_preference",
        natural_reply="可以借这份果断劲儿，探索更主动的交易风格。",
        strategy_inspiration="将果断、主动的角色意象转成待验证的交易风格。",
    )
    dialogue = _Dialogue(assessment)
    resolutions: list[str] = []
    compiler = _compiler(dialogue, resolutions)
    state = _pending_state()
    new_outcome = CompileOutcome(
        status=CompileStatus.NEEDS_CLARIFICATION,
        diagnostic_code="idea_guidance_required",
        clarification="新策略方向已准备好，请选一种试试。",
    )
    delegated_turn = ClarificationTurnOutcome(
        reply_kind="accepted", assistant_message=new_outcome.clarification or "",
        outcome=new_outcome,
        compile_input=CompileInput(utterance=answer, as_of_date=AS_OF),
        revision_changed=True,
    )
    delegate = AsyncMock(return_value=delegated_turn)
    monkeypatch.setattr(compiler, "answer_clarification", delegate)

    plan = await DialogueTurnOrchestrator(compiler).plan(state=state, answer=answer)

    delegate.assert_awaited_once_with(
        original_input=state.compile_input, prior_outcome=state.outcome,
        answer=answer, recent_turns=(), dialogue_assessment=assessment,
    )
    assert plan.clarification_turn is delegated_turn
    assert plan.clarification_turn.outcome is new_outcome
    assert plan.pending_instrument_reuse is None
    assert plan.verified_instrument is None
    assert resolutions == []
    assert len(dialogue.requests) == 1
    assert dialogue.requests[0].answer == answer
    assert not dialogue.requests[0].response_only


@pytest.mark.asyncio
async def test_numbered_stock_choice_binds_only_the_stored_candidate_without_name_lookup() -> None:
    answer = "第二个，先别跑"
    dialogue = _Dialogue(ClarificationDialogueAssessment(
        reply_kind="preference", acknowledgement_id="respect_preference",
        natural_reply="已选择同花顺，先不回测。",
        instrument_name=None, instrument_selected=False,
        selected_option_id="instrument:300033.SZ",
        run_requested=False, run_request_evidence="先别跑",
    ))
    resolutions: list[str] = []
    compiler = _compiler(dialogue, resolutions)
    original = _pending_state(pending_run=True)
    candidates = tuple(
        InstrumentNameCandidate(symbol, name, "unit_fixture", datetime(2026, 9, 4, tzinfo=UTC))
        for symbol, name in (("300059.SZ", "东方财富"), ("300033.SZ", "同花顺"))
    )
    state = replace(
        original, pending_instrument_reuse=None,
        outcome=replace(original.outcome, diagnostic_code="instrument_required",
                        clarification="想用东方财富还是同花顺？", instrument_candidates=candidates),
    )

    plan = await DialogueTurnOrchestrator(compiler).plan(state=state, answer=answer)

    turn = plan.clarification_turn
    assert turn is not None and turn.outcome.status is CompileStatus.READY
    assert turn.outcome.strategy is not None
    assert turn.outcome.strategy.instrument.symbol == "300033.SZ"
    assert turn.compile_input.utterance == state.compile_input.utterance
    assert not turn.outcome.run_requested
    assert not turn.outcome.pending_edit_run_requested
    assert not turn.outcome.pending_edit_refresh_data
    assert turn.outcome.execution_settings.slippage_bps == FEES.slippage_bps
    assert turn.outcome.execution_settings.commission_rate == FEES.commission_rate
    assert turn.outcome.execution_settings.minimum_commission_cny == FEES.minimum_commission_cny
    assert plan.pending_instrument_reuse is None
    assert resolutions == []
    assert len(dialogue.requests) == 1
    assert tuple((option.id, option.title) for option in dialogue.requests[0].options) == (
        ("instrument:300059.SZ", "东方财富"), ("instrument:300033.SZ", "同花顺"),
    )
