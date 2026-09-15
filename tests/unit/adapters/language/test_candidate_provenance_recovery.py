from datetime import date
from unittest.mock import Mock

import pytest

from ashare_lab.adapters.language.vibe_candidates import VibeBoundedCandidateGenerator
from ashare_lab.adapters.market_data.instrument_name_chain import (
    InstrumentNameProviderUnavailableError,
)
from ashare_lab.ports.candidate_generation import CompileInput
from tests.unit.adapters.language.test_candidate_semantic_review import provider_verdict
from tests.unit.adapters.language.test_vibe_candidates import CAPABILITY_MATRIX, _indicator_payload


def span(text, quote):
    start = text.index(quote)
    return {'start': start, 'end': start + len(quote), 'text': quote}


def payload(text, *, days=3, missing_name=False):
    return {'candidates': [{
        'instrument_name': None if missing_name else '贵州茅台',
        'instrument_span': None if missing_name else span(text, '贵州茅台'),
        'entry': [
            _indicator_payload('price.consecutive_up', 'at_least', {'days': 3}),
            _indicator_payload('price.return_pct', 'above',
                               {'period': days, 'price_field': 'close'}, value=5),
        ],
        'exit': [_indicator_payload('technical.ma', 'price_crosses_below',
                                   {'period': 10, 'price_field': 'close'})],
        'entry_spans': [
            span(text, '贵州茅台连续3个交易日上涨'), span(text, '且累计涨幅超过5%时买入'),
        ],
        'exit_spans': [span(text, '跌破10日均线卖')],
        'entry_join': 'all', 'exit_join': 'any', 'confidence': 0.95,
        'defaulted_fields': ['/entry/1/params/period', '/entry/1/params/price_field',
                             '/exit/0/params/price_field'],
    }]}


TEXT = '贵州茅台连续3个交易日上涨，且累计涨幅超过5%时买入；跌破10日均线卖'


class Transport:
    def __init__(self, body, identity='equivalent'):
        self.body, self.identity = body, identity
        self.requests = []

    async def generate_json(self, request):
        self.requests.append(request)
        if request.response_schema_name == 'strategy_semantic_review':
            return {**provider_verdict(request), 'instrument': self.identity}
        return self.body


@pytest.mark.asyncio
async def test_wrong_default_tag_is_removed_before_review_without_changing_rules():
    body = payload(TEXT)
    transport = Transport(body)
    resolver = Mock(return_value='600519.SH')
    result = await VibeBoundedCandidateGenerator(
        transport, capability_matrix=CAPABILITY_MATRIX, model_semantic_review=True,
        instrument_name_resolver=resolver,
    ).generate(CompileInput(TEXT, date(2026, 9, 9)))
    assert result[0].unsupported_code is None
    assert result[0].entry[0].params_dict()['days'] == 3
    assert result[0].entry[1].params_dict()['period'] == 3
    assert result[0].entry[1].value == 5
    assert result[0].entry_join == 'all'
    assert result[0].exit[0].params_dict()['period'] == 10
    assert result[0].exit[0].trigger == 'price_crosses_below'
    resolver.assert_called_once_with('贵州茅台')
    review = transport.requests[1]
    assert review.user_payload['verifiedInstrument'] == {
        'symbol': '600519.SH', 'matchedUserText': '贵州茅台',
        'sourceSpan': {'start': 0, 'end': 4},
    }
    # The provider input itself stays untouched; no extra generation is needed.
    assert '/entry/1/params/period' in body['candidates'][0]['defaulted_fields']
    assert len(transport.requests) == 2


@pytest.mark.asyncio
async def test_unspoken_non_default_number_is_not_promoted_to_a_default():
    transport = Transport(payload(TEXT, days=9))
    result = await VibeBoundedCandidateGenerator(
        transport, capability_matrix=CAPABILITY_MATRIX, model_semantic_review=True,
    ).generate(CompileInput(TEXT, date(2026, 9, 9)))
    assert result[0].unsupported_code == 'candidate_provider_invalid_output'
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_missing_identity_evidence_routes_to_resolution_not_repeated_schema_repair():
    transport = Transport(payload(TEXT, missing_name=True), identity='uncertain')
    result = await VibeBoundedCandidateGenerator(
        transport, capability_matrix=CAPABILITY_MATRIX, model_semantic_review=True,
        repair_invalid_output=True,
    ).generate(CompileInput(TEXT, date(2026, 9, 9)))
    assert result[0].unsupported_code == 'instrument_unconfirmed'
    assert len(transport.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('error,code', [
    (InstrumentNameProviderUnavailableError('offline'), 'instrument_resolution_unavailable'),
    (LookupError('ambiguous'), 'instrument_unconfirmed'),
])
async def test_lookup_failure_is_not_called_a_parser_error(error, code):
    transport = Transport(payload(TEXT))
    resolver = Mock(side_effect=error)
    result = await VibeBoundedCandidateGenerator(
        transport, capability_matrix=CAPABILITY_MATRIX, model_semantic_review=True,
        instrument_name_resolver=resolver,
    ).generate(CompileInput(TEXT, date(2026, 9, 9)))
    assert result[0].unsupported_code == code
    assert len(transport.requests) == 1
