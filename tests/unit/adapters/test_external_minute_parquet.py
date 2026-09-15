from datetime import date
from decimal import Decimal

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from ashare_lab.adapters.market_data.external_minute_parquet import ExternalMinuteParquet
from tests.unit.adapters.test_eastmoney_minute_snapshot import _make_rows


def write_source(root, *, missing=False, wrong_symbol=False):
    records = []
    for row in _make_rows():
        stamp = row.timestamp.replace(tzinfo=None)
        records.append(dict(ts_code='600519.SH' if wrong_symbol else '300059.SZ',
            trade_date=stamp.replace(hour=0, minute=0), trade_time=stamp,
            open=float(row.open), high=float(row.high), low=float(row.low),
            close=float(row.close), vol=float(row.volume_shares), amount=float(row.amount_cny)))
    # Non-flat opening label and a binary floating tail must preserve OHLC.
    records[0].update(high=10.02, close=10.010000000000002)
    pq.write_table(pa.Table.from_pylist(records[:-1] if missing else records), root / '300059.SZ.parquet')


def test_external_file_maps_auction_and_preserves_source(tmp_path):
    write_source(tmp_path)
    before = (tmp_path / '300059.SZ.parquet').read_bytes()
    result = ExternalMinuteParquet(tmp_path).load('300059.SZ', date(2025,1,2), date(2025,1,2))
    assert len(result.bars) == 240
    assert result.bars[0].open.amount == Decimal('10')
    assert result.bars[0].high.amount >= Decimal('10.02')
    assert result.evidence['sourceRows'] == 241
    assert result.evidence['provider'] == 'user_supplied_stock_1min_parquet'
    assert (tmp_path / '300059.SZ.parquet').read_bytes() == before


@pytest.mark.parametrize('option', ['missing', 'wrong_symbol'])
def test_external_file_rejects_partial_days_and_wrong_identity(tmp_path, option):
    write_source(tmp_path, **{option: True})
    with pytest.raises((ValueError, RuntimeError)):
        ExternalMinuteParquet(tmp_path).load('300059.SZ', date(2025,1,2), date(2025,1,2))


def test_downloading_is_not_selected(tmp_path):
    (tmp_path / '300059.SZ.parquet.baiduyun.p.downloading').write_bytes(b'incomplete')
    assert ExternalMinuteParquet(tmp_path).load('300059.SZ',date(2025,1,2),date(2025,1,2)) is None


def test_zero_liquidity_opening_placeholder_does_not_pollute_prices(tmp_path):
    write_source(tmp_path)
    path = tmp_path / '300059.SZ.parquet'
    records = pq.read_table(path).to_pylist()
    records[0].update(open=99., high=99., low=99., close=99., vol=0., amount=0.)
    pq.write_table(pa.Table.from_pylist(records), path)
    before = path.read_bytes()
    result = ExternalMinuteParquet(tmp_path).load('300059.SZ', date(2025,1,2), date(2025,1,2))
    assert len(result.bars) == 240
    assert result.bars[0].open.amount == Decimal(str(records[1]['open']))
    assert result.bars[0].high.amount < Decimal('99')
    assert path.read_bytes() == before

    # The same label can be a genuine auction price whose volume is omitted
    # by the export. Retain price only when the raw daily opening confirms it.
    confirmed = ExternalMinuteParquet(tmp_path).load('300059.SZ', date(2025,1,2), date(2025,1,2),
        confirmed_opening_prices={date(2025,1,2): Decimal('99')})
    assert confirmed.bars[0].open.amount == Decimal('99')
    assert confirmed.bars[0].volume == result.bars[0].volume
    assert confirmed.bars[0].turnover == result.bars[0].turnover


def test_missing_auction_uses_confirmed_open_only_inside_observed_first_minute(tmp_path):
    write_source(tmp_path)
    path = tmp_path / '300059.SZ.parquet'
    records = pq.read_table(path).to_pylist()
    records[0].update(open=9., high=9., low=9., close=9., vol=0., amount=0.)
    records[1].update(open=10.1, high=10.2, low=10., close=10.1)
    pq.write_table(pa.Table.from_pylist(records), path)
    def load(opening):
        return ExternalMinuteParquet(tmp_path).load('300059.SZ', date(2025,1,2), date(2025,1,2),
            confirmed_opening_prices={date(2025,1,2): Decimal(opening)})
    assert load('10.05').bars[0].open.amount == Decimal('10.05')
    assert load('11').bars[0].open.amount == Decimal('10.1')
    assert load('10.05').evidence['openingReconciliation'][0]['replayOpen'] == '10.05'
