"""Stored legacy threshold questions must recover through the HTTP dialogue path."""
import asyncio
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ashare_lab.adapters.language import RuleBasedCandidateGenerator
from ashare_lab.api import create_app
from ashare_lab.api.store import InMemoryDraftStore
from ashare_lab.application.compile_strategy import CompileStatus, StrategyCompiler
from ashare_lab.domain.catalog import load_catalog_directory
from ashare_lab.ports.candidate_generation import CompileInput


@pytest.mark.parametrize('answer', ['可以，按你说的来', '20', '是'])
def test_legacy_threshold_confirmation_is_consumed_once_over_http(answer):
    compiler = StrategyCompiler(generator=RuleBasedCandidateGenerator(),
        catalog=load_catalog_directory(Path(__file__).resolve().parents[3] / 'catalogs'),
        catalog_id='cn_a.signals', release_version='2026.09.01',
        trusted_date_provider=lambda: date(2026, 9, 11))
    source = CompileInput(utterance='KDJ的J值低于20买入，MACD死叉卖出',
        instrument_context='000001.SZ', as_of_date=date(2026, 9, 11))
    ready = asyncio.run(compiler.compile(source))
    assert ready.status is CompileStatus.READY
    question = ('买入条件「kdj超卖买入」的数值阈值尚未明确；当前候选值20是建议，'
        '不是指标目录默认值，请确认指标口径及阈值。其他已明确条件保留。')
    legacy = replace(ready, status=CompileStatus.NEEDS_CLARIFICATION,
        strategy=None, strategy_hash=None, diagnostic_code='semantic_confirmation_required',
        clarification=question, semantic_review_issues=(question,),
        suggested_strategy=ready.strategy, suggested_strategy_hash=ready.strategy_hash)
    store = InMemoryDraftStore()
    stored = asyncio.run(store.create(outcome=legacy,
        compile_input=replace(source, utterance='平安银行，kdj超卖买入，MACD死叉卖出'),
        request_hash='legacy-threshold-fixture', idempotency_key=None)).value
    with TestClient(create_app(compiler=compiler, draft_store=store)) as client:
        response = client.post(f'/api/v1/strategy-drafts/{stored.draft_id}'
            f'/revisions/{stored.revision}/clarification-answers', json={'answer': answer})
    assert response.status_code == 200, response.text
    draft = response.json()['draft']
    assert draft['status'] == 'ready', response.text
    assert draft['revision'] == 2
    assert draft['strategy'] == ready.strategy.model_dump(mode='json')
    assert not response.json().get('run_requested')
    latest = asyncio.run(store.load_latest_dialogue_state(draft_id=stored.draft_id))
    assert not latest.outcome.run_requested
    assert not latest.outcome.refresh_data
