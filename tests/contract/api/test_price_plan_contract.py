import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

from fastapi.testclient import TestClient

from ashare_lab.adapters.persistence.backtest_runs import InMemoryBacktestRunStore
from ashare_lab.api import create_app
from ashare_lab.api.result_schemas import BacktestResultBundle
from ashare_lab.api.routes.backtest_runs import _verified_review_facts
from ashare_lab.application.compile_strategy import StrategyCompiler
from ashare_lab.application.skill_backtest_service import SkillBacktestService
from ashare_lab.domain.catalog import load_catalog_directory
from ashare_lab.domain.shared import RunId
from ashare_lab.domain.strategy.price_plans import (
    ConditionalPlan, GridPlan, GridParameters, ScheduledPlan, ScheduledParameters,
    with_new_strategy_defaults,
)
from ashare_lab.ports.backtest_review import BacktestModelReview
from ashare_lab.ports.candidate_generation import CandidateAst
from tests.unit.application.test_conditional_orders import history, params
from tests.unit.application.test_grid_strategy import parameters


def test_new_grid_defaults_to_minute_without_changing_explicit_or_persisted_semantics():
    persisted = GridPlan(parameters=GridParameters(
        anchor_price=10, lower_price=5, upper_price=15, spacing=1,
    ))
    assert persisted.parameters.observation is None
    generated = with_new_strategy_defaults(persisted)
    assert generated.parameters.observation == "minute_bar"
    assert persisted.parameters.observation is None

    daily = persisted.model_copy(update={
        "parameters": persisted.parameters.model_copy(update={"observation": "daily_close"}),
    })
    assert with_new_strategy_defaults(daily) == daily


@pytest.mark.parametrize("minute_enabled", [True, False])
def test_execution_gate_preserves_daily_grid_and_schedule_and_checks_minute_conditions(minute_enabled):
    from ashare_lab.application.minute_grid_plan import MinuteGridCapabilityError
    from ashare_lab.domain.strategy.price_plans import ConditionParameters, ConditionRule

    service = SkillBacktestService(history=object(), indicators=object(),
        store=InMemoryBacktestRunStore(), minute_grid=object() if minute_enabled else None)
    try:
        for plan in (GridPlan(parameters=parameters(observation="daily_close", anchor_update="last_fill")),
                     ScheduledPlan(parameters=ScheduledParameters())):
            service.validate_execution_capability(SimpleNamespace(execution=None, trading_plan=plan))
        conditional = SimpleNamespace(execution=None, trading_plan=ConditionalPlan(parameters=ConditionParameters(
            observation="minute_bar", rules=(ConditionRule(kind="price", side="buy", target_price=9),))))
        if minute_enabled:
            service.validate_execution_capability(conditional)
        else:
            with pytest.raises(MinuteGridCapabilityError, match="minute_conditional_execution_disabled"):
                service.validate_execution_capability(conditional)
    finally:
        service.shutdown()


def test_new_take_profit_and_stop_loss_are_one_exclusive_exit_stage():
    from ashare_lab.domain.strategy.price_plans import ConditionParameters
    plan = ConditionalPlan(parameters=ConditionParameters.model_validate({
        "rules": [
            {"kind": "price", "side": "buy", "target_price": 18},
            {"kind": "take_profit", "side": "sell", "gap": 5},
            {"kind": "stop_loss", "side": "sell", "gap": 3},
        ],
    }))
    normalized = with_new_strategy_defaults(plan)
    assert normalized.parameters.rules[0].group is None
    assert normalized.parameters.rules[1].group == normalized.parameters.rules[2].group


@pytest.mark.parametrize("kind", ["grid", "conditional", "scheduled"])
def test_inventory_floor_allows_building_from_cash_without_implicit_initial_buy(kind):
    from ashare_lab.domain.strategy.price_plans import GridParameters, ConditionParameters
    cls, required = {
        "grid": (GridParameters, dict(anchor_price=10, lower_price=5, upper_price=15, spacing=1)),
        "conditional": (ConditionParameters, dict(rules=[dict(kind="price", side="buy", target_price=9)])),
        "scheduled": (ScheduledParameters, dict(frequency="weekly", day=3)),
    }[kind]
    parsed = cls.model_validate({**required, "min_shares": 100, "max_shares": 10000})
    assert parsed.initial_shares == 0
    assert parsed.min_shares == 100
    for invalid in ({"min_shares": 10001}, {"initial_shares": 10001}):
        with pytest.raises(ValueError, match="不得超过最大持仓"):
            cls.model_validate({**required, "min_shares": 100, "max_shares": 10000, **invalid})


@pytest.mark.parametrize("scheduled,issue,code,label", [
    (False, None, "minute_execution_unavailable", "分钟执行"),
    (False, "minute_snapshot_range_unavailable", "minute_data_unavailable", "未改用日线"),
    (False, "incomplete_minute_session:2026-09-10", "minute_data_unavailable", "未自动缩短区间"),
    (False, "minute_source_network_error", "minute_data_unavailable", "取数链路暂不可用"),
    (False, "daily_minute_volume_mismatch:2026-09-09", "minute_data_unavailable", "核对不一致"),
    (True, None, "market_calendar_unavailable", "交易日历"),
    (True, "corporate_action_source_missing", "corporate_action_data_unavailable", "分红送转"),
    (False, "corporate_action_coverage_insufficient", "corporate_action_data_unavailable", "覆盖不足"),
    (True, "market_calendar_source_missing", "market_calendar_unavailable", "交易日历"),
    (True, "daily_schedule_session_data_missing", "daily_execution_data_unavailable", "日行情"),
    (False, "capability:opening_holding_exceeds_initial_equity", "opening_holdings_exceed_equity", "初始总资产"),
    (False, "capability:grid_bounds_contain_no_complete_cell", "grid_parameters_unavailable", "完整的买卖网格"),
    (False, "capability:conditional_cost_provenance_missing", "condition_cost_unavailable", "持仓成交成本"),
])
def test_execution_unavailability_preserves_ready_draft(scheduled, issue, code, label):
    from unittest.mock import Mock
    from ashare_lab.application.minute_replay_input import MinuteReplayDataError
    from ashare_lab.application.minute_grid_plan import MinuteGridCapabilityError
    catalog = load_catalog_directory(Path(__file__).parents[3] / "catalogs")
    manifest = next(item for item in catalog.manifests if item.catalog_id == "cn_a.signals")
    plan = ScheduledPlan(parameters=ScheduledParameters()) if scheduled else GridPlan(
        parameters=parameters(**({"observation": "minute_bar"} if issue is None else {})))
    candidate = CandidateAst("300059.SZ", (), (), .99, trading_plan=plan,
                             backtest_start=date(2025, 1, 2), backtest_end=date(2025, 1, 7))
    compiler = StrategyCompiler(catalog=catalog, generator=SimpleNamespace(generate=AsyncMock(return_value=(candidate,))),
                                catalog_id=manifest.catalog_id, release_version=manifest.release_version)
    store = InMemoryBacktestRunStore()
    failure = (MinuteGridCapabilityError(issue.removeprefix("capability:"))
               if issue and issue.startswith("capability:") else MinuteReplayDataError(issue or "unused"))
    minute = SimpleNamespace(execute=Mock(side_effect=failure))
    # Calendar schedules now use the independent daily path, not minute.execute.
    calendar_loader = Mock(side_effect=MinuteReplayDataError(issue or "market_calendar_source_missing")) if scheduled else None
    service = SkillBacktestService(history=SimpleNamespace(load=AsyncMock(return_value=history([
        ("10", "9"), ("9", "10"), ("10", "10"), ("10", "10"),
    ]))), indicators=object(), store=store, minute_grid=None if scheduled or issue is None else minute,
        market_calendar_loader=calendar_loader)
    service.queue.shutdown()
    service.queue = SimpleNamespace(enqueue=lambda _: None, shutdown=lambda: None)
    try:
        with TestClient(create_app(compiler=compiler, backtest_submission=service, run_store=store)) as client:
            draft = client.post("/api/v1/strategy-drafts", json={
                "utterance": "东方财富以10元为基准，每跌1元买100股，每涨1元卖100股",
                "as_of_date": "2025-01-07", "instrument_context": "300059.SZ",
            }).json()
            assert draft["status"] == "ready"
            if not scheduled and issue is None:
                assert draft["execution_assessment"]["status"] == "temporarily_unavailable"
                assert draft["execution_assessment"]["interpreted_strategy"] == draft["strategy"]
                assert not draft.get("run_requested", False)
                prepared = client.post("/api/v1/backtest-runs/prepare", json={"strategy": draft["strategy"]})
                assert prepared.status_code == 503
                assert prepared.json()["error"]["code"] == "minute_execution_unavailable"
                assert "你的股票和买卖规则已保留" in prepared.json()["error"]["message"]
            started = client.post("/api/v1/backtest-runs", json={"strategy": draft["strategy"]})
            if not scheduled and issue is None:
                assert started.status_code == 503, started.text
                assert started.json()["error"]["code"] == code
                assert started.json()["error"]["message"] == prepared.json()["error"]["message"]
                minute.execute.assert_not_called()
                return
            assert started.status_code == 202
            record = service.execute(RunId(started.json()["id"]))
            assert record.state.value == "failed"
            assert record.error_code == code
            assert label in record.progress_label
            assert "策略已理解" in record.progress_label
            if scheduled:
                calendar_loader.assert_called_once()
                minute.execute.assert_not_called()
            else:
                minute.execute.assert_called_once()
    finally:
        service.shutdown()


@pytest.mark.parametrize("observation", ["minute_bar", None])
def test_unsupported_minute_anchor_is_rejected_before_data_or_run_creation(observation):
    import asyncio
    from unittest.mock import Mock
    from uuid import UUID
    from ashare_lab.application.backtest_submission import BacktestRunConfig
    from ashare_lab.application.minute_grid_plan import MinuteGridCapabilityError
    from ashare_lab.domain.strategy import StrategySpec
    from ashare_lab.domain.strategy.models import execution_for_price_plan

    catalog = load_catalog_directory(Path(__file__).parents[3] / "catalogs")
    manifest = next(item for item in catalog.manifests if item.catalog_id == "cn_a.signals")
    candidate = CandidateAst("300059.SZ", (), (), .99, trading_plan=GridPlan(
        parameters=parameters(anchor_update="last_fill", observation=observation)),
        backtest_start=date(2025, 1, 2), backtest_end=date(2025, 1, 7))
    compiler = StrategyCompiler(catalog=catalog,
        generator=SimpleNamespace(generate=AsyncMock(return_value=(candidate,))),
        catalog_id=manifest.catalog_id, release_version=manifest.release_version)
    store = InMemoryBacktestRunStore()
    create_run = Mock(wraps=store.create_or_get)
    store.create_or_get = create_run
    load = AsyncMock(return_value=history([("10", "9"), ("9", "10"), ("10", "10"), ("10", "10")]))
    minute = SimpleNamespace(execute=Mock())
    service = SkillBacktestService(history=SimpleNamespace(load=load), indicators=object(),
        store=store, minute_grid=minute)
    try:
        with TestClient(create_app(compiler=compiler, backtest_submission=service, run_store=store)) as client:
            draft = client.post("/api/v1/strategy-drafts", json={
                "utterance": "东方财富网格，基准10元，每格1元，成交后更新基准",
                "as_of_date": "2025-01-07", "instrument_context": "300059.SZ",
            })
            assert draft.status_code == 201 and draft.json()["status"] == "ready", draft.text
            strategy = draft.json()["strategy"]
            # Cover both an explicit minute plan and an old server-selected plan.
            strategy["trading_plan"]["parameters"]["observation"] = observation
            strategy["execution"] = execution_for_price_plan(
                GridPlan.model_validate(strategy["trading_plan"])).model_dump(mode="json")
            assert strategy["trading_plan"]["parameters"]["anchor_update"] == "last_fill"
            load.reset_mock()
            for endpoint in ("/prepare", ""):
                response = client.post("/api/v1/backtest-runs" + endpoint, json={"strategy": strategy})
                assert response.status_code == 422, response.text
                assert response.json()["error"]["code"] == "grid_execution_unavailable"
                assert "成交后移动基准" in response.json()["error"]["message"]
                assert "未改为固定基准" in response.json()["error"]["message"]
            with pytest.raises(MinuteGridCapabilityError, match="moving_anchor_execution_not_connected"):
                service.submit(StrategySpec.model_validate(strategy), BacktestRunConfig())
            load.assert_not_awaited()
            create_run.assert_not_called()
            minute.execute.assert_not_called()
            # The capability gate neither rejects interpretation nor overwrites the saved draft.
            saved = asyncio.run(client.app.state.container.drafts.latest_for_answer(
                draft_id=UUID(draft.json()["draft_id"]), revision=draft.json()["revision"]))
            assert saved.outcome.strategy.model_dump(mode="json") == draft.json()["strategy"]
            if observation is None:
                # The same old server-selected plan is accepted by the daily route.
                # If minute wiring appears after queueing, fail honestly before acquisition.
                service.minute_grid = None
                service.queue.enqueue = Mock()
                created = service.submit(StrategySpec.model_validate(strategy), BacktestRunConfig())
                service.minute_grid = minute
                failed = service.execute(created.record.run_id)
                assert failed.state.value == "failed"
                assert failed.error_code == "grid_execution_unavailable"
                assert "成交后移动基准" in failed.progress_label
                assert failed.strategy_json == created.record.strategy_json
                assert failed.config_json == created.record.config_json
                assert failed.result_json is None
                load.assert_not_awaited()
                minute.execute.assert_not_called()
    finally:
        service.shutdown()


def test_daily_limit_expiry_is_reported_at_close_without_a_future_fill():
    from ashare_lab.application.price_plan_result import execute_price_plan
    plan = ConditionalPlan(parameters=params([
        {"kind": "price", "side": "buy", "direction": "up", "target_price": 11, "limit_price": 10},
    ]))
    strategy = SimpleNamespace(trading_plan=plan, backtest=SimpleNamespace(
        start=date(2025, 1, 2), end=date(2025, 1, 6), initial_cash_cny=1000000))
    bundle = execute_price_plan("expiry", strategy, history([("10", "11"), ("12", "12"), ("10", "9")]))
    events = bundle.model_dump(mode="json", by_alias=True)["activities"]
    expiry = next(event for event in events if event["kind"] == "expired")
    assert expiry["occurredAt"] == "2025-01-03T15:00:00+08:00"
    assert expiry["status"] == "expired"
    assert not any(event["kind"] in {"fill", "partial_fill"} for event in events)
    assert expiry["executionDetails"]["remainder_disposition"] == "expired_day"


@pytest.mark.parametrize("side,target", [("buy", 9), ("sell", 11)])
def test_daily_limit_report_separates_effective_clock_from_intrabar_fill(side, target):
    from ashare_lab.application.price_plan_result import execute_price_plan
    plan = ConditionalPlan(parameters=params([
        {"kind": "price", "side": side, "direction": "down" if side == "buy" else "up",
         "target_price": target, "limit_price": target, "quantity": 100},
    ], opening_shares=100 if side == "sell" else 0))
    strategy = SimpleNamespace(trading_plan=plan, backtest=SimpleNamespace(
        start=date(2025, 1, 2), end=date(2025, 1, 3), initial_cash_cny=1000000))
    bundle = execute_price_plan("clock", strategy, history([("10", str(target)), ("10", "10")]))
    events = bundle.model_dump(mode="json", by_alias=True)["activities"]
    fill = next(event for event in events if event["kind"] == "fill")
    assert fill["occurredAt"] == "2025-01-03T15:00:00+08:00"
    assert fill["timeQuality"] == "daily_bar_available_at_proxy"
    assert fill["executionDetails"]["effective_at"] == "2025-01-03T09:30:00+08:00"
    assert fill["price"] == target


@pytest.mark.parametrize("scope,initial", [("total_equity", 1000000), ("cash_plus_opening_holdings", 1010000)])
def test_opening_holding_report_uses_same_initial_equity_and_hold_benchmark(scope, initial):
    from ashare_lab.application.price_plan_result import execute_price_plan
    plan = ConditionalPlan(parameters=params([
        {"kind": "price", "side": "sell", "direction": "up", "target_price": 10, "quantity": 1000},
    ], opening_shares=1000, initial_capital_scope=scope))
    strategy = SimpleNamespace(trading_plan=plan, backtest=SimpleNamespace(
        start=date(2025, 1, 2), end=date(2025, 1, 6), initial_cash_cny=1000000))
    bundle = execute_price_plan("opening-hold", strategy, history([("10", "10"), ("10", "11"), ("11", "11")]))
    data = bundle.model_dump(mode="json", by_alias=True)
    summary = data["summary"]
    assert summary["initialEquityCny"] == initial
    assert summary["benchmarkComparisonStatus"] == "comparable"
    assert summary["benchmarkReturn"] == pytest.approx(1000 / initial)
    assert summary["totalReturn"] == pytest.approx(summary["finalEquityCny"] / initial - 1)
    assert data["series"][-1]["equity"] == pytest.approx(summary["finalEquityCny"] / initial * 100)
    fills = [a for a in data["activities"] if a["kind"] in {"fill", "partial_fill"}]
    assert len(fills) == 1 and fills[0]["side"] == "sell"
    assert "期初已有持仓" in summary["interpretation"]


def test_end_of_run_report_preserves_unfinished_exit_without_fake_fill():
    from ashare_lab.application.price_plan_result import execute_price_plan
    plan = ConditionalPlan(parameters=params([
        {"kind": "take_profit", "side": "sell", "gap": 1, "gap_unit": "cny", "limit_price": 20},
    ], initial_shares=100))
    strategy = SimpleNamespace(trading_plan=plan, backtest=SimpleNamespace(
        start=date(2025, 1, 2), end=date(2025, 1, 3), initial_cash_cny=1000000))
    bundle = execute_price_plan("end-exit", strategy, history([("10", "12"), ("12", "12")]))
    data = bundle.model_dump(mode="json", by_alias=True)
    event = next(item for item in data["activities"] if item["title"] == "回测结束撤销剩余委托")
    assert event["quantity"] == 100 and event["status"] == "cancelled"
    assert event["occurredAt"] == "2025-01-03T15:00:00+08:00"
    assert not any(item["side"] == "sell" and item["kind"] == "fill" for item in data["activities"])
    assert any("100股退出意图未完成" in warning for warning in data["summary"]["warnings"])


@pytest.mark.parametrize('with_corporate_source', [False, True])
def test_price_plans_use_existing_draft_revision_queue_and_result_contract(with_corporate_source):
    catalog = load_catalog_directory(Path(__file__).parents[3] / "catalogs")
    # This contract exercises the synchronous daily test harness. New language
    # grids default to minute_bar in the separate assertion above.
    daily_grid = parameters().model_copy(update={"observation": "daily_close"})
    for plan in (GridPlan(parameters=daily_grid), ConditionalPlan(parameters=params([
        {"kind": "price", "side": "buy", "direction": "down", "target_price": 9},
        {"kind": "take_profit", "side": "sell", "gap": 1, "gap_unit": "cny"},
    ], slippage_bps=0))):
        candidate = CandidateAst("300059.SZ", (), (), .99, trading_plan=plan,
                                 backtest_start=date(2025, 1, 2), backtest_end=date(2025, 1, 7))
        generator = SimpleNamespace(generate=AsyncMock(return_value=(candidate,)))
        manifest = next(item for item in catalog.manifests if item.catalog_id == "cn_a.signals")
        compiler = StrategyCompiler(catalog=catalog, generator=generator,
                                    catalog_id=manifest.catalog_id,
                                    release_version=manifest.release_version)
        store = InMemoryBacktestRunStore()
        queue = SimpleNamespace(enqueue=lambda _: None, shutdown=lambda: None)
        service = SkillBacktestService(
            history=SimpleNamespace(load=AsyncMock(return_value=history([
                ("10", "9"), ("9", "10"), ("10", "10"), ("10", "10"),
            ]))), indicators=object(), store=store,
        )
        corporate_loads = []
        if with_corporate_source:
            from ashare_lab.application.corporate_action_timeline import TimelineCorporateActionApplier
            def load_corporate(strategy, history):
                corporate_loads.append(strategy.instrument.symbol)
                return SimpleNamespace(applier=TimelineCorporateActionApplier(()), price_rebases=(),
                    evidence={'source': 'contract_fixture_no_actions'})
            service.signal_corporate_loader = load_corporate
            service.market_calendar_loader = lambda: (
                tuple(date(2025, 1, day) for day in (1, 2, 3, 6, 7, 8)),
                {'provider': 'contract_fixture', 'sourceSha256': 'a' * 64, 'sessions': 6}, b'fixture')
        service.queue.shutdown()
        service.queue = queue
        advisor = SimpleNamespace(review=AsyncMock(return_value=BacktestModelReview(
            analysis="本次回测已完成，结果需要结合实际成交口径理解。",
            conclusion="可以修改参数后对比同一区间结果。", proposals=(),
            provider="test", model="test", prompt_version="test", schema_version="test",
            response_hash="sha256:" + "a" * 64,
        )))
        try:
            with TestClient(create_app(compiler=compiler, backtest_submission=service,
                                      run_store=store, backtest_review_advisor=advisor)) as client:
                response = client.post("/api/v1/strategy-drafts", json={
                    "utterance": "东方财富以10元为基准，每跌1元买100股，每涨1元卖100股",
                    "as_of_date": "2025-01-07", "instrument_context": "300059.SZ",
                })
                assert response.status_code == 201, response.text
                draft = response.json()
                assert draft["status"] == "ready", draft
                assert draft["strategy"]["entry"] is None
                assert draft["strategy"]["trading_plan"]["kind"] == plan.kind
                # Idea proposals pass the same plan through the rule defaults
                # and capability gate before a user can select them.
                from ashare_lab.domain.strategy import StrategySpec
                from ashare_lab.ports.candidate_generation import CompileInput
                proposed = StrategySpec.model_validate(draft["strategy"])
                assert compiler._normalize_idea_rule_defaults(proposed) == proposed
                assert compiler._validate_idea_rules(CompileInput(
                    utterance="东方财富网格策略", instrument_context="300059.SZ",
                    as_of_date=date(2025, 1, 7),
                ), proposed) == (f"strategy.{plan.kind}",)
                revised_strategy = json.loads(json.dumps(draft["strategy"]))
                revised_strategy["trading_plan"]["parameters"]["max_shares"] = 5000
                revised = client.post(
                    f"/api/v1/strategy-drafts/{draft['draft_id']}/revisions",
                    json={"strategy": revised_strategy},
                )
                assert revised.status_code == 201, revised.text
                assert revised.json()["strategy_hash"] != draft["strategy_hash"]
                assert revised.json()["revision"] > draft["revision"]
                assert draft["strategy"]["trading_plan"]["parameters"]["max_shares"] == 10000
                draft = revised.json()
                started = client.post("/api/v1/backtest-runs", json={"strategy": draft["strategy"]})
                assert started.status_code == 202, started.text
                run_id = started.json()["id"]
                record = service.execute(RunId(run_id))
                assert record.state.value == "succeeded", record
                summary = client.get(f"/api/v1/backtest-runs/{run_id}/summary")
                assert summary.status_code == 200, summary.text
                assert summary.json()["dataProvenance"]["priceBasis"] == "unadjusted"
                # Buy 100 at 9, sell at 10: gross gain 100, transfer
                # fees 0.01 on each side and sell stamp duty 0.50.
                assert summary.json()["finalEquityCny"] == 1000099.48
                ledger = json.loads(json.loads(record.result_json)["audit"]["pricePlanLedger"])
                if with_corporate_source:
                    assert corporate_loads == ['300059.SZ']
                    assert ledger['provenance']['corporate_actions']['source'] == 'contract_fixture_no_actions'
                filled_orders = [item for item in ledger["orders"] if item["filled_quantity"]]
                assert [item["transfer_fee_cny"] for item in filled_orders] == ["0.01", "0.01"]
                assert [item["stamp_tax_cny"] for item in filled_orders] == ["0.00", "0.50"]
                bundle = BacktestResultBundle.model_validate_json(record.result_json)
                facts = _verified_review_facts(record, bundle)
                assert "pricePlanLedger" not in facts["audit"]
                assert facts["audit"]["resultHash"] == bundle.audit.result_hash
                assert facts["benchmarkDefinition"]["type"] == "same_first_buy_allocation_hold"
                assert summary.json()["benchmarkComparisonStatus"] == "comparable"
                assert summary.json()["benchmarkReturn"] is not None
                trades = client.get(f"/api/v1/backtest-runs/{run_id}/trades").json()
                fill = next(item for item in trades if item["kind"] == "fill")
                assert fill["executionDetails"]["filled_quantity"] == 100
                assert fill["executionDetails"]["cash_before_cny"] == "1000000"
                order = next(item for item in trades if item["id"] == fill["parentId"])
                signal = next(item for item in trades if item["id"] == order["parentId"])
                assert signal["occurredAt"] <= order["occurredAt"] == fill["occurredAt"]
                assert signal["chainId"] == order["chainId"] == fill["chainId"]
                review = client.post(f"/api/v1/backtest-runs/{run_id}/review")
                assert review.status_code == 200, review.text
                assert review.json()["optimizationCandidates"] == []
        finally:
            service.shutdown()
