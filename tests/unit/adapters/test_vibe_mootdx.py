from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import cast
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from ashare_lab.adapters.market_data.vibe_mootdx import (
    CALLER_BYTES_HASH_SEMANTICS,
    CANONICAL_FRAME_HASH_SEMANTICS,
    SOURCE,
    SOURCE_COMMIT,
    VibeMootdxDailyResearchSource,
    VibeMootdxSourceError,
)
from ashare_lab.domain.market_data import BarInterval, PriceBasis
from ashare_lab.domain.shared import InstrumentId

SHANGHAI = ZoneInfo("Asia/Shanghai")
STARTED = datetime(2025, 1, 10, 16, 30, tzinfo=SHANGHAI)
RECEIVED = datetime(2025, 1, 10, 16, 30, 1, tzinfo=SHANGHAI)
SESSIONS = (date(2025, 1, 2), date(2025, 1, 3))


class FakeClient:
    def __init__(self, frame: object, *, failure: Exception | None = None) -> None:
        self.frame = frame
        self.failure = failure
        self.calls: list[dict[str, str]] = []

    def get_k_data(
        self,
        *,
        code: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame | None:
        self.calls.append({"code": code, "start_date": start_date, "end_date": end_date})
        if self.failure is not None:
            raise self.failure
        return cast(pd.DataFrame | None, self.frame)


class Clock:
    def __init__(self, *values: datetime) -> None:
        self._values = iter(values)

    def __call__(self) -> datetime:
        return next(self._values)


def _frame(
    *,
    code: str = "300059",
    dates: Sequence[str] = ("2025-01-02", "2025-01-03"),
    volume_lots: Sequence[object] = (1000, Decimal("950.25")),
    amount: Sequence[object] = (1_100_000, Decimal("1045275")),
) -> pd.DataFrame:
    size = len(dates)
    values = {
        "open": [Decimal("10.00"), Decimal("10.80")][:size],
        "close": [Decimal("11.00"), Decimal("11.10")][:size],
        "high": [Decimal("12.00"), Decimal("11.50")][:size],
        "low": [Decimal("9.50"), Decimal("10.50")][:size],
        "vol": list(volume_lots),
        "amount": list(amount),
        "date": list(dates),
        "code": [code] * size,
    }
    return pd.DataFrame(
        values,
        index=pd.DatetimeIndex(dates, name="date"),
    )


def _source(client: FakeClient, *clock_values: datetime) -> VibeMootdxDailyResearchSource:
    return VibeMootdxDailyResearchSource(
        client,
        clock=Clock(*(clock_values or (STARTED, RECEIVED))),
    )


def _fetch(
    client: FakeClient,
    *,
    instrument: str = "300059.SZ",
    start: date = date(2025, 1, 1),
    end: date = date(2025, 1, 3),
    sessions: Sequence[date] = SESSIONS,
    raw_response_bytes: bytes | None = None,
    interval: BarInterval = BarInterval.DAY_1,
) -> object:
    return _source(client).fetch(
        instrument_id=InstrumentId(instrument),
        start=start,
        end=end,
        expected_session_dates=sessions,
        raw_response_bytes=raw_response_bytes,
        interval=interval,
    )


def test_collects_strict_daily_rows_with_pinned_source_and_request_evidence() -> None:
    client = FakeClient(_frame())

    collection = _source(client).fetch(
        instrument_id=InstrumentId("300059.SZ"),
        start=date(2025, 1, 1),
        end=date(2025, 1, 3),
        expected_session_dates=SESSIONS,
    )

    assert client.calls == [
        {"code": "300059", "start_date": "2025-01-01", "end_date": "2025-01-03"}
    ]
    assert collection.instrument_id == InstrumentId("300059.SZ")
    assert collection.provider_code == "300059"
    assert collection.interval is BarInterval.DAY_1
    assert collection.price_basis is PriceBasis.UNADJUSTED
    assert collection.source == SOURCE
    assert collection.source_commit == SOURCE_COMMIT
    assert collection.request == {
        "code": "300059",
        "end_date": "2025-01-03",
        "interval": "1D",
        "method": "get_k_data",
        "start_date": "2025-01-01",
    }
    assert collection.source_volume_unit == "board_lot"
    assert collection.normalized_volume_unit == "share"
    assert collection.volume_lot_size_shares == 100
    assert [row.date for row in collection.rows] == list(SESSIONS)
    assert collection.rows[0].volume_lots == Decimal("1000")
    assert collection.rows[0].volume_shares == 100_000
    assert collection.rows[1].volume_lots == Decimal("950.25")
    assert collection.rows[1].volume_shares == 95_025
    assert collection.rows[1].as_snapshot_row(collection.instrument_id) == {
        "stock_code": "300059.SZ",
        "date": "2025-01-03",
        "open": Decimal("10.80"),
        "high": Decimal("11.50"),
        "low": Decimal("10.50"),
        "close": Decimal("11.10"),
        "volume": 95_025,
        "amount": Decimal("1045275"),
    }

    evidence = collection.evidence
    assert evidence.raw_response_hash_semantics == CANONICAL_FRAME_HASH_SEMANTICS
    assert collection.raw_response_sha256 == evidence.canonical_frame_sha256
    assert len(collection.raw_response_sha256) == 64
    assert evidence.expected_session_dates == SESSIONS
    assert evidence.received_session_dates == SESSIONS
    assert evidence.as_dict()["coverageComplete"] is True
    assert evidence.as_dict()["sourceCommit"] == SOURCE_COMMIT
    assert evidence.as_dict()["rawResponseHashSemantics"] == (
        "sha256_of_canonical_mootdx_dataframe_result_not_raw_wire_bytes"
    )


def test_caller_supplied_provider_bytes_receive_a_separate_explicit_hash() -> None:
    raw_bytes = b"captured native provider response"
    collection = _source(FakeClient(_frame())).fetch(
        instrument_id=InstrumentId("300059.SZ"),
        start=date(2025, 1, 1),
        end=date(2025, 1, 3),
        expected_session_dates=SESSIONS,
        raw_response_bytes=raw_bytes,
    )

    assert collection.raw_response_sha256 == hashlib.sha256(raw_bytes).hexdigest()
    assert collection.evidence.raw_response_hash_semantics == CALLER_BYTES_HASH_SEMANTICS
    assert collection.evidence.canonical_frame_sha256 != collection.raw_response_sha256


def test_sh_symbol_maps_to_bare_mootdx_code_without_a_fallback() -> None:
    client = FakeClient(_frame(code="600519"))
    collection = _source(client).fetch(
        instrument_id=InstrumentId("600519.SH"),
        start=date(2025, 1, 1),
        end=date(2025, 1, 3),
        expected_session_dates=SESSIONS,
    )

    assert collection.provider_code == "600519"
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "instrument, message",
    [
        ("835174.BJ", "does not support Beijing"),
        ("AAPL.US", "canonical mainland A-share"),
        ("00700.HK", "canonical mainland A-share"),
        ("300059", "must be canonical"),
        ("300059.SH", "canonical mainland A-share"),
    ],
)
def test_rejects_noncanonical_non_a_share_and_bj_before_call(
    instrument: str,
    message: str,
) -> None:
    client = FakeClient(_frame())

    with pytest.raises(ValueError, match=message):
        _fetch(client, instrument=instrument)

    assert client.calls == []


def test_rejects_non_daily_interval_before_call() -> None:
    client = FakeClient(_frame())

    with pytest.raises(ValueError, match="only canonical 1-day"):
        _fetch(client, interval=BarInterval.MINUTE_1)

    assert client.calls == []


@pytest.mark.parametrize(
    "start, end, message",
    [
        (date(2025, 1, 4), date(2025, 1, 3), "must not be later"),
        (date(2025, 1, 1), date(2025, 1, 11), "must not be in the future"),
    ],
)
def test_rejects_invalid_or_future_ranges_before_call(
    start: date,
    end: date,
    message: str,
) -> None:
    client = FakeClient(_frame())

    with pytest.raises(ValueError, match=message):
        _fetch(client, start=start, end=end)

    assert client.calls == []


def test_rejects_current_session_before_daily_data_is_stable() -> None:
    client = FakeClient(_frame(dates=("2025-01-10",), volume_lots=(1000,), amount=(1_100_000,)))
    before_stable = datetime(2025, 1, 10, 15, 30, tzinfo=SHANGHAI)
    source = _source(client, before_stable, before_stable)

    with pytest.raises(ValueError, match="not stable before 16:05"):
        source.fetch(
            instrument_id=InstrumentId("300059.SZ"),
            start=date(2025, 1, 10),
            end=date(2025, 1, 10),
            expected_session_dates=(date(2025, 1, 10),),
        )

    assert client.calls == []


@pytest.mark.parametrize(
    "sessions, message",
    [
        ((), "cannot be empty"),
        ((date(2025, 1, 2), date(2025, 1, 2)), "duplicates"),
        ((date(2025, 1, 3), date(2025, 1, 2)), "strictly increasing"),
        ((date(2024, 12, 31), date(2025, 1, 2)), "inside the requested range"),
    ],
)
def test_rejects_invalid_expected_session_proof_before_call(
    sessions: Sequence[date],
    message: str,
) -> None:
    client = FakeClient(_frame())

    with pytest.raises(ValueError, match=message):
        _fetch(client, sessions=sessions)

    assert client.calls == []


@pytest.mark.parametrize(
    "dates, sessions, message",
    [
        (("2025-01-02",), SESSIONS, "missing=\\('2025-01-03'"),
        (
            ("2025-01-02", "2025-01-03"),
            (date(2025, 1, 2),),
            "unexpected=\\('2025-01-03'",
        ),
    ],
)
def test_missing_or_unexpected_history_fails_closed(
    dates: Sequence[str],
    sessions: Sequence[date],
    message: str,
) -> None:
    size = len(dates)
    frame = _frame(
        dates=dates,
        volume_lots=(1000, 950)[:size],
        amount=(1_100_000, 1_045_000)[:size],
    )

    with pytest.raises(VibeMootdxSourceError, match=message):
        _fetch(FakeClient(frame), sessions=sessions)


def test_duplicate_response_dates_fail_closed() -> None:
    frame = _frame(
        dates=("2025-01-02", "2025-01-02"),
        volume_lots=(1000, 950),
        amount=(1_100_000, 1_045_000),
    )

    with pytest.raises(VibeMootdxSourceError, match="duplicate trade dates"):
        _fetch(FakeClient(frame))


@pytest.mark.parametrize("frame", [None, pd.DataFrame()])
def test_empty_or_missing_provider_history_fails_closed(frame: object) -> None:
    with pytest.raises(VibeMootdxSourceError, match=r"no daily history|did not return"):
        _fetch(FakeClient(frame))


def test_out_of_range_provider_date_fails_closed() -> None:
    frame = _frame(
        dates=("2025-01-02", "2025-01-04"),
        volume_lots=(1000, 950),
        amount=(1_100_000, 1_045_000),
    )

    with pytest.raises(VibeMootdxSourceError, match="outside the requested range"):
        _fetch(FakeClient(frame))


@pytest.mark.parametrize(
    "mutation, message",
    [
        ("missing_amount", "columns must exactly match"),
        ("extra_column", "columns must exactly match"),
        ("wrong_code", "does not match"),
        ("invalid_ohlc", "OHLC values are inconsistent"),
        ("nan_price", "must be finite"),
        ("fractional_share", "whole shares"),
        ("wrong_lot_unit", "inconsistent with amount"),
    ],
)
def test_schema_identity_prices_and_units_fail_closed(
    mutation: str,
    message: str,
) -> None:
    frame = _frame()
    if mutation == "missing_amount":
        del frame["amount"]
    elif mutation == "extra_column":
        frame["extra"] = 1
    elif mutation == "wrong_code":
        frame["code"] = "600519"
    elif mutation == "invalid_ohlc":
        frame["high"] = Decimal("9.00")
    elif mutation == "nan_price":
        frame["open"] = float("nan")
    elif mutation == "fractional_share":
        frame["vol"] = Decimal("1.001")
    elif mutation == "wrong_lot_unit":
        frame["amount"] = Decimal("11000")
    else:  # pragma: no cover - parametrization is exhaustive
        raise AssertionError(f"unknown mutation: {mutation}")

    with pytest.raises(VibeMootdxSourceError, match=message):
        _fetch(FakeClient(frame))


def test_date_column_must_match_datetime_index() -> None:
    frame = _frame()
    frame.loc[pd.Timestamp("2025-01-02"), "date"] = "2025-01-03"

    with pytest.raises(VibeMootdxSourceError, match="does not match its dataframe index"):
        _fetch(FakeClient(frame))


def test_client_failure_is_not_retried_or_fallen_back() -> None:
    client = FakeClient(_frame(), failure=ConnectionError("TDX disconnected"))

    with pytest.raises(VibeMootdxSourceError, match="request failed") as captured:
        _fetch(client)

    assert isinstance(captured.value.__cause__, ConnectionError)
    assert len(client.calls) == 1


def test_response_time_cannot_precede_request_time() -> None:
    client = FakeClient(_frame())
    source = _source(client, RECEIVED, STARTED)

    with pytest.raises(VibeMootdxSourceError, match="precedes"):
        source.fetch(
            instrument_id=InstrumentId("300059.SZ"),
            start=date(2025, 1, 1),
            end=date(2025, 1, 3),
            expected_session_dates=SESSIONS,
        )
