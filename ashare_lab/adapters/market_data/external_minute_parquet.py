"""Read completed stock_1min exports in place, without copying the dataset."""
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow.parquet as pq

from ashare_lab.domain.market_data import MinuteBar
from ashare_lab.domain.shared import InstrumentId, Price, Quantity
from .eastmoney_minute import EastmoneyMinuteRow
from .eastmoney_minute_snapshot import (
    EastmoneyMinuteSnapshotSpec, _canonicalize_bars, _validate_complete_sessions,
)


@dataclass(frozen=True)
class ExternalMinuteData:
    bars: tuple[MinuteBar, ...]
    evidence: dict


@dataclass(frozen=True)
class ExternalMinuteParquet:
    root: Path

    def load(self, symbol: str, start: date, end: date, *,
             confirmed_opening_prices: dict[date, Decimal] | None = None) -> ExternalMinuteData | None:
        # Canonical identity also prevents path traversal. Never read .downloading.
        from ashare_lab.domain.market_data import normalize_a_share_instrument
        normalize_a_share_instrument(symbol)
        if '/' in symbol or '\\' in symbol or start > end:
            raise ValueError('invalid external minute request')
        path = self.root / f'{symbol}.parquet'
        if not path.is_file():
            return None
        before = path.stat()
        columns = ['ts_code', 'trade_date', 'trade_time', 'open', 'high', 'low',
                   'close', 'vol', 'amount']
        table = pq.read_table(path, columns=columns, filters=[
            ('trade_date', '>=', datetime.combine(start, time.min)),
            ('trade_date', '<=', datetime.combine(end, time.min)),
        ])
        if not table.num_rows:
            return None
        digest = sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        rows = []
        shanghai = ZoneInfo('Asia/Shanghai')
        for record in table.to_pylist():
            if record['ts_code'] != symbol:
                raise ValueError('external minute symbol mismatch')
            stamp = record['trade_time']
            if hasattr(stamp, 'to_pydatetime'):
                stamp = stamp.to_pydatetime()
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=shanghai)
            else:
                stamp = stamp.astimezone(shanghai)
            if stamp.date() != record['trade_date'].date():
                raise ValueError('external minute date mismatch')
            prices = []
            for field in ('open', 'high', 'low', 'close'):
                value = Decimal(str(record[field]))
                rounded = value.quantize(Decimal('0.000001'))
                if not value.is_finite() or abs(value - rounded) > Decimal('0.00000001'):
                    raise ValueError('external minute price precision invalid')
                prices.append(rounded)
            volume, amount = Decimal(str(record['vol'])), Decimal(str(record['amount']))
            if (not volume.is_finite() or volume < 0 or volume != volume.to_integral_value()
                    or not amount.is_finite() or amount < 0):
                raise ValueError('external minute liquidity invalid')
            rows.append(EastmoneyMinuteRow(stamp, prices[0], prices[3], prices[1], prices[2],
                volume / 100, volume, amount, ''))
        # This export's 09:30 row may include an opening price range. Preserve
        # traded OHLCV in the first executable bar, available only at 09:31;
        # zero-volume/zero-amount 09:30 placeholders are not executions.
        canonical = _canonicalize_bars(rows, allow_opening_range=True,
                                      confirmed_opening_prices=confirmed_opening_prices)
        first_rows = {r.timestamp.date(): r for r in rows if r.timestamp.time() == time(9, 31)}
        opening_reconciliation = [dict(sessionDate=r['bar_start_at'].date().isoformat(),
            minuteOpen=str(first_rows[r['bar_start_at'].date()].open), replayOpen=str(r['open']),
            rawDailyOpen=str((confirmed_opening_prices or {}).get(r['bar_start_at'].date())))
            for r in canonical if r['bar_start_at'].time() == time(9, 30)
            and r['open'] != first_rows[r['bar_start_at'].date()].open]
        days = _validate_complete_sessions(canonical,
            spec=EastmoneyMinuteSnapshotSpec(symbol, start, end))
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                after.st_size, after.st_mtime_ns, after.st_ino):
            raise ValueError('external minute file changed during read')
        bars = tuple(MinuteBar(
            instrument_id=InstrumentId(symbol), bar_start_at=r['bar_start_at'],
            bar_end_at=r['bar_end_at'], available_at=r['available_at'],
            open=Price(r['open']), high=Price(r['high']), low=Price(r['low']),
            close=Price(r['close']), volume=Quantity(r['volume']), turnover=r['amount'],
        ) for r in canonical)
        return ExternalMinuteData(bars, {
            'snapshotId': 'external-minute:' + digest.hexdigest(),
            'provider': 'user_supplied_stock_1min_parquet', 'path': str(path.resolve()),
            'fileSha256': digest.hexdigest(), 'bytes': after.st_size,
            'sessions': [day.isoformat() for day in days],
            'sourceRows': table.num_rows, 'executionRows': len(bars),
            'priceBasis': 'unadjusted_execution', 'volumeUnit': 'shares', 'amountUnit': 'CNY',
            'timestampPolicy': 'Asia/Shanghai_bar_end',
            'openingAuctionBarPolicy': 'merged_09_30_into_09_31_ohlcv',
            'zeroLiquidityOpeningPolicy': 'retain_only_if_raw_daily_open_confirms_auction_price',
            'confirmedZeroLiquidityOpeningDates': [r.timestamp.date().isoformat() for r in rows
                if r.timestamp.time() == time(9, 30) and r.volume_shares == 0 and r.amount_cny == 0
                and (confirmed_opening_prices or {}).get(r.timestamp.date()) == r.open],
            'openingPriceSource': 'raw_daily_open_if_zero_liquidity_auction_and_within_first_minute_range',
            'openingReconciliation': opening_reconciliation,
            'normalizationVersion': 'external-stock-1min.v3',
            'pricePrecisionPolicy': 'six_decimal_places_float_tail_tolerance_1e-8',
            'adjustmentFactorUsedForExecution': False,
            'reconciliationPolicy': {
                'profile': 'external_stock_1min_research',
                'volumeTolerance': 'min(100*(bars+1),dailyVolume*0.0001)',
                'amountTolerance': 'min(bars+1,dailyAmount*0.0001)',
                'ohlcTolerance': 'exact_after_float_tail_normalization',
            },
        })
