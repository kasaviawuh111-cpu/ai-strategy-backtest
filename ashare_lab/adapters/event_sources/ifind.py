"""Normalize iFinD announcement rows without importing the vendor SDK.

The adapter boundary deliberately accepts plain mappings.  A caller may turn a
``THS_ReportQuery`` response, a DataFrame record, or a recorded fixture into a
mapping before it reaches this module.  Vendor timestamps without an explicit
offset are interpreted using iFinD's documented China-market clock.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from numbers import Integral, Real
from typing import Protocol, cast
from zoneinfo import ZoneInfo

from ashare_lab.domain.events.observations import EventObservation
from ashare_lab.domain.market_data import TimeQuality
from ashare_lab.domain.shared import InstrumentId

SHANGHAI = ZoneInfo("Asia/Shanghai")
VALIDATED = "validated"
BLOCKED_TIME_QUALITY = "blocked_time_quality"

_DATE_ONLY = re.compile(r"^\d{4}[-/]\d{2}[-/]\d{2}$")
_COMPACT_DATE = re.compile(r"^\d{8}$")
_SECOND_COMPONENT = re.compile(r"(?:^|[ T])\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?")
_SHA256 = re.compile(r"^(?:sha256:)?([0-9a-fA-F]{64})$")


class VendorRowsCallable(Protocol):
    """Minimal injectable client surface shared by the vendor adapters."""

    def __call__(
        self,
        *args: object,
        **kwargs: object,
    ) -> Iterable[object]: ...


class VendorEventRowError(ValueError):
    """Raised when a vendor row cannot be normalized without guessing."""


class IFindEventRowError(VendorEventRowError):
    """Raised when an iFinD announcement row violates the adapter contract."""


class IFindEventSourceAdapter:
    """Small callable wrapper suitable for an optional iFinD client."""

    def __init__(self, fetch_rows: VendorRowsCallable) -> None:
        self._fetch_rows = fetch_rows

    def normalize(
        self,
        row: Mapping[str, object],
        *,
        event_code: str | None = None,
        retrieved_at: datetime | None = None,
    ) -> EventObservation:
        return normalize_ifind_row(
            row,
            event_code=event_code,
            retrieved_at=retrieved_at,
        )

    def fetch(
        self,
        *args: object,
        event_code: str,
        retrieved_at: datetime,
        **kwargs: object,
    ) -> tuple[EventObservation, ...]:
        rows: Iterable[object] = self._fetch_rows(*args, **kwargs)
        observations: list[EventObservation] = []
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                raise IFindEventRowError(
                    f"iFinD row {index} must be a mapping, got {type(row).__name__}"
                )
            typed_row = cast(Mapping[str, object], row)
            observations.append(
                normalize_ifind_row(
                    typed_row,
                    event_code=event_code,
                    retrieved_at=retrieved_at,
                )
            )
        return tuple(observations)


def normalize_ifind_row(
    row: Mapping[str, object],
    *,
    event_code: str | None = None,
    retrieved_at: datetime | None = None,
) -> EventObservation:
    """Convert one ``THS_ReportQuery`` row into a point-in-time observation."""

    provider_event_id = vendor_row_required_identifier(
        row,
        "iFinD",
        ("provider_event_id", "seq", "announcement_id", "id"),
    )
    instrument_id = vendor_row_required_instrument(
        row,
        "iFinD",
        ("thscode", "ts_code", "instrument_id"),
    )
    canonical_event_code = vendor_row_required_event_code(row, event_code, "iFinD")
    title = vendor_row_required_text(row, "iFinD", ("reportTitle", "title"))
    document_url = vendor_row_optional_text(row, ("pdfURL", "url", "document_url"))
    released = vendor_row_required_timestamp(row, "iFinD", ("ctime",))
    ingested_at = vendor_row_required_retrieved_at(row, retrieved_at, "iFinD")
    quality, validation_status = vendor_row_observation_quality(released)

    return EventObservation(
        provider="ifind",
        provider_event_id=provider_event_id,
        instrument_id=instrument_id,
        event_code=canonical_event_code,
        title=title,
        occurred_at=None,
        source_released_at=released.value,
        vendor_first_available_at=None,
        retrieved_at=ingested_at,
        time_quality=quality,
        document_url=document_url,
        document_sha256=vendor_row_optional_sha256(row, "document_sha256", "iFinD"),
        raw_response_sha256=vendor_row_raw_response_sha256(row, "iFinD"),
        validation_status=validation_status,
        attributes=vendor_row_raw_attributes(row, "iFinD"),
        revision_no=vendor_row_revision_no(row, "iFinD"),
    )


class NormalizedVendorTimestamp:
    __slots__ = ("second_precision", "time_quality", "value")

    def __init__(
        self,
        value: datetime,
        time_quality: TimeQuality,
        *,
        second_precision: bool,
    ) -> None:
        self.value = value
        self.time_quality = time_quality
        self.second_precision = second_precision


def vendor_row_observation_quality(
    *timestamps: NormalizedVendorTimestamp,
) -> tuple[TimeQuality, str]:
    if any(item.time_quality is TimeQuality.DATE_ONLY_CONSERVATIVE for item in timestamps):
        return TimeQuality.DATE_ONLY_CONSERVATIVE, BLOCKED_TIME_QUALITY
    if not all(item.second_precision for item in timestamps):
        return TimeQuality.VENDOR_OBSERVED, BLOCKED_TIME_QUALITY
    return TimeQuality.EXACT, VALIDATED


def vendor_row_required_timestamp(
    row: Mapping[str, object],
    provider: str,
    names: Sequence[str],
) -> NormalizedVendorTimestamp:
    name, value = _required_field(row, provider, names)
    return _normalize_vendor_timestamp(value, f"{provider}.{name}")


def _normalize_vendor_timestamp(value: object, field_name: str) -> NormalizedVendorTimestamp:
    date_only = False
    second_precision = True

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
        date_only = True
        second_precision = False
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            raise VendorEventRowError(f"{field_name} must not be empty")
        if _DATE_ONLY.fullmatch(raw):
            parsed = datetime.combine(date.fromisoformat(raw.replace("/", "-")), time.min)
            date_only = True
            second_precision = False
        elif _COMPACT_DATE.fullmatch(raw):
            parsed = datetime.strptime(raw, "%Y%m%d")
            date_only = True
            second_precision = False
        else:
            second_precision = _SECOND_COMPONENT.search(raw) is not None
            parsed = _parse_datetime_text(raw, field_name)
    else:
        raise VendorEventRowError(
            f"{field_name} must be a date, datetime, or timestamp string; "
            f"got {type(value).__name__}"
        )

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        # iFinD, RQData and Tushare return China-market wall-clock values.  The
        # provider contract, rather than a machine-local timezone, supplies the
        # offset for naive SDK values.
        local = parsed.replace(tzinfo=SHANGHAI)
    else:
        local = parsed.astimezone(SHANGHAI)

    placeholder = (
        local.minute == 0 and local.second == 0 and local.microsecond == 0 and local.hour in {0, 12}
    )
    if date_only or placeholder:
        conservative = datetime.combine(local.date(), time(hour=15), tzinfo=SHANGHAI)
        return NormalizedVendorTimestamp(
            conservative,
            TimeQuality.DATE_ONLY_CONSERVATIVE,
            second_precision=False,
        )

    # Canonical event clocks are second-granular.  The unmodified raw value is
    # retained in ``attributes`` and covered by ``raw_response_sha256``.
    normalized = (
        local if local.microsecond == 0 else (local + timedelta(seconds=1)).replace(microsecond=0)
    )
    quality = TimeQuality.EXACT if second_precision else TimeQuality.VENDOR_OBSERVED
    return NormalizedVendorTimestamp(
        normalized,
        quality,
        second_precision=second_precision,
    )


def _parse_datetime_text(raw: str, field_name: str) -> datetime:
    normalized = raw.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        pass
    for pattern in (
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y%m%d%H%M%S",
    ):
        try:
            return datetime.strptime(raw, pattern)
        except ValueError:
            continue
    raise VendorEventRowError(f"{field_name} is not a supported timestamp: {raw!r}")


def vendor_row_required_retrieved_at(
    row: Mapping[str, object],
    supplied: datetime | None,
    provider: str,
) -> datetime:
    value: object
    if supplied is not None:
        value = supplied
    else:
        _, value = _required_field(row, provider, ("retrieved_at", "ingested_at"))
    if not isinstance(value, datetime):
        raise VendorEventRowError(f"{provider}.retrieved_at must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise VendorEventRowError(f"{provider}.retrieved_at must include an explicit timezone")
    return value.astimezone(SHANGHAI).replace(microsecond=0)


def vendor_row_required_event_code(
    row: Mapping[str, object],
    supplied: str | None,
    provider: str,
) -> str:
    value: object
    if supplied is not None:
        value = supplied
    else:
        _, value = _required_field(row, provider, ("event_code",))
    if not isinstance(value, str) or not value.strip():
        raise VendorEventRowError(
            f"{provider}.event_code must be supplied explicitly as a non-empty string"
        )
    return value.strip()


def vendor_row_required_instrument(
    row: Mapping[str, object],
    provider: str,
    names: Sequence[str],
) -> InstrumentId:
    name, value = _required_field(row, provider, names)
    if isinstance(value, InstrumentId):
        return value
    if not isinstance(value, str) or not value.strip():
        raise VendorEventRowError(f"{provider}.{name} must be a non-empty instrument code")
    raw = value.strip().upper()
    if raw.endswith(".XSHG"):
        raw = f"{raw.removesuffix('.XSHG')}.SH"
    elif raw.endswith(".XSHE"):
        raw = f"{raw.removesuffix('.XSHE')}.SZ"
    elif raw.endswith(".XBSE"):
        raw = f"{raw.removesuffix('.XBSE')}.BJ"
    if not re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", raw):
        raise VendorEventRowError(
            f"{provider}.{name} must include an explicit SH, SZ, or BJ exchange suffix"
        )
    return InstrumentId(raw)


def vendor_row_required_identifier(
    row: Mapping[str, object],
    provider: str,
    names: Sequence[str],
    *,
    fallback_names: Sequence[str] = (),
) -> str:
    for name in (*names, *fallback_names):
        if name not in row:
            continue
        value = row[name]
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, str):
            result = value.strip()
        elif isinstance(value, Integral):
            result = str(int(value))
        else:
            continue
        if result:
            return result
    joined = ", ".join((*names, *fallback_names))
    raise VendorEventRowError(
        f"{provider} row requires a stable provider event id in one of: {joined}"
    )


def vendor_row_required_text(
    row: Mapping[str, object],
    provider: str,
    names: Sequence[str],
) -> str:
    name, value = _required_field(row, provider, names)
    if not isinstance(value, str) or not value.strip():
        raise VendorEventRowError(f"{provider}.{name} must be a non-empty string")
    return value.strip()


def vendor_row_optional_text(row: Mapping[str, object], names: Sequence[str]) -> str | None:
    for name in names:
        value = row.get(name)
        if value is None:
            continue
        if not isinstance(value, str):
            raise VendorEventRowError(f"{name} must be a string when present")
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def _required_field(
    row: Mapping[str, object],
    provider: str,
    names: Sequence[str],
) -> tuple[str, object]:
    for name in names:
        if name in row and row[name] is not None:
            return name, row[name]
    joined = ", ".join(names)
    raise VendorEventRowError(f"{provider} row is missing required field ({joined})")


def vendor_row_optional_sha256(
    row: Mapping[str, object],
    name: str,
    provider: str,
) -> str | None:
    value = row.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise VendorEventRowError(f"{provider}.{name} must be a SHA-256 string")
    match = _SHA256.fullmatch(value.strip())
    if match is None:
        raise VendorEventRowError(f"{provider}.{name} must contain 64 hexadecimal characters")
    return match.group(1).lower()


def vendor_row_raw_response_sha256(row: Mapping[str, object], provider: str) -> str:
    supplied = vendor_row_optional_sha256(row, "raw_response_sha256", provider)
    if supplied is not None:
        return supplied
    payload = json.dumps(
        _json_safe(row, provider),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def vendor_row_raw_attributes(
    row: Mapping[str, object],
    provider: str,
) -> Mapping[str, str | int | Decimal | bool | None]:
    attributes: dict[str, str | int | Decimal | bool | None] = {}
    for name, value in row.items():
        if not name:
            raise VendorEventRowError(f"{provider} row keys must be non-empty strings")
        attributes[name] = _attribute_value(value, f"{provider}.{name}")
    return attributes


def _attribute_value(
    value: object,
    field_name: str,
) -> str | int | Decimal | bool | None:
    if value is None or isinstance(value, str | bool):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise VendorEventRowError(f"{field_name} must be finite")
        return value
    if isinstance(value, Integral):
        return int(value)
    if isinstance(value, Real):
        converted = Decimal(str(value))
        if not converted.is_finite():
            raise VendorEventRowError(f"{field_name} must be finite")
        return converted
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, Mapping):
        raw_value: object = cast(Mapping[object, object], value)
        return json.dumps(
            _json_safe(raw_value, field_name),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    if isinstance(value, Sequence):
        raw_value = cast(Sequence[object], value)
        return json.dumps(
            _json_safe(raw_value, field_name),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    raise VendorEventRowError(f"{field_name} has unsupported raw value type {type(value).__name__}")


def _json_safe(value: object, field_name: str) -> object:
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise VendorEventRowError(f"{field_name} must be finite")
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise VendorEventRowError(f"{field_name} must be finite")
        return value
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, Mapping):
        raw_mapping = cast(Mapping[object, object], value)
        result: dict[str, object] = {}
        for name, item in raw_mapping.items():
            if not isinstance(name, str) or not name:
                raise VendorEventRowError(f"{field_name} keys must be non-empty strings")
            result[name] = _json_safe(item, f"{field_name}.{name}")
        return result
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        raw_sequence = cast(Sequence[object], value)
        return [_json_safe(item, field_name) for item in raw_sequence]
    raise VendorEventRowError(f"{field_name} has unsupported raw value type {type(value).__name__}")


def vendor_row_revision_no(row: Mapping[str, object], provider: str) -> int:
    value = row.get("revision_no", 0)
    if isinstance(value, bool) or not isinstance(value, Integral) or int(value) < 0:
        raise VendorEventRowError(f"{provider}.revision_no must be a non-negative integer")
    return int(value)


__all__ = [
    "BLOCKED_TIME_QUALITY",
    "SHANGHAI",
    "VALIDATED",
    "IFindEventRowError",
    "IFindEventSourceAdapter",
    "VendorEventRowError",
    "VendorRowsCallable",
    "normalize_ifind_row",
]
