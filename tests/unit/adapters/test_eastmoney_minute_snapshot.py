from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ashare_lab.adapters.market_data.eastmoney_minute import (
    EastmoneyMinuteCollection,
    EastmoneyMinuteRow,
)
from ashare_lab.adapters.market_data.eastmoney_minute_snapshot import (
    EastmoneyMinuteSnapshotError,
    EastmoneyMinuteSnapshotSpec,
    build_eastmoney_minute_snapshot,
    load_eastmoney_minute_snapshot,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
SESSION_DATE = date(2025, 1, 2)


def _continuous_starts() -> list[datetime]:
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


def _make_rows() -> list[EastmoneyMinuteRow]:
    rows = [
        EastmoneyMinuteRow(
            timestamp=datetime.combine(SESSION_DATE, time(9, 30), tzinfo=SHANGHAI),
            open=Decimal("10"), close=Decimal("10"), high=Decimal("10"), low=Decimal("10"),
            volume_lots=Decimal("10"), volume_shares=Decimal("1000"),
            amount_cny=Decimal("10000"), source_row="auction",
        )
    ]
    for index, start in enumerate(_continuous_starts()):
        price = Decimal(10) + Decimal(index) / Decimal(1000)
        rows.append(
            EastmoneyMinuteRow(
                timestamp=start + timedelta(minutes=1),
                open=price,
                close=price + Decimal("0.01"),
                high=price + Decimal("0.02"),
                low=price - Decimal("0.02"),
                volume_lots=Decimal(100 + index),
                volume_shares=Decimal((100 + index) * 100),
                amount_cny=Decimal((100 + index) * 100) * (price + Decimal("0.01")),
                source_row="fixture",
            )
        )
    return rows


def _make_collection(rows: list[EastmoneyMinuteRow]) -> EastmoneyMinuteCollection:
    return EastmoneyMinuteCollection(
        instrument_id="300059.SZ",
        period_minutes=1,
        requested_start=SESSION_DATE,
        requested_end=SESSION_DATE,
        retrieved_at=datetime(2025, 1, 3, tzinfo=UTC),
        request_url="https://push2his.eastmoney.com/api/qt/stock/trends2/get",
        request_params={"secid": "0.300059"},
        response_sha256="sha256:" + "a" * 64,
        attempts=1,
        rows=tuple(rows),
    )


def _build(tmp_path: Path, **overrides):
    values = {
        "spec": EastmoneyMinuteSnapshotSpec(
            symbol="300059.SZ", start=SESSION_DATE, end=SESSION_DATE
        ),
        "collection": _make_collection(_make_rows()),
        "output_root": tmp_path,
        "captured_at": datetime(2025, 1, 3, tzinfo=UTC),
    }
    values.update(overrides)
    return build_eastmoney_minute_snapshot(**values)


def test_single_returned_day_does_not_claim_complete_requested_date_range(tmp_path: Path):
    built = _build(tmp_path, spec=EastmoneyMinuteSnapshotSpec(symbol="300059.SZ",
                                                            start=date(2025, 1, 1), end=date(2025, 1, 3)))
    manifest = json.loads((built.path / "snapshot_manifest.json").read_text())
    assert manifest["requestedRange"] == ["2025-01-01", "2025-01-03"]
    assert manifest["coverage"]["start"] == manifest["coverage"]["end"] == "2025-01-02"
    assert manifest["coverage"]["requestedRangeVerified"] is False


def test_builds_content_addressed_240_bar_snapshot(tmp_path: Path) -> None:
    result = _build(tmp_path)
    assert result.snapshot_id.startswith("eastmoney-minute:")
    assert (result.path / "minute_ohlcv.parquet").is_file()
    assert (result.path / "raw" / "collection.json").is_file()
    manifest = json.loads((result.path / "snapshot_manifest.json").read_text())
    assert manifest["rowCounts"]["executionMinute"] == 240
    assert manifest["coverage"]["missingBars"] == 0
    assert manifest["coverage"]["status"] == "complete_observed_sessions"
    assert manifest["coverage"]["requestedRangeVerified"] is False
    assert manifest["providerTimestampSemantics"] == "bar_end"
    assert manifest["openingAuctionBarPolicy"] == "merged_09_30_into_09_31_ohlcv"


def test_auction_is_merged_into_first_bar_without_changing_source_rows(tmp_path: Path) -> None:
    from dataclasses import replace
    import pyarrow.parquet as pq
    rows = _make_rows()
    rows[0] = replace(rows[0], open=Decimal(11), high=Decimal(11), low=Decimal(11), close=Decimal(11))
    result = _build(tmp_path, collection=_make_collection(rows))
    first = pq.read_table(result.path / "minute_ohlcv.parquet").to_pylist()[0]
    assert first["open"] == Decimal(11) and first["high"] == Decimal(11)
    assert first["close"] == rows[1].close
    assert first["volume"] == rows[0].volume_shares + rows[1].volume_shares
    assert first["amount"] == rows[0].amount_cny + rows[1].amount_cny
    assert rows[1].open == Decimal(10)


def test_same_input_is_idempotent(tmp_path: Path) -> None:
    first = _build(tmp_path)
    second = _build(tmp_path)
    assert first.snapshot_id == second.snapshot_id
    assert first.path == second.path


def test_rejects_incomplete_session(tmp_path: Path) -> None:
    rows = _make_rows()
    rows = [r for r in rows if r.timestamp.time() != time(15, 0)]
    with pytest.raises(EastmoneyMinuteSnapshotError, match="incomplete"):
        _build(tmp_path, collection=_make_collection(rows))


def test_rejects_missing_auction_bar(tmp_path: Path) -> None:
    rows = [r for r in _make_rows() if r.timestamp.time() != time(9, 30)]
    with pytest.raises(EastmoneyMinuteSnapshotError, match="opening-auction"):
        _build(tmp_path, collection=_make_collection(rows))


def test_rejects_non_single_price_auction_bar(tmp_path: Path) -> None:
    rows = _make_rows()
    rows[0] = EastmoneyMinuteRow(
        timestamp=datetime.combine(SESSION_DATE, time(9, 30), tzinfo=SHANGHAI),
        open=Decimal("10"), close=Decimal("10"), high=Decimal("10.5"), low=Decimal("9.5"),
        volume_lots=Decimal("10"), volume_shares=Decimal("1000"),
        amount_cny=Decimal("10000"), source_row="bad-auction",
    )
    with pytest.raises(EastmoneyMinuteSnapshotError, match="continuous one-minute grid"):
        _build(tmp_path, collection=_make_collection(rows))


def test_rejects_tampered_published_file(tmp_path: Path) -> None:
    result = _build(tmp_path)
    (result.path / "minute_ohlcv.parquet").write_bytes(b"corrupt")
    with pytest.raises(EastmoneyMinuteSnapshotError, match="hash mismatch"):
        _build(tmp_path)


def test_snapshot_reader_joins_explicit_daily_and_session_controls(tmp_path: Path) -> None:
    from dataclasses import replace
    from ashare_lab.application.minute_replay_input import prepare_minute_replay, MinuteReplayDataError
    from ashare_lab.domain.market_data import DailyBar, InstrumentSession, Board, TradingStatus
    from ashare_lab.domain.shared import Price, Quantity
    result = _build(tmp_path)
    minutes = load_eastmoney_minute_snapshot(result.path)
    first = minutes[0]
    reference = DailyBar(first.instrument_id, SESSION_DATE, first.open,
                         max((b.high for b in minutes), key=lambda p: p.amount),
                         min((b.low for b in minutes), key=lambda p: p.amount), minutes[-1].close,
                         Quantity(sum(b.volume.value for b in minutes)), sum(b.turnover for b in minutes),
                         minutes[-1].available_at)
    session = InstrumentSession(first.instrument_id, SESSION_DATE, Board.CHINEXT, TradingStatus.TRADING,
                                Price(Decimal(10)), Price(Decimal(12)), Price(Decimal(8)), 100, 100)
    inputs = dict(instrument=first.instrument_id, start=SESSION_DATE, end=SESSION_DATE,
                  minutes=minutes, daily=(reference,), sessions=(session,),
                  market_calendar=(SESSION_DATE, date(2025, 1, 3)))
    prepared = prepare_minute_replay(**inputs)
    assert len(prepared.bars) == 240 and prepared.bars[0].next_session == date(2025, 1, 3)
    assert not prepared.reconciliation[0].approximate
    external_reference = replace(reference, volume=Quantity(reference.volume.value + 1),
                                 turnover=reference.turnover + Decimal('0.36'))
    external = prepare_minute_replay(**{**inputs, 'daily': (external_reference,)},
                                    reconciliation_profile='external_stock_1min_research')
    assert external.reconciliation[0].approximate
    assert external.reconciliation[0].turnover_delta_cny == Decimal('-0.36')
    for field, value, message in (
        ('volume', Quantity(reference.volume.value + 24101), 'volume_mismatch'),
        ('turnover', reference.turnover + Decimal('241.01'), 'turnover_mismatch'),
    ):
        with pytest.raises(MinuteReplayDataError, match=message):
            prepare_minute_replay(**{**inputs, 'daily': (replace(reference, **{field: value}),)},
                                 reconciliation_profile='external_stock_1min_research')
    suspended_day = date(2025, 1, 3)
    suspended = replace(session, session_date=suspended_day, status=TradingStatus.SUSPENDED)
    suspended_close = replace(reference, session_date=suspended_day, volume=Quantity(0), turnover=Decimal(0),
                              available_at=reference.available_at.replace(day=3))
    extended_inputs = {**inputs, "end": suspended_day, "daily": (reference, suspended_close),
                       "sessions": (session, suspended), "market_calendar": (SESSION_DATE, suspended_day, date(2025, 1, 6))}
    extended = prepare_minute_replay(**extended_inputs)
    assert extended.bars == prepared.bars  # No invented bars for the suspended session.
    assert extended.nontrading_closes == ((suspended, suspended_close),)
    placeholders = tuple(replace(b,
        bar_start_at=b.bar_start_at.replace(day=3), bar_end_at=b.bar_end_at.replace(day=3),
        available_at=b.available_at.replace(day=3),
        open=reference.close, high=reference.close, low=reference.close, close=reference.close,
        volume=Quantity(0), turnover=Decimal(0)) for b in minutes)
    with_placeholders = {**extended_inputs, "minutes": (*minutes, *placeholders)}
    normalized = prepare_minute_replay(**with_placeholders,
        reconciliation_profile="external_stock_1min_research")
    assert normalized.bars == extended.bars
    assert normalized.nontrading_closes == extended.nontrading_closes
    with pytest.raises(MinuteReplayDataError, match="minute_data_on_nontrading_session"):
        prepare_minute_replay(**with_placeholders)
    with pytest.raises(MinuteReplayDataError, match="minute_data_on_nontrading_session"):
        prepare_minute_replay(**{**with_placeholders,
            "minutes": (*minutes, replace(placeholders[0], volume=Quantity(1)), *placeholders[1:])},
            reconciliation_profile="external_stock_1min_research")
    with pytest.raises(MinuteReplayDataError, match="raw_daily_control_missing"):
        prepare_minute_replay(**{**extended_inputs, "daily": (reference,)})
    with pytest.raises(MinuteReplayDataError, match="security_session_missing"):
        prepare_minute_replay(**{**inputs, "sessions": ()})
    with pytest.raises(MinuteReplayDataError, match="incomplete_minute_session"):
        prepare_minute_replay(**{**inputs, "minutes": minutes[:-1]})
    with pytest.raises(MinuteReplayDataError, match="volume_mismatch"):
        prepare_minute_replay(**{**inputs, "daily": (replace(reference, volume=Quantity(1)),)})
    rounded = tuple(replace(b, turnover=b.turnover.to_integral_value()) for b in minutes)
    rounded_reference = replace(reference, volume=Quantity(reference.volume.value + 11),
                                turnover=sum(b.turnover for b in rounded) + Decimal(33))
    approximate_inputs = {**inputs, "minutes": rounded, "daily": (rounded_reference,)}
    with pytest.raises(MinuteReplayDataError, match="volume_mismatch"):
        prepare_minute_replay(**approximate_inputs)
    approximate = prepare_minute_replay(**approximate_inputs,
                                        reconciliation_profile="eastmoney_whole_lot_yuan_research")
    assert approximate.reconciliation[0].volume_delta_shares == -11
    assert approximate.reconciliation[0].turnover_delta_cny == -33
    assert approximate.reconciliation[0].approximate
    with pytest.raises(MinuteReplayDataError, match="turnover_mismatch"):
        prepare_minute_replay(**{**approximate_inputs, "daily": (replace(rounded_reference, turnover=Decimal(1)),)},
                              reconciliation_profile="eastmoney_whole_lot_yuan_research")
