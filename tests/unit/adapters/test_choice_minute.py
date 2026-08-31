from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from ashare_lab.adapters.market_data.choice_minute import (
    ChoiceMinuteDecodeError,
    classify_choice_error,
    compare_cmc_prefix,
    decode_cmc_batch,
    decode_csd_batch,
    infer_timestamp_semantics,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass
class _Result:
    ErrorCode: int
    ErrorMsg: str
    Codes: list[str]
    Indicators: list[str]
    Dates: list[object]
    Data: object


def _end_labels(session_date: date) -> list[datetime]:
    values: list[datetime] = []
    current = datetime.combine(session_date, time(9, 30), tzinfo=SHANGHAI)
    while current < datetime.combine(session_date, time(11, 30), tzinfo=SHANGHAI):
        values.append(current + timedelta(minutes=1))
        current += timedelta(minutes=1)
    current = datetime.combine(session_date, time(13), tzinfo=SHANGHAI)
    while current < datetime.combine(session_date, time(15), tzinfo=SHANGHAI):
        values.append(current + timedelta(minutes=1))
        current += timedelta(minutes=1)
    return values


def test_decodes_batch_shape_and_infers_completed_bar_labels() -> None:
    timestamps = _end_labels(date(2025, 1, 2))
    result = _Result(
        ErrorCode=0,
        ErrorMsg="success",
        Codes=["300059.SZ"],
        Indicators=["CLOSE", "OPEN"],
        Dates=timestamps,
        Data={
            "300059.SZ": [
                [10 + index / 1000 for index in range(240)],
                [9.9 + index / 1000 for index in range(240)],
            ]
        },
    )
    rows = decode_cmc_batch(
        result,
        symbol="300059.SZ",
        expected_indicators=("OPEN", "CLOSE"),
    )
    contract = infer_timestamp_semantics(rows)

    assert len(rows) == 240
    assert rows[0]["open"] == 9.9
    assert contract["status"] == "verified"
    assert contract["semantics"] == "bar_end"


def test_rejects_non_batch_or_misaligned_result() -> None:
    result = _Result(
        ErrorCode=0,
        ErrorMsg="success",
        Codes=["300059.SZ"],
        Indicators=["CLOSE"],
        Dates=["2025-01-02 09:31:00"],
        Data=[[10]],
    )
    with pytest.raises(ChoiceMinuteDecodeError, match="mode=batch"):
        decode_cmc_batch(result, symbol="300059.SZ", expected_indicators=("CLOSE",))


def test_daily_decoder_rejects_duplicate_dates_and_extra_values() -> None:
    duplicate_dates = _Result(
        ErrorCode=0,
        ErrorMsg="success",
        Codes=["300059.SZ"],
        Indicators=["CLOSE"],
        Dates=["2025-01-02", "2025-01-02"],
        Data={"300059.SZ": [[10, 11]]},
    )
    with pytest.raises(ChoiceMinuteDecodeError, match="Dates contain duplicates"):
        decode_csd_batch(
            duplicate_dates,
            symbol="300059.SZ",
            expected_indicators=("CLOSE",),
        )

    extra_value = _Result(
        ErrorCode=0,
        ErrorMsg="success",
        Codes=["300059.SZ"],
        Indicators=["CLOSE"],
        Dates=["2025-01-02"],
        Data={"300059.SZ": [[10, 11]]},
    )
    with pytest.raises(ChoiceMinuteDecodeError, match="length differs from Dates"):
        decode_csd_batch(
            extra_value,
            symbol="300059.SZ",
            expected_indicators=("CLOSE",),
        )


def test_prefix_stability_requires_a_complete_calibration_session() -> None:
    row = {"timestamp": "2025-01-02 09:31:00", "close": 10}
    comparison = compare_cmc_prefix(
        [row],
        [row],
        fields=("close",),
        expected_rows=240,
    )

    assert comparison["status"] == "failed"
    assert comparison["reason"] == "prefix_row_count_mismatch"


def test_prefix_stability_rejects_a_later_full_session() -> None:
    first = _end_labels(date(2025, 1, 2))
    second = _end_labels(date(2025, 1, 3))
    full = [{"timestamp": item, "close": 10} for item in (*first, *second)]
    wrong_prefix = [{"timestamp": item, "close": 10} for item in second]

    comparison = compare_cmc_prefix(
        full,
        wrong_prefix,
        fields=("close",),
        expected_rows=240,
    )

    assert comparison["status"] == "failed"
    assert comparison["reason"] == "prefix_axis_not_leading_subset"


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (10000017, "network_region_restricted"),
        (10001012, "account_permission_denied"),
        (10001014, "activation_required"),
        (12345678, "provider_error"),
    ],
)
def test_classifies_choice_errors(code: int, expected: str) -> None:
    assert classify_choice_error(code) == expected
