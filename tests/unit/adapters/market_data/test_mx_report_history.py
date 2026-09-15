"""The provider adapter must discard raw daily ROE, not only shift its dates."""
import asyncio
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ashare_lab.adapters.financial_sources.eastmoney_operator import EastmoneyOperatorReadingSource
from ashare_lab.adapters.market_data.mx_saas import MxSaasMarketDataClient, MxSaasProviderNoDataError
from ashare_lab.ports.live_market_data import LiveFinanceDataResult, LiveMarketDataProvenance


def response(source, dates, values, *, symbol='300059.SZ'):
    field = {'returnCode': 'metric', 'returnSourceCode': source, 'returnName': 'ROE(TTM)',
             'unitName': '%', 'unitDesc': '10802:%:%', 'dateGranularity': 'FISCAL_QUARTER'}
    return LiveFinanceDataResult(
        provider='fixture', query='fixture', indicators=None,
        tables=({'code': symbol, 'fieldSet': [field],
                 'rawTable': {'headName': dates, 'metric': values}},),
        provenance=LiveMarketDataProvenance(response_sha256='sha256:' + '1' * 64,
                   retrieved_at=datetime(2026, 9, 13, tzinfo=UTC), schema_version='fixture.v1'),
    )


def test_adapter_joins_report_dates_and_preserves_audit(monkeypatch):
    daily = response('ROETTM', ['2025-09-30', '2025-10-24', '2025-10-27'], ['.148', '.148', '.143'])
    quarterly = response('ROE_TTM_RPT', ['2025三季报', '2025中报'], ['14.25', '13'])
    rows = [
        {'SECUCODE': '300059.SZ', 'REPORT_DATE': '2025-06-30', 'NOTICE_DATE': '2025-08-16', 'UPDATE_DATE': '2026-08-22'},
        {'SECUCODE': '300059.SZ', 'REPORT_DATE': '2025-09-30', 'NOTICE_DATE': '2025-10-25', 'UPDATE_DATE': '2025-10-25'},
    ]
    monkeypatch.setattr(EastmoneyOperatorReadingSource, 'fetch_main_financial_data',
                        lambda *a, **k: SimpleNamespace(rows=rows, canonical_rows_sha256='2' * 64))
    client = MxSaasMarketDataClient(api_key='fixture-key')
    monkeypatch.setattr(client, 'query_finance', AsyncMock(side_effect=[daily, quarterly]))
    result = asyncio.run(client.query_finance_history(query='fixture', indicators=None,
                        instrument_id='300059.SZ', start=date(2025, 9, 12), end=date(2025, 10, 31)))
    table = result.tables[0]
    assert table['rawTable']['metric'] == ['13', '13', '14.25']
    assert table['reportAsOfAudit'][0]['revisionRisk'] is True
    assert table['fieldSet'][0]['originalSourceField']['dateGranularity'] == 'FISCAL_QUARTER'
    assert len(table['sourceResponseHashes']) == 3
    assert result.provenance.response_sha256 != daily.provenance.response_sha256


def test_other_metrics_do_not_trigger_report_lookup(monkeypatch):
    daily = response('PETTM', ['2025-09-12', '2025-09-15'], ['20', '21'])
    client = MxSaasMarketDataClient(api_key='fixture-key')
    query = AsyncMock(return_value=daily)
    monkeypatch.setattr(client, 'query_finance', query)
    result = asyncio.run(client.query_finance_history(query='fixture', indicators=None,
                        instrument_id='300059.SZ', start=date(2025, 9, 12), end=date(2025, 9, 15)))
    assert result is daily
    assert query.await_count == 1


@pytest.mark.parametrize('source,unit,name', [('ROEJQ', '108:%:%', '净资产收益率ROE(加权)'),
                                           ('YSTZ', '10802:%:%', '营业收入同比增长率')])
def test_weighted_roe_uses_disclosures_and_authoritative_sessions(monkeypatch, source, unit, name):
    original = response(source, ['2025三季报'], ['9'])
    quarterly = response(source, ['2025三季报', '2025中报'], ['9', '6'])
    quarterly.tables[0]['fieldSet'][0].update(unitDesc=unit, returnName=name)
    rows = [
        {'SECUCODE': '300059.SZ', 'REPORT_DATE': '2025-06-30', 'NOTICE_DATE': '2025-08-16'},
        {'SECUCODE': '300059.SZ', 'REPORT_DATE': '2025-09-30', 'NOTICE_DATE': '2025-10-25'},
    ]
    monkeypatch.setattr(EastmoneyOperatorReadingSource, 'fetch_main_financial_data',
                        lambda *a, **k: SimpleNamespace(rows=rows, canonical_rows_sha256='2' * 64))
    client = MxSaasMarketDataClient(api_key='fixture-key')
    query = AsyncMock(side_effect=[original, quarterly])
    monkeypatch.setattr(client, 'query_finance', query)
    result = asyncio.run(client.query_finance_history(
        query='ROE', indicators=None, instrument_id='300059.SZ',
        start=date(2025, 9, 12), end=date(2025, 10, 31),
        expected_session_dates=(date(2025, 9, 30), date(2025, 10, 24), date(2025, 10, 27)),
    ))
    assert result.tables[0]['rawTable']['metric'] == ['6', '6', '9']
    assert result.tables[0]['fieldSet'][0]['returnSourceCode'] == source
    assert ('加权' if source == 'ROEJQ' else '营业收入同比增长率') in query.call_args.kwargs['indicators']
    assert 'TTM' not in query.call_args.kwargs['indicators']


def test_weighted_roe_does_not_invent_daily_calendar(monkeypatch):
    client = MxSaasMarketDataClient(api_key='fixture-key')
    query = AsyncMock(return_value=response('ROEJQ', ['2025中报'], ['6']))
    monkeypatch.setattr(client, 'query_finance', query)
    with pytest.raises(MxSaasProviderNoDataError):
        asyncio.run(client.query_finance_history(
            query='ROE', indicators=None, instrument_id='300059.SZ',
            start=date(2025, 9, 12), end=date(2025, 10, 31),
        ))
    assert query.await_count == 1


def test_wrong_report_security_does_not_fall_back_to_future_daily_values(monkeypatch):
    client = MxSaasMarketDataClient(api_key='fixture-key')
    monkeypatch.setattr(client, 'query_finance', AsyncMock(side_effect=[
        response('ROETTM', ['2025-09-12'], ['.13']),
        response('ROE_TTM_RPT', ['2025中报'], ['13'], symbol='600519.SH'),
    ]))
    with pytest.raises(MxSaasProviderNoDataError):
        asyncio.run(client.query_finance_history(query='fixture', indicators=None,
                    instrument_id='300059.SZ', start=date(2025, 9, 12), end=date(2025, 9, 15)))
