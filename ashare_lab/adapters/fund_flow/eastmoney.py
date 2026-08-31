"""Acquire Eastmoney Push2 daily fund-flow rows for offline research snapshots.

This adapter intentionally has no market-data port and is not wired into the
backtest runtime.  The public endpoint's order-size methodology and publication
clock are undocumented, so its output remains provider-defined research data.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import cast
from zoneinfo import ZoneInfo

import httpx

from ashare_lab.domain.shared import InstrumentId

EASTMONEY_FUND_FLOW_URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
PROVIDER = "eastmoney_push2his_public"
DATASET = "stock_fflow_daykline"
DAILY_FIELD_CODES = tuple(f"f{number}" for number in range(51, 64))
FIELDS1 = ("f1", "f2", "f3", "f7")
SHANGHAI = ZoneInfo("Asia/Shanghai")
CURRENT_SESSION_STABILITY_MARKER = time(16, 5)
PUBLIC_REQUEST_LIMIT_MAX_ROWS = 120
DEFAULT_LIMIT = 120
_MAX_LIMIT = PUBLIC_REQUEST_LIMIT_MAX_ROWS
_DEFAULT_TIMEOUT_SECONDS = 10.0
_MAIN_ABSOLUTE_TOLERANCE_CNY = Decimal("1")
_MAIN_RELATIVE_TOLERANCE = Decimal("0.000000001")
_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://data.eastmoney.com/",
    "User-Agent": "Mozilla/5.0 (compatible; AShareLab/1.0; fund-flow-research)",
}

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


class FundFlowSourceError(RuntimeError):
    """The public response cannot be safely normalized for replay research."""


@dataclass(frozen=True, slots=True)
class FundFlowDailyRow:
    """One provider-defined daily row, retaining its original wire representation."""

    trade_date: date
    main_net_inflow_cny: Decimal
    small_net_inflow_cny: Decimal
    medium_net_inflow_cny: Decimal
    large_net_inflow_cny: Decimal
    extra_large_net_inflow_cny: Decimal
    main_net_inflow_pct: Decimal
    small_net_inflow_pct: Decimal
    medium_net_inflow_pct: Decimal
    large_net_inflow_pct: Decimal
    extra_large_net_inflow_pct: Decimal
    close_cny: Decimal
    change_pct: Decimal
    source_row: str


@dataclass(frozen=True, slots=True)
class FundFlowCollection:
    """A validated public response plus the exact acquisition provenance."""

    instrument_id: InstrumentId
    secid: str
    provider_name: str
    dataset_name: str
    request_started_at: datetime
    response_received_at: datetime
    request_url: str
    request_params: Mapping[str, str]
    raw_wire_sha256: str
    raw_payload_canonical_sha256: str
    raw_payload: JsonObject
    rows: tuple[FundFlowDailyRow, ...]
    requested_limit: int

    @property
    def retrieved_at(self) -> datetime:
        """The adapter-controlled time at which this payload became known locally."""

        return self.response_received_at


class EastmoneyFundFlowResearchSource:
    """Fetch recent daily fund-flow data for an immutable research snapshot."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float | httpx.Timeout = _DEFAULT_TIMEOUT_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if client is not None and transport is not None:
            raise ValueError("inject client or transport, not both")
        self._timeout = timeout if isinstance(timeout, httpx.Timeout) else httpx.Timeout(timeout)
        self._clock: Callable[[], object] = clock or _utc_now
        self._owns_client = client is None
        self._client = client or httpx.Client(
            transport=transport,
            timeout=self._timeout,
            follow_redirects=False,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> EastmoneyFundFlowResearchSource:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def fetch(
        self,
        *,
        instrument_id: InstrumentId,
        limit: int = DEFAULT_LIMIT,
    ) -> FundFlowCollection:
        """Fetch, validate and return a non-empty daily collection.

        Acquisition timestamps come only from the adapter clock. They record
        this collection attempt and never substitute for an unknown historical
        source publication time.
        """

        if type(limit) is not int or not 1 <= limit <= _MAX_LIMIT:
            raise ValueError(f"limit must be between 1 and {_MAX_LIMIT}")
        secid, expected_market, stock_code = _secid(instrument_id)
        params = {
            "fields1": ",".join(FIELDS1),
            "fields2": ",".join(DAILY_FIELD_CODES),
            "klt": "101",
            "lmt": str(limit),
            "secid": secid,
        }
        request_started_at = _read_clock(self._clock, "request_started_at")
        try:
            response = self._client.get(
                EASTMONEY_FUND_FLOW_URL,
                params=params,
                headers=_HEADERS,
                timeout=self._timeout,
            )
            response_received_at = _read_clock(self._clock, "response_received_at")
            if response_received_at < request_started_at:
                raise FundFlowSourceError("response_received_at precedes request_started_at")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise FundFlowSourceError(f"Eastmoney fund-flow request failed: {exc}") from exc

        raw_bytes = response.content
        payload = _decode_json(raw_bytes)
        rows = _parse_response(
            payload,
            expected_code=stock_code,
            expected_market=expected_market,
            response_received_at=response_received_at,
            requested_limit=limit,
        )
        return FundFlowCollection(
            instrument_id=instrument_id,
            secid=secid,
            provider_name=PROVIDER,
            dataset_name=DATASET,
            request_started_at=request_started_at,
            response_received_at=response_received_at,
            request_url=EASTMONEY_FUND_FLOW_URL,
            request_params=params,
            raw_wire_sha256=hashlib.sha256(raw_bytes).hexdigest(),
            raw_payload_canonical_sha256=hashlib.sha256(_canonical_json_bytes(payload)).hexdigest(),
            raw_payload=payload,
            rows=rows,
            requested_limit=limit,
        )


def _parse_response(
    payload: JsonObject,
    *,
    expected_code: str,
    expected_market: int,
    response_received_at: datetime,
    requested_limit: int,
) -> tuple[FundFlowDailyRow, ...]:
    if payload.get("rc") != 0:
        raise FundFlowSourceError(f"Eastmoney response rc is not zero: {payload.get('rc')!r}")
    data = _required_object(payload.get("data"), "data")
    if data.get("code") != expected_code:
        raise FundFlowSourceError("response stock code does not match the requested instrument")
    if data.get("market") != expected_market:
        raise FundFlowSourceError("response market does not match the requested instrument")
    if not isinstance(data.get("name"), str) or not cast(str, data["name"]).strip():
        raise FundFlowSourceError("data.name must be non-empty text")
    raw_klines = data.get("klines")
    if not isinstance(raw_klines, list) or not raw_klines:
        raise FundFlowSourceError("data.klines must be a non-empty array")
    if len(raw_klines) > requested_limit:
        raise FundFlowSourceError("response row count exceeds the requested limit")

    parsed: list[FundFlowDailyRow] = []
    for index, value in enumerate(raw_klines):
        if not isinstance(value, str):
            raise FundFlowSourceError(f"data.klines[{index}] must be text")
        parsed.append(_parse_kline(value, index=index))

    dates = [row.trade_date for row in parsed]
    if len(set(dates)) != len(dates):
        raise FundFlowSourceError("data.klines contains duplicate trade dates")
    ordered = tuple(sorted(parsed, key=lambda row: row.trade_date))
    received_local = response_received_at.astimezone(SHANGHAI)
    latest_trade_date = ordered[-1].trade_date
    if latest_trade_date > received_local.date():
        raise FundFlowSourceError("latest trade date is later than response_received_at")
    if (
        latest_trade_date == received_local.date()
        and received_local.time() < CURRENT_SESSION_STABILITY_MARKER
    ):
        raise FundFlowSourceError(
            "current-session row arrived before the 16:05 Asia/Shanghai stability marker"
        )
    return ordered


def _parse_kline(value: str, *, index: int) -> FundFlowDailyRow:
    fields = value.split(",")
    if len(fields) != len(DAILY_FIELD_CODES):
        raise FundFlowSourceError(
            f"data.klines[{index}] has {len(fields)} fields; expected "
            f"{len(DAILY_FIELD_CODES)} for f51-f63"
        )
    try:
        trade_date = date.fromisoformat(fields[0])
    except ValueError as exc:
        raise FundFlowSourceError(f"data.klines[{index}] f51 is not an ISO date") from exc
    values = tuple(
        _decimal(raw, field_code=field_code, index=index)
        for field_code, raw in zip(DAILY_FIELD_CODES[1:], fields[1:], strict=True)
    )
    row = FundFlowDailyRow(
        trade_date=trade_date,
        main_net_inflow_cny=values[0],
        small_net_inflow_cny=values[1],
        medium_net_inflow_cny=values[2],
        large_net_inflow_cny=values[3],
        extra_large_net_inflow_cny=values[4],
        main_net_inflow_pct=values[5],
        small_net_inflow_pct=values[6],
        medium_net_inflow_pct=values[7],
        large_net_inflow_pct=values[8],
        extra_large_net_inflow_pct=values[9],
        close_cny=values[10],
        change_pct=values[11],
        source_row=value,
    )
    if row.close_cny <= 0:
        raise FundFlowSourceError(f"data.klines[{index}] f62 close must be positive")
    component_sum = row.large_net_inflow_cny + row.extra_large_net_inflow_cny
    scale = max(abs(row.main_net_inflow_cny), abs(component_sum), Decimal("1"))
    tolerance = max(
        _MAIN_ABSOLUTE_TOLERANCE_CNY,
        scale * _MAIN_RELATIVE_TOLERANCE,
    )
    if abs(row.main_net_inflow_cny - component_sum) > tolerance:
        raise FundFlowSourceError(
            f"data.klines[{index}] violates f52 ~= f55 + f56 within {tolerance} CNY"
        )
    return row


def _decimal(value: str, *, field_code: str, index: int) -> Decimal:
    if not value or value == "-":
        raise FundFlowSourceError(f"data.klines[{index}] {field_code} is missing")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise FundFlowSourceError(f"data.klines[{index}] {field_code} is not numeric") from exc
    if not parsed.is_finite():
        raise FundFlowSourceError(f"data.klines[{index}] {field_code} must be finite")
    return parsed


def _decode_json(raw_bytes: bytes) -> JsonObject:
    try:
        decoded: object = json.loads(raw_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FundFlowSourceError("Eastmoney response is not valid JSON") from exc
    normalized = _json_value(decoded, field_name="response")
    if not isinstance(normalized, dict):
        raise FundFlowSourceError("Eastmoney response must be a JSON object")
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
            raise FundFlowSourceError(f"{field_name} contains a non-finite number")
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
            raise FundFlowSourceError(f"{field_name} contains a non-text object key")
        return {
            cast(str, key): _json_value(item, field_name=f"{field_name}.{key}")
            for key, item in raw.items()
        }
    raise FundFlowSourceError(f"{field_name} contains unsupported JSON data")


def _required_object(value: JsonValue | None, field_name: str) -> JsonObject:
    if not isinstance(value, dict):
        raise FundFlowSourceError(f"{field_name} must be an object")
    return value


def _secid(instrument_id: InstrumentId) -> tuple[str, int, str]:
    try:
        code, exchange = instrument_id.value.split(".", maxsplit=1)
    except ValueError as exc:
        raise ValueError("instrument_id must use 000000.EXCHANGE format") from exc
    if len(code) != 6 or not code.isdigit() or exchange not in {"SH", "SZ", "BJ"}:
        raise ValueError("instrument_id must be a canonical mainland A-share identifier")
    market = 1 if exchange == "SH" else 0
    return f"{market}.{code}", market, code


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include an explicit timezone")


def _read_clock(clock: Callable[[], object], field_name: str) -> datetime:
    value = clock()
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} clock value must be a datetime")
    _require_aware(value, field_name)
    return value


def _utc_now() -> datetime:
    return datetime.now(UTC)
