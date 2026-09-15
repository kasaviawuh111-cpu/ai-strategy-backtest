"""Aggregate validated 1-minute continuous-auction bars into higher periods.

1-minute is the only base period; 5/15/30/60 are derived locally so that a
future formal data source can replace the 1-minute adapter without touching
strategy or aggregation logic.

Fixed aggregation rules (decision 2026-09-09):
- Morning and afternoon are bucketed separately; a bucket never crosses the
  lunch break (11:30-13:00).
- Labels are ``bar_end`` (the last base bar's bar_end).
- OHLCV: open = first base open, high = max, low = min, close = last base
  close, volume = sum (shares), amount = sum.
- A bucket missing any base minute is invalid and is dropped, never padded.
- The 09:30 opening-auction bar is already excluded from the base bars.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence, cast
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq

from .eastmoney_minute import EastmoneyMinuteCollection
from .eastmoney_minute_snapshot import (
    EastmoneyMinuteSnapshotSpec,
    _canonicalize_bars,
    _validate_complete_sessions,
)

MinutePeriod = Literal[5, 15, 30, 60]
_SUPPORTED_PERIODS = (5, 15, 30, 60)
AGGREGATION_VERSION = "v1"
AGGREGATE_SCHEMA_VERSION = "eastmoney.minute-aggregate-snapshot.v1"
MANIFEST_FILENAME = "snapshot_manifest.json"
_HASH_CHUNK_BYTES = 1024 * 1024
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_ARROW: Any = pa
_PARQUET: Any = pq


class EastmoneyAggregationError(RuntimeError):
    """One-minute bars cannot be aggregated into the requested period."""


@dataclass(frozen=True, slots=True)
class AggregationResult:
    bars: tuple[dict[str, object], ...]
    skipped_incomplete_buckets: int


@dataclass(frozen=True, slots=True)
class AggregateSnapshotResult:
    snapshot_id: str
    path: Path
    manifest: Mapping[str, object]


def aggregate_minute_bars(
    bars: Sequence[Mapping[str, object]],
    *,
    period_minutes: MinutePeriod,
) -> AggregationResult:
    """Merge continuous 1-minute bars into ``period_minutes`` bars.

    Buckets are aligned to absolute session boundaries (not to running order),
    so a missing base minute only invalidates the bucket it belongs to.
    """
    if period_minutes not in _SUPPORTED_PERIODS:
        raise EastmoneyAggregationError("period must be 5, 15, 30 or 60 minutes")
    if not bars:
        return AggregationResult((), 0)
    merged: list[dict[str, object]] = []
    skipped = 0
    for members in _group_sessions(bars):
        session_start = _fixed_session_start(cast(datetime, members[0]["bar_start_at"]))
        buckets: dict[int, list[Mapping[str, object]]] = {}
        for bar in members:
            start = cast(datetime, bar["bar_start_at"])
            index = int((start - session_start).total_seconds() // (period_minutes * 60))
            buckets.setdefault(index, []).append(bar)
        for index in sorted(buckets):
            bucket = buckets[index]
            if len(bucket) != period_minutes:
                skipped += 1
                continue
            merged.append(_merge_bucket(bucket, period_minutes=period_minutes))
    merged.sort(key=lambda bar: cast(datetime, bar["bar_start_at"]))
    return AggregationResult(tuple(merged), skipped)


def _group_sessions(
    bars: Sequence[Mapping[str, object]],
) -> list[list[Mapping[str, object]]]:
    """Split bars into (date, morning/afternoon) groups, never crossing lunch."""
    grouped: dict[tuple[date, str], list[Mapping[str, object]]] = {}
    for bar in bars:
        start = cast(datetime, bar["bar_start_at"])
        session = "am" if start.time() < time(12, 0) else "pm"
        grouped.setdefault((start.date(), session), []).append(bar)
    for members in grouped.values():
        members.sort(key=lambda bar: cast(datetime, bar["bar_start_at"]))
    return list(grouped.values())


def _fixed_session_start(bar_start: datetime) -> datetime:
    """Return the fixed session boundary (09:30 am / 13:00 pm) for bucketing."""
    session_time = time(9, 30) if bar_start.time() < time(12, 0) else time(13, 0)
    return datetime.combine(bar_start.date(), session_time, tzinfo=_SHANGHAI)


def _merge_bucket(
    bucket: Sequence[Mapping[str, object]],
    *,
    period_minutes: MinutePeriod,
) -> dict[str, object]:
    first, last = bucket[0], bucket[-1]
    bar_start = cast(datetime, first["bar_start_at"])
    bar_end = cast(datetime, last["bar_end_at"])
    if bar_end - bar_start != timedelta(minutes=period_minutes):
        raise EastmoneyAggregationError(
            f"bucket span {bar_end - bar_start} does not match period {period_minutes}m"
        )
    high = max(cast(Decimal, bar["high"]) for bar in bucket)
    low = min(cast(Decimal, bar["low"]) for bar in bucket)
    volume = sum(cast(int, bar["volume"]) for bar in bucket)
    amount = sum(cast(Decimal, bar["amount"]) for bar in bucket)
    return {
        "bar_start_at": bar_start,
        "bar_end_at": bar_end,
        "available_at": bar_end,
        "open": first["open"],
        "high": high,
        "low": low,
        "close": last["close"],
        "volume": volume,
        "amount": amount,
    }


def build_aggregate_snapshot(
    *,
    collection: EastmoneyMinuteCollection,
    period_minutes: MinutePeriod,
    symbol: str,
    start: date,
    end: date,
    output_root: Path,
    captured_at: datetime,
    base_snapshot_id: str | None = None,
) -> AggregateSnapshotResult:
    """Publish a content-addressed aggregate snapshot derived from 1-minute rows."""
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise EastmoneyAggregationError("captured_at must include an explicit timezone")
    if collection.period_minutes != 1:
        raise EastmoneyAggregationError("aggregation requires the 1-minute trends2 source")
    base_bars = _canonicalize_bars(collection.rows)
    spec = EastmoneyMinuteSnapshotSpec(symbol=symbol, start=start, end=end)
    session_dates = _validate_complete_sessions(base_bars, spec=spec)
    result = aggregate_minute_bars(base_bars, period_minutes=period_minutes)

    destination_root = output_root.expanduser().resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".eastmoney-agg-", dir=destination_root))
    try:
        bars_path = temporary / f"minute_{period_minutes}m_ohlcv.parquet"
        _write_aggregate_bars(bars_path, result.bars, symbol=symbol, period_minutes=period_minutes)

        provenance_path = temporary / "raw" / "collection.json"
        provenance_path.parent.mkdir(parents=True)
        _write_json(provenance_path, collection.manifest())

        file_manifest = {
            str(path.relative_to(temporary)): {
                "bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
            for path in (bars_path, provenance_path)
        }
        manifest_body: dict[str, object] = {
            "schemaVersion": AGGREGATE_SCHEMA_VERSION,
            "aggregationVersion": AGGREGATION_VERSION,
            "provider": "eastmoney_push2his_public",
            "symbol": symbol,
            "periodMinutes": period_minutes,
            "derivedFrom": "1m",
            "baseResponseSha256": collection.response_sha256,
            "baseSnapshotId": base_snapshot_id,
            "requestedRange": [start.isoformat(), end.isoformat()],
            "capturedAt": captured_at.astimezone(UTC).isoformat(),
            "providerTimestampSemantics": "bar_end",
            "priceBasis": "unadjusted",
            "aggregationRules": {
                "open": "first_base_open",
                "high": "max_base_high",
                "low": "min_base_low",
                "close": "last_base_close",
                "volume": "sum_base_shares",
                "amount": "sum_base_amount",
                "lunchBreak": "never_crossed",
                "incompleteBucket": "dropped_not_padded",
            },
            "rowCounts": {
                "aggregateMinute": len(result.bars),
                "sessions": len(session_dates),
                "skippedIncompleteBuckets": result.skipped_incomplete_buckets,
            },
            "coverage": {
                "status": "complete" if result.skipped_incomplete_buckets == 0 else "partial",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "sessionDates": [item.isoformat() for item in session_dates],
                "expectedBarsPerFullSession": (240 // period_minutes),
            },
            "limitations": [
                "derived locally from 1-minute continuous-auction bars",
                "the 09:30 opening-auction bar is excluded before aggregation",
                "unadjusted prices only; no corporate-action adjustment source",
                "not an execution fill simulator",
            ],
            "files": file_manifest,
        }
        digest = hashlib.sha256(_canonical_json_bytes(manifest_body)).hexdigest()
        snapshot_id = f"eastmoney-min-{period_minutes}m:{digest}"
        manifest: dict[str, object] = {"snapshotId": snapshot_id, **manifest_body}
        _write_json(temporary / MANIFEST_FILENAME, manifest)

        destination = destination_root / digest
        if destination.exists():
            return AggregateSnapshotResult(snapshot_id, destination, manifest)
        temporary.replace(destination)
        temporary = destination
        return AggregateSnapshotResult(snapshot_id, destination, manifest)
    finally:
        if (
            temporary.exists()
            and temporary.parent == destination_root
            and temporary.name.startswith(".eastmoney-agg-")
        ):
            shutil.rmtree(temporary)


def _write_aggregate_bars(
    path: Path,
    bars: Sequence[Mapping[str, object]],
    *,
    symbol: str,
    period_minutes: MinutePeriod,
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
            **bar,
            "price_basis": "unadjusted",
            "interval": f"{period_minutes}m",
        }
        for bar in bars
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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()
