"""Fail-closed instrument metadata from the official BSE listed-company feed.

The public endpoint exposes the current Beijing Stock Exchange security master
used by the exchange website.  It can prove current membership, code, short
name, listing date, security level and current trading/suspension state.  It
does not expose a historical delisting universe, so a missing code is never
interpreted as a delisted security and ``delisting_date`` remains explicitly
unsupported.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from enum import StrEnum
from typing import Literal, cast
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import httpx

from ashare_lab.domain.market_data import Board, normalize_a_share_instrument
from ashare_lab.domain.shared import InstrumentId

BSE_LISTED_COMPANY_URL = "https://www.bse.cn/nqxxController/nqxxCnzq.do"
BSE_LISTED_COMPANY_PAGE = "https://www.bse.cn/nq/listedcompany.html"
PROVIDER = "beijing_stock_exchange_public_list"
LISTING_DATE_SEMANTICS = "official_security_master_listing_date"
PROVIDER_DATE_SEMANTICS = "undocumented_provider_xxjsrq_preserved_without_inference"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_DEFAULT_TIMEOUT_SECONDS = 20.0
_DATE = re.compile(r"^[0-9]{8}$")
_TIME = re.compile(r"^[0-9]{6}$")
_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Referer": BSE_LISTED_COMPANY_PAGE,
    "User-Agent": "Mozilla/5.0 (compatible; AShareLab/1.0; instrument-reference)",
    "X-Requested-With": "XMLHttpRequest",
}

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]


class BseInstrumentReferenceError(RuntimeError):
    """The official response cannot prove one usable instrument reference."""


class BseInstrumentNotFoundError(BseInstrumentReferenceError):
    """The exact code is absent from the current BSE listed-company universe."""


class BseListingStatus(StrEnum):
    LISTED = "listed"


class BseTradingState(StrEnum):
    NORMAL = "normal"
    FIRST_LISTING_DAY = "first_listing_day"
    NEW_STOCK_TRADING = "new_stock_trading"


class BseSuspensionState(StrEnum):
    TRADING = "trading"
    SUSPENDED_REJECT_ORDERS = "suspended_reject_orders"
    SUSPENDED_ACCEPT_ORDERS = "suspended_accept_orders"


_TRADING_STATES = {
    "N": BseTradingState.NORMAL,
    "Y": BseTradingState.FIRST_LISTING_DAY,
    "D": BseTradingState.NEW_STOCK_TRADING,
}
_SUSPENSION_STATES = {
    "F": BseSuspensionState.TRADING,
    "T": BseSuspensionState.SUSPENDED_REJECT_ORDERS,
    "H": BseSuspensionState.SUSPENDED_ACCEPT_ORDERS,
}


@dataclass(frozen=True, slots=True)
class BseInstrumentReference:
    instrument_id: InstrumentId
    name: str
    listing_date: date
    listing_date_semantics: Literal["official_security_master_listing_date"]
    delisting_date: None
    board: Literal[Board.BSE]
    status: Literal[BseListingStatus.LISTED]
    trading_state: BseTradingState
    suspension_state: BseSuspensionState
    provider_reference_date: date
    provider_record_update_time: time
    observed_at: datetime
    currency: Literal["CNY"] = "CNY"


@dataclass(frozen=True, slots=True)
class BseReferenceAudit:
    provider: str
    request_url: str
    request_params: tuple[tuple[str, str], ...]
    request_started_at: datetime
    response_received_at: datetime
    http_status: int
    waf_cookie_retry: bool
    raw_response_sha256: str
    canonical_payload_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "requestUrl": self.request_url,
            "requestParams": dict(self.request_params),
            "requestStartedAt": self.request_started_at.isoformat(),
            "responseReceivedAt": self.response_received_at.isoformat(),
            "httpStatus": self.http_status,
            "wafCookieRetry": self.waf_cookie_retry,
            "rawResponseSha256": self.raw_response_sha256,
            "canonicalPayloadSha256": self.canonical_payload_sha256,
        }


@dataclass(frozen=True, slots=True)
class BseInstrumentReferenceResult:
    instrument: BseInstrumentReference
    audit: BseReferenceAudit
    coverage: Mapping[str, object]


class BseInstrumentReferenceSource:
    """Fetch one exact current BSE stock through the official public list."""

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
        self._clock = clock or (lambda: datetime.now(UTC))
        self._owns_client = client is None
        self._client = client or httpx.Client(
            transport=transport,
            timeout=self._timeout,
            follow_redirects=False,
            trust_env=False,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> BseInstrumentReferenceSource:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def fetch(self, symbol: InstrumentId | str) -> BseInstrumentReferenceResult:
        instrument_id = normalize_a_share_instrument(symbol)
        if not str(instrument_id).endswith(".BJ"):
            raise BseInstrumentReferenceError(
                "BSE reference source requires a Beijing Stock Exchange instrument"
            )
        code = str(instrument_id).split(".", maxsplit=1)[0]
        params = {
            "page": "0",
            "typejb": "T",
            "xxfcbj[]": "2",
            "xxzqdm": code,
            "sortfield": "xxzqdm",
            "sorttype": "asc",
        }
        request_started_at = _read_clock(self._clock, "request_started_at")
        response, retried = self._post_with_bounded_waf_retry(params)
        response_received_at = _read_clock(self._clock, "response_received_at")
        if response_received_at < request_started_at:
            raise BseInstrumentReferenceError("response_received_at precedes request_started_at")
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise BseInstrumentReferenceError(
                f"official BSE listed-company request failed with HTTP {response.status_code}"
            ) from exc

        raw_bytes = response.content
        payload = _decode_jsonp(raw_bytes)
        instrument = _parse_payload(
            payload,
            expected_instrument=instrument_id,
            response_received_at=response_received_at,
        )
        raw_response_sha256 = "sha256:" + hashlib.sha256(raw_bytes).hexdigest()
        canonical_payload_sha256 = (
            "sha256:" + hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()
        )
        audit = BseReferenceAudit(
            provider=PROVIDER,
            request_url=BSE_LISTED_COMPANY_URL,
            request_params=tuple(sorted(params.items())),
            request_started_at=request_started_at,
            response_received_at=response_received_at,
            http_status=response.status_code,
            waf_cookie_retry=retried,
            raw_response_sha256=raw_response_sha256,
            canonical_payload_sha256=canonical_payload_sha256,
        )
        coverage = {
            "schemaVersion": "bse.current-instrument-reference.v1",
            "provider": PROVIDER,
            "querySucceeded": True,
            "exactCodeMatch": True,
            "currentListedUniverse": True,
            "instrumentId": str(instrument.instrument_id),
            "observedAt": instrument.observed_at.isoformat(),
            "providerReferenceDate": {
                "value": instrument.provider_reference_date.isoformat(),
                "sourceField": "xxjsrq",
                "semantics": PROVIDER_DATE_SEMANTICS,
            },
            "providerRecordUpdateTime": {
                "value": instrument.provider_record_update_time.isoformat(),
                "sourceField": "xxgxsj",
                "semantics": "official_record_update_time",
            },
            "listingDate": {
                "status": "complete",
                "sourceField": "xxgprq",
                "semantics": LISTING_DATE_SEMANTICS,
                "note": (
                    "the official field is the security-master listing date; "
                    "for companies migrated from the former Select Layer it can predate "
                    "the opening of the Beijing Stock Exchange"
                ),
            },
            "bseVenueAdmissionDate": {
                "status": "unsupported",
                "reason": (
                    "the current listed-company endpoint does not separately publish the "
                    "Beijing Stock Exchange venue-admission date for migrated securities"
                ),
            },
            "delistingDate": {
                "status": "unsupported",
                "reason": "official endpoint publishes the current listed universe only",
            },
            "rawResponseSha256": raw_response_sha256,
        }
        return BseInstrumentReferenceResult(
            instrument=instrument,
            audit=audit,
            coverage=coverage,
        )

    def _post_with_bounded_waf_retry(
        self,
        params: Mapping[str, str],
    ) -> tuple[httpx.Response, bool]:
        try:
            response = self._client.post(
                BSE_LISTED_COMPANY_URL,
                data=params,
                headers=_HEADERS,
                timeout=self._timeout,
            )
            if response.status_code != 307:
                return response, False
            location = response.headers.get("Location")
            if (
                location is None
                or urljoin(BSE_LISTED_COMPANY_URL, location) != BSE_LISTED_COMPANY_URL
            ):
                raise BseInstrumentReferenceError(
                    "official BSE endpoint redirected to an unexpected location"
                )
            if "set-cookie" not in response.headers:
                raise BseInstrumentReferenceError(
                    "official BSE endpoint returned a redirect without a session cookie"
                )
            retry = self._client.post(
                BSE_LISTED_COMPANY_URL,
                data=params,
                headers=_HEADERS,
                timeout=self._timeout,
            )
            if retry.status_code == 307:
                raise BseInstrumentReferenceError(
                    "official BSE endpoint repeated its session-cookie challenge"
                )
            return retry, True
        except httpx.HTTPError as exc:
            raise BseInstrumentReferenceError(
                f"official BSE listed-company request failed: {type(exc).__name__}"
            ) from exc


def to_instrument_reference_payload(
    result: BseInstrumentReferenceResult,
) -> dict[str, object]:
    instrument = result.instrument
    return {
        "schemaVersion": "ashare.instrument-reference.v1",
        "instrument": {
            "instrument_id": str(instrument.instrument_id),
            "name": instrument.name,
            "listing_date": instrument.listing_date.isoformat(),
            "listing_date_semantics": instrument.listing_date_semantics,
            "delisting_date": None,
            "status": instrument.status.value,
            "board": instrument.board.value,
            "currency": instrument.currency,
            "trading_state": instrument.trading_state.value,
            "suspension_state": instrument.suspension_state.value,
            "provider_reference_date": instrument.provider_reference_date.isoformat(),
            "provider_reference_date_semantics": PROVIDER_DATE_SEMANTICS,
            "provider_record_update_time": instrument.provider_record_update_time.isoformat(),
            "observed_at": instrument.observed_at.isoformat(),
        },
        "coverage": dict(result.coverage),
        "queryAudit": result.audit.as_dict(),
    }


def _parse_payload(
    payload: JsonValue,
    *,
    expected_instrument: InstrumentId,
    response_received_at: datetime,
) -> BseInstrumentReference:
    pages = _required_list(payload, "root")
    if len(pages) != 1:
        raise BseInstrumentReferenceError("official BSE response must contain one page object")
    page = _required_object(pages[0], "root[0]")
    content = _required_list(page.get("content"), "root[0].content")
    page_number = _required_int(page.get("number"), "root[0].number")
    number_of_elements = _required_int(
        page.get("numberOfElements"),
        "root[0].numberOfElements",
    )
    total_elements = _required_int(page.get("totalElements"), "root[0].totalElements")
    total_pages = _required_int(page.get("totalPages"), "root[0].totalPages")
    first_page = _required_bool(page.get("firstPage"), "root[0].firstPage")
    last_page = _required_bool(page.get("lastPage"), "root[0].lastPage")
    if (
        page_number == 0
        and number_of_elements == 0
        and total_elements == 0
        and total_pages in {0, 1}
        and first_page
        and last_page
        and not content
    ):
        raise BseInstrumentNotFoundError(
            "instrument is absent from the current official BSE listed-company universe"
        )
    if (
        page_number != 0
        or number_of_elements != 1
        or total_elements != 1
        or total_pages != 1
        or not first_page
        or not last_page
        or len(content) != 1
    ):
        raise BseInstrumentReferenceError(
            "exact-code BSE query did not return exactly one listed security"
        )
    row = _required_object(content[0], "root[0].content[0]")
    code = _required_text(row.get("xxzqdm"), "xxzqdm")
    if str(expected_instrument) != f"{code}.BJ":
        raise BseInstrumentReferenceError(
            "official BSE response code does not match the requested instrument"
        )
    if _required_text(row.get("xxzqjb"), "xxzqjb") != "T":
        raise BseInstrumentReferenceError("official BSE row is not a listed-company stock")
    if _required_text(row.get("xxfcbj"), "xxfcbj") != "2":
        raise BseInstrumentReferenceError("official BSE row is outside the listed market level")
    if _required_text(row.get("xxhbzl"), "xxhbzl") != "00":
        raise BseInstrumentReferenceError("official BSE row is not CNY-denominated")

    listing_date = _compact_date(row.get("xxgprq"), "xxgprq")
    provider_reference_date = _compact_date(row.get("xxjsrq"), "xxjsrq")
    provider_record_update_time = _compact_time(row.get("xxgxsj"), "xxgxsj")
    observed_at = response_received_at.astimezone(_SHANGHAI)
    if listing_date > provider_reference_date:
        raise BseInstrumentReferenceError("listing date is later than provider field xxjsrq")
    if provider_reference_date > observed_at.date():
        raise BseInstrumentReferenceError("provider field xxjsrq is later than observed_at")
    raw_trading_state = _required_text(row.get("xxzrzt"), "xxzrzt")
    raw_suspension_state = _required_text(row.get("xxtpbz"), "xxtpbz")
    trading_state = _TRADING_STATES.get(raw_trading_state)
    suspension_state = _SUSPENSION_STATES.get(raw_suspension_state)
    if trading_state is None:
        raise BseInstrumentReferenceError("official BSE trading state is unsupported")
    if suspension_state is None:
        raise BseInstrumentReferenceError("official BSE suspension state is unsupported")
    return BseInstrumentReference(
        instrument_id=expected_instrument,
        name=_required_text(row.get("xxzqjc"), "xxzqjc"),
        listing_date=listing_date,
        listing_date_semantics=LISTING_DATE_SEMANTICS,
        delisting_date=None,
        board=Board.BSE,
        status=BseListingStatus.LISTED,
        trading_state=trading_state,
        suspension_state=suspension_state,
        provider_reference_date=provider_reference_date,
        provider_record_update_time=provider_record_update_time,
        observed_at=observed_at,
    )


def _decode_jsonp(raw_bytes: bytes) -> JsonValue:
    try:
        text = raw_bytes.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise BseInstrumentReferenceError("official BSE response is not UTF-8") from exc
    if text.startswith("null(") and text.endswith(")"):
        text = text[5:-1]
    elif not text.startswith("["):
        raise BseInstrumentReferenceError("official BSE response has an unexpected JSONP wrapper")
    try:
        return cast(JsonValue, json.loads(text))
    except json.JSONDecodeError as exc:
        raise BseInstrumentReferenceError("official BSE response is not valid JSON") from exc


def _required_object(value: JsonValue | None, label: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise BseInstrumentReferenceError(f"{label} must be an object")
    return value


def _required_list(value: JsonValue | None, label: str) -> list[JsonValue]:
    if not isinstance(value, list):
        raise BseInstrumentReferenceError(f"{label} must be an array")
    return value


def _required_text(value: JsonValue | None, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BseInstrumentReferenceError(f"{label} must be non-empty text")
    return value.strip()


def _required_int(value: JsonValue | None, label: str) -> int:
    if type(value) is not int or value < 0:
        raise BseInstrumentReferenceError(f"{label} must be a non-negative integer")
    return value


def _required_bool(value: JsonValue | None, label: str) -> bool:
    if type(value) is not bool:
        raise BseInstrumentReferenceError(f"{label} must be a boolean")
    return value


def _compact_date(value: JsonValue | None, label: str) -> date:
    raw = _required_text(value, label)
    if _DATE.fullmatch(raw) is None:
        raise BseInstrumentReferenceError(f"{label} must use YYYYMMDD")
    try:
        return date(int(raw[:4]), int(raw[4:6]), int(raw[6:]))
    except ValueError as exc:
        raise BseInstrumentReferenceError(f"{label} is not a valid date") from exc


def _compact_time(value: JsonValue | None, label: str) -> time:
    if type(value) is int:
        raw = str(value)
    elif isinstance(value, str):
        raw = value.strip()
    else:
        raise BseInstrumentReferenceError(f"{label} must be an HHMMSS number")
    if not raw.isdigit() or not 1 <= len(raw) <= 6:
        raise BseInstrumentReferenceError(f"{label} must use HHMMSS")
    raw = raw.zfill(6)
    if _TIME.fullmatch(raw) is None:
        raise BseInstrumentReferenceError(f"{label} must use HHMMSS")
    try:
        return time(int(raw[:2]), int(raw[2:4]), int(raw[4:]))
    except ValueError as exc:
        raise BseInstrumentReferenceError(f"{label} is not a valid time") from exc


def _read_clock(clock: Callable[[], datetime], label: str) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise BseInstrumentReferenceError(f"{label} clock value must include a timezone")
    return value


def _canonical_json_bytes(value: JsonValue) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


__all__ = [
    "BSE_LISTED_COMPANY_PAGE",
    "BSE_LISTED_COMPANY_URL",
    "LISTING_DATE_SEMANTICS",
    "PROVIDER",
    "PROVIDER_DATE_SEMANTICS",
    "BseInstrumentNotFoundError",
    "BseInstrumentReference",
    "BseInstrumentReferenceError",
    "BseInstrumentReferenceResult",
    "BseInstrumentReferenceSource",
    "BseListingStatus",
    "BseReferenceAudit",
    "BseSuspensionState",
    "BseTradingState",
    "to_instrument_reference_payload",
]
