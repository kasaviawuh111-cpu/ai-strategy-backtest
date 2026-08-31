#!/usr/bin/env python3
"""Create a versioned Choice daily research snapshot for 300059.SZ."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from ashare_lab.adapters.market_data.choice_snapshot import (
    CHOICE_DATA_INTEGRITY_EXIT_CODE,
    CHOICE_PROVIDER_UNAVAILABLE_EXIT_CODE,
    ChoiceSnapshotSpec,
    build_choice_snapshot,
)
from ashare_lab.domain.market_data import (
    Board,
    CorporateAction,
    CorporateActionKind,
    TimeQuality,
)
from ashare_lab.domain.shared import InstrumentId, StrongId

EXECUTION_INDICATORS = (
    "OPEN",
    "HIGH",
    "LOW",
    "CLOSE",
    "PRECLOSE",
    "VOLUME",
    "AMOUNT",
    "HIGHLIMIT",
    "LOWLIMIT",
    "TRADESTATUS",
)
SIGNAL_INDICATORS = ("OPEN", "HIGH", "LOW", "CLOSE")
CALENDAR_OPTIONS = "Market=CNSESH,RECVtimeout=30"
_TRANSIENT_READ_ERROR_CODES = frozenset({10002004})
_TRANSIENT_LOGIN_ERROR_CODES = frozenset({10002002, 10002004})
_PROVIDER_UNAVAILABLE_ERROR_CODES = frozenset({10000017, 10002002, 10002004})
_MAX_READ_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = (0.25, 0.75)


class ChoiceProviderUnavailableError(RuntimeError):
    """The Choice service, login, network, or entitlement is unavailable."""


class ChoiceResponseValidationError(RuntimeError):
    """Choice returned an unexpected or semantically invalid response."""


@dataclass(frozen=True, slots=True)
class _ChoiceAcquisition:
    calendar_batches: tuple[Any, ...]
    calendar_attempts: tuple[int, ...]
    calendar_requests: tuple[tuple[date, date, str], ...]
    execution: Any
    execution_attempts: int
    signal: Any
    signal_attempts: int
    prefix: Any
    prefix_attempts: int


def main() -> int:
    args = _parse_args()
    try:
        (
            session_reference_rows,
            session_reference_coverage,
            session_reference_source_payload,
        ) = _load_session_reference_source(
            args.session_reference_json,
            symbol=args.symbol,
        )
        action_rows, action_coverage, action_source_payload = _load_corporate_action_source(
            args.corporate_actions_json,
            symbol=args.symbol,
        )
    except (OSError, TypeError, ValueError) as error:
        print(f"snapshot reference source is invalid: {error}")
        return CHOICE_DATA_INTEGRITY_EXIT_CODE
    try:
        from EmQuantAPI import c
    except ImportError:
        print(
            "Choice SDK is not registered in this virtual environment; "
            "follow docs/runbooks/choice-quant-api.md."
        )
        return CHOICE_PROVIDER_UNAVAILABLE_EXIT_CODE

    try:
        _login, login_attempts = _start_with_retry(
            lambda: c.start("ForceLogin=0,RecordLoginInfo=0", _quiet_log),
            c,
        )
    except (ChoiceProviderUnavailableError, ConnectionError, OSError, TimeoutError) as error:
        print(f"Choice login failed: {error}")
        return CHOICE_PROVIDER_UNAVAILABLE_EXIT_CODE
    except Exception as error:
        print(f"Choice login response failed validation: {type(error).__name__}")
        return CHOICE_DATA_INTEGRITY_EXIT_CODE

    try:
        try:
            acquisition = _acquire_choice_responses(args, c)
        except (ChoiceProviderUnavailableError, ConnectionError, OSError, TimeoutError) as error:
            print(f"Choice provider became unavailable: {error}")
            return CHOICE_PROVIDER_UNAVAILABLE_EXIT_CODE
        except Exception as error:
            print(f"Choice provider response failed validation: {type(error).__name__}: {error}")
            return CHOICE_DATA_INTEGRITY_EXIT_CODE

        market_calendar = sorted(
            {
                session_date
                for batch in acquisition.calendar_batches
                for session_date in _extract_dates(batch.Data)
            }
        )
        execution_rows = _series_rows(acquisition.execution, args.symbol)
        signal_rows = _series_rows(acquisition.signal, args.symbol)
        prefix_rows = _series_rows(acquisition.prefix, args.symbol)
        prefix_stability = _compare_prefix(signal_rows, prefix_rows, args.prefix_end)
        captured_at = datetime.now(UTC)
        request_audit: dict[str, object] = {
            "login": {
                "function": "start",
                "options": "ForceLogin=0,RecordLoginInfo=0",
                "attempts": login_attempts,
            },
            "calendar": [
                {
                    "function": "tradedates",
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "options": options,
                    "attempts": attempts,
                }
                for (start, end, options), attempts in zip(
                    acquisition.calendar_requests,
                    acquisition.calendar_attempts,
                    strict=True,
                )
            ],
            "execution": {
                "function": "csd",
                "indicators": list(EXECUTION_INDICATORS),
                "start": args.start.isoformat(),
                "end": args.end.isoformat(),
                "options": {
                    "Period": 1,
                    "AdjustFlag": 1,
                    "Order": 1,
                    "Ispandas": 0,
                },
                "attempts": acquisition.execution_attempts,
            },
            "signal": {
                "function": "csd",
                "indicators": list(SIGNAL_INDICATORS),
                "start": args.start.isoformat(),
                "end": args.end.isoformat(),
                "options": {
                    "Period": 1,
                    "AdjustFlag": 2,
                    "Order": 1,
                    "Ispandas": 0,
                },
                "attempts": acquisition.signal_attempts,
            },
            "prefixStability": {
                "function": "csd",
                "end": args.prefix_end.isoformat(),
                "purpose": "detect future rewrites of historical adjusted values",
                "attempts": acquisition.prefix_attempts,
            },
            "corporateActions": {
                "input": str(args.corporate_actions_json),
                "inputSha256": _optional_sha256(args.corporate_actions_json),
                "provider": action_coverage["provider"],
            },
            "historicalSessions": {
                "input": str(args.session_reference_json),
                "inputSha256": _optional_sha256(args.session_reference_json),
                "provider": session_reference_coverage["provider"],
                "normalizedResponseSha256": session_reference_coverage["normalizedResponseSha256"],
            },
        }
        raw_payload: dict[str, object] = {
            "calendar": [_serialize_result(batch) for batch in acquisition.calendar_batches],
            "execution": _serialize_result(acquisition.execution),
            "signal": _serialize_result(acquisition.signal),
            "signalPrefix": _serialize_result(acquisition.prefix),
            "baostockSessionReference": session_reference_source_payload,
            "eastmoneyCorporateActionReference": action_source_payload,
        }
        result = build_choice_snapshot(
            spec=ChoiceSnapshotSpec(
                symbol=args.symbol,
                start=args.start,
                end=args.end,
                listing_date=args.listing_date,
                board=Board(args.board),
            ),
            execution_rows=execution_rows,
            signal_rows=signal_rows,
            market_calendar=market_calendar,
            raw_audit_payload=raw_payload,
            request_audit=request_audit,
            prefix_stability=prefix_stability,
            output_root=args.output_root,
            captured_at=captured_at,
            sdk_archive_sha256=_optional_sha256(args.sdk_archive),
            session_reference_rows=session_reference_rows,
            session_reference_coverage=session_reference_coverage,
            corporate_actions=action_rows,
            corporate_action_coverage=action_coverage,
        )
        output = {
            "status": "ok",
            "snapshotId": result.snapshot_id,
            "path": str(result.path),
            "rows": result.manifest["rowCounts"],
            "prefixStability": prefix_stability,
            "runWith": {
                "DATA_ROOT": str(result.path),
                "SESSION_REFERENCE_MODE": "parquet",
                "SESSION_REFERENCE_PATH": str(result.path / "instrument_sessions.parquet"),
            },
        }
        print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
        return 0
    except Exception as error:
        print(f"Choice snapshot failed integrity validation: {type(error).__name__}: {error}")
        return CHOICE_DATA_INTEGRITY_EXIT_CODE
    finally:
        c.stop()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="300059.SZ")
    parser.add_argument(
        "--start",
        type=date.fromisoformat,
        default=date(2021, 2, 7),
        help="Snapshot start, including the strategy warmup extension.",
    )
    parser.add_argument(
        "--end",
        type=date.fromisoformat,
        default=date(2026, 8, 20),
        help="Snapshot end, including the post-backtest settlement extension.",
    )
    parser.add_argument("--prefix-end", type=date.fromisoformat)
    parser.add_argument("--listing-date", type=date.fromisoformat, default=date(2010, 3, 19))
    parser.add_argument(
        "--board",
        choices=tuple(item.value for item in Board),
        default=Board.CHINEXT.value,
    )
    parser.add_argument("--output-root", type=Path, default=Path("var/snapshots/choice"))
    parser.add_argument(
        "--session-reference-json",
        "--reference-json",
        dest="session_reference_json",
        type=Path,
        required=True,
        help=(
            "Validated BaoStock instrument/session reference artifact. "
            "Historical preclose, tradestatus and isST evidence is mandatory."
        ),
    )
    parser.add_argument(
        "--corporate-actions-json",
        type=Path,
        help=(
            "Validated Eastmoney corporate-action reference artifact. If omitted, the "
            "session reference is reused only for backwards-compatible fixture workflows."
        ),
    )
    parser.add_argument(
        "--sdk-archive",
        type=Path,
        default=Path.home() / "Downloads" / "EMQuantAPI_Python.zip",
    )
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("--start must not be after --end")
    if args.prefix_end is None:
        args.prefix_end = args.end - timedelta(days=365)
    if not args.start < args.prefix_end < args.end:
        parser.error("--prefix-end must be strictly inside the requested range")
    if args.corporate_actions_json is None:
        args.corporate_actions_json = args.session_reference_json
    return args


def _quiet_log(_: bytes) -> int:
    return 1


def _acquire_choice_responses(args: argparse.Namespace, client: Any) -> _ChoiceAcquisition:
    """Run only Choice SDK I/O so provider failures cannot cover local publication."""

    calendar_batches: list[Any] = []
    calendar_attempts: list[int] = []
    calendar_requests = _calendar_requests(args.start, args.end)
    for start, end, options in calendar_requests:
        batch, attempts = _read_with_retry(
            lambda start=start, end=end, options=options: client.tradedates(
                start.isoformat(),
                end.isoformat(),
                options,
            ),
            f"market calendar {start.isoformat()}..{end.isoformat()}",
            client,
        )
        calendar_batches.append(batch)
        calendar_attempts.append(attempts)
    execution, execution_attempts = _read_with_retry(
        lambda: client.csd(
            args.symbol,
            ",".join(EXECUTION_INDICATORS),
            args.start.isoformat(),
            args.end.isoformat(),
            "Period=1,AdjustFlag=1,Order=1,Ispandas=0,RECVtimeout=60",
        ),
        operation="unadjusted daily series",
        client=client,
    )
    signal, signal_attempts = _read_with_retry(
        lambda: client.csd(
            args.symbol,
            ",".join(SIGNAL_INDICATORS),
            args.start.isoformat(),
            args.end.isoformat(),
            "Period=1,AdjustFlag=2,Order=1,Ispandas=0,RECVtimeout=60",
        ),
        operation="back-adjusted signal series",
        client=client,
    )
    prefix, prefix_attempts = _read_with_retry(
        lambda: client.csd(
            args.symbol,
            ",".join(SIGNAL_INDICATORS),
            args.start.isoformat(),
            args.prefix_end.isoformat(),
            "Period=1,AdjustFlag=2,Order=1,Ispandas=0,RECVtimeout=60",
        ),
        operation="back-adjusted prefix series",
        client=client,
    )
    return _ChoiceAcquisition(
        calendar_batches=tuple(calendar_batches),
        calendar_attempts=tuple(calendar_attempts),
        calendar_requests=calendar_requests,
        execution=execution,
        execution_attempts=execution_attempts,
        signal=signal,
        signal_attempts=signal_attempts,
        prefix=prefix,
        prefix_attempts=prefix_attempts,
    )


def _read_with_retry(
    read: Callable[[], Any],
    operation: str,
    client: Any,
    *,
    transient_error_codes: frozenset[int] = _TRANSIENT_READ_ERROR_CODES,
    max_attempts: int = _MAX_READ_ATTEMPTS,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[Any, int]:
    """Retry only explicitly classified transient errors from idempotent reads."""

    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("max_attempts must be a positive integer")
    for attempt in range(1, max_attempts + 1):
        result = read()
        is_provider_result = isinstance(result, client.EmQuantData)
        error_code = result.ErrorCode if is_provider_result else None
        if error_code == 0:
            return result, attempt
        if (
            not is_provider_result
            or error_code not in transient_error_codes
            or attempt == max_attempts
        ):
            _check(result, operation, client)
        backoff_index = min(attempt - 1, len(_RETRY_BACKOFF_SECONDS) - 1)
        sleeper(_RETRY_BACKOFF_SECONDS[backoff_index])
    raise AssertionError("unreachable Choice retry state")


def _start_with_retry(
    start: Callable[[], Any],
    client: Any,
    *,
    max_attempts: int = _MAX_READ_ATTEMPTS,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[Any, int]:
    return _read_with_retry(
        start,
        "login",
        client,
        transient_error_codes=_TRANSIENT_LOGIN_ERROR_CODES,
        max_attempts=max_attempts,
        sleeper=sleeper,
    )


def _check(result: Any, operation: str, client: Any) -> Any:
    if not isinstance(result, client.EmQuantData):
        raise ChoiceResponseValidationError(f"{operation} returned {type(result).__name__}")
    if result.ErrorCode != 0:
        error_type = (
            ChoiceProviderUnavailableError
            if result.ErrorCode in _PROVIDER_UNAVAILABLE_ERROR_CODES
            else ChoiceResponseValidationError
        )
        raise error_type(f"{operation} failed: {result.ErrorCode} {result.ErrorMsg}")
    return result


def _series_rows(result: Any, symbol: str) -> list[dict[str, object]]:
    indicator_series = result.Data.get(symbol)
    if indicator_series is None:
        raise RuntimeError(f"Choice result is missing {symbol}")
    if len(indicator_series) != len(result.Indicators):
        raise RuntimeError("Choice indicator and data dimensions differ")
    rows: list[dict[str, object]] = []
    for date_index, raw_date in enumerate(result.Dates):
        item: dict[str, object] = {"date": raw_date}
        for indicator_index, indicator in enumerate(result.Indicators):
            values = indicator_series[indicator_index]
            if date_index >= len(values):
                raise RuntimeError(f"Choice {indicator} series is shorter than its date axis")
            item[str(indicator).lower()] = values[date_index]
        rows.append(item)
    return rows


def _annual_intervals(start: date, end: date) -> tuple[tuple[date, date], ...]:
    intervals: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        interval_end = min(end, date(cursor.year, 12, 31))
        intervals.append((cursor, interval_end))
        cursor = date(cursor.year + 1, 1, 1)
    return tuple(intervals)


def _calendar_requests(start: date, end: date) -> tuple[tuple[date, date, str], ...]:
    return tuple(
        (interval_start, interval_end, CALENDAR_OPTIONS)
        for interval_start, interval_end in _annual_intervals(start, end)
    )


def _extract_dates(value: object) -> list[date]:
    found: list[date] = []

    def visit(item: object) -> None:
        if isinstance(item, Mapping):
            for nested in item.values():
                visit(nested)
            return
        if isinstance(item, Sequence) and not isinstance(item, str | bytes | bytearray):
            for nested in item:
                visit(nested)
            return
        if isinstance(item, datetime):
            found.append(item.date())
            return
        if isinstance(item, date):
            found.append(item)
            return
        if isinstance(item, str):
            try:
                found.append(_provider_date(item))
            except ValueError:
                return

    visit(value)
    dates = sorted(set(found))
    if not dates:
        raise RuntimeError("Choice market calendar did not contain any dates")
    return dates


def _compare_prefix(
    full_rows: Sequence[Mapping[str, object]],
    prefix_rows: Sequence[Mapping[str, object]],
    prefix_end: date,
) -> dict[str, object]:
    full_by_date = {_provider_date(str(item["date"])).isoformat(): item for item in full_rows}
    prefix_by_date = {_provider_date(str(item["date"])).isoformat(): item for item in prefix_rows}
    expected_dates = sorted(key for key in full_by_date if date.fromisoformat(key) <= prefix_end)
    if expected_dates != sorted(prefix_by_date):
        return {
            "status": "failed",
            "prefixEnd": prefix_end.isoformat(),
            "reason": "date_axes_differ",
            "fullOverlapRows": len(expected_dates),
            "prefixRows": len(prefix_by_date),
        }
    max_absolute_difference = Decimal("0")
    mismatches = 0
    tolerance = Decimal("0.000001")
    for key in expected_dates:
        for field in ("open", "high", "low", "close"):
            difference = abs(
                Decimal(str(full_by_date[key][field])) - Decimal(str(prefix_by_date[key][field]))
            )
            max_absolute_difference = max(max_absolute_difference, difference)
            if difference > tolerance:
                mismatches += 1
    return {
        "status": "passed" if mismatches == 0 else "failed",
        "prefixEnd": prefix_end.isoformat(),
        "overlapRows": len(expected_dates),
        "tolerance": str(tolerance),
        "maxAbsoluteDifference": str(max_absolute_difference),
        "mismatchedCells": mismatches,
    }


def _serialize_result(result: Any) -> dict[str, object]:
    return {
        "errorCode": result.ErrorCode,
        "errorMessage": result.ErrorMsg,
        "codes": _safe_json(result.Codes),
        "indicators": _safe_json(result.Indicators),
        "dates": _safe_json(result.Dates),
        "data": _safe_json(result.Data),
    }


def _safe_json(value: object) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, date | datetime | Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _safe_json(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_safe_json(item) for item in value]
    return str(value)


def _optional_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _load_session_reference_source(
    path: Path,
    *,
    symbol: str,
) -> tuple[
    tuple[dict[str, object], ...],
    dict[str, object],
    dict[str, object],
]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("top-level JSON must be an object")
    if payload.get("schemaVersion") != "baostock.internal-demo-reference.v2":
        raise TypeError("reference artifact must use baostock.internal-demo-reference.v2")
    instrument = payload.get("instrument")
    if not isinstance(instrument, dict) or instrument.get("instrument_id") != symbol:
        raise TypeError("reference artifact belongs to a different instrument")
    historical_sessions = payload.get("historicalSessions")
    if not isinstance(historical_sessions, dict):
        raise TypeError("historicalSessions must be an object")
    session_rows = historical_sessions.get("rows")
    session_coverage = historical_sessions.get("coverage")
    if not isinstance(session_rows, list):
        raise TypeError("historicalSessions.rows must be an array")
    if not isinstance(session_coverage, dict):
        raise TypeError("historicalSessions.coverage must be an object")
    typed_session_rows: list[dict[str, object]] = []
    for index, row in enumerate(session_rows):
        if not isinstance(row, dict):
            raise TypeError(f"historicalSessions.rows[{index}] must be an object")
        typed_session_rows.append(dict(row))
    return (
        tuple(typed_session_rows),
        dict(session_coverage),
        payload,
    )


def _load_corporate_action_source(
    path: Path,
    *,
    symbol: str,
) -> tuple[tuple[CorporateAction, ...], dict[str, object], dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("top-level corporate-action JSON must be an object")
    schema_version = payload.get("schemaVersion")
    if schema_version == "eastmoney.corporate-action-reference.v1":
        if payload.get("instrumentId") != symbol:
            raise TypeError("corporate-action artifact belongs to a different instrument")
    elif schema_version == "baostock.internal-demo-reference.v2":
        instrument = payload.get("instrument")
        if not isinstance(instrument, dict) or instrument.get("instrument_id") != symbol:
            raise TypeError("corporate-action artifact belongs to a different instrument")
    else:
        raise TypeError("unsupported corporate-action reference schema")
    coverage = payload.get("coverage")
    actions = payload.get("actions")
    if not isinstance(coverage, dict):
        raise TypeError("corporate-action coverage must be an object")
    if not isinstance(actions, list):
        raise TypeError("corporate-action actions must be an array")
    parsed = tuple(
        _corporate_action_from_json(item, symbol=symbol, index=index)
        for index, item in enumerate(actions)
    )
    return parsed, dict(coverage), payload


def _corporate_action_from_json(
    value: object,
    *,
    symbol: str,
    index: int,
) -> CorporateAction:
    if not isinstance(value, dict):
        raise TypeError(f"actions[{index}] must be an object")
    row = value
    return CorporateAction(
        action_id=StrongId(_required_text(row, "action_id")),
        source_action_id=_required_text(row, "source_action_id"),
        instrument_id=InstrumentId(symbol),
        action_type=CorporateActionKind(_required_text(row, "action_type")),
        record_date=date.fromisoformat(_required_text(row, "record_date")),
        ex_date=date.fromisoformat(_required_text(row, "ex_date")),
        source_released_at=_optional_datetime(row.get("source_released_at")),
        vendor_first_available_at=_optional_datetime(row.get("vendor_first_available_at")),
        ingested_at=datetime.fromisoformat(_required_text(row, "ingested_at")),
        replay_available_at=datetime.fromisoformat(_required_text(row, "replay_available_at")),
        revision_no=_required_int(row, "revision_no"),
        time_quality=TimeQuality(_required_text(row, "time_quality")),
        provider=_required_text(row, "provider"),
        source_url=_required_text(row, "source_url"),
        raw_response_sha256=_required_text(row, "raw_response_sha256"),
        validation_status=_required_text(row, "validation_status"),
        currency=str(row.get("currency", "CNY")),
        gross_cash_per_share=_optional_decimal(row.get("gross_cash_per_share")),
        cash_pay_date=_optional_date(row.get("cash_pay_date")),
        share_multiplier=_optional_decimal(row.get("share_multiplier")),
        share_credit_date=_optional_date(row.get("share_credit_date")),
        share_sellable_date=_optional_date(row.get("share_sellable_date")),
        rights_ratio=_optional_decimal(row.get("rights_ratio")),
        rights_subscription_price=_optional_decimal(row.get("rights_subscription_price")),
        rights_payment_deadline=_optional_date(row.get("rights_payment_deadline")),
        rights_listing_date=_optional_date(row.get("rights_listing_date")),
    )


def _required_text(value: Mapping[str, object], field_name: str) -> str:
    item = value.get(field_name)
    if not isinstance(item, str) or not item.strip():
        raise TypeError(f"{field_name} must be non-empty text")
    return item


def _required_int(value: Mapping[str, object], field_name: str) -> int:
    item = value.get(field_name)
    if isinstance(item, bool) or not isinstance(item, int):
        raise TypeError(f"{field_name} must be an integer")
    return item


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("optional datetime must be ISO text")
    return datetime.fromisoformat(value)


def _optional_date(value: object) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("optional date must be ISO text")
    return date.fromisoformat(value)


def _optional_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise TypeError("optional decimal must be string or number")
    return Decimal(str(value))


def _provider_date(value: str) -> date:
    parts = value[:10].replace("/", "-").split("-")
    if len(parts) != 3:
        raise ValueError(f"invalid Choice date: {value!r}")
    return date(*(int(part) for part in parts))


# Shared, provider-neutral CLI helpers used by other daily snapshot collectors.
compare_price_prefix = _compare_prefix
load_session_reference_source = _load_session_reference_source
load_corporate_action_source = _load_corporate_action_source


if __name__ == "__main__":
    raise SystemExit(main())
