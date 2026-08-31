"""Build immutable Choice one-minute research snapshots.

The adapter accepts already decoded SDK rows and refuses publication unless
the execution and signal price bases align, every requested trading session is
complete, daily aggregation reconciles, and a shorter-endpoint prefix check
passes.  It never logs in to Choice and is safe to exercise with fixtures.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq

from .local_parquet import normalize_instrument_id

SNAPSHOT_SCHEMA_VERSION = "choice.minute-research-snapshot.v1"
ACCOUNT_SCOPE = "personal_research_demo"
EXECUTION_FILENAME = "minute_ohlcv.parquet"
SIGNAL_FILENAME = "signal_minute_close.parquet"
MANIFEST_FILENAME = "snapshot_manifest.json"
INTERVAL = "1m"
EXPECTED_BARS_PER_FULL_SESSION = 240
_HASH_CHUNK_BYTES = 1024 * 1024
_SECRET_KEY_FRAGMENTS = ("password", "passwd", "token", "userinfo", "mobile", "phone")
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_ARROW: Any = pa
_PARQUET: Any = pq

type TimestampSemantics = Literal["bar_start", "bar_end"]


class ChoiceMinuteSnapshotError(RuntimeError):
    """Choice minute rows cannot be promoted into a replayable snapshot."""


@dataclass(frozen=True, slots=True)
class ChoiceMinuteSnapshotSpec:
    symbol: str
    start: date
    end: date
    timestamp_semantics: TimestampSemantics

    def __post_init__(self) -> None:
        canonical = normalize_instrument_id(self.symbol)
        if str(canonical) != self.symbol.upper():
            raise ChoiceMinuteSnapshotError(f"symbol must be canonical: {canonical}")
        if self.start > self.end:
            raise ChoiceMinuteSnapshotError("snapshot start must not exceed end")
        if self.timestamp_semantics not in {"bar_start", "bar_end"}:
            raise ChoiceMinuteSnapshotError("timestamp semantics must be bar_start or bar_end")


@dataclass(frozen=True, slots=True)
class ChoiceMinuteSnapshotResult:
    snapshot_id: str
    path: Path
    manifest: Mapping[str, object]


def build_choice_minute_snapshot(
    *,
    spec: ChoiceMinuteSnapshotSpec,
    execution_rows: Sequence[Mapping[str, object]],
    signal_rows: Sequence[Mapping[str, object]],
    market_calendar: Sequence[date],
    raw_response_chunks: Sequence[Mapping[str, object]],
    request_audit: Mapping[str, object],
    daily_reconciliation: Mapping[str, object],
    prefix_stability: Mapping[str, object],
    output_root: Path,
    captured_at: datetime,
    sdk_archive_sha256: str | None,
) -> ChoiceMinuteSnapshotResult:
    """Validate dual-price minute rows and publish a content-addressed snapshot."""

    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise ChoiceMinuteSnapshotError("captured_at must include an explicit timezone")
    if not raw_response_chunks:
        raise ChoiceMinuteSnapshotError("at least one successful SDK response is required")
    for payload in raw_response_chunks:
        _reject_secret_keys(payload)
    _reject_secret_keys(request_audit)

    execution = _canonicalize_execution_rows(execution_rows, spec=spec)
    signal = _canonicalize_signal_rows(signal_rows, spec=spec)
    _validate_alignment(execution, signal)
    latest_available_at = max(cast(datetime, row["available_at"]) for row in execution)
    if captured_at.astimezone(UTC) < latest_available_at.astimezone(UTC):
        raise ChoiceMinuteSnapshotError("captured_at cannot precede the latest minute data")
    session_dates = _validate_complete_sessions(execution, market_calendar, spec=spec)
    _validate_complete_sessions(signal, market_calendar, spec=spec)
    _validate_gate(
        daily_reconciliation,
        name="daily reconciliation",
        expected_sessions=len(session_dates),
    )
    _validate_gate(prefix_stability, name="prefix stability", expected_rows=240)

    destination_root = output_root.expanduser().resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".choice-minute-", dir=destination_root))
    try:
        raw_root = temporary / "raw"
        response_root = raw_root / "responses"
        response_root.mkdir(parents=True)
        response_paths: list[Path] = []
        for index, payload in enumerate(raw_response_chunks):
            target = response_root / f"{index:05d}.json"
            _write_json(target, payload)
            response_paths.append(target)
        request_path = raw_root / "requests.json"
        _write_json(request_path, request_audit)

        execution_path = temporary / EXECUTION_FILENAME
        signal_path = temporary / SIGNAL_FILENAME
        _write_minute_for_symbol(
            execution_path,
            execution,
            symbol=spec.symbol,
            price_basis="unadjusted",
        )
        _write_signal_minute_for_symbol(
            signal_path,
            signal,
            symbol=spec.symbol,
        )

        data_paths = (execution_path, signal_path, request_path, *response_paths)
        file_manifest = {
            str(path.relative_to(temporary)): {
                "bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
            for path in data_paths
        }
        manifest_body: dict[str, object] = {
            "schemaVersion": SNAPSHOT_SCHEMA_VERSION,
            "provider": "Choice Quant API",
            "accountScope": ACCOUNT_SCOPE,
            "symbol": spec.symbol,
            "requestedRange": [spec.start.isoformat(), spec.end.isoformat()],
            "capturedAt": captured_at.astimezone(UTC).isoformat(),
            "interval": INTERVAL,
            "providerTimestampTimezone": "Asia/Shanghai",
            "providerTimestampSemantics": spec.timestamp_semantics,
            "availableAtPolicy": "completed_bar_end.v1",
            "decimalPolicy": {
                "prices": "round_half_even_to_6_decimal_places",
                "amount": "round_half_even_to_4_decimal_places",
                "volume": "exact_integer",
            },
            "rowCounts": {
                "executionMinute": len(execution),
                "signalMinute": len(signal),
                "sessions": len(session_dates),
                "sourceResponses": len(response_paths),
            },
            "coverage": {
                "status": "complete",
                "querySucceeded": True,
                "start": spec.start.isoformat(),
                "end": spec.end.isoformat(),
                "sessionDates": [item.isoformat() for item in session_dates],
                "expectedBarsPerFullSession": EXPECTED_BARS_PER_FULL_SESSION,
                "missingBars": 0,
                "duplicateBars": 0,
            },
            "priceBases": {
                EXECUTION_FILENAME: {
                    "basis": "unadjusted",
                    "choiceAdjustFlag": 1,
                    "uses": ["matching", "capacity", "fees", "ledger", "valuation"],
                },
                SIGNAL_FILENAME: {
                    "basis": "back_adjusted",
                    "choiceAdjustFlag": 2,
                    "choiceIndicators": ["CLOSE"],
                    "uses": ["intraday_daily_indicator_estimation"],
                },
            },
            "dailyReconciliation": dict(daily_reconciliation),
            "adjustmentPrefixStability": dict(prefix_stability),
            "rawResponseHashSemantics": (
                "sha256_of_canonical_sdk_decoded_response_json; "
                "native SDK does not expose exact HTTP response bytes"
            ),
            "sdkArchiveSha256": sdk_archive_sha256,
            "files": file_manifest,
            "capabilities": {
                "technicalMinute": "validated_for_demo",
                "minuteExecution": "next_tradable_minute_only",
                "tick": "unavailable",
                "l1Quotes": "unavailable",
                "l2Queue": "unavailable",
            },
            "limitations": [
                "personal research account; not authorized for production use",
                "only complete normal A-share continuous-auction sessions are accepted",
                "no tick, quote, or queue-position evidence is included",
                "the signal minute cannot fill at its own close",
                "historical depth is limited to the explicitly proven requested range",
            ],
        }
        digest = hashlib.sha256(_canonical_json_bytes(manifest_body)).hexdigest()
        snapshot_id = f"choice-minute:{digest}"
        manifest: dict[str, object] = {"snapshotId": snapshot_id, **manifest_body}
        _write_json(temporary / MANIFEST_FILENAME, manifest)

        destination = destination_root / digest
        if destination.exists():
            _validate_published_snapshot(destination, manifest)
            return ChoiceMinuteSnapshotResult(snapshot_id, destination, manifest)
        temporary.replace(destination)
        temporary = destination
        _validate_published_snapshot(destination, manifest)
        return ChoiceMinuteSnapshotResult(snapshot_id, destination, manifest)
    finally:
        if (
            temporary.exists()
            and temporary.parent == destination_root
            and temporary.name.startswith(".choice-minute-")
        ):
            shutil.rmtree(temporary)


def _canonicalize_execution_rows(
    rows: Sequence[Mapping[str, object]],
    *,
    spec: ChoiceMinuteSnapshotSpec,
) -> list[dict[str, object]]:
    if not rows:
        raise ChoiceMinuteSnapshotError("minute rows cannot be empty")
    canonical: list[dict[str, object]] = []
    for index, row in enumerate(rows):
        raw_label = row.get("timestamp", row.get("time", row.get("date")))
        label = _provider_datetime(raw_label, f"row {index} timestamp")
        if spec.timestamp_semantics == "bar_end":
            bar_end = label
            bar_start = label - timedelta(minutes=1)
        else:
            bar_start = label
            bar_end = label + timedelta(minutes=1)
        if not spec.start <= bar_start.date() <= spec.end:
            raise ChoiceMinuteSnapshotError(f"row {index} is outside the requested date range")
        if not _is_continuous_auction_minute(bar_start, bar_end):
            raise ChoiceMinuteSnapshotError(
                f"row {index} is outside the A-share continuous one-minute grid"
            )
        open_price = _positive_decimal(row.get("open"), f"row {index} open", scale=6)
        high_price = _positive_decimal(row.get("high"), f"row {index} high", scale=6)
        low_price = _positive_decimal(row.get("low"), f"row {index} low", scale=6)
        close_price = _positive_decimal(row.get("close"), f"row {index} close", scale=6)
        if low_price > min(open_price, high_price, close_price) or high_price < max(
            open_price, low_price, close_price
        ):
            raise ChoiceMinuteSnapshotError(f"row {index} OHLC is inconsistent")
        canonical.append(
            {
                "bar_start_at": bar_start,
                "bar_end_at": bar_end,
                "available_at": bar_end,
                "open": open_price,
                "high": high_price,
                "low": low_price,
                "close": close_price,
                "volume": _non_negative_integer(row.get("volume"), f"row {index} volume"),
                "amount": _non_negative_decimal(row.get("amount"), f"row {index} amount", scale=4),
            }
        )
    canonical.sort(key=lambda item: cast(datetime, item["bar_start_at"]))
    timestamps = [cast(datetime, item["bar_start_at"]) for item in canonical]
    if len(timestamps) != len(set(timestamps)):
        raise ChoiceMinuteSnapshotError("minute rows contain duplicate bar timestamps")
    return canonical


def _canonicalize_signal_rows(
    rows: Sequence[Mapping[str, object]],
    *,
    spec: ChoiceMinuteSnapshotSpec,
) -> list[dict[str, object]]:
    if not rows:
        raise ChoiceMinuteSnapshotError("minute signal rows cannot be empty")
    canonical: list[dict[str, object]] = []
    for index, row in enumerate(rows):
        raw_label = row.get("timestamp", row.get("time", row.get("date")))
        label = _provider_datetime(raw_label, f"signal row {index} timestamp")
        if spec.timestamp_semantics == "bar_end":
            bar_end = label
            bar_start = label - timedelta(minutes=1)
        else:
            bar_start = label
            bar_end = label + timedelta(minutes=1)
        if not spec.start <= bar_start.date() <= spec.end:
            raise ChoiceMinuteSnapshotError(
                f"signal row {index} is outside the requested date range"
            )
        if not _is_continuous_auction_minute(bar_start, bar_end):
            raise ChoiceMinuteSnapshotError(
                f"signal row {index} is outside the A-share continuous one-minute grid"
            )
        canonical.append(
            {
                "bar_start_at": bar_start,
                "bar_end_at": bar_end,
                "available_at": bar_end,
                "close": _positive_decimal(row.get("close"), f"signal row {index} close", scale=6),
            }
        )
    canonical.sort(key=lambda item: cast(datetime, item["bar_start_at"]))
    timestamps = [cast(datetime, item["bar_start_at"]) for item in canonical]
    if len(timestamps) != len(set(timestamps)):
        raise ChoiceMinuteSnapshotError("minute signal rows contain duplicate bar timestamps")
    return canonical


def _validate_alignment(
    execution: Sequence[Mapping[str, object]],
    signal: Sequence[Mapping[str, object]],
) -> None:
    execution_times = [item["bar_start_at"] for item in execution]
    signal_times = [item["bar_start_at"] for item in signal]
    if execution_times != signal_times:
        raise ChoiceMinuteSnapshotError("minute execution and signal rows must align one-to-one")


def _validate_complete_sessions(
    rows: Sequence[Mapping[str, object]],
    market_calendar: Sequence[date],
    *,
    spec: ChoiceMinuteSnapshotSpec,
) -> tuple[date, ...]:
    session_dates = tuple(
        sorted({item for item in market_calendar if spec.start <= item <= spec.end})
    )
    if not session_dates:
        raise ChoiceMinuteSnapshotError("market calendar contains no requested sessions")
    if len(session_dates) != len(
        [item for item in market_calendar if spec.start <= item <= spec.end]
    ):
        raise ChoiceMinuteSnapshotError("market calendar contains duplicate requested sessions")

    rows_by_date: dict[date, list[datetime]] = defaultdict(list)
    for row in rows:
        timestamp = cast(datetime, row["bar_start_at"])
        rows_by_date[timestamp.date()].append(timestamp)
    if set(rows_by_date) != set(session_dates):
        raise ChoiceMinuteSnapshotError(
            "minute row dates do not align with the requested market calendar"
        )
    for session_date in session_dates:
        expected = _expected_session_starts(session_date)
        actual = tuple(rows_by_date[session_date])
        if actual != expected:
            missing = len(set(expected) - set(actual))
            extra = len(set(actual) - set(expected))
            raise ChoiceMinuteSnapshotError(
                f"minute session {session_date.isoformat()} is incomplete: "
                f"expected {len(expected)}, got {len(actual)}, missing {missing}, extra {extra}"
            )
    return session_dates


def _validate_gate(
    value: Mapping[str, object],
    *,
    name: str,
    expected_sessions: int | None = None,
    expected_rows: int | None = None,
) -> None:
    if value.get("status") != "passed":
        raise ChoiceMinuteSnapshotError(f"{name} must pass before snapshot publication")
    if expected_sessions is not None and value.get("sessionCount") != expected_sessions:
        raise ChoiceMinuteSnapshotError(f"{name} session count does not match minute coverage")
    if expected_rows is not None and value.get("overlapRows") != expected_rows:
        raise ChoiceMinuteSnapshotError(f"{name} row count does not prove a complete session")


def _expected_session_starts(session_date: date) -> tuple[datetime, ...]:
    starts: list[datetime] = []
    current = datetime.combine(session_date, time(9, 30), tzinfo=_SHANGHAI)
    morning_end = datetime.combine(session_date, time(11, 30), tzinfo=_SHANGHAI)
    while current < morning_end:
        starts.append(current)
        current += timedelta(minutes=1)
    current = datetime.combine(session_date, time(13, 0), tzinfo=_SHANGHAI)
    afternoon_end = datetime.combine(session_date, time(15, 0), tzinfo=_SHANGHAI)
    while current < afternoon_end:
        starts.append(current)
        current += timedelta(minutes=1)
    return tuple(starts)


def _is_continuous_auction_minute(bar_start: datetime, bar_end: datetime) -> bool:
    if (
        bar_end - bar_start != timedelta(minutes=1)
        or bar_start.tzinfo is None
        or bar_start.date() != bar_end.date()
        or bar_start.second != 0
        or bar_start.microsecond != 0
    ):
        return False
    local_time = bar_start.timetz().replace(tzinfo=None)
    return time(9, 30) <= local_time < time(11, 30) or time(13, 0) <= local_time < time(15, 0)


def _write_minute_for_symbol(
    path: Path,
    rows: Sequence[Mapping[str, object]],
    *,
    symbol: str,
    price_basis: str,
) -> None:
    schema = _ARROW.schema(
        [
            ("stock_code", _ARROW.string()),
            ("bar_start_at", _ARROW.timestamp("us", tz="Asia/Shanghai")),
            ("bar_end_at", _ARROW.timestamp("us", tz="Asia/Shanghai")),
            ("available_at", _ARROW.timestamp("us", tz="Asia/Shanghai")),
            ("open", _ARROW.decimal128(20, 6)),
            ("high", _ARROW.decimal128(20, 6)),
            ("low", _ARROW.decimal128(20, 6)),
            ("close", _ARROW.decimal128(20, 6)),
            ("volume", _ARROW.int64()),
            ("amount", _ARROW.decimal128(24, 4)),
            ("price_basis", _ARROW.string()),
            ("interval", _ARROW.string()),
        ]
    )
    provider_code = symbol.split(".", maxsplit=1)[0]
    payload = [
        {
            "stock_code": provider_code,
            **row,
            "price_basis": price_basis,
            "interval": INTERVAL,
        }
        for row in rows
    ]
    _PARQUET.write_table(_ARROW.Table.from_pylist(payload, schema=schema), path)


def _write_signal_minute_for_symbol(
    path: Path,
    rows: Sequence[Mapping[str, object]],
    *,
    symbol: str,
) -> None:
    schema = _ARROW.schema(
        [
            ("stock_code", _ARROW.string()),
            ("bar_start_at", _ARROW.timestamp("us", tz="Asia/Shanghai")),
            ("bar_end_at", _ARROW.timestamp("us", tz="Asia/Shanghai")),
            ("available_at", _ARROW.timestamp("us", tz="Asia/Shanghai")),
            ("close", _ARROW.decimal128(20, 6)),
            ("price_basis", _ARROW.string()),
            ("interval", _ARROW.string()),
        ]
    )
    provider_code = symbol.split(".", maxsplit=1)[0]
    payload = [
        {
            "stock_code": provider_code,
            **row,
            "price_basis": "back_adjusted",
            "interval": INTERVAL,
        }
        for row in rows
    ]
    _PARQUET.write_table(_ARROW.Table.from_pylist(payload, schema=schema), path)


def _write_json(path: Path, value: object) -> None:
    path.write_bytes(_canonical_json_bytes(value) + b"\n")


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
        default=_json_default,
    ).encode("utf-8")


def _json_default(value: object) -> str:
    if isinstance(value, date | datetime | Decimal):
        return value.isoformat() if isinstance(value, date | datetime) else str(value)
    raise TypeError(f"cannot encode {type(value).__name__}")


def _validate_published_snapshot(
    snapshot_path: Path,
    manifest: Mapping[str, object],
) -> None:
    manifest_path = snapshot_path / MANIFEST_FILENAME
    try:
        stored_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ChoiceMinuteSnapshotError("published minute manifest is unreadable") from exc
    if not isinstance(stored_manifest, dict) or stored_manifest != manifest:
        raise ChoiceMinuteSnapshotError("published minute manifest differs from this build")
    snapshot_id = manifest.get("snapshotId")
    manifest_body = {key: value for key, value in manifest.items() if key != "snapshotId"}
    expected_digest = hashlib.sha256(_canonical_json_bytes(manifest_body)).hexdigest()
    if snapshot_id != f"choice-minute:{expected_digest}" or snapshot_path.name != expected_digest:
        raise ChoiceMinuteSnapshotError("published minute snapshot identity is invalid")
    raw_files = manifest.get("files")
    if not isinstance(raw_files, Mapping):
        raise ChoiceMinuteSnapshotError("published minute manifest files must be an object")
    root = snapshot_path.resolve()
    for raw_relative, raw_metadata in cast(Mapping[object, object], raw_files).items():
        if not isinstance(raw_relative, str) or not raw_relative:
            raise ChoiceMinuteSnapshotError("published minute manifest path is invalid")
        declared = snapshot_path / raw_relative
        candidate = declared.resolve()
        if (
            Path(raw_relative).is_absolute()
            or not candidate.is_relative_to(root)
            or declared.is_symlink()
        ):
            raise ChoiceMinuteSnapshotError("published minute manifest path is unsafe")
        if not isinstance(raw_metadata, Mapping):
            raise ChoiceMinuteSnapshotError("published minute file metadata is invalid")
        metadata = cast(Mapping[object, object], raw_metadata)
        size = metadata.get("bytes")
        sha256 = metadata.get("sha256")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(sha256, str)
            or len(sha256) != 64
        ):
            raise ChoiceMinuteSnapshotError("published minute file metadata is invalid")
        if (
            not candidate.is_file()
            or candidate.stat().st_size != size
            or _sha256_file(candidate) != sha256
        ):
            raise ChoiceMinuteSnapshotError(f"published minute file hash mismatch: {raw_relative}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_secret_keys(value: object, *, path: str = "root") -> None:
    if isinstance(value, Mapping):
        for key, item in cast(Mapping[object, object], value).items():
            name = str(key)
            if any(fragment in name.lower() for fragment in _SECRET_KEY_FRAGMENTS):
                raise ChoiceMinuteSnapshotError(
                    f"audit payload contains a forbidden secret key: {path}.{name}"
                )
            _reject_secret_keys(item, path=f"{path}.{name}")
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        for index, item in enumerate(cast(Sequence[object], value)):
            _reject_secret_keys(item, path=f"{path}[{index}]")


def _provider_datetime(value: object, field_name: str) -> datetime:
    if isinstance(value, datetime):
        return (
            value.replace(tzinfo=_SHANGHAI) if value.tzinfo is None else value.astimezone(_SHANGHAI)
        )
    if not isinstance(value, str) or not value.strip():
        raise ChoiceMinuteSnapshotError(f"{field_name} must be a provider datetime")
    raw = value.strip()
    parsed: datetime
    try:
        if raw.isdigit() and len(raw) == 14:
            parsed = datetime.strptime(raw, "%Y%m%d%H%M%S")
        else:
            parsed = datetime.fromisoformat(raw.replace("/", "-").replace("Z", "+00:00"))
    except ValueError as exc:
        raise ChoiceMinuteSnapshotError(f"{field_name} is invalid: {value!r}") from exc
    return (
        parsed.replace(tzinfo=_SHANGHAI) if parsed.tzinfo is None else parsed.astimezone(_SHANGHAI)
    )


def _decimal(value: object, field_name: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ChoiceMinuteSnapshotError(f"{field_name} must be numeric")
    try:
        converted = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ChoiceMinuteSnapshotError(f"{field_name} must be numeric") from exc
    if not converted.is_finite():
        raise ChoiceMinuteSnapshotError(f"{field_name} must be finite")
    return converted


def _positive_decimal(value: object, field_name: str, *, scale: int) -> Decimal:
    converted = _scaled_decimal(value, field_name, scale=scale)
    if converted <= 0:
        raise ChoiceMinuteSnapshotError(f"{field_name} must be positive")
    return converted


def _non_negative_decimal(
    value: object,
    field_name: str,
    *,
    scale: int | None = None,
) -> Decimal:
    converted = (
        _decimal(value, field_name)
        if scale is None
        else _scaled_decimal(value, field_name, scale=scale)
    )
    if converted < 0:
        raise ChoiceMinuteSnapshotError(f"{field_name} must be non-negative")
    return converted


def _non_negative_integer(value: object, field_name: str) -> int:
    converted = _non_negative_decimal(value, field_name)
    integral = converted.to_integral_value()
    if converted != integral:
        raise ChoiceMinuteSnapshotError(f"{field_name} must be a whole number")
    return int(integral)


def _scaled_decimal(value: object, field_name: str, *, scale: int) -> Decimal:
    converted = _decimal(value, field_name)
    quantum = Decimal(1).scaleb(-scale)
    try:
        return converted.quantize(quantum)
    except InvalidOperation as exc:
        raise ChoiceMinuteSnapshotError(
            f"{field_name} cannot be represented at scale {scale}"
        ) from exc
