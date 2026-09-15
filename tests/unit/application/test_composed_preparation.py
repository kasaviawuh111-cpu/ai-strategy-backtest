"""Verify service preparation retains independent signal legs (synthetic data)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ashare_lab.adapters.persistence.backtest_runs import InMemoryBacktestRunStore
from ashare_lab.application.skill_backtest_service import SkillBacktestService
from ashare_lab.domain.strategy import ComposedExecutionPolicy, FirstOfExit, StrategySpec
from ashare_lab.domain.strategy.price_plans import ScheduledPlan, ScheduledParameters
from tests.unit.application.test_skill_indicator_integration import oscillating_history, ROUTES
from tests.unit.application.test_skill_backtest import _strategy, _config


@pytest.mark.asyncio
async def test_periodic_plan_prepares_independent_daily_exit_and_pins_adjustment_source(oscillating_history):
    history = oscillating_history
    original = _strategy((history.rows[140].session_date, history.rows[-1].session_date),
                         holding_sessions=5)
    payload = original.model_dump()
    payload.update(entry=None, exit=FirstOfExit(children=(original.entry,)),
        trading_plan=ScheduledPlan(parameters=ScheduledParameters(
            frequency="monthly", initial_cash_cny=original.backtest.initial_cash_cny)),
        execution=ComposedExecutionPolicy())
    strategy = StrategySpec.model_validate(payload)
    corporate = SimpleNamespace(price_rebases=(), evidence={"provider": "synthetic-actions"})
    service = SkillBacktestService(
        history=SimpleNamespace(load=AsyncMock(return_value=history)),
        indicators=SimpleNamespace(query_indicator_history=AsyncMock()),
        store=InMemoryBacktestRunStore(), indicator_routes=ROUTES,
        minute_grid=SimpleNamespace(), signal_corporate_loader=lambda *args, **kwargs: corporate)
    service._signals = AsyncMock(return_value=(
        (None,) * len(history.rows), (None,) * len(history.rows), ()))
    prepared = await service._prepare_candidate(strategy, _config(run_robustness=False), run_id=None)
    service._signals.assert_awaited_once()
    assert service._signals.await_args.args[0] == strategy
    assert service._signals.await_args.kwargs["price_rebases"] == ()
    assert "synthetic-actions" in prepared.signal_adjustment_source
