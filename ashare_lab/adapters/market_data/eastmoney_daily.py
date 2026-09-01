"""Acquire Eastmoney Push2His daily OHLCV for offline A-share research.

The public ``stock/kline/get`` request shape is adapted from AKShare
``stock_zh_a_hist`` at commit
``8e95744b79ae22326308ccd2b4e62650c5b53c55`` (MIT).  This adapter deliberately
stops at a validated acquisition result: it is not a live market-data port and
it does not silently promote public data into a strict backtest snapshot.

Eastmoney field ``f56`` is expressed in lots (``手``), not individual shares.
For Shanghai and Shenzhen shares one lot is 100 shares.  The normalized share
count therefore has a conservative 100-share resolution; callers must not
claim odd-share precision from this source.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time as time_module
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import cast
from zoneinfo import ZoneInfo

import httpx

from ashare_lab.adapters.host_http import HostThrottle, HostThrottledHttpClient
from ashare_lab.domain.market_data import PriceBasis, normalize_a_share_instrument
from ashare_lab.domain.shared import InstrumentId

EASTMONEY_DAILY_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
AKSHARE_SOURCE_REPOSITORY = "https://github.com/akfamily/akshare"
AKSHARE_SOURCE_COMMIT = "8e95744b79ae22326308ccd2b4e62650c5b53c55"
AKSHARE_SOURCE_PATH = "akshare/stock_feature/stock_hist_em.py:stock_zh_a_hist"
AKSHARE_SOURCE_LICENSE = "MIT"
AKSHARE_REQUEST_TOKEN = "7eea3edcaed734bea9cbfc24409ed989"
PROVIDER = "eastmoney_push2his_public"
DATASET = "stock_daily_kline"
TURNOVER_RATE_METHODOLOGY = "eastmoney_push2his.f61.provider_reported_turnover_rate_pct.v1"
FIELDS1 = ("f1", "f2", "f3", "f4", "f5", "f6")
DAILY_FIELD_CODES = tuple(f"f{number}" for number in range(51, 62))
DAILY_REQUEST_FIELD_CODES = (*DAILY_FIELD_CODES, "f116")
SHANGHAI = ZoneInfo("Asia/Shanghai")
CURRENT_SESSION_STABILITY_MARKER = time(16, 5)
VOLUME_SOURCE_UNIT = "lot"
VOLUME_LOT_SIZE_SHARES = 100
VOLUME_RESOLUTION_SHARES = 100
_DEFAULT_TIMEOUT_SECONDS = 10.0
_DEFAULT_MAX_ATTEMPTS = 3
EASTMONEY_MIN_REQUEST_INTERVAL_SECONDS = 1.0
_RETRY_BACKOFF_SECONDS = (0.25, 0.75)
_RETRYABLE_HTTP_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
_FQT_BY_PRICE_BASIS = {
    PriceBasis.UNADJUSTED: "0",
    PriceBasis.BACK_ADJUSTED: "2",
}
_NON_NEGATIVE_INTEGER_RE = re.compile(r"^[0-9]+$")
_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://quote.eastmoney.com/",
    "User-Agent": "Mozilla/5.0 (compatible; AShareLab/1.0; daily-price-research)",
}

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


class EastmoneyDailySourceError(RuntimeError):
    """The public response cannot be safely normalized for replay research."""


@dataclass(frozen=True, slots=True)
class EastmoneyDailyRow:
    """One validated provider row with its original wire representation."""

    date: date
    open: Decimal
    close: Decimal
    high: Decimal
    low: Decimal
    volume_lots: int
    volume_shares: int
    amount: Decimal
    amplitude_pct: Decimal
    change_pct: Decimal
    change_cny: Decimal
    turnover_rate_pct: Decimal
    source_row: str

    def as_snapshot_row(self, instrument_id: InstrumentId) -> dict[str, object]:
        """Return the common daily-snapshot shape without inventing precision."""

        return {
            "stock_code": str(instrument_id),
            "date": self.date.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume_shares,
            "amount": self.amount,
            "turnover_rate_pct": self.turnover_rate_pct,
            "turnover_rate_provider": PROVIDER,
            "turnover_rate_methodology": TURNOVER_RATE_METHODOLOGY,
        }


@dataclass(frozen=True, slots=True)
class EastmoneyDailyCollection:
    """A validated public response plus exact acquisition provenance."""

    instrument_id: InstrumentId
    secid: str
    price_basis: PriceBasis
    provider_name: str
    dataset_name: str
    request_started_at: datetime
    response_received_at: datetime
    request_url: str
    request_params: Mapping[str, str]
    raw_wire_sha256: str
    raw_payload_canonical_sha256: str
    raw_payload: JsonObject
    rows: tuple[EastmoneyDailyRow, ...]
    requested_start: date
    requested_end: date
    attempt_count: int
    transient_errors: tuple[str, ...]
    source_volume_unit: str = VOLUME_SOURCE_UNIT
    volume_lot_size_shares: int = VOLUME_LOT_SIZE_SHARES
    volume_resolution_shares: int = VOLUME_RESOLUTION_SHARES

    @property
    def retrieved_at(self) -> datetime:
        """Adapter-controlled acquisition time; never a historical publication time."""

        return self.response_received_at


class EastmoneyDailyResearchSource:
    """Fetch one unadjusted or back-adjusted SH/SZ daily series."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float | httpx.Timeout = _DEFAULT_TIMEOUT_SECONDS,
        clock: Callable[[], datetime] | None = None,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        sleeper: Callable[[float], None] = time_module.sleep,
        request_throttle: HostThrottle | None = None,
    ) -> None:
        if client is not None and transport is not None:
            raise ValueError("inject client or transport, not both")
        self._timeout = timeout if isinstance(timeout, httpx.Timeout) else httpx.Timeout(timeout)
        if type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        self._max_attempts = max_attempts
        self._sleeper = sleeper
        self._clock: Callable[[], object] = clock or _utc_now
        self._http = HostThrottledHttpClient(
            client=client,
            transport=transport,
            timeout=self._timeout,
            throttle=request_throttle,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> EastmoneyDailyResearchSource:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def fetch(
        self,
        *,
        instrument_id: InstrumentId,
        start: date,
        end: date,
        price_basis: PriceBasis,
    ) -> EastmoneyDailyCollection:
        """Fetch and validate a non-empty, range-bounded daily series."""

        _validate_date_range(start, end)
        if type(price_basis) is not PriceBasis or price_basis not in _FQT_BY_PRICE_BASIS:
            raise ValueError("price_basis must be UNADJUSTED or BACK_ADJUSTED")
        canonical_instrument, secid, expected_market, stock_code = _identity(instrument_id)
        params = {
            "beg": start.strftime("%Y%m%d"),
            "end": end.strftime("%Y%m%d"),
            "fields1": ",".join(FIELDS1),
            "fields2": ",".join(DAILY_REQUEST_FIELD_CODES),
            "fqt": _FQT_BY_PRICE_BASIS[price_basis],
            "klt": "101",
            "secid": secid,
            "ut": AKSHARE_REQUEST_TOKEN,
        }
        transient_errors: list[str] = []
        for attempt in range(1, self._max_attempts + 1):
            request_started_at = _read_clock(self._clock, "request_started_at")
            try:
                response = self._http.get(
                    EASTMONEY_DAILY_URL,
                    min_interval=EASTMONEY_MIN_REQUEST_INTERVAL_SECONDS,
                    params=params,
                    headers=_HEADERS,
                    timeout=self._timeout,
                )
                response_received_at = _read_clock(self._clock, "response_received_at")
                if response_received_at < request_started_at:
                    raise EastmoneyDailySourceError(
                        "response_received_at precedes request_started_at"
                    )
                response.raise_for_status()
                raw_bytes = response.content
                payload = _decode_json(raw_bytes)
                rows = _parse_response(
                    payload,
                    expected_code=stock_code,
                    expected_market=expected_market,
                    validate_turnover_units=price_basis is PriceBasis.UNADJUSTED,
                    response_received_at=response_received_at,
                    requested_start=start,
                    requested_end=end,
                )
            except httpx.HTTPError as exc:
                if attempt < self._max_attempts and _retryable_http_error(exc):
                    transient_errors.append(_error_summary(exc))
                    self._sleep_before_retry(attempt)
                    continue
                raise EastmoneyDailySourceError(
                    f"Eastmoney daily request failed after {attempt} attempt(s): {exc}"
                ) from exc
            except EastmoneyDailySourceError as exc:
                if attempt < self._max_attempts and _retryable_payload_error(exc):
                    transient_errors.append(_error_summary(exc))
                    self._sleep_before_retry(attempt)
                    continue
                raise
            return EastmoneyDailyCollection(
                instrument_id=canonical_instrument,
                secid=secid,
                price_basis=price_basis,
                provider_name=PROVIDER,
                dataset_name=DATASET,
                request_started_at=request_started_at,
                response_received_at=response_received_at,
                request_url=EASTMONEY_DAILY_URL,
                request_params=params,
                raw_wire_sha256=hashlib.sha256(raw_bytes).hexdigest(),
                raw_payload_canonical_sha256=hashlib.sha256(
                    _canonical_json_bytes(payload)
                ).hexdigest(),
                raw_payload=payload,
                rows=rows,
                requested_start=start,
                requested_end=end,
                attempt_count=attempt,
                transient_errors=tuple(transient_errors),
            )
        raise AssertionError("unreachable Eastmoney retry state")

    def _sleep_before_retry(self, attempt: int) -> None:
        backoff_index = min(attempt - 1, len(_RETRY_BACKOFF_SECONDS) - 1)
        self._sleeper(_RETRY_BACKOFF_SECONDS[backoff_index])


def _parse_response(
    payload: JsonObject,
    *,
    expected_code: str,
    expected_market: int,
    validate_turnover_units: bool,
    response_received_at: datetime,
    requested_start: date,
    requested_end: date,
) -> tuple[EastmoneyDailyRow, ...]:
    rc = payload.get("rc")
    if type(rc) is not int or rc != 0:
        raise EastmoneyDailySourceError(f"Eastmoney response rc is not zero: {rc!r}")
    data = _required_object(payload.get("data"), "data")
    if data.get("code") != expected_code:
        raise EastmoneyDailySourceError(
            "response stock code does not match the requested instrument"
        )
    market = data.get("market")
    if type(market) is not int or market != expected_market:
        raise EastmoneyDailySourceError("response market does not match the requested instrument")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise EastmoneyDailySourceError("data.name must be non-empty text")
    raw_klines = data.get("klines")
    if not isinstance(raw_klines, list) or not raw_klines:
        raise EastmoneyDailySourceError("data.klines must be a non-empty array")

    parsed: list[EastmoneyDailyRow] = []
    for index, value in enumerate(raw_klines):
        if not isinstance(value, str):
            raise EastmoneyDailySourceError(f"data.klines[{index}] must be text")
        row = _parse_kline(
            value,
            index=index,
            validate_turnover_units=validate_turnover_units,
        )
        if not requested_start <= row.date <= requested_end:
            raise EastmoneyDailySourceError(
                f"data.klines[{index}] date is outside the requested range"
            )
        parsed.append(row)

    dates = [row.date for row in parsed]
    if len(set(dates)) != len(dates):
        raise EastmoneyDailySourceError("data.klines contains duplicate trade dates")
    ordered = tuple(sorted(parsed, key=lambda row: row.date))
    received_local = response_received_at.astimezone(SHANGHAI)
    latest_trade_date = ordered[-1].date
    if latest_trade_date > received_local.date():
        raise EastmoneyDailySourceError("latest trade date is later than response_received_at")
    if (
        latest_trade_date == received_local.date()
        and received_local.time() < CURRENT_SESSION_STABILITY_MARKER
    ):
        raise EastmoneyDailySourceError(
            "current-session row arrived before the 16:05 Asia/Shanghai stability marker"
        )
    return ordered


def _parse_kline(
    value: str,
    *,
    index: int,
    validate_turnover_units: bool,
) -> EastmoneyDailyRow:
    fields = value.split(",")
    if len(fields) != len(DAILY_FIELD_CODES):
        raise EastmoneyDailySourceError(
            f"data.klines[{index}] has {len(fields)} fields; expected "
            f"{len(DAILY_FIELD_CODES)} for f51-f61"
        )
    try:
        trade_date = date.fromisoformat(fields[0])
    except ValueError as exc:
        raise EastmoneyDailySourceError(f"data.klines[{index}] f51 is not an ISO date") from exc

    open_cny = _decimal(fields[1], field_code="f52", index=index)
    close_cny = _decimal(fields[2], field_code="f53", index=index)
    high_cny = _decimal(fields[3], field_code="f54", index=index)
    low_cny = _decimal(fields[4], field_code="f55", index=index)
    volume_lots = _non_negative_integer(fields[5], field_code="f56", index=index)
    turnover_cny = _decimal(fields[6], field_code="f57", index=index)
    amplitude_pct = _decimal(fields[7], field_code="f58", index=index)
    change_pct = _decimal(fields[8], field_code="f59", index=index)
    change_cny = _decimal(fields[9], field_code="f60", index=index)
    turnover_rate_pct = _decimal(fields[10], field_code="f61", index=index)

    prices = (open_cny, close_cny, high_cny, low_cny)
    if any(price <= 0 for price in prices):
        raise EastmoneyDailySourceError(
            f"data.klines[{index}] f52-f55 OHLC prices must be positive"
        )
    if high_cny < max(open_cny, close_cny, low_cny) or low_cny > min(open_cny, close_cny, high_cny):
        raise EastmoneyDailySourceError(
            f"data.klines[{index}] f52-f55 OHLC values are inconsistent"
        )
    if turnover_cny < 0:
        raise EastmoneyDailySourceError(f"data.klines[{index}] f57 turnover must be non-negative")
    volume_shares = volume_lots * VOLUME_LOT_SIZE_SHARES
    if volume_shares == 0:
        if turnover_cny != 0:
            raise EastmoneyDailySourceError(
                f"data.klines[{index}] f56/f57 zero volume must have zero turnover"
            )
    elif validate_turnover_units and not (
        low_cny * volume_shares <= turnover_cny <= high_cny * volume_shares
    ):
        raise EastmoneyDailySourceError(
            f"data.klines[{index}] f56/f57 volume and turnover units are inconsistent"
        )
    if amplitude_pct < 0:
        raise EastmoneyDailySourceError(f"data.klines[{index}] f58 amplitude must be non-negative")
    if turnover_rate_pct < 0:
        raise EastmoneyDailySourceError(
            f"data.klines[{index}] f61 turnover rate must be non-negative"
        )

    return EastmoneyDailyRow(
        date=trade_date,
        open=open_cny,
        close=close_cny,
        high=high_cny,
        low=low_cny,
        volume_lots=volume_lots,
        volume_shares=volume_shares,
        amount=turnover_cny,
        amplitude_pct=amplitude_pct,
        change_pct=change_pct,
        change_cny=change_cny,
        turnover_rate_pct=turnover_rate_pct,
        source_row=value,
    )


def _identity(instrument_id: InstrumentId) -> tuple[InstrumentId, str, int, str]:
    try:
        canonical = normalize_a_share_instrument(instrument_id)
    except ValueError as exc:
        raise ValueError("instrument_id must be a canonical mainland A-share identifier") from exc
    if str(canonical) != str(instrument_id):
        raise ValueError(f"instrument_id must be canonical: {canonical}")
    code, exchange = str(canonical).split(".", maxsplit=1)
    if exchange not in {"SH", "SZ"}:
        raise ValueError("Eastmoney daily fallback supports SH and SZ A-shares only")
    market = 1 if exchange == "SH" else 0
    return canonical, f"{market}.{code}", market, code


def _retryable_http_error(error: httpx.HTTPError) -> bool:
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in _RETRYABLE_HTTP_STATUS_CODES
    return isinstance(error, (httpx.TimeoutException, httpx.TransportError))


def _retryable_payload_error(error: EastmoneyDailySourceError) -> bool:
    return str(error) == "data.klines must be a non-empty array"


def _error_summary(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"


def _validate_date_range(start: date, end: date) -> None:
    if type(start) is not date or type(end) is not date:
        raise TypeError("start and end must be date values")
    if start > end:
        raise ValueError("start must not be later than end")


def _decimal(value: str, *, field_code: str, index: int) -> Decimal:
    if not value or value == "-":
        raise EastmoneyDailySourceError(f"data.klines[{index}] {field_code} is missing")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise EastmoneyDailySourceError(
            f"data.klines[{index}] {field_code} is not numeric"
        ) from exc
    if not parsed.is_finite():
        raise EastmoneyDailySourceError(f"data.klines[{index}] {field_code} must be finite")
    return parsed


def _non_negative_integer(value: str, *, field_code: str, index: int) -> int:
    if not _NON_NEGATIVE_INTEGER_RE.fullmatch(value):
        raise EastmoneyDailySourceError(
            f"data.klines[{index}] {field_code} must be non-negative whole lots"
        )
    return int(value)


def _decode_json(raw_bytes: bytes) -> JsonObject:
    try:
        decoded: object = json.loads(raw_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise EastmoneyDailySourceError("Eastmoney response is not valid JSON") from exc
    normalized = _json_value(decoded, field_name="response")
    if not isinstance(normalized, dict):
        raise EastmoneyDailySourceError("Eastmoney response must be a JSON object")
    return normalized


def _canonical_json_bytes(value: JsonValue) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _json_value(value: object, *, field_name: str) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise EastmoneyDailySourceError(f"{field_name} contains a non-finite number")
        return value
    if isinstance(value, list):
        raw_list = cast(list[object], value)
        return [
            _json_value(item, field_name=f"{field_name}[{index}]")
            for index, item in enumerate(raw_list)
        ]
    if isinstance(value, Mapping):
        raw = cast(Mapping[object, object], value)
        if any(not isinstance(key, str) for key in raw):
            raise EastmoneyDailySourceError(f"{field_name} contains a non-text object key")
        return {
            cast(str, key): _json_value(item, field_name=f"{field_name}.{key}")
            for key, item in raw.items()
        }
    raise EastmoneyDailySourceError(f"{field_name} contains unsupported JSON data")


def _required_object(value: JsonValue | None, field_name: str) -> JsonObject:
    if not isinstance(value, dict):
        raise EastmoneyDailySourceError(f"{field_name} must be an object")
    return value


def _read_clock(clock: Callable[[], object], field_name: str) -> datetime:
    value = clock()
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} clock value must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include an explicit timezone")
    return value


def _utc_now() -> datetime:
    return datetime.now(UTC)
