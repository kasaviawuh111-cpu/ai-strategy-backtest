"""Pinned fixture wiring, not acceptance of a live data provider."""
import json
import pytest
from types import SimpleNamespace
from decimal import Decimal
from ashare_lab.ports.provider_indicator_data import ProviderIndicatorValue

from ashare_lab.application.backtest_submission import BacktestRunConfig
from ashare_lab.application.corporate_action_timeline import TimelineCorporateActionApplier
from ashare_lab.application.mx_daily_share_replay import replay_mx_daily_shares
from tests.unit.application.test_daily_backtest import bars, strategy, _provider_fact
from tests.unit.application.test_skill_backtest import _history, _row


@pytest.mark.parametrize("change_prior_at_execution", [False, True])
def test_boundary_ex_date_preparation_and_worker_share_recovery(change_prior_at_execution, monkeypatch, caplog):
    import asyncio
    from dataclasses import asdict, replace
    from datetime import date, timedelta
    from unittest.mock import AsyncMock
    from ashare_lab.adapters.market_data.local_price_plan_actions import source_backed_price_rebases
    from ashare_lab.adapters.persistence.backtest_runs import InMemoryBacktestRunStore
    from ashare_lab.application.local_minute_grid import PricePlanCorporateInputs
    from ashare_lab.application.skill_backtest_service import SkillBacktestService
    from ashare_lab.domain.market_data import CorporateActionKind
    from ashare_lab.domain.shared import InstrumentId
    from tests.unit.application.test_skill_backtest import _strategy, _condition, _config, SYMBOL
    from tests.unit.portfolio.test_corporate_actions import _action

    previous = date(2025, 1, 2)
    days = tuple(previous + timedelta(days=i) for i in range(1, 7))
    rows = tuple(_row(day, raw_open=price)
                 for day, price in zip(days, ("9.5", "9", "11", "12", "8", "7")))
    history = _history(rows)
    history = replace(history, query_evidence=(replace(history.query_evidence[0], purpose="raw_prices"),))
    supplement = _history((_row(previous, raw_open="10"), rows[0]))
    supplement = replace(supplement, query_evidence=(replace(supplement.query_evidence[0],
        purpose="raw_prices", response_sha256="sha256:" + "b" * 64),))
    supplement_reads = 0

    async def load_history(*, start, **kwargs):
        nonlocal supplement_reads
        if start != previous:
            return history
        supplement_reads += 1
        if change_prior_at_execution and supplement_reads == 3:
            return replace(supplement, rows=(replace(supplement.rows[0], raw_close=Decimal("9.99")), rows[0]))
        return supplement

    action = _action(CorporateActionKind.CASH_DIVIDEND, cash_per_share=Decimal(".5"),
                     pay_date=date(2025, 1, 6))
    action_result = SimpleNamespace(instrument_id=InstrumentId(SYMBOL), start=days[0],
                                   end=days[-1], corporate_actions=(action,))
    context_first_days = []

    def corporate_loader(spec, context, *, required_start):
        assert required_start == days[0]
        context_first_days.append(context.rows[0].session_date)
        rebases = source_backed_price_rebases(action_result, context)
        return PricePlanCorporateInputs(TimelineCorporateActionApplier((action,)), rebases,
                                       {"priceRebases": [asdict(item) for item in rebases]})

    calendar_days = (previous, *days, days[-1] + timedelta(days=1))
    calendar = dict(provider="fixture", sourceSha256="a" * 64,
                    sessions=[day.isoformat() for day in calendar_days])
    service = SkillBacktestService(history=SimpleNamespace(load=AsyncMock(side_effect=load_history)),
        indicators=object(), store=InMemoryBacktestRunStore(), signal_corporate_loader=corporate_loader,
        market_calendar_loader=lambda: (calendar_days, calendar, json.dumps(calendar).encode()))
    monkeypatch.setattr(service.queue, "enqueue", lambda _: None)
    condition = _condition(trigger="price_crosses_above").model_copy(
        update={"params": {"period": 2, "price_field": "close"}})
    spec = _strategy(days, holding_sessions=1).model_copy(update={"entry": condition})
    config = _config(run_robustness=False)
    try:
        # Neither preparation nor the worker is stubbed; only source retrieval
        # is pinned. A stateless action loader forces recovery in both stages.
        prepared = asyncio.run(service.prepare_candidate(spec, config))
        assert prepared.history is history
        assert len(prepared.entry_timeline) == len(rows)
        created = service.submit(spec, config, prepared_inputs=True)
        finished = service.execute(created.record.run_id)
        assert supplement_reads == 3
        assert context_first_days == [days[0], previous] * 3
        if change_prior_at_execution:
            assert finished.state.value == "failed"
            assert finished.error_code == "corporate_action_data_unavailable"
            assert "invariant=corporate_action_source_changed_after_signal_preparation" in caplog.text
            assert finished.result_json is None
        else:
            assert finished.state.value == "succeeded", finished
            saved = json.loads(finished.result_json)
            ledger = json.loads(saved["audit"]["pricePlanLedger"])
            assert len(ledger["portfolio"]["fills"]) == 2
            assert ledger["sourceEvidence"]["history"][0]["session_date"] == days[0].isoformat()
            assert ledger["sourceEvidence"]["signalAdjustmentSource"] == prepared.signal_adjustment_source
    finally:
        service.shutdown()


def test_mx_signals_produce_actual_shares_fees_and_funded_report():
    source = bars(("10",) * 6)
    history = _history(tuple(_row(bar.session_date, raw_open="10") for bar in source))
    source_value = ProviderIndicatorValue(field_code="ROE_TTM_RPT", field_name="value", value=Decimal('13'),
        source_parameters=json.dumps({"policy": "announcement_date_next_trading_session.v1", "revisionRisk": True}))
    prepared = SimpleNamespace(history=history, signal_adjustment_source=None,
        indicator_series=(SimpleNamespace(provider="fixture", indicator_id="provider.numeric",
            response_sha256="sha256:" + "c" * 64, points=(SimpleNamespace(values=(source_value,)),)),),
        entry_timeline=tuple(_provider_fact(bar, condition_ref="entry", triggered=i == 0) for i, bar in enumerate(source)),
        exit_timeline=tuple(_provider_fact(bar, condition_ref="exit", triggered=i == 3) for i, bar in enumerate(source)))
    from datetime import timedelta
    dates = tuple(bar.session_date for bar in source) + (source[-1].session_date + timedelta(days=1),)
    calendar = dict(provider="fixture", sourceSha256="a" * 64, sessions=[day.isoformat() for day in dates])
    corporate = SimpleNamespace(applier=TimelineCorporateActionApplier(()), price_rebases=(), evidence=dict(status="source_export_empty"))
    bundle = replay_mx_daily_shares(run_id="share-fixture", strategy=strategy(), prepared=prepared,
        config=BacktestRunConfig(), calendar_input=(dates, calendar, json.dumps(calendar).encode()), corporate=corporate)
    payload = bundle.model_dump(mode="json", by_alias=True)
    fills = [a for a in payload["activities"] if a["kind"] == "fill"]
    assert len(fills) == 2 and all(a["quantity"] > 0 and a["quantity"] % 100 == 0 for a in fills)
    assert payload["audit"]["openPositionShares"] == 0
    assert payload["summary"]["benchmarkReturn"] is not None
    ledger = json.loads(payload["audit"]["pricePlanLedger"])
    assert len(ledger["portfolio"]["fills"]) == 2
    assert ledger["sourceEvidence"]["indicatorSources"][0]["fields"][0]["source_parameters"] == source_value.source_parameters
    assert any("较晚更新可能包含修订" in w for w in payload["summary"]["warnings"])
    from hashlib import sha256
    assert ledger["sourceEvidence"]["calendarSha256"] == "sha256:" + sha256(json.dumps(calendar).encode()).hexdigest()
    assert ledger["sourceEvidence"]["calendarSessions"] == [day.isoformat() for day in dates]
    changed_calendar = replay_mx_daily_shares(run_id="share-fixture", strategy=strategy(), prepared=prepared,
        config=BacktestRunConfig(), calendar_input=(dates, calendar, b"different-calendar-source"), corporate=corporate)
    changed_ledger = json.loads(changed_calendar.model_dump(mode="json", by_alias=True)["audit"]["pricePlanLedger"])
    assert changed_ledger["sourceId"] != ledger["sourceId"]
    # Exercise the actual worker branch as well as the adapter. Only provider
    # preparation is stubbed; execution and result serialization remain real.
    from unittest.mock import AsyncMock
    from ashare_lab.adapters.persistence.backtest_runs import InMemoryBacktestRunStore
    from ashare_lab.application.skill_backtest_service import SkillBacktestService
    prepared.indicator_series = ()
    prepared.signal_adjustment_source = json.dumps(corporate.evidence, default=str, sort_keys=True)
    service = SkillBacktestService(history=object(), indicators=object(), store=InMemoryBacktestRunStore(),
        signal_corporate_loader=lambda *args, **kwargs: corporate,
        market_calendar_loader=lambda: (dates, calendar, json.dumps(calendar).encode()),
        runtime_evidence=dict(
            catalog_hash="sha256:" + "b" * 64,
            code_revision="fixture-local-dirty",
            engine_version="fixture-engine",
            opening_auction_policy=(
                "cn.a_share.daily.published_open_proxy.latency_1s.cutoff_0915."
                "recorded_0930.not_exact.day_order.v2"
            ),
            benchmark_policy="funded_buy_and_hold.same_cash_same_execution.v1",
            corporate_action_policy="timeline_as_of_publication.v1",
            dividend_tax_policy="gross_research_no_withholding.v1",
            rights_issue_policy="fail_closed_without_explicit_participation.v1",
        ))
    service.queue.shutdown()
    service.queue = SimpleNamespace(enqueue=lambda _: None, shutdown=lambda: None)
    from ashare_lab.ports.backtest_runs import BacktestJobState
    async def prepare(*args, run_id, **kwargs):
        service._stage(run_id, BacktestJobState.RUNNING_SIGNAL, 45, "fixture signal preparation")
        return prepared
    service._prepare_candidate = AsyncMock(side_effect=prepare)
    created = service.submit(strategy(), BacktestRunConfig())
    finished = service.execute(created.record.run_id)
    assert finished.state.value == "succeeded", finished
    saved = json.loads(finished.result_json)
    assert saved["audit"]["openPositionShares"] == 0
    assert saved["summary"]["runEvidence"]["catalogHash"] == "sha256:" + "b" * 64
    assert saved["summary"]["runEvidence"]["codeRevision"] == "fixture-local-dirty"
    assumptions = saved["summary"]["runEvidence"]["executionAssumptions"]
    assert assumptions["edge_entry_validity_sessions"] == "1"
    assert assumptions["entry_signal_validity_policy"].endswith(
        "persistent_account_exits.explicit_new_entry_occurrences.v5")
    warnings = saved["summary"]["warnings"]
    assert not any("未声明当前日线开盘价代理策略" in item for item in warnings)
    assert not any("未声明公司行动账本政策" in item for item in warnings)
    assert len(json.loads(saved["audit"]["pricePlanLedger"])["portfolio"]["fills"]) == 2
