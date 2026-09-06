"""Strict Tencent HTTPS daily acquisition for mainland A-share stocks.

The request shape is a thin, dependency-free adaptation of AKShare
``stock_zh_a_hist_tx`` at the immutable MIT-licensed commit recorded below.
This module only acquires and validates provider rows.  It does not resolve a
security identity, publish a snapshot, or silently fall back to another source.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time as time_module
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import cast
from zoneinfo import ZoneInfo

import httpx

from ashare_lab.domain.instruments import AssetType, InstrumentRef
from ashare_lab.domain.market_data import normalize_a_share_instrument
from ashare_lab.domain.shared import InstrumentId

TENCENT_KLINE_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
AKSHARE_SOURCE_REPOSITORY = "https://github.com/akfamily/akshare"
AKSHARE_SOURCE_COMMIT = "8e95744b79ae22326308ccd2b4e62650c5b53c55"
AKSHARE_SOURCE_PATH = "akshare/stock_feature/stock_hist_tx.py:stock_zh_a_hist_tx"
AKSHARE_SOURCE_LICENSE = "MIT"
PROVIDER = "tencent_finance_public"
DATASET = "mainland_stock_daily_kline"
TURNOVER_RATE_METHODOLOGY = (
    "tencent.newfqkline.row7.provider_reported_turnover_rate_pct.v1"
)
AMOUNT_SOURCE_UNIT = "ten_thousand_cny"
AMOUNT_MULTIPLIER_CNY = Decimal("10000")
LOT_SIZE_SHARES = 100
CURRENT_SESSION_STABILITY_MARKER = time(16, 5)
SHANGHAI = ZoneInfo("Asia/Shanghai")
_DEFAULT_TIMEOUT_SECONDS = 15.0
_DEFAULT_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = (0.25, 0.75)
_RETRYABLE_HTTP_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://gu.qq.com/",
    "User-Agent": "Mozilla/5.0 (compatible; AShareLab/1.0; stock-daily-research)",
}

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


class TencentAdjustment(StrEnum):
    """Tencent provider adjustment values without overloading the domain enum."""

    UNADJUSTED = ""
    FRONT_ADJUSTED = "qfq"
    BACK_ADJUSTED = "hfq"

    @property
    def response_key(self) -> str:
        return {
            TencentAdjustment.UNADJUSTED: "day",
            TencentAdjustment.FRONT_ADJUSTED: "qfqday",
            TencentAdjustment.BACK_ADJUSTED: "hfqday",
        }[self]


class TencentStockDailySourceError(RuntimeError):
    """Base error for Tencent stock daily acquisition."""


class TencentStockDailyProviderUnavailableError(TencentStockDailySourceError):
    """Transport or HTTP failure; another real provider may be attempted."""


class TencentStockDailyIntegrityError(TencentStockDailySourceError):
    """Provider bytes conflict with the requested identity or data contract."""


@dataclass(frozen=True, slots=True)
class TencentStockDailyRow:
    session_date: date
    open: Decimal
    close: Decimal
    high: Decimal
    low: Decimal
    provider_volume: Decimal
    volume_source_unit: str
    volume_shares: int
    provider_amount: Decimal
    amount_cny: Decimal
    turnover_rate_pct: Decimal

    def as_snapshot_row(self, instrument_id: InstrumentId) -> dict[str, object]:
        return {
            "stock_code": str(instrument_id),
            "date": self.session_date.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume_shares,
            "amount": self.amount_cny,
            "turnover_rate_pct": self.turnover_rate_pct,
            "turnover_rate_provider": PROVIDER,
            "turnover_rate_methodology": TURNOVER_RATE_METHODOLOGY,
        }

    def identity(self) -> dict[str, object]:
        return {
            "date": self.session_date.isoformat(),
            "open": str(self.open),
            "close": str(self.close),
            "high": str(self.high),
            "low": str(self.low),
            "providerVolume": str(self.provider_volume),
            "volumeSourceUnit": self.volume_source_unit,
            "volumeShares": self.volume_shares,
            "providerAmount": str(self.provider_amount),
            "amountCny": str(self.amount_cny),
            "turnoverRatePct": str(self.turnover_rate_pct),
        }


@dataclass(frozen=True, slots=True)
class TencentStockDailyRequestAudit:
    url: str
    params: Mapping[str, str]
    requested_at: datetime
    received_at: datetime
    http_status: int
    raw_wire_sha256: str
    canonical_payload_sha256: str
    canonical_rows_sha256: str
    row_count: int
    returned_start: date | None
    returned_end: date | None
    attempt_count: int
    transient_errors: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "url": self.url,
            "params": dict(sorted(self.params.items())),
            "requestedAt": self.requested_at.isoformat(),
            "receivedAt": self.received_at.isoformat(),
            "httpStatus": self.http_status,
            "rawWireSha256": self.raw_wire_sha256,
            "canonicalPayloadSha256": self.canonical_payload_sha256,
            "canonicalRowsSha256": self.canonical_rows_sha256,
            "rowCount": self.row_count,
            "returnedStart": self.returned_start.isoformat() if self.returned_start else None,
            "returnedEnd": self.returned_end.isoformat() if self.returned_end else None,
            "attemptCount": self.attempt_count,
            "transientErrors": list(self.transient_errors),
        }


@dataclass(frozen=True, slots=True)
class TencentStockDailyCollection:
    instrument_id: InstrumentId
    instrument_name: str
    tencent_symbol: str
    adjustment: TencentAdjustment
    provider_name: str
    dataset_name: str
    requested_start: date
    requested_end: date
    rows: tuple[TencentStockDailyRow, ...]
    request_audits: tuple[TencentStockDailyRequestAudit, ...]
    raw_payloads: tuple[JsonObject, ...]

    @property
    def raw_wire_sha256s(self) -> tuple[str, ...]:
        return tuple(item.raw_wire_sha256 for item in self.request_audits)


class TencentStockDailySource:
    """Acquire one raw/qfq/hfq daily series for a trusted A-share STOCK."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float | httpx.Timeout = _DEFAULT_TIMEOUT_SECONDS,
        clock: Callable[[], datetime] | None = None,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        sleeper: Callable[[float], None] = time_module.sleep,
    ) -> None:
        if client is not None and transport is not None:
            raise ValueError("inject client or transport, not both")
        if type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        self._timeout = timeout if isinstance(timeout, httpx.Timeout) else httpx.Timeout(timeout)
        self._owns_client = client is None
        self._client = client or httpx.Client(
            transport=transport,
            timeout=self._timeout,
            follow_redirects=False,
            headers=_HEADERS,
        )
        self._clock = clock or (lambda: datetime.now(UTC))
        self._max_attempts = max_attempts
        self._sleeper = sleeper

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> TencentStockDailySource:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def fetch(
        self,
        *,
        instrument: InstrumentRef,
        start: date,
        end: date,
        adjustment: TencentAdjustment,
    ) -> TencentStockDailyCollection:
        instrument_id, tencent_symbol = _validate_request(
            instrument=instrument,
            start=start,
            end=end,
            adjustment=adjustment,
        )
        rows_by_date: dict[date, TencentStockDailyRow] = {}
        audits: list[TencentStockDailyRequestAudit] = []
        payloads: list[JsonObject] = []
        for year in range(start.year, end.year + 1):
            variable = f"kline_day{adjustment.value}{year}"
            params = {
                "_var": variable,
                "param": (
                    f"{tencent_symbol},day,{year}-01-01,{year}-12-31,640,"
                    f"{adjustment.value}"
                ),
                "r": "0.8205512681390605",
            }
            payload, parsed, audit = self._request(
                params=params,
                variable=variable,
                tencent_symbol=tencent_symbol,
                expected_name=instrument.name,
                adjustment=adjustment,
            )
            payloads.append(payload)
            audits.append(audit)
            for row in parsed:
                existing = rows_by_date.get(row.session_date)
                if existing is not None and existing != row:
                    raise TencentStockDailyIntegrityError(
                        "Tencent overlapping annual responses disagree"
                    )
                rows_by_date[row.session_date] = row

        selected = tuple(
            row for day, row in sorted(rows_by_date.items()) if start <= day <= end
        )
        if not selected:
            raise TencentStockDailyIntegrityError(
                "Tencent response contains no rows in the requested range"
            )
        _validate_latest_row(selected, received_at=audits[-1].received_at)
        return TencentStockDailyCollection(
            instrument_id=instrument_id,
            instrument_name=instrument.name,
            tencent_symbol=tencent_symbol,
            adjustment=adjustment,
            provider_name=PROVIDER,
            dataset_name=DATASET,
            requested_start=start,
            requested_end=end,
            rows=selected,
            request_audits=tuple(audits),
            raw_payloads=tuple(payloads),
        )

    def _request(
        self,
        *,
        params: Mapping[str, str],
        variable: str,
        tencent_symbol: str,
        expected_name: str,
        adjustment: TencentAdjustment,
    ) -> tuple[
        JsonObject,
        tuple[TencentStockDailyRow, ...],
        TencentStockDailyRequestAudit,
    ]:
        transient_errors: list[str] = []
        for attempt in range(1, self._max_attempts + 1):
            requested_at = _read_clock(self._clock, "requested_at")
            try:
                response = self._client.get(
                    TENCENT_KLINE_URL,
                    params=params,
                    headers=_HEADERS,
                    timeout=self._timeout,
                )
                received_at = _read_clock(self._clock, "received_at")
                if received_at < requested_at:
                    raise TencentStockDailyIntegrityError(
                        "received_at precedes requested_at"
                    )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                if attempt < self._max_attempts and _retryable_http_error(exc):
                    transient_errors.append(_error_summary(exc))
                    self._sleep_before_retry(attempt)
                    continue
                status = (
                    f"HTTP {exc.response.status_code}"
                    if isinstance(exc, httpx.HTTPStatusError)
                    else type(exc).__name__
                )
                raise TencentStockDailyProviderUnavailableError(
                    f"Tencent daily request failed after {attempt} attempt(s): {status}"
                ) from exc

            raw = response.content
            payload, parsed = _parse_response(
                raw,
                expected_variable=variable,
                expected_symbol=tencent_symbol,
                expected_name=expected_name,
                adjustment=adjustment,
            )
            audit = TencentStockDailyRequestAudit(
                url=str(response.request.url.copy_with(query=None)),
                params=dict(params),
                requested_at=requested_at,
                received_at=received_at,
                http_status=response.status_code,
                raw_wire_sha256=hashlib.sha256(raw).hexdigest(),
                canonical_payload_sha256=_canonical_sha256(payload),
                canonical_rows_sha256=_canonical_sha256(
                    [row.identity() for row in parsed]
                ),
                row_count=len(parsed),
                returned_start=parsed[0].session_date if parsed else None,
                returned_end=parsed[-1].session_date if parsed else None,
                attempt_count=attempt,
                transient_errors=tuple(transient_errors),
            )
            return payload, parsed, audit
        raise AssertionError("unreachable Tencent retry state")

    def _sleep_before_retry(self, attempt: int) -> None:
        index = min(attempt - 1, len(_RETRY_BACKOFF_SECONDS) - 1)
        self._sleeper(_RETRY_BACKOFF_SECONDS[index])


def _validate_request(
    *,
    instrument: InstrumentRef,
    start: date,
    end: date,
    adjustment: TencentAdjustment,
) -> tuple[InstrumentId, str]:
    if type(start) is not date or type(end) is not date:
        raise TypeError("start and end must be date values")
    if start > end:
        raise ValueError("start must not be later than end")
    if type(adjustment) is not TencentAdjustment:
        raise ValueError("adjustment must be a TencentAdjustment")
    if instrument.asset_type is not AssetType.STOCK:
        raise ValueError("Tencent stock daily source accepts asset_type=STOCK only")
    try:
        canonical = normalize_a_share_instrument(instrument.symbol)
    except ValueError as exc:
        raise ValueError("instrument must be a canonical mainland A-share STOCK") from exc
    if str(canonical) != instrument.symbol:
        raise ValueError(f"instrument symbol must be canonical: {canonical}")
    if start < instrument.listing_date:
        raise ValueError("requested range starts before the instrument listing date")
    if instrument.delisting_date is not None and end > instrument.delisting_date:
        raise ValueError("requested range ends after the instrument delisting date")
    instrument.require_tradable_on(end)
    code, suffix = instrument.symbol.split(".", maxsplit=1)
    return canonical, f"{suffix.lower()}{code}"


def _parse_response(
    raw: bytes,
    *,
    expected_variable: str,
    expected_symbol: str,
    expected_name: str,
    adjustment: TencentAdjustment,
) -> tuple[JsonObject, tuple[TencentStockDailyRow, ...]]:
    prefix = f"{expected_variable}=".encode()
    if not raw.startswith(prefix):
        raise TencentStockDailyIntegrityError(
            "Tencent response is not the expected JSONP variable"
        )
    try:
        decoded: object = json.loads(raw[len(prefix) :])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TencentStockDailyIntegrityError(
            "Tencent response is not valid JSONP"
        ) from exc
    value = _json_value(decoded, field_name="response")
    if not isinstance(value, dict):
        raise TencentStockDailyIntegrityError("Tencent response must be an object")
    payload = value
    if payload.get("code") != 0:
        raise TencentStockDailyIntegrityError("Tencent response status is not successful")
    data = payload.get("data")
    if not isinstance(data, dict) or set(data) != {expected_symbol}:
        raise TencentStockDailyIntegrityError(
            "Tencent response identity does not match the requested stock"
        )
    item = data.get(expected_symbol)
    if not isinstance(item, dict):
        raise TencentStockDailyIntegrityError("Tencent response identity is malformed")
    qt = item.get("qt")
    if not isinstance(qt, dict):
        raise TencentStockDailyIntegrityError(
            "Tencent response identity metadata is missing"
        )
    identity = qt.get(expected_symbol)
    if not isinstance(identity, list) or len(identity) < 3:
        raise TencentStockDailyIntegrityError(
            "Tencent response identity metadata is inconsistent"
        )
    provider_name = identity[1]
    if (
        identity[2] != expected_symbol[2:]
        or not isinstance(provider_name, str)
        or _normalize_name(provider_name) != _normalize_name(expected_name)
    ):
        raise TencentStockDailyIntegrityError(
            "Tencent response code or name does not match the security master"
        )
    raw_rows = item.get(adjustment.response_key)
    if not isinstance(raw_rows, list):
        raise TencentStockDailyIntegrityError(
            f"Tencent response does not contain {adjustment.response_key}"
        )
    rows = _parse_rows(
        raw_rows,
        code=expected_symbol[2:],
        exchange=expected_symbol[:2],
        validate_amount=adjustment is TencentAdjustment.UNADJUSTED,
    )
    return payload, rows


def _parse_rows(
    values: Sequence[JsonValue],
    *,
    code: str,
    exchange: str,
    validate_amount: bool,
) -> tuple[TencentStockDailyRow, ...]:
    rows: dict[date, TencentStockDailyRow] = {}
    volume_source_unit = _volume_source_unit(code=code, exchange=exchange)
    volume_multiplier = Decimal(1 if volume_source_unit == "share" else LOT_SIZE_SHARES)
    for index, value in enumerate(values):
        if not isinstance(value, list) or len(value) < 9:
            raise TencentStockDailyIntegrityError(f"Tencent row {index} is incomplete")
        try:
            session_date = date.fromisoformat(_text(value[0], f"row {index} date"))
        except ValueError as exc:
            raise TencentStockDailyIntegrityError(
                f"Tencent row {index} date is invalid"
            ) from exc
        open_price = _decimal(value[1], f"row {index} open")
        close_price = _decimal(value[2], f"row {index} close")
        high_price = _decimal(value[3], f"row {index} high")
        low_price = _decimal(value[4], f"row {index} low")
        provider_volume = _decimal(value[5], f"row {index} volume")
        turnover_rate = _decimal(value[7], f"row {index} turnover rate")
        provider_amount = _decimal(value[8], f"row {index} amount")
        if any(price <= 0 for price in (open_price, close_price, high_price, low_price)):
            raise TencentStockDailyIntegrityError(
                f"Tencent row {index} OHLC prices must be positive"
            )
        if high_price < max(open_price, close_price, low_price) or low_price > min(
            open_price, close_price, high_price
        ):
            raise TencentStockDailyIntegrityError(
                f"Tencent row {index} OHLC values are inconsistent"
            )
        if provider_volume < 0 or provider_amount < 0 or turnover_rate < 0:
            raise TencentStockDailyIntegrityError(
                f"Tencent row {index} volume, amount and turnover must be non-negative"
            )
        normalized_volume = provider_volume * volume_multiplier
        if normalized_volume != normalized_volume.to_integral_value():
            raise TencentStockDailyIntegrityError(
                f"Tencent row {index} volume cannot be normalized to whole shares"
            )
        volume_shares = int(normalized_volume)
        amount_cny = provider_amount * AMOUNT_MULTIPLIER_CNY
        if volume_shares == 0 and amount_cny != 0:
            raise TencentStockDailyIntegrityError(
                f"Tencent row {index} zero volume must have zero amount"
            )
        if validate_amount and volume_shares > 0 and not (
            low_price * volume_shares <= amount_cny <= high_price * volume_shares
        ):
            raise TencentStockDailyIntegrityError(
                f"Tencent row {index} amount and volume units are inconsistent"
            )
        row = TencentStockDailyRow(
            session_date=session_date,
            open=open_price,
            close=close_price,
            high=high_price,
            low=low_price,
            provider_volume=provider_volume,
            volume_source_unit=volume_source_unit,
            volume_shares=volume_shares,
            provider_amount=provider_amount,
            amount_cny=amount_cny,
            turnover_rate_pct=turnover_rate,
        )
        existing = rows.get(session_date)
        if existing is not None and existing != row:
            raise TencentStockDailyIntegrityError(
                "Tencent response contains conflicting duplicate dates"
            )
        rows[session_date] = row
    return tuple(rows[day] for day in sorted(rows))


def _validate_latest_row(
    rows: Sequence[TencentStockDailyRow],
    *,
    received_at: datetime,
) -> None:
    local = received_at.astimezone(SHANGHAI)
    latest = rows[-1].session_date
    if latest > local.date():
        raise TencentStockDailyIntegrityError(
            "latest trade date is later than received_at"
        )
    if latest == local.date() and local.time() < CURRENT_SESSION_STABILITY_MARKER:
        raise TencentStockDailyIntegrityError(
            "current-session row arrived before the 16:05 Asia/Shanghai stability marker"
        )


def _volume_source_unit(*, code: str, exchange: str) -> str:
    return "share" if exchange == "sh" and code.startswith(("688", "689")) else "lot"


def _normalize_name(value: str) -> str:
    return re.sub(r"\s+", "", value).upper()


def _text(value: JsonValue, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise TencentStockDailyIntegrityError(f"Tencent {label} must be text")
    return value


def _decimal(value: JsonValue, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise TencentStockDailyIntegrityError(f"Tencent {label} must be numeric")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise TencentStockDailyIntegrityError(f"Tencent {label} is not numeric") from exc
    if not parsed.is_finite():
        raise TencentStockDailyIntegrityError(f"Tencent {label} must be finite")
    return parsed


def _json_value(value: object, *, field_name: str) -> JsonValue:
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TencentStockDailyIntegrityError(
                f"{field_name} contains a non-finite number"
            )
        return value
    if isinstance(value, list):
        return [
            _json_value(item, field_name=f"{field_name}[{index}]")
            for index, item in enumerate(cast(list[object], value))
        ]
    if isinstance(value, Mapping):
        raw = cast(Mapping[object, object], value)
        if any(not isinstance(key, str) for key in raw):
            raise TencentStockDailyIntegrityError(
                f"{field_name} contains a non-text object key"
            )
        return {
            cast(str, key): _json_value(item, field_name=f"{field_name}.{key}")
            for key, item in raw.items()
        }
    raise TencentStockDailyIntegrityError(
        f"{field_name} contains unsupported JSON data"
    )


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_clock(clock: Callable[[], object], label: str) -> datetime:
    value: object = clock()
    if not isinstance(value, datetime):
        raise TypeError(f"{label} clock value must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include an explicit timezone")
    return value


def _retryable_http_error(error: httpx.HTTPError) -> bool:
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in _RETRYABLE_HTTP_STATUS_CODES
    return isinstance(error, (httpx.TimeoutException, httpx.TransportError))


def _error_summary(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"


__all__ = [
    "AKSHARE_SOURCE_COMMIT",
    "AKSHARE_SOURCE_LICENSE",
    "AKSHARE_SOURCE_PATH",
    "AKSHARE_SOURCE_REPOSITORY",
    "DATASET",
    "PROVIDER",
    "TENCENT_KLINE_URL",
    "TURNOVER_RATE_METHODOLOGY",
    "TencentAdjustment",
    "TencentStockDailyCollection",
    "TencentStockDailyIntegrityError",
    "TencentStockDailyProviderUnavailableError",
    "TencentStockDailyRequestAudit",
    "TencentStockDailyRow",
    "TencentStockDailySource",
    "TencentStockDailySourceError",
]
