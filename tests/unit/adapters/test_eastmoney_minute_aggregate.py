from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from ashare_lab.adapters.market_data.eastmoney_minute_aggregate import (
    EastmoneyAggregationError,
    aggregate_minute_bars,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
SESSION_DATE = date(2025, 1, 2)


def _session_starts() -> list[datetime]:
    starts: list[datetime] = []
    current = datetime.combine(SESSION_DATE, time(9, 30), tzinfo=SHANGHAI)
    while current < datetime.combine(SESSION_DATE, time(11, 30), tzinfo=SHANGHAI):
        starts.append(current)
        current += timedelta(minutes=1)
    current = datetime.combine(SESSION_DATE, time(13, 0), tzinfo=SHANGHAI)
    while current < datetime.combine(SESSION_DATE, time(15, 0), tzinfo=SHANGHAI):
        starts.append(current)
        current += timedelta(minutes=1)
    return starts


def _base_bars() -> list[dict[str, object]]:
    bars: list[dict[str, object]] = []
    for index, start in enumerate(_session_starts()):
        price = Decimal(10) + Decimal(index) / Decimal(1000)
        bars.append(
            {
                "bar_start_at": start,
                "bar_end_at": start + timedelta(minutes=1),
                "available_at": start + timedelta(minutes=1),
                "open": price,
                "high": price + Decimal("0.02"),
                "low": price - Decimal("0.02"),
                "close": price + Decimal("0.01"),
                "volume": 100 + index,
                "amount": Decimal(100 + index) * (price + Decimal("0.01")),
            }
        )
    return bars


def test_5m_aggregates_240_bars_into_48_with_bar_end_labels() -> None:
    result = aggregate_minute_bars(_base_bars(), period_minutes=5)
    assert len(result.bars) == 48
    assert result.skipped_incomplete_buckets == 0
    first, last = result.bars[0], result.bars[-1]
    assert first["bar_start_at"] == datetime(2025, 1, 2, 9, 30, tzinfo=SHANGHAI)
    assert first["bar_end_at"] == datetime(2025, 1, 2, 9, 35, tzinfo=SHANGHAI)
    assert last["bar_end_at"] == datetime(2025, 1, 2, 15, 0, tzinfo=SHANGHAI)


def test_5m_ohclv_rules_are_exact() -> None:
    base = _base_bars()
    result = aggregate_minute_bars(base, period_minutes=5)
    first_bucket = base[:5]
    merged = result.bars[0]
    assert merged["open"] == first_bucket[0]["open"]
    assert merged["close"] == first_bucket[-1]["close"]
    assert merged["high"] == max(b["high"] for b in first_bucket)
    assert merged["low"] == min(b["low"] for b in first_bucket)
    assert merged["volume"] == sum(b["volume"] for b in first_bucket)
    assert merged["amount"] == sum(b["amount"] for b in first_bucket)


def test_never_crosses_lunch_break() -> None:
    result = aggregate_minute_bars(_base_bars(), period_minutes=5)
    bars = result.bars
    morning_last = [b for b in bars if b["bar_end_at"].time() <= time(11, 30)][-1]
    afternoon_first = [b for b in bars if b["bar_end_at"].time() > time(12, 0)][0]
    assert morning_last["bar_end_at"] == datetime(2025, 1, 2, 11, 30, tzinfo=SHANGHAI)
    assert afternoon_first["bar_start_at"] == datetime(2025, 1, 2, 13, 0, tzinfo=SHANGHAI)
    assert afternoon_first["bar_end_at"] == datetime(2025, 1, 2, 13, 5, tzinfo=SHANGHAI)


@pytest.mark.parametrize("period,expected", [(15, 16), (30, 8), (60, 4)])
def test_higher_periods_produce_expected_bar_counts(period: int, expected: int) -> None:
    result = aggregate_minute_bars(_base_bars(), period_minutes=period)
    assert len(result.bars) == expected
    assert result.skipped_incomplete_buckets == 0


def test_missing_base_minute_invalidates_its_bucket_without_shifting_others() -> None:
    base = _base_bars()
    # Drop the 09:31-labelled bar (bar_start 09:30) from the first bucket.
    base = [b for b in base if b["bar_start_at"].time() != time(9, 30)]
    result = aggregate_minute_bars(base, period_minutes=5)
    assert result.skipped_incomplete_buckets == 1
    assert len(result.bars) == 47
    # The first surviving bucket is the 09:35-09:39 bar, still aligned to 09:40.
    first = result.bars[0]
    assert first["bar_end_at"] == datetime(2025, 1, 2, 9, 40, tzinfo=SHANGHAI)


def test_unsupported_period_is_rejected() -> None:
    with pytest.raises(EastmoneyAggregationError, match="5, 15, 30 or 60"):
        aggregate_minute_bars(_base_bars(), period_minutes=7)
