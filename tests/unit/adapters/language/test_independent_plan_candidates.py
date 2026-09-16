from datetime import date

import pytest
from pydantic import ValidationError

from ashare_lab.adapters.language.vibe_candidates import (
    BoundedCandidate, _to_candidate_ast, _translate_transport_payload, _has_minute_price_plan,
)
from ashare_lab.ports.candidate_generation import CompileInput
from tests.contract.api.test_independent_price_plan_contract import pair_strategy


@pytest.mark.parametrize('entry_kind', ['grid', 'conditional', 'scheduled'])
@pytest.mark.parametrize('exit_kind', ['grid', 'conditional', 'scheduled'])
def test_pair_candidate_requires_both_sources_and_preserves_legs(entry_kind, exit_kind):
    pair = pair_strategy(entry_kind, exit_kind).independent_plans
    raw = dict(independent_plans=pair.model_dump(mode='json'), confidence=.9,
               entry_plan_span=dict(start=0, end=4, text='买入计划'),
               exit_plan_span=dict(start=5, end=9, text='卖出计划'))
    candidate = BoundedCandidate.model_validate(raw)
    request = CompileInput(utterance='买入计划；卖出计划', as_of_date=date(2026, 9, 16))
    ast = _to_candidate_ast(candidate, request, provenance=None)
    assert ast.independent_plans == pair
    assert ast.unsupported_code is None
    assert _has_minute_price_plan(candidate) == ('scheduled' not in (entry_kind, exit_kind)
                                                or entry_kind != exit_kind)
    assert {item.path for item in ast.grounding_evidence} == {
        '/independent_plans/entry_plan', '/independent_plans/exit_plan'}
    with pytest.raises(ValidationError, match='分别引用'):
        BoundedCandidate.model_validate({**raw, 'exit_plan_span': None})
    with pytest.raises(ValidationError, match='占用'):
        BoundedCandidate.model_validate({**raw, 'trading_plan': raw['independent_plans']['entry_plan']})
    with pytest.raises(ValidationError, match='初始资金'):
        BoundedCandidate.model_validate({**raw, 'initial_cash_cny': 200000})
    forged = candidate.model_copy(update={'exit_plan_span': candidate.entry_plan_span.model_copy(
        update={'text': '每天买入'})})
    with pytest.raises(ValueError):
        _to_candidate_ast(forged, request, provenance=None)


@pytest.mark.asyncio
async def test_pair_survives_full_candidate_translation():
    from tests.unit.adapters.language.test_vibe_candidates import CAPABILITY_MATRIX
    pair = pair_strategy('scheduled', 'conditional').independent_plans
    buy = '每周四买入100股'
    sell = '涨到11元卖出100股，重复2次'
    utterance = buy + '；' + sell
    raw = dict(independent_plans=pair.model_dump(mode='json'), confidence=.9,
               entry_plan_span=dict(start=0, end=len(buy), text=buy),
               exit_plan_span=dict(start=len(buy)+1, end=len(utterance), text=sell))
    feedback = []
    result = _translate_transport_payload({'candidates': [raw]},
        request=CompileInput(utterance=utterance, as_of_date=date(2026, 9, 16),
                             instrument_context='300059.SZ'),
        matrix=CAPABILITY_MATRIX, provider_identity=None, min_confidence=.7,
        validation_feedback=feedback)
    assert not feedback
    assert len(result) == 1
    assert result[0].unsupported_code is None
    assert result[0].independent_plans == pair
    from unittest.mock import AsyncMock, Mock
    from ashare_lab.application.compile_strategy import StrategyCompiler, CompileStatus
    from tests.unit.adapters.language.test_vibe_candidates import CATALOG, CATALOG_RELEASE
    compiler = StrategyCompiler(generator=Mock(generate=AsyncMock(return_value=result)),
        catalog=CATALOG, catalog_id='cn_a.signals', release_version=CATALOG_RELEASE)
    outcome = await compiler.compile(CompileInput(utterance=utterance,
        as_of_date=date(2026, 9, 16), instrument_context='300059.SZ'))
    assert outcome.status is CompileStatus.READY, outcome
    assert outcome.strategy.independent_plans == pair
    assert outcome.strategy.instrument.symbol == '300059.SZ'
