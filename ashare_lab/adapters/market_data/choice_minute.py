"""Pure decoding and validation helpers for Choice ``c.cmc`` responses."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal, cast
from zoneinfo import ZoneInfo

_SHANGHAI = ZoneInfo("Asia/Shanghai")

type TimestampSemantics = Literal["bar_start", "bar_end"]


class ChoiceMinuteDecodeError(RuntimeError):
    """A successful SDK response does not satisfy the expected CMC schema."""


def classify_choice_error(error_code: int) -> str:
    """Return a stable product-facing classification for known SDK failures."""

    return {
        0: "success",
        10000017: "network_region_restricted",
        10001012: "account_permission_denied",
        10001014: "activation_required",
        10001020: "activation_required",
    }.get(error_code, "provider_error")


def decode_cmc_batch(
    result: object,
    *,
    symbol: str,
    expected_indicators: Sequence[str],
) -> list[dict[str, object]]:
    """Decode the ``mode=batch`` CMC shape into timestamp-keyed rows."""

    error_code = getattr(result, "ErrorCode", None)
    if error_code != 0:
        raise ChoiceMinuteDecodeError(
            f"CMC result is not successful: {error_code} {getattr(result, 'ErrorMsg', '')}"
        )
    raw_dates_value = getattr(result, "Dates", None)
    raw_indicators_value = getattr(result, "Indicators", None)
    raw_data_value = getattr(result, "Data", None)
    if not isinstance(raw_dates_value, Sequence) or isinstance(
        raw_dates_value, str | bytes | bytearray
    ):
        raise ChoiceMinuteDecodeError("CMC Dates must be an array")
    if not isinstance(raw_indicators_value, Sequence) or isinstance(
        raw_indicators_value, str | bytes | bytearray
    ):
        raise ChoiceMinuteDecodeError("CMC Indicators must be an array")
    if not isinstance(raw_data_value, Mapping):
        raise ChoiceMinuteDecodeError("CMC mode=batch Data must be keyed by symbol")
    raw_dates = cast(Sequence[object], raw_dates_value)
    raw_indicators = cast(Sequence[object], raw_indicators_value)
    raw_data = cast(Mapping[object, object], raw_data_value)

    indicators = [str(item).upper() for item in raw_indicators]
    expected = [item.upper() for item in expected_indicators]
    if len(indicators) != len(set(indicators)):
        raise ChoiceMinuteDecodeError("CMC Indicators contain duplicates")
    missing = sorted(set(expected) - set(indicators))
    if missing:
        raise ChoiceMinuteDecodeError("CMC response is missing indicators: " + ", ".join(missing))
    symbol_key = next((key for key in raw_data if str(key).upper() == symbol.upper()), None)
    if symbol_key is None:
        raise ChoiceMinuteDecodeError(f"CMC response is missing symbol {symbol}")
    series_value = raw_data[symbol_key]
    if not isinstance(series_value, Sequence) or isinstance(series_value, str | bytes | bytearray):
        raise ChoiceMinuteDecodeError("CMC symbol data must be an indicator matrix")
    series = cast(Sequence[object], series_value)
    if len(series) != len(indicators):
        raise ChoiceMinuteDecodeError("CMC indicator and data dimensions differ")

    axis_length = len(raw_dates)
    values_by_indicator: dict[str, Sequence[object]] = {}
    for index, indicator in enumerate(indicators):
        values = series[index]
        if not isinstance(values, Sequence) or isinstance(values, str | bytes | bytearray):
            raise ChoiceMinuteDecodeError(f"CMC {indicator} values must be an array")
        typed_values = cast(Sequence[object], values)
        if len(typed_values) != axis_length:
            raise ChoiceMinuteDecodeError(f"CMC {indicator} length differs from Dates")
        values_by_indicator[indicator] = typed_values

    rows: list[dict[str, object]] = []
    for row_index, timestamp in enumerate(raw_dates):
        row: dict[str, object] = {"timestamp": timestamp}
        for indicator in expected:
            row[indicator.lower()] = values_by_indicator[indicator][row_index]
        rows.append(row)
    return rows


def decode_csd_batch(
    result: object,
    *,
    symbol: str,
    expected_indicators: Sequence[str],
) -> list[dict[str, object]]:
    """Decode a non-pandas CSD response with exact dimensions and a unique date axis."""

    error_code = getattr(result, "ErrorCode", None)
    if error_code != 0:
        raise ChoiceMinuteDecodeError(
            f"CSD result is not successful: {error_code} {getattr(result, 'ErrorMsg', '')}"
        )
    raw_dates_value = getattr(result, "Dates", None)
    raw_indicators_value = getattr(result, "Indicators", None)
    raw_data_value = getattr(result, "Data", None)
    if not isinstance(raw_dates_value, Sequence) or isinstance(
        raw_dates_value, str | bytes | bytearray
    ):
        raise ChoiceMinuteDecodeError("CSD Dates must be an array")
    if not isinstance(raw_indicators_value, Sequence) or isinstance(
        raw_indicators_value, str | bytes | bytearray
    ):
        raise ChoiceMinuteDecodeError("CSD Indicators must be an array")
    if not isinstance(raw_data_value, Mapping):
        raise ChoiceMinuteDecodeError("CSD Data must be keyed by symbol")
    raw_dates = cast(Sequence[object], raw_dates_value)
    raw_indicators = cast(Sequence[object], raw_indicators_value)
    raw_data = cast(Mapping[object, object], raw_data_value)

    dates = [_daily_date({"date": item}) for item in raw_dates]
    if len(dates) != len(set(dates)):
        raise ChoiceMinuteDecodeError("CSD Dates contain duplicates")
    indicators = [str(item).upper() for item in raw_indicators]
    if len(indicators) != len(set(indicators)):
        raise ChoiceMinuteDecodeError("CSD Indicators contain duplicates")
    expected = [item.upper() for item in expected_indicators]
    if missing := sorted(set(expected) - set(indicators)):
        raise ChoiceMinuteDecodeError("CSD response is missing indicators: " + ", ".join(missing))
    symbol_key = next((key for key in raw_data if str(key).upper() == symbol.upper()), None)
    if symbol_key is None:
        raise ChoiceMinuteDecodeError(f"CSD response is missing symbol {symbol}")
    series_value = raw_data[symbol_key]
    if not isinstance(series_value, Sequence) or isinstance(series_value, str | bytes | bytearray):
        raise ChoiceMinuteDecodeError("CSD symbol data must be an indicator matrix")
    series = cast(Sequence[object], series_value)
    if len(series) != len(indicators):
        raise ChoiceMinuteDecodeError("CSD indicator and data dimensions differ")

    axis_length = len(raw_dates)
    values_by_indicator: dict[str, Sequence[object]] = {}
    for index, indicator in enumerate(indicators):
        values = series[index]
        if not isinstance(values, Sequence) or isinstance(values, str | bytes | bytearray):
            raise ChoiceMinuteDecodeError(f"CSD {indicator} values must be an array")
        typed_values = cast(Sequence[object], values)
        if len(typed_values) != axis_length:
            raise ChoiceMinuteDecodeError(f"CSD {indicator} length differs from Dates")
        values_by_indicator[indicator] = typed_values

    return [
        {
            "date": raw_date,
            **{
                indicator.lower(): values_by_indicator[indicator][row_index]
                for indicator in expected
            },
        }
        for row_index, raw_date in enumerate(raw_dates)
    ]


def infer_timestamp_semantics(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Infer start/end labels only from a complete normal A-share session."""

    by_date: dict[date, list[datetime]] = defaultdict(list)
    for index, row in enumerate(rows):
        by_date[_provider_datetime(row.get("timestamp"), f"row {index} timestamp").date()].append(
            _provider_datetime(row.get("timestamp"), f"row {index} timestamp")
        )
    for session_date in sorted(by_date):
        actual = tuple(sorted(by_date[session_date]))
        start_grid = _session_grid(session_date, semantics="bar_start")
        end_grid = _session_grid(session_date, semantics="bar_end")
        if actual == start_grid:
            return {
                "status": "verified",
                "semantics": "bar_start",
                "calibrationDate": session_date.isoformat(),
                "firstTimestamp": actual[0].isoformat(),
                "lastTimestamp": actual[-1].isoformat(),
                "rowCount": len(actual),
            }
        if actual == end_grid:
            return {
                "status": "verified",
                "semantics": "bar_end",
                "calibrationDate": session_date.isoformat(),
                "firstTimestamp": actual[0].isoformat(),
                "lastTimestamp": actual[-1].isoformat(),
                "rowCount": len(actual),
            }
    return {
        "status": "unverified",
        "semantics": "unverified",
        "reason": "no_complete_normal_session_matched_the_240_bar_grid",
    }


def compare_cmc_prefix(
    full_rows: Sequence[Mapping[str, object]],
    prefix_rows: Sequence[Mapping[str, object]],
    *,
    fields: Sequence[str],
    expected_rows: int | None = None,
) -> dict[str, object]:
    """Require every shorter-endpoint cell to match the full request exactly."""

    full = {_timestamp_key(item): item for item in full_rows}
    prefix = {_timestamp_key(item): item for item in prefix_rows}
    if len(full) != len(full_rows) or len(prefix) != len(prefix_rows):
        return {"status": "failed", "reason": "duplicate_timestamps"}
    if expected_rows is not None and len(prefix) != expected_rows:
        return {
            "status": "failed",
            "reason": "prefix_row_count_mismatch",
            "expectedRows": expected_rows,
            "overlapRows": len(prefix),
        }
    if not prefix or sorted(prefix) != sorted(full)[: len(prefix)]:
        return {"status": "failed", "reason": "prefix_axis_not_leading_subset"}
    mismatches = 0
    for key, prefix_row in prefix.items():
        full_row = full[key]
        for field in fields:
            if _numeric(prefix_row.get(field), field) != _numeric(full_row.get(field), field):
                mismatches += 1
    return {
        "status": "passed" if mismatches == 0 else "failed",
        "overlapRows": len(prefix),
        "mismatchedCells": mismatches,
        "comparison": "exact_decimal",
    }


def reconcile_minute_daily(
    *,
    execution_rows: Sequence[Mapping[str, object]],
    signal_rows: Sequence[Mapping[str, object]],
    daily_execution_rows: Sequence[Mapping[str, object]],
    daily_signal_rows: Sequence[Mapping[str, object]],
    timestamp_semantics: TimestampSemantics,
) -> dict[str, object]:
    """Compare minute aggregates with Choice daily execution and signal rows."""

    raw_by_date = _minute_rows_by_session(execution_rows, timestamp_semantics)
    signal_by_date = _minute_rows_by_session(signal_rows, timestamp_semantics)
    daily_raw = {_daily_date(item): item for item in daily_execution_rows}
    daily_signal = {_daily_date(item): item for item in daily_signal_rows}
    axes = set(raw_by_date)
    if axes != set(signal_by_date) or axes != set(daily_raw) or axes != set(daily_signal):
        return {
            "status": "failed",
            "reason": "daily_and_minute_date_axes_differ",
            "sessionCount": len(axes),
        }

    mismatches: list[dict[str, str]] = []
    for session_date in sorted(axes):
        raw = raw_by_date[session_date]
        signal = signal_by_date[session_date]
        expected_raw = daily_raw[session_date]
        expected_signal = daily_signal[session_date]
        actual_values = {
            "open": _numeric(raw[0].get("open"), "open"),
            "high": max(_numeric(item.get("high"), "high") for item in raw),
            "low": min(_numeric(item.get("low"), "low") for item in raw),
            "close": _numeric(raw[-1].get("close"), "close"),
            "volume": sum((_numeric(item.get("volume"), "volume") for item in raw), Decimal(0)),
            "amount": sum((_numeric(item.get("amount"), "amount") for item in raw), Decimal(0)),
        }
        for field in ("open", "high", "low", "close"):
            _record_difference(
                mismatches,
                session_date,
                field,
                actual_values[field],
                _numeric(expected_raw.get(field), field),
                tolerance=Decimal("0.000001"),
            )
        _record_difference(
            mismatches,
            session_date,
            "volume",
            actual_values["volume"],
            _numeric(expected_raw.get("volume"), "volume"),
            tolerance=Decimal(0),
        )
        expected_amount = _numeric(expected_raw.get("amount"), "amount")
        _record_difference(
            mismatches,
            session_date,
            "amount",
            actual_values["amount"],
            expected_amount,
            tolerance=max(Decimal("0.01"), abs(expected_amount) * Decimal("0.000001")),
        )
        _record_difference(
            mismatches,
            session_date,
            "signal_close",
            _numeric(signal[-1].get("close"), "signal close"),
            _numeric(expected_signal.get("close"), "daily signal close"),
            tolerance=Decimal("0.000001"),
        )
    return {
        "status": "passed" if not mismatches else "failed",
        "sessionCount": len(axes),
        "mismatchCount": len(mismatches),
        "mismatches": mismatches[:20],
        "amountTolerance": "max(0.01, daily_amount*0.000001)",
    }


def serialize_sdk_result(result: object) -> dict[str, object]:
    """Create a credential-free decoded-response audit payload."""

    return {
        "errorCode": getattr(result, "ErrorCode", None),
        "errorMessage": getattr(result, "ErrorMsg", None),
        "codes": _safe_json(getattr(result, "Codes", None)),
        "indicators": _safe_json(getattr(result, "Indicators", None)),
        "dates": _safe_json(getattr(result, "Dates", None)),
        "data": _safe_json(getattr(result, "Data", None)),
    }


def canonical_payload_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _session_grid(session_date: date, *, semantics: TimestampSemantics) -> tuple[datetime, ...]:
    starts: list[datetime] = []
    current = datetime.combine(session_date, time(9, 30), tzinfo=_SHANGHAI)
    while current < datetime.combine(session_date, time(11, 30), tzinfo=_SHANGHAI):
        starts.append(current)
        current += timedelta(minutes=1)
    current = datetime.combine(session_date, time(13, 0), tzinfo=_SHANGHAI)
    while current < datetime.combine(session_date, time(15, 0), tzinfo=_SHANGHAI):
        starts.append(current)
        current += timedelta(minutes=1)
    if semantics == "bar_start":
        return tuple(starts)
    return tuple(item + timedelta(minutes=1) for item in starts)


def _minute_rows_by_session(
    rows: Sequence[Mapping[str, object]],
    semantics: TimestampSemantics,
) -> dict[date, list[Mapping[str, object]]]:
    grouped: dict[date, list[Mapping[str, object]]] = defaultdict(list)
    for row in rows:
        label = _provider_datetime(row.get("timestamp"), "timestamp")
        session_date = (label if semantics == "bar_start" else label - timedelta(minutes=1)).date()
        grouped[session_date].append(row)
    for values in grouped.values():
        values.sort(key=_timestamp_key)
    return grouped


def _timestamp_key(row: Mapping[str, object]) -> str:
    return _provider_datetime(row.get("timestamp"), "timestamp").isoformat()


def _daily_date(row: Mapping[str, object]) -> date:
    raw = row.get("date")
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str):
        try:
            return date.fromisoformat(raw[:10].replace("/", "-"))
        except ValueError as exc:
            raise ChoiceMinuteDecodeError(f"invalid daily date: {raw!r}") from exc
    raise ChoiceMinuteDecodeError("daily row date is missing")


def _record_difference(
    mismatches: list[dict[str, str]],
    session_date: date,
    field: str,
    actual: Decimal,
    expected: Decimal,
    *,
    tolerance: Decimal,
) -> None:
    if abs(actual - expected) > tolerance:
        mismatches.append(
            {
                "date": session_date.isoformat(),
                "field": field,
                "actual": str(actual),
                "expected": str(expected),
                "tolerance": str(tolerance),
            }
        )


def _provider_datetime(value: object, field_name: str) -> datetime:
    if isinstance(value, datetime):
        return (
            value.replace(tzinfo=_SHANGHAI) if value.tzinfo is None else value.astimezone(_SHANGHAI)
        )
    if not isinstance(value, str) or not value.strip():
        raise ChoiceMinuteDecodeError(f"{field_name} must be a provider datetime")
    raw = value.strip()
    try:
        if raw.isdigit() and len(raw) == 14:
            parsed = datetime.strptime(raw, "%Y%m%d%H%M%S")
        else:
            parsed = datetime.fromisoformat(raw.replace("/", "-").replace("Z", "+00:00"))
    except ValueError as exc:
        raise ChoiceMinuteDecodeError(f"invalid {field_name}: {value!r}") from exc
    return (
        parsed.replace(tzinfo=_SHANGHAI) if parsed.tzinfo is None else parsed.astimezone(_SHANGHAI)
    )


def _numeric(value: object, field_name: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ChoiceMinuteDecodeError(f"{field_name} must be numeric")
    try:
        converted = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ChoiceMinuteDecodeError(f"{field_name} must be numeric") from exc
    if not converted.is_finite():
        raise ChoiceMinuteDecodeError(f"{field_name} must be finite")
    return converted


def _safe_json(value: object) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, date | datetime | Decimal):
        return str(value)
    if isinstance(value, Mapping):
        items = cast(Mapping[object, object], value)
        return {str(key): _safe_json(item) for key, item in items.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_safe_json(item) for item in cast(Sequence[object], value)]
    return str(value)
