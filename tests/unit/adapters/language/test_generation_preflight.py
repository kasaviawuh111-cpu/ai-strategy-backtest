"""Generation/preparation regressions. Fixtures are not live market evidence."""

from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ashare_lab.adapters.language.generation_preflight import (
    GENERATION_PREFLIGHT_CONTRACT, current_plan_failures, validate_generated_plan, has_unsized_start_purchase,
)


@pytest.mark.parametrize("text,expected", [
    ("平安银行一年前买，涨20%卖", True),
    ("平安银行回测开始时买入，涨20%卖", True),
    ("平安银行一年前买入10000股，涨20%卖", False),
    ("平安银行一年前买入1万元，涨20%卖", False),
    ("平安银行一年前全仓买入，涨20%卖", False),
    ("平安银行不要一年前买，涨20%卖", False),
    ("平安银行近一年回测，涨20%卖", False),
])
def test_unsized_start_purchase_routes_only_missing_size(text, expected):
    assert has_unsized_start_purchase(text) == expected
from ashare_lab.adapters.language.vibe_ideas import VibeIdeaRouter
from ashare_lab.api.backtest_preflight import preflight_backtest_strategy
from ashare_lab.api.errors import ApiProblem
from ashare_lab.adapters.persistence.backtest_runs import InMemoryBacktestRunStore
from ashare_lab.application.backtest_submission import BacktestRunConfig
from ashare_lab.application.skill_backtest_service import SkillBacktestService
from ashare_lab.domain.strategy import BacktestConfig, CatalogRef, Instrument, StrategySpec, execution_for_price_plan
from ashare_lab.domain.strategy.price_plans import (
    GridParameters, GridPlan, GridSpecificationError,
    ConditionalPlan, ConditionParameters, ConditionRule, ScheduledPlan, ScheduledParameters,
)
from ashare_lab.ports.candidate_generation import CompileInput
from tests.unit.adapters.language.test_vibe_ideas import (
    _RecordingTransport, _provider_payload, capability_matrix,
)
from tests.unit.application.test_skill_backtest import _history, _row, _strategy, START


@pytest.mark.parametrize("mode,gap", [("cny", 5), ("anchor_percent", 1), ("percent", 1)])
def test_known_grid_conflicts_report_numeric_cause_without_mutating(mode, gap):
    plan = GridPlan(parameters=GridParameters(
        anchor_price=D("351.4"), lower_price=200, upper_price=400,
        spacing_mode=mode, spacing=gap, levels_above=15,
    ))
    original = plan.model_dump(mode="json")
    with pytest.raises(GridSpecificationError) as caught:
        validate_generated_plan(plan)
    assert caught.value.code == "grid_geometry_conflict"
    assert "351.4" in caught.value.safe_message and "400.00" in caught.value.safe_message
    if mode == "cny":
        assert "426.40" in caught.value.safe_message
    assert plan.model_dump(mode="json") == original
    assert current_plan_failures(plan)[0]["type"] == "grid_geometry_conflict"
    # Old conflicting drafts remain readable, so users can edit them.
    assert GridPlan.model_validate(original) == plan


@pytest.mark.parametrize("side,bounds,required_edge", [
    ("below", ("1369.81", "1674.22"), "1369.80"),
    ("above", ("1369.80", "1674.21"), "1674.22"),
])
def test_fractional_cent_conflict_reports_executable_edge_without_relaxing_bounds(
    side, bounds, required_edge,
):
    plan = GridPlan(parameters=GridParameters(
        anchor_price=D("1522.01"), lower_price=D(bounds[0]), upper_price=D(bounds[1]),
        spacing_mode="anchor_percent", spacing=1, **{f"levels_{side}": 10},
    ))
    with pytest.raises(GridSpecificationError) as caught:
        validate_generated_plan(plan)
    assert required_edge in caught.value.safe_message
    assert "理论值" in caught.value.safe_message
    # Correct outward rounding passes, while the conflicting user bounds stay unchanged.
    corrected = plan.model_copy(update={"parameters": plan.parameters.model_copy(update={
        "lower_price": D("1369.80"), "upper_price": D("1674.22"),
    })})
    validate_generated_plan(corrected)
    assert (plan.parameters.lower_price, plan.parameters.upper_price) == tuple(map(D, bounds))


@pytest.mark.asyncio
@pytest.mark.parametrize("anchor_mode", ["manual", "first_open", "previous_close"])
async def test_real_preparation_blocks_conflict_before_creating_run(anchor_mode):
    rows = tuple(_row(START + timedelta(days=i), raw_open="351.4") for i in range(4))
    plan = GridPlan(parameters=GridParameters(
        anchor_mode=anchor_mode, anchor_price=D("351.4") if anchor_mode == "manual" else None,
        lower_price=200, upper_price=400, spacing=5, levels_above=15,
        initial_cash_cny=10000,
    ))
    strategy = _strategy((rows[1].session_date, rows[-1].session_date)).model_copy(update={
        "entry": None, "exit": None, "trading_plan": plan,
        "execution": execution_for_price_plan(plan),
    })
    load = AsyncMock(return_value=_history(rows))
    store = InMemoryBacktestRunStore()
    store.create_or_get = AsyncMock(side_effect=AssertionError("preflight must not create a run"))
    service = SkillBacktestService(
        history=SimpleNamespace(load=load), indicators=object(), store=store,
        market_calendar_loader=lambda: (tuple(row.session_date for row in rows), {}, b"fixture-calendar"),
    )
    try:
        with pytest.raises(ApiProblem) as caught:
            await preflight_backtest_strategy(
                strategy=strategy, config=BacktestRunConfig(),
                container=SimpleNamespace(backtest_submission=service),
            )
        assert caught.value.status_code == 422
        assert caught.value.code == "grid_geometry_conflict"
        assert "426.40" in caught.value.message
        assert "GridSpecificationError" not in caught.value.message
        store.create_or_get.assert_not_called()
        assert strategy.trading_plan == plan
    finally:
        service.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("case,expected", [
    ("grid", "grid_geometry_conflict"),
    ("scheduled", "周定投日期须为1至7"),
    ("conditional", "到价条件须填写触发价"),
    ("cash", "交易计划与回测初始资金必须一致"),
    ("dates", "backtest start must be on or before end"),
])
async def test_generation_repairs_actual_failure_with_original_boundary(capability_matrix, case, expected):
    catalog = CatalogRef(catalog_id="cn_a.signals", release_version="2026.09.01")
    if case == "scheduled":
        plan = ScheduledPlan(parameters=ScheduledParameters(frequency="weekly", day=5))
    elif case == "conditional":
        plan = ConditionalPlan(parameters=ConditionParameters(rules=(
            ConditionRule(kind="price", side="buy", target_price=9),
        )))
    else:
        plan = GridPlan(parameters=GridParameters(anchor_mode="previous_close" if case == "grid" else "manual",
            anchor_price=None if case == "grid" else D("351.4"),
            lower_price=200, upper_price=400, spacing=5, levels_above=9))
    strategy = StrategySpec(
        catalog=catalog, instrument=Instrument(symbol="300059.SZ"), trading_plan=plan,
        execution=execution_for_price_plan(plan),
        backtest=BacktestConfig(start=date(2025, 9, 5), end=date(2026, 9, 5),
                                initial_cash_cny=plan.parameters.initial_cash_cny),
    )
    valid = _provider_payload()
    for proposal in valid["proposals"]:
        proposal["strategy"] = strategy.model_dump(mode="json")
    invalid = deepcopy(valid)
    for proposal in invalid["proposals"]:
        params = proposal["strategy"]["trading_plan"]["parameters"]
        if case == "grid":
            params["levels_above"] = 15
        elif case == "scheduled":
            params["day"] = 8
        elif case == "conditional":
            params["rules"][0]["target_price"] = None
        elif case == "cash":
            params["initial_cash_cny"] = 123456
        else:
            proposal["strategy"]["backtest"]["start"] = "2026-09-06"
    primary, repair = _RecordingTransport([invalid]), _RecordingTransport([valid])
    context = {"symbol": "300059.SZ", "backtest": {"start": "2025-09-05", "end": "2026-09-05"},
               "initialGridAnchor": {"status": "ready", "price": "351.4", "date": "2025-09-04",
                                     "source": "historical_daily_raw_close"}}
    loader = AsyncMock(return_value=context)
    router = VibeIdeaRouter(primary, capability_matrix=capability_matrix,
                           strategy_catalog=catalog, repair_transport=repair,
                           market_context_loader=loader if case == "grid" else None)
    request = CompileInput(utterance="给我几个可以修改的策略", instrument_context="300059.SZ",
                           as_of_date=date(2026, 9, 5), semantic_intent="vague_strategy")
    route = await router.route(request)
    assert route is not None and len(route.proposals) == 3
    assert len(primary.requests) == len(repair.requests) == 1
    sent, resent = primary.requests[0], repair.requests[0]
    assert GENERATION_PREFLIGHT_CONTRACT in sent.system_contract
    assert expected in str(resent.user_payload["validationFeedback"])
    assert resent.user_payload["strategyBoundary"] == sent.user_payload["strategyBoundary"]
    assert resent.user_payload["utterance"] == request.utterance
    assert resent.user_payload["previousResponse"] == invalid
    if case == "grid":
        loader.assert_awaited_once_with("300059.SZ", strategy.backtest)
        assert sent.user_payload["verifiedMarketContext"] == [context]
        assert resent.user_payload["verifiedMarketContext"] == [context]
        for item in route.proposals:
            assert item.strategy.trading_plan.parameters.resolved_anchor == D("351.4")
            assert item.strategy.trading_plan.parameters.upper_price == 400
            assert item.strategy.trading_plan.parameters.levels_above == 9
    else:
        assert all(item.strategy.trading_plan == plan for item in route.proposals)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_price_context_uses_real_previous_close_without_fabrication(failure):
    from ashare_lab.api import generation_market_context as module
    params = GridParameters(anchor_mode="previous_close", anchor_price=D("351.4"),
                            lower_price=1, upper_price=1000,
                            anchor_quote_time_label="2025-01-01", anchor_quote_source="historical_daily_raw_close")
    strategy = _strategy((START, START + timedelta(days=10)))
    bound = strategy.model_copy(update={"trading_plan": GridPlan(parameters=params)})
    resolve = AsyncMock(return_value=bound)
    if failure:
        resolve.side_effect = RuntimeError("private-token-and-provider-payload")
    context = await module.load_generation_market_context(
        "300059.SZ", strategy.backtest, service=SimpleNamespace(resolve_grid_anchor=resolve),
        catalog=strategy.catalog,
    )
    assert context["positionsKnown"] is False
    assert "latestQuote" not in context
    if failure:
        assert context["initialGridAnchor"]["status"] == "unavailable"
        assert "price" not in context["initialGridAnchor"] and "private" not in str(context)
    else:
        assert context["initialGridAnchor"]["price"] == "351.4"
        assert context["initialGridAnchor"]["date"] == "2025-01-01"
@pytest.mark.parametrize('symbol,quantity,valid', [('688152.SH', 100, False), ('688152.SH', 200, True),
    ('688152.SH', 201, True), ('300059.SZ', 101, False), ('300059.SZ', 100, True)])
def test_grid_buy_quantity_uses_symbol_board_rules(symbol, quantity, valid):
    plan = GridPlan(parameters=GridParameters(anchor_price=50, lower_price=40, upper_price=60,
        order_shares=quantity, initial_shares=200))
    if valid:
        validate_generated_plan(plan, symbol)
    else:
        with pytest.raises(GridSpecificationError, match='invalid grid buy quantity'):
            validate_generated_plan(plan, symbol)
