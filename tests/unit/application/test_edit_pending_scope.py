"""A new edit must not inherit settings or authority from an unconfirmed plan."""

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal
from unittest.mock import AsyncMock, Mock

import pytest

from ashare_lab.adapters.language import RuleBasedCandidateGenerator
from ashare_lab.application.backtest_submission import resolve_execution_settings
from ashare_lab.application.compile_strategy import CompileOutcome, CompileStatus, StrategyCompiler
from ashare_lab.domain.catalog import load_catalog_directory
from ashare_lab.domain.strategy import StrategySpec
from ashare_lab.ports.candidate_generation import CandidateProvenance, CompileInput
from ashare_lab.ports.clarification_dialogue import ClarificationOption
from ashare_lab.ports.execution_settings import ExecutionSettingsPatch
from ashare_lab.ports.strategy_editing import StrategyEditResult

Relation = Literal["continuation", "new_edit", "unclear"]
ROOT = Path(__file__).parents[3]
PROVENANCE = CandidateProvenance(
    source="bounded_provider", provider="test", model="test", prompt_version="test",
    schema_version="test", capability_projection_version="test",
    capability_projection_hash="test", upstream_pattern_commit="test", candidate_rank=0,
)


def setup_editor(
    result: StrategyEditResult,
) -> tuple[StrategyCompiler, CompileOutcome, CompileInput]:
    base = StrategySpec.model_validate_json(
        (ROOT / "contracts/examples/strategy.macd-volume.daily.v1.json").read_text(),
    )
    current = ExecutionSettingsPatch(slippage_bps=Decimal(10), commission_rate=Decimal("0.0003"))
    pending = ExecutionSettingsPatch(slippage_bps=Decimal(100))
    prior = CompileOutcome(
        status=CompileStatus.NEEDS_CLARIFICATION, revision_base_strategy=base,
        execution_settings=current, pending_execution_settings=pending,
        pending_edit_inputs=("旧的未确认提案，滑点改成100基点并重新回测",),
        pending_edit_run_requested=True, pending_edit_refresh_data=True,
        clarification="旧提案还缺什么？",
        edit_clarification_options=(ClarificationOption(
            id="edit-choice-1", title="旧选择", preview="仅属于旧提案",
        ),),
    )
    compiler = StrategyCompiler(
        generator=RuleBasedCandidateGenerator(), catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01",
        backtest_anchor_date=date(2026, 9, 7),
        strategy_editor=Mock(edit=AsyncMock(return_value=result)),
    )
    original = CompileInput(
        utterance="旧策略", instrument_context=base.instrument.symbol, as_of_date=date(2026, 9, 7),
    )
    return compiler, prior, original


@pytest.mark.asyncio
@pytest.mark.parametrize("relation", ["continuation", "new_edit", "unclear"])
@pytest.mark.parametrize("disposition", ["apply", "change_instrument"])
async def test_pending_settings_follow_resolved_scope_not_stock_mention(
    relation: Relation, disposition: Literal["apply", "change_instrument"],
) -> None:
    base = StrategySpec.model_validate_json(
        (ROOT / "contracts/examples/strategy.macd-volume.daily.v1.json").read_text(),
    )
    changed = base.model_copy(update={
        "backtest": base.backtest.model_copy(update={"initial_cash_cny": 500_000}),
    })
    result = StrategyEditResult(
        disposition=disposition, message="本轮规则已整理，暂不运行。", strategy=changed,
        provenance=PROVENANCE, pending_relation=relation,
        instrument_refs=("300033.SZ",) if disposition == "change_instrument" else (),
        execution_settings=ExecutionSettingsPatch(commission_rate=Decimal("0.0002")),
    )
    compiler, prior, original = setup_editor(result)
    turn = await compiler.edit_current_strategy(
        original_input=original, prior_outcome=prior,
        answer="300033.SZ本金改50万，佣金万二，先不运行。",
    )
    assert turn is not None and turn.outcome.status is CompileStatus.READY
    assert turn.outcome.strategy is not None
    expected_slippage = (100 if relation == "continuation"
                         or (relation == "unclear" and disposition == "change_instrument") else 10)
    assert turn.outcome.execution_settings.slippage_bps == expected_slippage
    assert turn.outcome.execution_settings.commission_rate == Decimal("0.0002")
    assert not turn.outcome.run_requested and not turn.outcome.refresh_data
    assert not turn.outcome.pending_edit_inputs and not turn.outcome.pending_edit_run_requested


@pytest.mark.asyncio
@pytest.mark.parametrize("relation", ["continuation", "new_edit", "unclear"])
async def test_clarification_does_not_reattach_superseded_pending_settings_or_run_intent(
    relation: Relation,
) -> None:
    result = StrategyEditResult(
        disposition="clarify", message="本轮还需核实当前数据口径。", strategy=None,
        provenance=PROVENANCE, pending_relation=relation,
    )
    compiler, prior, original = setup_editor(result)
    answer = "独立的新条件，先不运行。"
    turn = await compiler.edit_current_strategy(
        original_input=original, prior_outcome=prior, answer=answer,
    )
    assert turn is not None and turn.outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert turn.outcome.execution_settings == resolve_execution_settings(prior.execution_settings)
    assert not turn.outcome.run_requested and not turn.outcome.refresh_data
    if relation == "new_edit":
        assert turn.outcome.pending_execution_settings == ExecutionSettingsPatch()
        assert turn.outcome.pending_edit_inputs == (answer,)
        assert not turn.outcome.pending_edit_run_requested
        assert not turn.outcome.pending_edit_refresh_data
    else:
        assert turn.outcome.pending_execution_settings == prior.pending_execution_settings
        assert turn.outcome.pending_edit_inputs == (*prior.pending_edit_inputs, answer)
        assert turn.outcome.pending_edit_run_requested and turn.outcome.pending_edit_refresh_data


@pytest.mark.asyncio
@pytest.mark.parametrize("relation", ["continuation", "new_edit", "unclear"])
async def test_unresolved_new_stock_retains_only_the_current_pending_plan(
    relation: Relation,
) -> None:
    result = StrategyEditResult(
        disposition="change_instrument", message="核实股票后继续。", strategy=None,
        provenance=PROVENANCE, pending_relation=relation, instrument_refs=("示例股票",),
    )
    compiler, prior, original = setup_editor(result)
    # A missing resolver is an isolated identity failure, not an external request.
    answer = "换成示例股票，先不运行。"
    turn = await compiler.edit_current_strategy(
        original_input=original, prior_outcome=prior, answer=answer,
    )
    assert turn is not None and turn.outcome.status is CompileStatus.NEEDS_CLARIFICATION
    assert not turn.outcome.run_requested and not turn.outcome.pending_edit_run_requested
    expected_prior = replace(prior, pending_edit_inputs=(), pending_execution_settings=
                             ExecutionSettingsPatch()) if relation == "new_edit" else prior
    assert turn.outcome.pending_execution_settings == expected_prior.pending_execution_settings
    assert turn.outcome.pending_edit_inputs == (*expected_prior.pending_edit_inputs, answer)


@pytest.mark.asyncio
async def test_conversation_does_not_discard_a_paused_edit() -> None:
    result = StrategyEditResult(
        disposition="conversation", message="可以，先休息一下。", strategy=None,
        provenance=PROVENANCE, pending_relation="new_edit",
    )
    compiler, prior, original = setup_editor(result)
    turn = await compiler.edit_current_strategy(
        original_input=original, prior_outcome=prior, answer="先休息一下",
    )
    assert turn is not None and not turn.revision_changed
    assert turn.outcome.pending_edit_inputs == prior.pending_edit_inputs
    assert turn.outcome.pending_execution_settings == prior.pending_execution_settings
    assert not turn.outcome.run_requested and not turn.outcome.refresh_data
