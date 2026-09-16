from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest

from ashare_lab.application.compile_strategy import CompileOutcome, CompileStatus, StrategyCompiler
from ashare_lab.domain.catalog import load_catalog_directory
from ashare_lab.domain.strategy import ComposedExecutionPolicy
from ashare_lab.domain.strategy.price_plans import ScheduledPlan, ScheduledParameters
from ashare_lab.ports.candidate_generation import CandidateAst, CompileInput, IndicatorIntent
from ashare_lab.ports.idea_routing import IdeaProposal
from ashare_lab.ports.candidate_generation import FinancialIntent, EventIntent
from ashare_lab.domain.financials.models import FinancialMetricId, FinancialUnit


@pytest.mark.parametrize('entry_kind', ['grid', 'conditional', 'scheduled'])
@pytest.mark.parametrize('exit_kind', ['grid', 'conditional', 'scheduled'])
def test_independent_plan_ast_compiles_both_legs_without_changing_parameters(entry_kind, exit_kind):
    from tests.contract.api.test_independent_price_plan_contract import pair_strategy
    from ashare_lab.application.compile_strategy import _candidate_capability_ids, _strategy_capability_ids
    pair = pair_strategy(entry_kind, exit_kind).independent_plans
    original = pair.model_dump_json()
    candidate = CandidateAst(instrument_symbol='300059.SZ', entry=(), exit=(), confidence=.9,
        independent_plans=pair, initial_cash_cny=100000,
        backtest_start=date(2025, 1, 2), backtest_end=date(2025, 1, 6))
    compiler = StrategyCompiler(generator=Mock(),
        catalog=load_catalog_directory(Path(__file__).resolve().parents[3] / 'catalogs'),
        catalog_id='cn_a.signals', release_version='2026.09.01')
    template = compiler._build_strategy_template(candidate, date(2026, 9, 16))
    bound = compiler._build_strategy(candidate, date(2026, 9, 16), instrument_symbol='300059.SZ')
    assert template.bind('300059.SZ') == bound
    assert template.bind('600519.SH').independent_plans == pair
    assert bound.independent_plans.model_dump_json() == original
    assert bound.backtest.start == date(2025, 1, 2)
    assert bound.backtest.end == date(2025, 1, 6)
    assert bound.backtest.initial_cash_cny == 100000
    assert isinstance(bound.execution, ComposedExecutionPolicy)
    assert bound.entry is None and bound.exit is None and bound.trading_plan is None
    assert set(_candidate_capability_ids(candidate)) == {f'strategy.{entry_kind}', f'strategy.{exit_kind}'}
    assert _candidate_capability_ids(candidate) == _strategy_capability_ids(bound)
    from dataclasses import replace
    from ashare_lab.ports.candidate_generation import CandidateGroundingEvidence
    utterance = '买入计划；卖出计划'
    request = CompileInput(utterance=utterance, as_of_date=date(2026, 9, 16))
    unbound = replace(candidate, instrument_symbol=None, grounding_evidence=(
        CandidateGroundingEvidence('/independent_plans/entry_plan', 0, 4, '买入计划'),
        CandidateGroundingEvidence('/independent_plans/exit_plan', 5, 9, '卖出计划'),
    ))
    proposal = compiler._unbound_candidate_proposal(request, unbound)
    assert proposal is not None
    assert proposal.entry_summary == '买入计划'
    assert proposal.exit_summary == '卖出计划'
    assert proposal.strategy_template.independent_plans == pair
    assert compiler._unbound_candidate_proposal(request, replace(
        unbound, grounding_evidence=unbound.grounding_evidence[:1])) is None
    with pytest.raises(ValueError, match='初始资金'):
        replace(candidate, initial_cash_cny=200000)
    with pytest.raises(ValueError, match='占用'):
        replace(candidate, trading_plan=pair.entry_plan)


@pytest.mark.parametrize("condition", [
    FinancialIntent(FinancialMetricId.ROE, "gt", Decimal("1"), FinancialUnit.PERCENT,
                    report_type="annual", period_basis="full_year", statement_scope="consolidated"),
    FinancialIntent(FinancialMetricId.REVENUE_YOY, "gt", Decimal("10"), FinancialUnit.PERCENT,
                    report_type="annual", period_basis="full_year", statement_scope="consolidated"),
    EventIntent("event.dividends_corporate_actions.cash_dividend_proposal", "1.0.0"),
])
@pytest.mark.parametrize("side", ["buy", "sell"])
def test_composed_non_indicator_capabilities_survive_binding(condition, side):
    from ashare_lab.application.compile_strategy import _candidate_capability_ids, _strategy_capability_ids
    compiler = StrategyCompiler(generator=Mock(),
        catalog=load_catalog_directory(Path(__file__).resolve().parents[3] / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01")
    candidate = CandidateAst(instrument_symbol="300059.SZ", confidence=.9,
        entry=(condition,) if side == "sell" else (),
        exit=(condition,) if side == "buy" else (),
        trading_plan=ScheduledPlan(parameters=ScheduledParameters(
            side=side, sizing_mode="shares", frequency="monthly", day=1)))
    bound = compiler._build_strategy(candidate, date(2026, 9, 14), instrument_symbol="300059.SZ")
    assert _strategy_capability_ids(bound) == _candidate_capability_ids(candidate)


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_plan_builder_retains_opposite_indicator_leg_in_bound_and_unbound_forms(side):
    compiler = StrategyCompiler(generator=Mock(),
        catalog=load_catalog_directory(Path(__file__).resolve().parents[3] / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01")
    condition = IndicatorIntent("technical.macd", "1.0.0",
        "death_cross" if side == "buy" else "golden_cross", ())
    candidate = CandidateAst(instrument_symbol="300059.SZ", confidence=.9,
        entry=(condition,) if side == "sell" else (),
        exit=(condition,) if side == "buy" else (),
        trading_plan=ScheduledPlan(parameters=ScheduledParameters(
            side=side, sizing_mode="shares", frequency="monthly", day=1)))
    template = compiler._build_strategy_template(candidate, date(2026, 9, 14))
    bound = compiler._build_strategy(candidate, date(2026, 9, 14), instrument_symbol="300059.SZ")
    assert template.bind("300059.SZ") == bound
    assert isinstance(bound.execution, ComposedExecutionPolicy)
    actual = bound.exit.children[0] if side == "buy" else bound.entry
    assert actual.indicator_id == condition.indicator_id
    assert actual.trigger == condition.trigger
    assert bound.trading_plan.parameters.side == side
    from ashare_lab.application.compile_strategy import _strategy_capability_ids, _candidate_capability_ids
    assert _strategy_capability_ids(bound) == ("strategy.scheduled", "technical.macd")
    assert _candidate_capability_ids(candidate) == _strategy_capability_ids(bound)


@pytest.mark.parametrize("trigger", ["take_profit", "stop_loss", "trailing_drawdown"])
def test_calendar_and_minute_exit_keep_capability_after_binding(trigger):
    from ashare_lab.application.compile_strategy import _candidate_capability_ids, _strategy_capability_ids
    from ashare_lab.ports.candidate_generation import PositionReturnIntent, TrailingDrawdownIntent
    compiler = StrategyCompiler(generator=Mock(),
        catalog=load_catalog_directory(Path(__file__).resolve().parents[3] / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01")
    exit_rule = (TrailingDrawdownIntent(5, observation="minute_bar")
                 if trigger == "trailing_drawdown" else
                 PositionReturnIntent(trigger, 5, observation="minute_bar"))
    candidate = CandidateAst(instrument_symbol="300059.SZ", confidence=.9,
        entry=(), exit=(exit_rule,),
        trading_plan=ScheduledPlan(parameters=ScheduledParameters(
            side="buy", sizing_mode="shares", frequency="monthly", day=1)))
    bound = compiler._build_strategy(candidate, date(2026, 9, 14), instrument_symbol="300059.SZ")
    assert _strategy_capability_ids(bound) == _candidate_capability_ids(candidate)
    assert _strategy_capability_ids(bound) == ("strategy.scheduled", f"strategy.{trigger}")


@pytest.mark.parametrize("explicit_budget", [None, 20000])
def test_confirming_stock_binds_monthly_buy_and_macd_exit_without_losing_either_leg(explicit_budget):
    compiler = StrategyCompiler(generator=Mock(),
        catalog=load_catalog_directory(Path(__file__).resolve().parents[3] / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01")
    params = {"frequency": "monthly", "day": 1}
    if explicit_budget is not None:
        params["budget_cny"] = explicit_budget
    candidate = CandidateAst(instrument_symbol=None, confidence=.9,
        entry=(), exit=(IndicatorIntent("technical.macd", "1.0.0", "death_cross",
            (("fast", 12), ("slow", 26), ("signal", 9))),),
        trading_plan=ScheduledPlan(parameters=ScheduledParameters.model_validate(params)))
    template = compiler._build_strategy_template(candidate, date(2026, 9, 14))
    # Retained provider templates may still carry an omitted budget default.
    template = template.model_copy(update={"trading_plan": candidate.trading_plan})
    proposal = IdeaProposal(id="monthly_macd", title="定投与MACD退出",
        hypothesis="按已给定规则测试", entry_summary="每月月初买入",
        exit_summary="MACD死叉卖出", suggested_utterance="每月月初买入，MACD死叉卖出",
        capability_ids=(), assumptions=(), confidence=.9, strategy_template=template)

    outcome = compiler.bind_selected_idea(
        CompileInput(utterance="东财每个月买一次，死叉卖", instrument_context="300059.SZ",
                     as_of_date=date(2026, 9, 14)),
        CompileOutcome(status=CompileStatus.NEEDS_CLARIFICATION, selected_idea_proposal=proposal),
    )

    assert outcome is not None and outcome.status is CompileStatus.READY
    assert outcome.strategy.instrument.symbol == "300059.SZ"
    assert outcome.strategy.trading_plan.parameters.frequency == "monthly"
    assert outcome.strategy.exit.children[0].trigger == "death_cross"
    assert outcome.strategy.exit.children[0].indicator_id == "technical.macd"
    assert isinstance(outcome.strategy.execution, ComposedExecutionPolicy)
    if explicit_budget is not None:
        assert outcome.strategy.trading_plan.parameters.budget_cny == explicit_budget
    assert template.trading_plan == candidate.trading_plan
