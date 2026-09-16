"""Synthetic source data, real preparation/queue/ledger/report integration.

This matrix does not claim natural-language or public-version acceptance.
"""
import json
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from ashare_lab.adapters.market_data.eastmoney_minute_snapshot import (
    EastmoneyMinuteSnapshotSpec, load_eastmoney_minute_snapshot,
)
from ashare_lab.adapters.persistence.backtest_runs import InMemoryBacktestRunStore
from ashare_lab.api import create_app
from ashare_lab.application.corporate_action_timeline import TimelineCorporateActionApplier
from ashare_lab.application.local_minute_grid import LocalMinuteGrid, PricePlanCorporateInputs
from ashare_lab.application.skill_backtest_service import SkillBacktestService
from ashare_lab.domain.shared import RunId
from ashare_lab.domain.strategy import ComposedExecutionPolicy, StrategySpec
from tests.unit.adapters.test_eastmoney_minute_snapshot import _build, _make_collection, _make_rows
from tests.unit.application.test_skill_backtest import _history, _row


@pytest.fixture
def pair_sources(tmp_path):
    days = (date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6))
    controls = []
    for day, later_price in zip(days, (9, 11, 9)):
        collection = _make_collection(_make_rows())
        delta = timedelta(days=(day - days[0]).days)
        rows = tuple(replace(row, timestamp=row.timestamp + delta,
            open=Decimal(10 if i < 2 else later_price),
            high=Decimal(10 if i < 2 else later_price),
            low=Decimal(10 if i < 2 else later_price),
            close=Decimal(10 if i < 2 else later_price),
            amount_cny=row.volume_shares * Decimal(10 if i < 2 else later_price))
            for i, row in enumerate(collection.rows))
        collection = replace(collection, requested_start=day, requested_end=day,
                             retrieved_at=collection.retrieved_at + delta, rows=rows)
        snapshot = _build(tmp_path / 'snapshots', collection=collection,
            spec=EastmoneyMinuteSnapshotSpec(symbol='300059.SZ', start=day, end=day),
            captured_at=collection.retrieved_at)
        bars = load_eastmoney_minute_snapshot(snapshot.path)
        controls.append(replace(_row(day, raw_open='10'), raw_close=bars[-1].close.amount,
            raw_high=max(b.high.amount for b in bars), raw_low=min(b.low.amount for b in bars),
            volume=sum(b.volume.value for b in bars), amount=sum(b.turnover for b in bars)))
    calendar = tmp_path / 'calendar.json'
    calendar.write_text(json.dumps(dict(schemaVersion='market-calendar.v1', provider='fixture',
        sourceSha256='a' * 64, sessions=[str(day) for day in (*days, date(2025, 1, 7))])))
    corporate = PricePlanCorporateInputs(TimelineCorporateActionApplier(()), (),
        dict(provider='fixture', status='verified_empty', sourceSha256='b' * 64))
    return tmp_path / 'snapshots', calendar, _history(tuple(controls)), corporate


def pair_strategy(entry_kind, exit_kind):
    def plan(kind, side):
        p = dict(initial_cash_cny=100000, slippage_bps=0)
        if kind == 'scheduled':
            p.update(frequency='weekly', day=4 if side == 'buy' else 5,
                     sizing_mode='shares', quantity=100, side=side)
        elif kind == 'grid':
            p.update(observation='minute_bar', anchor_mode='first_open', lower_price=8,
                     upper_price=12, spacing=1, price_mode='grid_limit', order_shares=100)
        else:
            p.update(observation='minute_bar', repeat_cycles=2, rules=[dict(kind='price',
                side=side, direction='down' if side == 'buy' else 'up', quantity=100,
                target_price=9 if side == 'buy' else 11)])
        return dict(kind=kind, parameters=p)
    payload = json.loads((Path(__file__).parents[3] /
        'contracts/examples/strategy.macd-volume.daily.v1.json').read_text())
    payload.update(entry=None, exit=None, execution=ComposedExecutionPolicy().model_dump(),
        independent_plans=dict(entry_plan=plan(entry_kind, 'buy'), exit_plan=plan(exit_kind, 'sell')))
    payload['backtest'].update(start='2025-01-02', end='2025-01-06', initial_cash_cny=100000)
    return StrategySpec.model_validate(payload)


@pytest.mark.parametrize('entry_kind', ['grid', 'conditional', 'scheduled'])
@pytest.mark.parametrize('exit_kind', ['grid', 'conditional', 'scheduled'])
def test_both_legs_reach_one_source_pinned_ledger_and_all_http_views(pair_sources, entry_kind, exit_kind):
    root, calendar, history, corporate = pair_sources
    strategy = pair_strategy(entry_kind, exit_kind)
    # A second loader must never replace already prepared corporate inputs.
    duplicate_loader = Mock(side_effect=AssertionError('unexpected second corporate source'))
    loader = Mock(return_value=corporate)
    store = InMemoryBacktestRunStore()
    service = SkillBacktestService(history=SimpleNamespace(load=AsyncMock(return_value=history)),
        indicators=object(), store=store, signal_corporate_loader=loader,
        minute_grid=LocalMinuteGrid(root, calendar, corporate_loader=duplicate_loader),
        runtime_evidence=dict(catalog_hash='sha256:' + 'c' * 64,
                              code_revision='fixture-worktree', engine_version='fixture'))
    service._signals = AsyncMock(side_effect=AssertionError('pair has no indicator tree'))
    service.queue.shutdown()
    service.queue = SimpleNamespace(enqueue=lambda _: None, shutdown=lambda: None)
    try:
        with TestClient(create_app(backtest_submission=service, run_store=store)) as client:
            prepared = client.post('/api/v1/backtest-runs/prepare', json={'strategy': strategy.model_dump(mode='json')})
            assert prepared.status_code == 200, prepared.text
            queued = client.post('/api/v1/backtest-runs', json={'strategy': strategy.model_dump(mode='json')})
            assert queued.status_code == 202, queued.text
            run_id = queued.json()['id']
            record = service.execute(RunId(run_id))
            assert record.state.value == 'succeeded', (record.error_code, record.progress_label)
            responses = {view: client.get(f'/api/v1/backtest-runs/{run_id}/{view}')
                         for view in ('summary', 'series', 'trades')}
            assert all(r.status_code == 200 for r in responses.values())
            result = json.loads(record.result_json)
            evidence = json.loads(result['audit']['pricePlanLedger'])['sourceEvidence']
            assert evidence['independentPlans'] == strategy.independent_plans.model_dump(mode='json')
            assert evidence['corporateActions'] == corporate.evidence
            fills = [e for e in result['activities'] if e['kind'] in {'fill', 'partial_fill'}]
            assert {e['side'] for e in fills} == {'buy', 'sell'}
            # This source has no corporate actions or opening inventory. Rebuild
            # positions and cash from actual fills: a partial exit is not a
            # closed position cycle, even when both legs have traded.
            shares = cycles = 0
            cash = Decimal('100000')
            for fill in fills:
                before = shares
                quantity = fill['quantity']
                shares += quantity if fill['side'] == 'buy' else -quantity
                assert shares >= 0
                cycles += int(before > 0 and shares == 0)
                gross = Decimal(str(fill['notionalCny']))
                fees = Decimal(str(fill['executionDetails']['totalFeesCny']))
                cash += (gross if fill['side'] == 'sell' else -gross) - fees
                assert cash >= 0
            assert result['summary']['tradeCountSemantics'] == 'closed_position_cycles'
            assert result['summary']['tradeCount'] == cycles
            assert result['audit']['openPositionShares'] == shares
            assert float(cash + Decimal(str(result['audit']['openPositionNotionalCny']))) == pytest.approx(
                result['summary']['finalEquityCny'], abs=0.01, rel=0)
            assert result['summary']['initialEquityCny'] == 100000
            assert result['series'][-1]['equity'] == pytest.approx(
                result['summary']['finalEquityCny'] / 100000 * 100)
            assert StrategySpec.model_validate_json(record.strategy_json).independent_plans == strategy.independent_plans
            service._signals.assert_not_called()
            duplicate_loader.assert_not_called()
    finally:
        service.shutdown()
