"""Strict glue around the Vibe-Trading mootdx daily query shape.

The narrow ``get_k_data`` call and its daily-frame schema are adapted from
HKUDS/Vibe-Trading at commit ``e90b6c6cd9fea23067a85667e7fbf74f9d73ea48``
(Copyright 2026 Vibe-Trading Contributors, MIT License).  No Vibe runtime,
registry, cache, fallback chain, or network client is imported here.

Mootdx exposes a parsed dataframe rather than the native TDX response bytes.
When the caller cannot capture those bytes, this adapter records a clearly
labelled hash of the canonical dataframe result; it never calls that digest a
wire hash.  A caller that captures the response bytes may provide them and get
their byte-for-byte SHA-256 in the acquisition evidence.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Protocol, cast
from zoneinfo import ZoneInfo

import pandas as pd

from ashare_lab.domain.market_data import BarInterval, PriceBasis, normalize_a_share_instrument
from ashare_lab.domain.shared import InstrumentId

SOURCE = "HKUDS/Vibe-Trading:agent/backtest/loaders/mootdx_loader.py"
SOURCE_REPOSITORY = "https://github.com/HKUDS/Vibe-Trading"
SOURCE_COMMIT = "e90b6c6cd9fea23067a85667e7fbf74f9d73ea48"
SOURCE_LICENSE = "MIT"
PROVIDER = "mootdx_std"
DATASET = "get_k_data_daily"
PROVIDER_INTERVAL = "1D"
SOURCE_VOLUME_UNIT = "board_lot"
NORMALIZED_VOLUME_UNIT = "share"
VOLUME_LOT_SIZE_SHARES = 100
SHANGHAI = ZoneInfo("Asia/Shanghai")
CURRENT_SESSION_STABILITY_MARKER = time(16, 5)

CALLER_BYTES_HASH_SEMANTICS = "sha256_of_caller_supplied_provider_response_bytes"
CANONICAL_FRAME_HASH_SEMANTICS = "sha256_of_canonical_mootdx_dataframe_result_not_raw_wire_bytes"

_REQUIRED_COLUMNS = (
    "open",
    "close",
    "high",
    "low",
    "vol",
    "amount",
    "date",
    "code",
)


class MootdxDailyClient(Protocol):
    """Only the Vibe loader's daily mootdx client surface."""

    def get_k_data(
        self,
        *,
        code: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame | None: ...


class VibeMootdxSourceError(RuntimeError):
    """A mootdx result cannot prove the requested strict daily coverage."""


@dataclass(frozen=True, slots=True)
class VibeMootdxDailyRow:
    """One provider-neutral daily row, preserving the declared lot input."""

    date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume_lots: Decimal
    volume_shares: int
    amount: Decimal

    def as_snapshot_row(self, instrument_id: InstrumentId) -> dict[str, object]:
        """Return the existing snapshot publisher's neutral daily-row shape."""

        return {
            "stock_code": str(instrument_id),
            "date": self.date.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume_shares,
            "amount": self.amount,
        }


@dataclass(frozen=True, slots=True)
class VibeMootdxAcquisitionEvidence:
    """Immutable query identity, result digest, and exact date-coverage proof."""

    request_items: tuple[tuple[str, str], ...]
    request_started_at: datetime
    response_received_at: datetime
    raw_response_sha256: str
    raw_response_hash_semantics: str
    canonical_frame_sha256: str
    expected_session_dates: tuple[date, ...]
    received_session_dates: tuple[date, ...]

    @property
    def source(self) -> str:
        return SOURCE

    @property
    def source_commit(self) -> str:
        return SOURCE_COMMIT

    @property
    def request(self) -> Mapping[str, str]:
        return dict(self.request_items)

    def as_dict(self) -> dict[str, object]:
        return {
            "source": SOURCE,
            "sourceRepository": SOURCE_REPOSITORY,
            "sourceCommit": SOURCE_COMMIT,
            "sourceLicense": SOURCE_LICENSE,
            "provider": PROVIDER,
            "dataset": DATASET,
            "request": dict(self.request_items),
            "requestStartedAt": self.request_started_at.isoformat(),
            "responseReceivedAt": self.response_received_at.isoformat(),
            "rawResponseSha256": self.raw_response_sha256,
            "rawResponseHashSemantics": self.raw_response_hash_semantics,
            "canonicalFrameSha256": self.canonical_frame_sha256,
            "rowCount": len(self.received_session_dates),
            "expectedSessionDates": [item.isoformat() for item in self.expected_session_dates],
            "receivedSessionDates": [item.isoformat() for item in self.received_session_dates],
            "coverageComplete": self.received_session_dates == self.expected_session_dates,
            "sourceVolumeUnit": SOURCE_VOLUME_UNIT,
            "normalizedVolumeUnit": NORMALIZED_VOLUME_UNIT,
            "volumeLotSizeShares": VOLUME_LOT_SIZE_SHARES,
        }


@dataclass(frozen=True, slots=True)
class VibeMootdxDailyCollection:
    """Validated daily rows and acquisition evidence; not a published snapshot."""

    instrument_id: InstrumentId
    provider_code: str
    requested_start: date
    requested_end: date
    rows: tuple[VibeMootdxDailyRow, ...]
    evidence: VibeMootdxAcquisitionEvidence
    price_basis: PriceBasis = PriceBasis.UNADJUSTED
    interval: BarInterval = BarInterval.DAY_1
    source_volume_unit: str = SOURCE_VOLUME_UNIT
    normalized_volume_unit: str = NORMALIZED_VOLUME_UNIT
    volume_lot_size_shares: int = VOLUME_LOT_SIZE_SHARES

    @property
    def source(self) -> str:
        return self.evidence.source

    @property
    def source_commit(self) -> str:
        return self.evidence.source_commit

    @property
    def request(self) -> Mapping[str, str]:
        return self.evidence.request

    @property
    def raw_response_sha256(self) -> str:
        return self.evidence.raw_response_sha256


class VibeMootdxDailyResearchSource:
    """Acquire exactly one SH/SZ daily series through an injected client."""

    def __init__(
        self,
        client: MootdxDailyClient,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._clock = clock or _shanghai_now

    def fetch(
        self,
        *,
        instrument_id: InstrumentId,
        start: date,
        end: date,
        expected_session_dates: Sequence[date],
        interval: BarInterval = BarInterval.DAY_1,
        raw_response_bytes: bytes | None = None,
    ) -> VibeMootdxDailyCollection:
        """Collect daily rows and fail closed unless exact coverage is proven.

        ``expected_session_dates`` must come from an authoritative, point-in-time
        instrument-session source.  Requiring it is intentional: a price API by
        itself cannot distinguish a holiday or suspension from missing history.
        """

        request_started_at = _read_clock(self._clock, "request_started_at")
        _validate_date_range(start, end, request_started_at=request_started_at)
        _validate_interval(interval)
        canonical_instrument, provider_code = _identity(instrument_id)
        expected = _validate_expected_sessions(
            expected_session_dates,
            start=start,
            end=end,
        )
        if raw_response_bytes is not None and type(raw_response_bytes) is not bytes:
            raise TypeError("raw_response_bytes must be bytes when supplied")

        request_items = tuple(
            sorted(
                {
                    "method": "get_k_data",
                    "code": provider_code,
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                    "interval": PROVIDER_INTERVAL,
                }.items()
            )
        )
        try:
            frame = self._client.get_k_data(
                code=provider_code,
                start_date=start.isoformat(),
                end_date=end.isoformat(),
            )
        except Exception as exc:  # pragma: no cover - concrete SDK failures vary
            raise VibeMootdxSourceError("mootdx get_k_data request failed") from exc
        response_received_at = _read_clock(self._clock, "response_received_at")
        if response_received_at < request_started_at:
            raise VibeMootdxSourceError("response_received_at precedes request_started_at")
        if not isinstance(frame, pd.DataFrame):
            raise VibeMootdxSourceError("mootdx get_k_data did not return a dataframe")
        if frame.empty:
            raise VibeMootdxSourceError("mootdx returned no daily history")

        frame_bytes = _canonical_frame_bytes(frame)
        canonical_frame_sha256 = hashlib.sha256(frame_bytes).hexdigest()
        if raw_response_bytes is None:
            raw_response_sha256 = canonical_frame_sha256
            hash_semantics = CANONICAL_FRAME_HASH_SEMANTICS
        else:
            raw_response_sha256 = hashlib.sha256(raw_response_bytes).hexdigest()
            hash_semantics = CALLER_BYTES_HASH_SEMANTICS

        rows = _parse_frame(
            frame,
            provider_code=provider_code,
            start=start,
            end=end,
        )
        received = tuple(row.date for row in rows)
        if received != expected:
            missing = tuple(item.isoformat() for item in expected if item not in received)
            unexpected = tuple(item.isoformat() for item in received if item not in expected)
            raise VibeMootdxSourceError(
                "mootdx daily coverage differs from expected instrument sessions: "
                f"missing={missing}, unexpected={unexpected}"
            )

        evidence = VibeMootdxAcquisitionEvidence(
            request_items=request_items,
            request_started_at=request_started_at,
            response_received_at=response_received_at,
            raw_response_sha256=raw_response_sha256,
            raw_response_hash_semantics=hash_semantics,
            canonical_frame_sha256=canonical_frame_sha256,
            expected_session_dates=expected,
            received_session_dates=received,
        )
        return VibeMootdxDailyCollection(
            instrument_id=canonical_instrument,
            provider_code=provider_code,
            requested_start=start,
            requested_end=end,
            rows=rows,
            evidence=evidence,
        )


def _identity(instrument_id: InstrumentId) -> tuple[InstrumentId, str]:
    try:
        canonical = normalize_a_share_instrument(instrument_id)
    except ValueError as exc:
        raise ValueError("instrument_id must be a canonical mainland A-share identifier") from exc
    if str(canonical) != str(instrument_id):
        raise ValueError(f"instrument_id must be canonical: {canonical}")
    code, exchange = str(canonical).split(".", maxsplit=1)
    if exchange == "BJ":
        raise ValueError("mootdx std daily acquisition does not support Beijing A-shares")
    if exchange not in {"SH", "SZ"}:
        raise ValueError("mootdx std daily acquisition supports SH and SZ A-shares only")
    return canonical, code


def _validate_interval(interval: BarInterval) -> None:
    if type(interval) is not BarInterval or interval is not BarInterval.DAY_1:
        raise ValueError("Vibe/mootdx glue supports only canonical 1-day bars")


def _validate_date_range(start: date, end: date, *, request_started_at: datetime) -> None:
    if type(start) is not date or type(end) is not date:
        raise TypeError("start and end must be date values")
    if start > end:
        raise ValueError("start must not be later than end")
    local_now = request_started_at.astimezone(SHANGHAI)
    if end > local_now.date():
        raise ValueError("requested end date must not be in the future")
    if end == local_now.date() and local_now.time() < CURRENT_SESSION_STABILITY_MARKER:
        raise ValueError("current daily session is not stable before 16:05 Asia/Shanghai")


def _validate_expected_sessions(
    values: Sequence[date],
    *,
    start: date,
    end: date,
) -> tuple[date, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError("expected_session_dates must be a date sequence")
    sessions = tuple(values)
    if not sessions:
        raise ValueError("expected_session_dates cannot be empty")
    if any(type(item) is not date for item in sessions):
        raise TypeError("expected_session_dates must contain date values")
    if len(sessions) != len(set(sessions)):
        raise ValueError("expected_session_dates cannot contain duplicates")
    if sessions != tuple(sorted(sessions)):
        raise ValueError("expected_session_dates must be strictly increasing")
    if any(not start <= item <= end for item in sessions):
        raise ValueError("expected_session_dates must stay inside the requested range")
    return sessions


def _parse_frame(
    frame: pd.DataFrame,
    *,
    provider_code: str,
    start: date,
    end: date,
) -> tuple[VibeMootdxDailyRow, ...]:
    raw_columns = tuple(cast(Sequence[object], frame.columns))
    if any(not isinstance(item, str) for item in raw_columns):
        raise VibeMootdxSourceError("mootdx dataframe columns must be text")
    columns = cast(tuple[str, ...], raw_columns)
    if len(columns) != len(set(columns)):
        raise VibeMootdxSourceError("mootdx dataframe contains duplicate columns")
    if set(columns) != set(_REQUIRED_COLUMNS) or len(columns) != len(_REQUIRED_COLUMNS):
        raise VibeMootdxSourceError(
            "mootdx daily dataframe columns must exactly match " + ",".join(_REQUIRED_COLUMNS)
        )
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise VibeMootdxSourceError("mootdx daily dataframe must have a DatetimeIndex")
    if frame.index.tz is not None:
        raise VibeMootdxSourceError("mootdx daily dataframe index must be timezone-naive dates")

    to_records: object = frame.to_dict  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    records = cast(
        Callable[..., list[dict[str, object]]],
        to_records,
    )(orient="records")
    index_values = tuple(cast(Sequence[pd.Timestamp], frame.index))
    parsed: list[VibeMootdxDailyRow] = []
    seen_dates: set[date] = set()
    for position, (record, index_value) in enumerate(zip(records, index_values, strict=True)):
        index_date = index_value.date()
        trade_date = _provider_date(record["date"], field_name="date", position=position)
        if trade_date != index_date:
            raise VibeMootdxSourceError(
                f"row {position} date column does not match its dataframe index"
            )
        if not start <= trade_date <= end:
            raise VibeMootdxSourceError(f"row {position} date is outside the requested range")
        if trade_date in seen_dates:
            raise VibeMootdxSourceError("mootdx daily dataframe contains duplicate trade dates")
        seen_dates.add(trade_date)

        code = record["code"]
        if not isinstance(code, str) or code.strip() != provider_code:
            raise VibeMootdxSourceError(
                f"row {position} stock code does not match the requested instrument"
            )
        open_cny = _positive_decimal(record["open"], field_name="open", position=position)
        high_cny = _positive_decimal(record["high"], field_name="high", position=position)
        low_cny = _positive_decimal(record["low"], field_name="low", position=position)
        close_cny = _positive_decimal(record["close"], field_name="close", position=position)
        if high_cny < max(open_cny, close_cny, low_cny) or low_cny > min(
            open_cny, close_cny, high_cny
        ):
            raise VibeMootdxSourceError(f"row {position} OHLC values are inconsistent")

        volume_lots = _non_negative_decimal(record["vol"], field_name="vol", position=position)
        volume_shares_decimal = volume_lots * VOLUME_LOT_SIZE_SHARES
        if volume_shares_decimal != volume_shares_decimal.to_integral_value():
            raise VibeMootdxSourceError(
                f"row {position} lot volume cannot be converted to whole shares"
            )
        volume_shares = int(volume_shares_decimal)
        amount = _non_negative_decimal(record["amount"], field_name="amount", position=position)
        if (volume_shares == 0) != (amount == 0):
            raise VibeMootdxSourceError(
                f"row {position} volume and amount must both be zero or both be positive"
            )
        if volume_shares > 0:
            share_count = Decimal(volume_shares)
            if not low_cny * share_count <= amount <= high_cny * share_count:
                raise VibeMootdxSourceError(
                    f"row {position} declared lot volume is inconsistent with amount and OHLC"
                )

        parsed.append(
            VibeMootdxDailyRow(
                date=trade_date,
                open=open_cny,
                high=high_cny,
                low=low_cny,
                close=close_cny,
                volume_lots=volume_lots,
                volume_shares=volume_shares,
                amount=amount,
            )
        )
    return tuple(sorted(parsed, key=lambda item: item.date))


def _provider_date(value: object, *, field_name: str, position: int) -> date:
    if isinstance(value, pd.Timestamp):
        if value.tz is not None:
            raise VibeMootdxSourceError(
                f"row {position} {field_name} must be a timezone-naive date"
            )
        return value.date()
    if type(value) is date:
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError as exc:
            raise VibeMootdxSourceError(f"row {position} {field_name} is not an ISO date") from exc
    raise VibeMootdxSourceError(f"row {position} {field_name} is not a date")


def _decimal(value: object, *, field_name: str, position: int) -> Decimal:
    if value is None or isinstance(value, bool):
        raise VibeMootdxSourceError(f"row {position} {field_name} is not numeric")
    text = str(value).strip()
    if not text:
        raise VibeMootdxSourceError(f"row {position} {field_name} is missing")
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise VibeMootdxSourceError(f"row {position} {field_name} is not numeric") from exc
    if not parsed.is_finite():
        raise VibeMootdxSourceError(f"row {position} {field_name} must be finite")
    return parsed


def _positive_decimal(value: object, *, field_name: str, position: int) -> Decimal:
    parsed = _decimal(value, field_name=field_name, position=position)
    if parsed <= 0:
        raise VibeMootdxSourceError(f"row {position} {field_name} must be positive")
    return parsed


def _non_negative_decimal(value: object, *, field_name: str, position: int) -> Decimal:
    parsed = _decimal(value, field_name=field_name, position=position)
    if parsed < 0:
        raise VibeMootdxSourceError(f"row {position} {field_name} must be non-negative")
    return parsed


def _canonical_frame_bytes(frame: pd.DataFrame) -> bytes:
    columns = tuple(str(item) for item in cast(Sequence[object], frame.columns))
    to_records: object = frame.to_dict  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    records = cast(
        Callable[..., list[dict[str, object]]],
        to_records,
    )(orient="records")
    index_values = tuple(cast(Sequence[object], frame.index))
    payload = {
        "columns": list(columns),
        "index": [_audit_scalar(item) for item in index_values],
        "rows": [[_audit_scalar(record.get(column)) for column in columns] for record in records],
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _audit_scalar(value: object) -> dict[str, str | None]:
    if value is None:
        return {"type": "NoneType", "value": None}
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return {"type": type(value).__name__, "value": value.isoformat()}
    return {"type": type(value).__name__, "value": str(value)}


def _read_clock(clock: Callable[[], datetime], field_name: str) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} clock value must be timezone-aware")
    return value


def _shanghai_now() -> datetime:
    return datetime.now(SHANGHAI)
