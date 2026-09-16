"""Local minute wiring contract; fixtures do not claim real-provider acceptance."""
import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest

from fastapi.testclient import TestClient

from ashare_lab.adapters.market_data.eastmoney_minute_snapshot import load_eastmoney_minute_snapshot
from ashare_lab.adapters.persistence.backtest_runs import InMemoryBacktestRunStore
from ashare_lab.api import create_app
from ashare_lab.application.compile_strategy import StrategyCompiler
from ashare_lab.application.local_minute_grid import LocalMinuteGrid, PricePlanCorporateInputs
from ashare_lab.application.corporate_action_timeline import TimelineCorporateActionApplier
from ashare_lab.application.skill_backtest_service import SkillBacktestService
from ashare_lab.domain.catalog import load_catalog_directory
from ashare_lab.domain.shared import RunId
from ashare_lab.domain.strategy.price_plans import GridPlan, ConditionalPlan, ConditionParameters, ConditionRule, ScheduledPlan, ScheduledParameters
from ashare_lab.ports.candidate_generation import CandidateAst
from tests.unit.adapters.test_eastmoney_minute_snapshot import _build
from tests.unit.application.test_conditional_orders import history
from tests.unit.application.test_grid_strategy import parameters


@pytest.mark.parametrize("minute_enabled", [True, False])
def test_hybrid_draft_default_config_queue_and_report_use_same_pinned_inputs(tmp_path, minute_enabled):
    """Synthetic signal/provider inputs; compiler, ledger and HTTP views are real."""
    from datetime import datetime, time, timedelta
    from decimal import Decimal
    from zoneinfo import ZoneInfo
    from ashare_lab.adapters.market_data.eastmoney_minute_snapshot import EastmoneyMinuteSnapshotSpec
    from ashare_lab.domain.shared import InstrumentId
    from ashare_lab.domain.signals import SignalFact
    from ashare_lab.ports.candidate_generation import IndicatorIntent, PositionReturnIntent
    from tests.unit.adapters.test_eastmoney_minute_snapshot import _make_collection, _make_rows
    from tests.unit.application.test_skill_backtest import _row, _history

    days = [date(2025, 1, day) for day in (2, 3, 6)]
    controls, source_ids = [], []
    for day in days:
        collection = _make_collection(_make_rows())
        delta = timedelta(days=(day - days[0]).days)
        collection = replace(collection, requested_start=day, requested_end=day,
            retrieved_at=collection.retrieved_at + delta,
            rows=tuple(replace(row, timestamp=row.timestamp + delta) for row in collection.rows))
        snapshot = _build(tmp_path / "snapshots", collection=collection,
            spec=EastmoneyMinuteSnapshotSpec(symbol="300059.SZ", start=day, end=day),
            captured_at=collection.retrieved_at)
        source_ids.append(snapshot.snapshot_id)
        bars = load_eastmoney_minute_snapshot(snapshot.path)
        controls.append(replace(_row(day, raw_open="10"), raw_open=bars[0].open.amount,
            raw_close=bars[-1].close.amount, raw_high=max(bar.high.amount for bar in bars),
            raw_low=min(bar.low.amount for bar in bars), volume=sum(bar.volume.value for bar in bars),
            amount=sum(bar.turnover for bar in bars)))
    h = _history((_row(date(2024, 12, 31), raw_open="10"), *controls))
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps(dict(schemaVersion="market-calendar.v1", provider="fixture",
        sourceSha256="a" * 64, sessions=[str(day) for day in (*days, date(2025, 1, 7))])))
    catalog = load_catalog_directory(Path(__file__).parents[3] / "catalogs")
    manifest = next(m for m in catalog.manifests if m.catalog_id == "cn_a.signals")
    candidate = CandidateAst("300059.SZ", (
        IndicatorIntent("technical.ma", "1.0.0", "price_crosses_above", (("period", 3), ("price_field", "close"))),
    ), (PositionReturnIntent("take_profit", 1, "minute_bar"),
        PositionReturnIntent("stop_loss", 3, "minute_bar")), .99,
        backtest_start=days[0], backtest_end=days[-1])
    compiler = StrategyCompiler(catalog=catalog, generator=SimpleNamespace(generate=AsyncMock(return_value=(candidate,))),
        catalog_id=manifest.catalog_id, release_version=manifest.release_version)
    store = InMemoryBacktestRunStore()
    corporate = PricePlanCorporateInputs(TimelineCorporateActionApplier(()), (),
        dict(provider="fixture", status="verified_empty", sourceSha256="b" * 64))
    loader = Mock(return_value=corporate)
    service = SkillBacktestService(history=SimpleNamespace(load=AsyncMock(return_value=h)),
        indicators=object(), store=store, signal_corporate_loader=loader,
        minute_grid=LocalMinuteGrid(tmp_path / "snapshots", calendar) if minute_enabled else None,
        runtime_evidence=dict(catalog_hash=catalog.content_hash, code_revision="fixture-worktree", engine_version="fixture"))
    confirmed = datetime.combine(days[0], time(15), ZoneInfo("Asia/Shanghai"))
    fact = SignalFact(InstrumentId("300059.SZ"), days[0], "fixture:daily-entry", True,
                      confirmed, confirmed, "synthetic confirmed signal")
    service._signals = AsyncMock(return_value=((None, fact, None, None), (None,) * 4, ()))
    service.queue.shutdown()
    service.queue = SimpleNamespace(enqueue=lambda _: None, shutdown=lambda: None)
    try:
        with TestClient(create_app(compiler=compiler, backtest_submission=service, run_store=store)) as client:
            draft = client.post("/api/v1/strategy-drafts", json={
                "utterance": "东方财富上穿3日均线买入，分钟止盈1%止损3%",
                "instrument_context": "300059.SZ", "as_of_date": "2025-01-06"})
            assert draft.status_code == 201, draft.text
            assert draft.json()["status"] == "ready", draft.text
            strategy = draft.json()["strategy"]
            assert strategy["exit"]["children"][0]["type"] == "minute_protection_exit"
            if not minute_enabled:
                # Parsing may prepare daily signals; execution gates must do no additional IO.
                loader.reset_mock()
            prepared = client.post("/api/v1/backtest-runs/prepare", json={"strategy": strategy})
            if not minute_enabled:
                assert prepared.status_code == 503, prepared.text
                assert prepared.json()["error"]["code"] == "minute_execution_unavailable"
                assert "你的股票和买卖规则已保留" in prepared.json()["error"]["message"]
                assert "本次未启动回测" in prepared.json()["error"]["message"]
            else:
                assert prepared.status_code == 200 and prepared.json() == {"ready": True}
            queued = client.post("/api/v1/backtest-runs", json={"strategy": strategy})
            if not minute_enabled:
                assert queued.status_code == 503, queued.text
                assert queued.json()["error"]["code"] == "minute_execution_unavailable"
                assert queued.json()["error"]["message"] == prepared.json()["error"]["message"]
                loader.assert_not_called()
                return
            assert queued.status_code == 202, queued.text
            run_id = queued.json()["id"]
            record = service.execute(RunId(run_id))
            assert record.state.value == "succeeded", (record.error_code, record.progress_label)
            summary = client.get(f"/api/v1/backtest-runs/{run_id}/summary")
            assert summary.status_code == 200, summary.text
            trades = client.get(f"/api/v1/backtest-runs/{run_id}/trades")
            assert trades.status_code == 200, trades.text
            result = json.loads(record.result_json)
            assert result["robustness"]["scenarios"][0]["id"] == "higher_slippage"
            ledger = json.loads(result["audit"]["pricePlanLedger"])
            evidence = ledger["sourceEvidence"]
            assert {item["snapshotId"] for item in evidence["minute"]["snapshots"]} == set(source_ids)
            assert evidence["corporateActions"] == corporate.evidence
            assert evidence["dailySignals"]["executionConfig"]["run_robustness"] is True
            assert evidence["dailySignals"]["executionConfig"]["capacity_mode"] == "point_in_time_volume"
            fills = [event for event in trades.json() if event["kind"] in {"fill", "partial_fill"}]
            assert {event["side"] for event in fills} == {"buy", "sell"}
            buy = next(event for event in fills if event["side"] == "buy")
            assert buy["quantity"] == 1695  # prior completed minute: 33900 shares x 5%
            assert buy["status"] == "partially_filled"
            assert buy["occurredAt"] == "2025-01-03T09:30:00+08:00"
            assert all(Decimal(str(event["price"])) > 0 for event in fills)
    finally:
        service.shutdown()


def test_daily_snapshots_combine_into_one_replay_with_source_ids(tmp_path):
    from datetime import timedelta
    from tests.unit.adapters.test_eastmoney_minute_snapshot import _make_rows, _make_collection
    from ashare_lab.adapters.market_data.eastmoney_minute_snapshot import EastmoneyMinuteSnapshotSpec
    root = tmp_path / "snapshots"
    first = _build(root)
    second_day = date(2025, 1, 3)
    collection = _make_collection(_make_rows())
    collection = replace(collection, requested_start=second_day, requested_end=second_day,
        retrieved_at=collection.retrieved_at + timedelta(days=1),
        rows=tuple(replace(r, timestamp=r.timestamp + timedelta(days=1)) for r in collection.rows))
    second = _build(tmp_path / "acquired", collection=collection,
        spec=EastmoneyMinuteSnapshotSpec(symbol="300059.SZ", start=second_day, end=second_day),
        captured_at=collection.retrieved_at)
    h = history([("10", "10")] * 4)
    controls = {}
    for snapshot in (first, second):
        bars = load_eastmoney_minute_snapshot(snapshot.path)
        day = bars[0].bar_end_at.date()
        row = next(r for r in h.rows if r.session_date == day)
        controls[day] = replace(row, raw_open=bars[0].open.amount, raw_close=bars[-1].close.amount,
            raw_high=max(b.high.amount for b in bars), raw_low=min(b.low.amount for b in bars),
            volume=sum(b.volume.value for b in bars), amount=sum(b.turnover for b in bars))
    h = replace(h, rows=tuple(controls.get(r.session_date, r) for r in h.rows))
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps(dict(schemaVersion="market-calendar.v1", provider="fixture",
        sourceSha256="a" * 64, sessions=["2025-01-02", "2025-01-03", "2025-01-06"])))
    strategy = SimpleNamespace(trading_plan=GridPlan(parameters=parameters(initial_shares=100)),
        backtest=SimpleNamespace(start=date(2025, 1, 2), end=second_day))
    acquirer = Mock(return_value=(second.path,))
    result, source_id, reconciliation, evidence = LocalMinuteGrid(root, calendar, minute_acquirer=acquirer).execute(strategy, h)
    acquirer.assert_called_once_with(h.instrument_id, (second_day,))
    assert source_id.startswith("eastmoney-minute-set:")
    assert len(reconciliation) == 2
    assert len(evidence["minute"]["snapshots"]) == 2
    assert result.equity[-1].observed_at.date() == second_day
    # A later capture of the same day replaces that entire session, not bars
    # or volume added on top of the earlier capture.
    newer = _build(root, collection=collection,
        spec=EastmoneyMinuteSnapshotSpec(symbol="300059.SZ", start=second_day, end=second_day),
        captured_at=collection.retrieved_at + timedelta(hours=1))
    acquirer.reset_mock()
    again, _, checks, evidence = LocalMinuteGrid(root, calendar, minute_acquirer=acquirer).execute(strategy, h)
    acquirer.assert_not_called()
    assert len(checks) == 2
    assert again.portfolio.cash == result.portfolio.cash
    assert {s["snapshotId"] for s in evidence["minute"]["snapshots"]} == {first.snapshot_id, newer.snapshot_id}
    # Overlap with a request is not evidence of full coverage.
    from ashare_lab.application.minute_replay_input import MinuteReplayDataError, MinuteReplayCoverageError
    strategy.backtest.end = date(2025, 1, 6)
    h = replace(h, rows=tuple(r for r in h.rows if r.session_date < date(2025, 1, 6)) +
        (replace(controls[second_day], session_date=date(2025, 1, 6)),))
    calendar.write_text(json.dumps(dict(schemaVersion="market-calendar.v1", provider="fixture",
        sourceSha256="a" * 64, sessions=["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07"])))
    with pytest.raises(MinuteReplayCoverageError) as coverage:
        LocalMinuteGrid(root, calendar).execute(strategy, h)
    assert coverage.value.available_start == date(2025, 1, 2)
    assert coverage.value.available_end == second_day
    # A failed provider attempt is not evidence that it lacks the missing day.
    failing_acquirer = Mock(side_effect=TimeoutError('fixture timeout'))
    with pytest.raises(TimeoutError):
        LocalMinuteGrid(root, calendar, minute_acquirer=failing_acquirer).execute(strategy, h)


@pytest.mark.parametrize("grid", [True, False])
def test_complete_suspension_returns_cash_only_without_requesting_minute_snapshot(tmp_path, grid):
    from decimal import Decimal
    from ashare_lab.domain.market_data import TradingStatus
    from ashare_lab.application.minute_result import minute_result_bundle
    from tests.unit.application.test_skill_backtest import _history, _row
    days = [date(2025, 1, day) for day in (2, 3, 6)]
    h = _history(tuple(_row(day, raw_open="10", status=TradingStatus.SUSPENDED) for day in days[:2]))
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps(dict(schemaVersion="market-calendar.v1", provider="fixture",
        sourceSha256="a" * 64, sessions=[day.isoformat() for day in days])))
    plan = (GridPlan(parameters=parameters(initial_shares=100)) if grid else
            ConditionalPlan(parameters=ConditionParameters(initial_shares=100,
                rules=[ConditionRule(kind="holding_period", side="sell", sessions=1)])))
    strategy = SimpleNamespace(trading_plan=plan, backtest=SimpleNamespace(start=days[0], end=days[1]))
    result, source_id, reconciliation, evidence = LocalMinuteGrid(tmp_path / "absent-minutes", calendar).execute(strategy, h)
    assert not result.portfolio.fills
    assert result.portfolio.cash.amount == plan.parameters.initial_cash_cny
    assert len(result.equity) == 2 and all(p.shares == 0 for p in result.equity)
    assert len(result.events) == 1 and result.events[0].reason == "security_not_trading"
    assert source_id.startswith("mx-nontrading:")
    assert evidence["minute"]["status"] == "not_required_no_trading_sessions"
    bundle = minute_result_bundle(run_id="halt", result=result, initial_cash=plan.parameters.initial_cash_cny,
                                  snapshot_id=source_id, reconciliation=reconciliation, source_evidence=evidence)
    assert bundle.summary.total_return == 0
    assert bundle.summary.final_equity_cny == Decimal("1000000")
    assert bundle.summary.data_range.sessions == 2
    assert bundle.summary.benchmark_return is None
    from ashare_lab.application.minute_replay_input import MinuteReplayDataError
    with pytest.raises(MinuteReplayDataError, match="security_session_missing"):
        LocalMinuteGrid(tmp_path / "absent-minutes", calendar).execute(strategy, replace(h, rows=h.rows[:1]))


def test_daily_holding_uses_calendar_without_minute_adapter_and_retries_after_suspension(tmp_path):
    from ashare_lab.domain.market_data import TradingStatus
    from tests.unit.application.test_skill_backtest import _history, _row
    days = [date(2025, 1, day) for day in (2, 3, 6)]
    h = _history((_row(date(2024, 12, 31), raw_open="10"),
                  _row(days[0], raw_open="10"),
                  _row(days[1], raw_open="10", status=TradingStatus.SUSPENDED),
                  _row(days[2], raw_open="11")))
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps(dict(schemaVersion="market-calendar.v1", provider="fixture",
        sourceSha256="a" * 64, sessions=[day.isoformat() for day in days])))
    catalog = load_catalog_directory(Path(__file__).parents[3] / "catalogs")
    manifest = next(m for m in catalog.manifests if m.catalog_id == "cn_a.signals")
    plan = ConditionalPlan(parameters=ConditionParameters(initial_shares=100, slippage_bps=0,
        observation="daily_close", rules=[ConditionRule(kind="holding_period", side="sell", sessions=1)]))
    candidate = CandidateAst("300059.SZ", (), (), .99, trading_plan=plan,
                             backtest_start=days[0], backtest_end=days[-1])
    compiler = StrategyCompiler(catalog=catalog, generator=SimpleNamespace(generate=AsyncMock(return_value=(candidate,))),
                                catalog_id=manifest.catalog_id, release_version=manifest.release_version)
    store = InMemoryBacktestRunStore()
    service = SkillBacktestService(history=SimpleNamespace(load=AsyncMock(return_value=h)), indicators=object(), store=store,
        market_calendar_loader=LocalMinuteGrid(tmp_path / "no-minutes", calendar).load_calendar)
    service.queue.shutdown()
    service.queue = SimpleNamespace(enqueue=lambda _: None, shutdown=lambda: None)
    try:
        with TestClient(create_app(compiler=compiler, backtest_submission=service, run_store=store)) as client:
            response = client.post("/api/v1/strategy-drafts", json={"utterance": "东方财富买100股，持有1个交易日卖出",
                "instrument_context": "300059.SZ", "as_of_date": "2025-01-06"})
            assert response.status_code == 201, response.text
            assert response.json()["status"] == "ready"
            queued = client.post("/api/v1/backtest-runs", json={"strategy": response.json()["strategy"],
                "config": {"capacityMode": "unlimited"}})
            assert queued.status_code == 202, queued.text
            record = service.execute(RunId(queued.json()["id"]))
            assert record.state.value == "succeeded", record
            ledger = json.loads(json.loads(record.result_json)["audit"]["pricePlanLedger"])
            sell_orders = [order for order in ledger["orders"] if order["side"] == "sell"]
            assert [(order["date"], order["filled_quantity"]) for order in sell_orders] == [(str(days[1]), 0), (str(days[2]), 100)]
            assert sell_orders[0]["reason"] == "security_suspended"
            assert sell_orders[1]["signal_date"] == str(days[1])
            assert sell_orders[1]["signal_at"] == "2025-01-03T09:30:00+08:00"
            assert sell_orders[1]["effective_at"] == "2025-01-06T09:30:00+08:00"
            trades = client.get(f"/api/v1/backtest-runs/{record.run_id.value}/trades").json()
            sell_signals = [event for event in trades if event["side"] == "sell" and event["kind"] == "signal"]
            assert all(event["occurredAt"] == "2025-01-03T09:30:00+08:00" for event in sell_signals)
            assert ledger["provenance"]["market_calendar"]["sessions"] == [str(day) for day in days]
            assert ledger["summary"]["final_shares"] == 0
    finally:
        service.shutdown()


@pytest.mark.parametrize("kind", ["grid", "conditional", "grid_capped", "conditional_capped",
                                 "holding", "scheduled", "scheduled_without_minute_adapter"])
def test_existing_draft_queue_and_report_routes_execute_pinned_minute_snapshot(tmp_path, kind):
    snapshot = _build(tmp_path / "snapshots")
    minutes = load_eastmoney_minute_snapshot(snapshot.path)
    h = history([("10", "9"), ("9", "10"), ("10", "10"), ("10", "10")])
    day = date(2025, 1, 2)
    source = next(row for row in h.rows if row.session_date == day)
    control = replace(source, raw_open=minutes[0].open.amount, raw_close=minutes[-1].close.amount,
                      raw_high=max(b.high.amount for b in minutes), raw_low=min(b.low.amount for b in minutes),
                      volume=sum(b.volume.value for b in minutes), amount=sum(b.turnover for b in minutes))
    # Exact reconciliation needs no rounding allowance, even with fractional yuan.
    h = replace(h, rows=tuple(control if r.session_date == day else r for r in h.rows))
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps(dict(schemaVersion="market-calendar.v1", provider="fixture",
                                        sourceSha256="a" * 64, sessions=["2024-12-31", "2025-01-02", "2025-01-03"])))
    catalog = load_catalog_directory(Path(__file__).parents[3] / "catalogs")
    manifest = next(m for m in catalog.manifests if m.catalog_id == "cn_a.signals")
    plan = (GridPlan(parameters=parameters(initial_shares=100)) if kind.startswith("grid") else
            ConditionalPlan(parameters=ConditionParameters(observation="minute_bar", rules=[
                ConditionRule(kind="price", side="buy", direction="down", target_price=100)])))
    capped = kind.endswith("_capped")
    if kind == "grid_capped":
        plan = GridPlan(parameters=parameters(initial_shares=0, anchor_price=11))
    if kind == "holding":
        plan = ConditionalPlan(parameters=ConditionParameters(observation="minute_bar", initial_shares=100,
                                                              rules=[ConditionRule(kind="holding_period", side="sell", sessions=1)]))
    wants_schedule = kind.startswith("scheduled")
    if wants_schedule:
        plan = ScheduledPlan(parameters=ScheduledParameters(frequency="once", budget_cny=10000))
    candidate = CandidateAst("300059.SZ", (), (), .99,
                             trading_plan=plan,
                             backtest_start=day, backtest_end=day)
    compiler = StrategyCompiler(catalog=catalog, generator=SimpleNamespace(generate=AsyncMock(return_value=(candidate,))),
                                catalog_id=manifest.catalog_id, release_version=manifest.release_version)
    store = InMemoryBacktestRunStore()
    action_loader = Mock(return_value=PricePlanCorporateInputs(TimelineCorporateActionApplier(()), (),
                         dict(provider="fixture", status="verified_empty", sourceSha256="b" * 64)))
    service = SkillBacktestService(history=SimpleNamespace(load=AsyncMock(return_value=h)),
                                  indicators=object(), store=store,
                                  runtime_evidence=dict(catalog_hash=catalog.content_hash,
                                      code_revision="fixture-worktree", engine_version="fixture-engine"),
                                  minute_grid=(None if kind == "scheduled_without_minute_adapter" else
                                      LocalMinuteGrid(tmp_path / ("no-minute-data" if wants_schedule else "snapshots"), calendar, action_loader)),
                                  market_calendar_loader=LocalMinuteGrid(tmp_path / "unused", calendar).load_calendar,
                                  signal_corporate_loader=action_loader if kind == "scheduled_without_minute_adapter" else None)
    service.queue.shutdown()
    service.queue = SimpleNamespace(enqueue=lambda _: None, shutdown=lambda: None)
    try:
        with TestClient(create_app(compiler=compiler, backtest_submission=service, run_store=store)) as client:
            draft_response = client.post("/api/v1/strategy-drafts", json={
                "utterance": "东方财富网格，基准10元，每格1元，每次100股，初始买100股",
                "instrument_context": "300059.SZ", "as_of_date": "2025-01-02"})
            assert draft_response.status_code == 201, draft_response.text
            draft = draft_response.json()
            assert draft["status"] == "ready", draft
            queued = client.post("/api/v1/backtest-runs", json={"strategy": draft["strategy"],
                "config": {} if wants_schedule else {
                    "capacityMode": "point_in_time_volume" if capped else "unlimited",
                    "participationRate": "0.0057"}})
            assert queued.status_code == 202, queued.text
            run_id = queued.json()["id"]
            record = service.execute(RunId(run_id))
            assert record.state.value == "succeeded", record
            summary = client.get(f"/api/v1/backtest-runs/{run_id}/summary")
            assert summary.status_code == 200, summary.text
            identity = summary.json()["runEvidence"]
            from ashare_lab.domain.strategy import canonical_hash
            assert identity["strategyHash"] == canonical_hash(draft["strategy"])
            assert identity["catalogHash"] == catalog.content_hash
            assert identity["codeRevision"] == "fixture-worktree"
            assert identity["engineVersion"] == "fixture-engine"
            assert identity["dataSnapshotId"].startswith("price-plan-input:")
            assert identity["dataSnapshotChecksum"] == "sha256:" + identity["dataSnapshotId"].split(":", 1)[1]
            trades = client.get(f"/api/v1/backtest-runs/{run_id}/trades").json()
            fills = [event for event in trades if event["kind"] in {"fill", "partial_fill"}]
            if capped:
                # Fixture 09:31 includes the 1,000-share auction plus 10,000
                # continuous shares: floor(11,000 * 0.0057) = 62.
                assert [event["quantity"] for event in fills] == [62, 38]
                assert [event["status"] for event in fills] == ["partially_filled", "filled"]
            quality = "daily_bar_open_proxy" if wants_schedule else "minute_bar_end_proxy"
            assert fills and all(event["timeQuality"] == quality for event in fills)
            assert all(event["executionDetails"]["signalAt"] and event["executionDetails"]["effectiveAt"] for event in fills)
            assert ("mx-daily:" if wants_schedule else snapshot.snapshot_id) in record.result_json
            ledger = json.loads(json.loads(record.result_json)["audit"]["pricePlanLedger"])
            evidence = ledger["sourceEvidence"]
            if not wants_schedule:
                assert ledger["replay"]["capacity_assumption"] == (
                    "previous_completed_minute_volume:0.0057" if capped else "unlimited_ohlc_research")
                assert evidence["executionConfig"]["participation_rate"] == "0.0057"
                assert evidence["executionConfig"]["capacity_mode"] == (
                    "point_in_time_volume" if capped else "unlimited")
            action_loader.assert_called_once()
            assert evidence["corporateActions"]["status"] == "verified_empty"
            assert ledger["replay"]["corporate_action_policy"] is not None
            assert evidence["calendar"]["sessions"] == ["2024-12-31", "2025-01-02", "2025-01-03"]
            assert evidence["dailyControls"]["instrumentId"] == "300059.SZ"
            assert len(evidence["dailyControls"]["rows"]) == (2 if wants_schedule else 1)
            assert len(evidence["calendar"]["fileSha256"]) == 64
    finally:
        service.shutdown()
