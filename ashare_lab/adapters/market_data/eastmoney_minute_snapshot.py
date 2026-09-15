"""Build immutable Eastmoney one-minute research snapshots.

Accepts a decoded :class:`EastmoneyMinuteCollection` (bar_end labels; a full
session is 241 rows = 240 continuous-auction bars plus the 09:30 opening-auction
bar) and publishes a content-addressed snapshot of 240 bars per
complete session.  Eastmoney trends2 returns unadjusted prices only (fqt/复权
not applicable), so no back-adjusted signal basis is fabricated.  This module
never logs in and never touches the Choice adapter.

Label semantics (calibrated 2026-09-09, post-close, both 300059.SZ/600519.SH):
``bar_end``. The separate 09:30 auction result is merged into the 09:31 bar
under the explicit v2 execution policy; raw source rows remain in provenance.
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
from typing import Any, Mapping, Sequence, cast
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq

from .eastmoney_minute import EastmoneyMinuteCollection, EastmoneyMinuteRow
from ashare_lab.domain.market_data import MinuteBar
from ashare_lab.domain.shared import InstrumentId, Price, Quantity

SNAPSHOT_SCHEMA_VERSION = "eastmoney.minute-research-snapshot.v2"
EXECUTION_FILENAME = "minute_ohlcv.parquet"
MANIFEST_FILENAME = "snapshot_manifest.json"
INTERVAL = "1m"
EXPECTED_BARS_PER_FULL_SESSION = 240
_HASH_CHUNK_BYTES = 1024 * 1024
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_ARROW: Any = pa
_PARQUET: Any = pq


class EastmoneyMinuteSnapshotError(RuntimeError):
    """Eastmoney minute rows cannot be promoted into a replayable snapshot."""


@dataclass(frozen=True, slots=True)
class EastmoneyMinuteSnapshotSpec:
    symbol: str
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise EastmoneyMinuteSnapshotError("snapshot start must not exceed end")


@dataclass(frozen=True, slots=True)
class EastmoneyMinuteSnapshotResult:
    snapshot_id: str
    path: Path
    manifest: Mapping[str, object]


def load_eastmoney_minute_snapshot(snapshot_path: Path) -> tuple[MinuteBar, ...]:
    """Read v2 raw execution bars, verifying immutable files before and after."""
    manifest = json.loads((snapshot_path / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    _validate_published_snapshot(snapshot_path, manifest)
    if (manifest.get("schemaVersion") != SNAPSHOT_SCHEMA_VERSION
            or manifest.get("openingAuctionBarPolicy") != "merged_09_30_into_09_31_ohlcv"
            or manifest.get("priceBasis") != "unadjusted"):
        raise EastmoneyMinuteSnapshotError("snapshot does not prove phase-one opening-price semantics")
    if EXECUTION_FILENAME not in manifest["files"]:
        raise EastmoneyMinuteSnapshotError("execution file missing from hash manifest")
    records = _PARQUET.read_table(snapshot_path / EXECUTION_FILENAME).to_pylist()
    bars = []
    for row in records:
        if row["stock_code"] != manifest["symbol"].split(".", maxsplit=1)[0]:
            raise EastmoneyMinuteSnapshotError("minute row identity differs from manifest")
        bars.append(MinuteBar(
            instrument_id=InstrumentId(manifest["symbol"]),
            bar_start_at=row["bar_start_at"], bar_end_at=row["bar_end_at"], available_at=row["available_at"],
            open=Price(row["open"]), high=Price(row["high"]), low=Price(row["low"]), close=Price(row["close"]),
            volume=Quantity(row["volume"]), turnover=row["amount"],
        ))
    _validate_published_snapshot(snapshot_path, manifest)
    return tuple(bars)


def build_eastmoney_minute_snapshot(
    *,
    spec: EastmoneyMinuteSnapshotSpec,
    collection: EastmoneyMinuteCollection,
    output_root: Path,
    captured_at: datetime,
) -> EastmoneyMinuteSnapshotResult:
    """Validate decoded trends2 rows and publish a content-addressed snapshot."""

    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise EastmoneyMinuteSnapshotError("captured_at must include an explicit timezone")
    if collection.period_minutes != 1:
        raise EastmoneyMinuteSnapshotError(
            "eastmoney minute snapshot currently supports only the 1-minute trends2 source"
        )
    canonical_symbol = collection.instrument_id
    if canonical_symbol != spec.symbol.upper():
        raise EastmoneyMinuteSnapshotError("symbol must match the decoded collection")

    bars = _canonicalize_bars(collection.rows)
    session_dates = _validate_complete_sessions(bars, spec=spec)

    destination_root = output_root.expanduser().resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".eastmoney-minute-", dir=destination_root))
    try:
        execution_path = temporary / EXECUTION_FILENAME
        _write_minute_bars(execution_path, bars, symbol=spec.symbol)

        provenance_path = temporary / "raw" / "collection.json"
        provenance_path.parent.mkdir(parents=True)
        _write_json(provenance_path, collection.manifest())

        file_manifest = {
            str(path.relative_to(temporary)): {
                "bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
            for path in (execution_path, provenance_path)
        }
        manifest_body: dict[str, object] = {
            "schemaVersion": SNAPSHOT_SCHEMA_VERSION,
            "provider": "eastmoney_push2his_public",
            "symbol": spec.symbol,
            "requestedRange": [spec.start.isoformat(), spec.end.isoformat()],
            "capturedAt": captured_at.astimezone(UTC).isoformat(),
            "interval": INTERVAL,
            "providerTimestampTimezone": "Asia/Shanghai",
            "providerTimestampSemantics": "bar_end",
            "priceBasis": "unadjusted",
            "openingAuctionBarPolicy": "merged_09_30_into_09_31_ohlcv",
            "signalMinuteCloseBasis": "unavailable_no_adjustment_factor",
            "rowCounts": {
                "executionMinute": len(bars),
                "sessions": len(session_dates),
            },
            "coverage": {
                "status": "complete_observed_sessions",
                "start": session_dates[0].isoformat(),
                "end": session_dates[-1].isoformat(),
                "requestedRangeVerified": False,
                "completenessScope": "observed_sessions_only_not_market_calendar_coverage",
                "sessionDates": [item.isoformat() for item in session_dates],
                "expectedBarsPerFullSession": EXPECTED_BARS_PER_FULL_SESSION,
                "missingBars": 0,
                "missingBarsScope": "observed_sessions_only",
                "duplicateBars": 0,
            },
            "sourceProvenance": {
                "requestUrl": collection.request_url,
                "requestParams": collection.request_params,
                "responseSha256": collection.response_sha256,
                "attempts": collection.attempts,
                "retrievedAt": collection.retrieved_at.astimezone(UTC).isoformat(),
            },
            "capabilities": {
                "technicalMinute": "validated_for_research",
                "minuteExecution": "research_snapshot_only",
                "tick": "unavailable",
                "signalMinuteClose": "unavailable",
            },
            "limitations": [
                "public interface; not authorized for production execution",
                "only complete normal A-share continuous-auction sessions are accepted",
                "unadjusted prices only; no corporate-action adjustment source",
                "09:30 auction is merged into the first bar; not vendor-native minute OHLCV",
            ],
            "files": file_manifest,
        }
        digest = hashlib.sha256(_canonical_json_bytes(manifest_body)).hexdigest()
        snapshot_id = f"eastmoney-minute:{digest}"
        manifest: dict[str, object] = {"snapshotId": snapshot_id, **manifest_body}
        _write_json(temporary / MANIFEST_FILENAME, manifest)

        destination = destination_root / digest
        if destination.exists():
            _validate_published_snapshot(destination, manifest)
            return EastmoneyMinuteSnapshotResult(snapshot_id, destination, manifest)
        temporary.replace(destination)
        temporary = destination
        _validate_published_snapshot(destination, manifest)
        return EastmoneyMinuteSnapshotResult(snapshot_id, destination, manifest)
    finally:
        if (
            temporary.exists()
            and temporary.parent == destination_root
            and temporary.name.startswith(".eastmoney-minute-")
        ):
            shutil.rmtree(temporary)


def _canonicalize_bars(rows: Sequence[EastmoneyMinuteRow], *,
                       allow_opening_range: bool = False,
                       confirmed_opening_prices: Mapping[date, Decimal] | None = None) -> list[dict[str, object]]:
    if not rows:
        raise EastmoneyMinuteSnapshotError("minute rows cannot be empty")
    canonical: list[dict[str, object]] = []
    auctions: dict[date, EastmoneyMinuteRow] = {}
    for index, row in enumerate(rows):
        label = row.timestamp.astimezone(_SHANGHAI)
        bar_end = label
        bar_start = label - timedelta(minutes=1)
        if not _is_continuous_auction_minute(bar_start, bar_end):
            if (
                label.time() == time(9, 30)
                and (allow_opening_range or row.open == row.high == row.low == row.close)
            ):
                if label.date() in auctions:
                    raise EastmoneyMinuteSnapshotError("duplicate opening-auction bar")
                if (not row.open.is_finite() or row.open <= 0
                        or not row.volume_shares.is_finite() or row.volume_shares < 0
                        or row.volume_shares != row.volume_shares.to_integral_value()
                        or not row.amount_cny.is_finite() or row.amount_cny < 0):
                    raise EastmoneyMinuteSnapshotError("invalid opening-auction values")
                if (any(not v.is_finite() or v <= 0 for v in (row.high, row.low, row.close))
                        or row.low > min(row.open, row.close)
                        or row.high < max(row.open, row.close)):
                    raise EastmoneyMinuteSnapshotError("invalid opening-auction OHLC")
                auctions[label.date()] = row
                continue
            raise EastmoneyMinuteSnapshotError(
                f"row {index} is outside the A-share continuous one-minute grid"
            )
        for value in (row.open, row.high, row.low, row.close):
            if value <= 0 or not value.is_finite():
                raise EastmoneyMinuteSnapshotError(f"row {index} has a non-positive price")
        if row.low > min(row.open, row.close) or row.high < max(row.open, row.close):
            raise EastmoneyMinuteSnapshotError(f"row {index} OHLC is inconsistent")
        volume = int(row.volume_shares)
        if row.volume_shares != volume:
            raise EastmoneyMinuteSnapshotError(f"row {index} volume is not whole shares")
        canonical.append(
            {
                "bar_start_at": bar_start,
                "bar_end_at": bar_end,
                "available_at": bar_end,
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "volume": volume,
                "amount": row.amount_cny,
            }
        )
    if not auctions:
        raise EastmoneyMinuteSnapshotError("missing the 09:30 opening-auction bar")
    session_dates = {cast(datetime, bar["bar_start_at"]).date() for bar in canonical}
    if set(auctions) != session_dates:
        raise EastmoneyMinuteSnapshotError("each session requires one opening-auction bar")
    for bar in canonical:
        stamp = cast(datetime, bar["bar_start_at"])
        if stamp.time() == time(9, 30):
            auction = auctions[stamp.date()]
            if (allow_opening_range and auction.volume_shares == 0 and auction.amount_cny == 0
                    and (confirmed_opening_prices or {}).get(stamp.date()) != auction.open):
                # External exports may repeat yesterday's close at 09:30 with
                # no executions. Only an independently confirmed daily opening
                # price can retain this auction label; never invent liquidity.
                opening = (confirmed_opening_prices or {}).get(stamp.date())
                if opening is not None and cast(Decimal, bar['low']) <= opening <= cast(Decimal, bar['high']):
                    # The auction is missing: use the sourced session opening,
                    # already observable at the open, within this minute's
                    # observed range. Never borrow the day's high/low/close.
                    bar['open'] = opening
                continue
            bar["open"] = auction.open
            bar["high"] = max(cast(Decimal, bar["high"]), auction.high)
            bar["low"] = min(cast(Decimal, bar["low"]), auction.low)
            bar["volume"] = cast(int, bar["volume"]) + int(auction.volume_shares)
            bar["amount"] = cast(Decimal, bar["amount"]) + auction.amount_cny
    canonical.sort(key=lambda item: cast(datetime, item["bar_start_at"]))
    starts = [cast(datetime, item["bar_start_at"]) for item in canonical]
    if len(starts) != len(set(starts)):
        raise EastmoneyMinuteSnapshotError("minute rows contain duplicate bar timestamps")
    return canonical


def _validate_complete_sessions(
    bars: Sequence[Mapping[str, object]],
    *,
    spec: EastmoneyMinuteSnapshotSpec,
) -> tuple[date, ...]:
    session_dates = tuple(sorted({cast(datetime, b["bar_start_at"]).date() for b in bars}))
    for session_date in session_dates:
        if not spec.start <= session_date <= spec.end:
            raise EastmoneyMinuteSnapshotError(
                f"session {session_date} is outside the requested snapshot range"
            )
        expected = _expected_session_starts(session_date)
        actual = tuple(
            cast(datetime, b["bar_start_at"])
            for b in bars
            if cast(datetime, b["bar_start_at"]).date() == session_date
        )
        if actual != expected:
            missing = len(set(expected) - set(actual))
            extra = len(set(actual) - set(expected))
            raise EastmoneyMinuteSnapshotError(
                f"minute session {session_date.isoformat()} is incomplete: "
                f"expected {len(expected)}, got {len(actual)}, missing {missing}, extra {extra}"
            )
    return session_dates


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


def _write_minute_bars(
    path: Path,
    bars: Sequence[Mapping[str, object]],
    *,
    symbol: str,
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
            "price_basis": "unadjusted",
            "interval": INTERVAL,
        }
        for row in bars
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
        raise EastmoneyMinuteSnapshotError("published minute manifest is unreadable") from exc
    if not isinstance(stored_manifest, dict) or stored_manifest != manifest:
        raise EastmoneyMinuteSnapshotError("published minute manifest differs from this build")
    snapshot_id = manifest.get("snapshotId")
    manifest_body = {key: value for key, value in manifest.items() if key != "snapshotId"}
    expected_digest = hashlib.sha256(_canonical_json_bytes(manifest_body)).hexdigest()
    if snapshot_id != f"eastmoney-minute:{expected_digest}" or snapshot_path.name != expected_digest:
        raise EastmoneyMinuteSnapshotError("published minute snapshot identity is invalid")
    raw_files = manifest.get("files")
    if not isinstance(raw_files, Mapping):
        raise EastmoneyMinuteSnapshotError("published minute manifest files must be an object")
    root = snapshot_path.resolve()
    for raw_relative, raw_metadata in cast(Mapping[object, object], raw_files).items():
        if not isinstance(raw_relative, str) or not raw_relative:
            raise EastmoneyMinuteSnapshotError("published minute manifest path is invalid")
        declared = snapshot_path / raw_relative
        candidate = declared.resolve()
        if (
            Path(raw_relative).is_absolute()
            or not candidate.is_relative_to(root)
            or declared.is_symlink()
        ):
            raise EastmoneyMinuteSnapshotError("published minute manifest path is unsafe")
        if not isinstance(raw_metadata, Mapping):
            raise EastmoneyMinuteSnapshotError("published minute file metadata is invalid")
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
            raise EastmoneyMinuteSnapshotError("published minute file metadata is invalid")
        if (
            not candidate.is_file()
            or candidate.stat().st_size != size
            or _sha256_file(candidate) != sha256
        ):
            raise EastmoneyMinuteSnapshotError(f"published minute file hash mismatch: {raw_relative}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()
