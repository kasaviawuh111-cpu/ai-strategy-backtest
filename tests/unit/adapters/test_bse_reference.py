from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from copy import deepcopy
from datetime import date, datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

import httpx
import pytest

from ashare_lab.adapters.market_data.bse_reference import (
    BSE_LISTED_COMPANY_URL,
    LISTING_DATE_SEMANTICS,
    PROVIDER_DATE_SEMANTICS,
    BseInstrumentNotFoundError,
    BseInstrumentReferenceError,
    BseInstrumentReferenceResult,
    BseInstrumentReferenceSource,
    BseListingStatus,
    BseSuspensionState,
    BseTradingState,
    to_instrument_reference_payload,
)
from ashare_lab.domain.market_data import Board
from ashare_lab.domain.shared import InstrumentId

SHANGHAI = ZoneInfo("Asia/Shanghai")
STARTED = datetime(2026, 8, 30, 10, 0, 0, tzinfo=SHANGHAI)
RECEIVED = datetime(2026, 8, 30, 10, 0, 1, tzinfo=SHANGHAI)


def _payload(*, code: str = "920001", update_time: str | int = "083000") -> list[dict[str, Any]]:
    return [
        {
            "content": [
                {
                    "xxzqdm": code,
                    "xxzqjc": "纬达光电",
                    "xxgprq": "20221227",
                    "xxjsrq": "20260828",
                    "xxgxsj": update_time,
                    "xxzqjb": "T",
                    "xxfcbj": "2",
                    "xxhbzl": "00",
                    "xxzrzt": "N",
                    "xxtpbz": "F",
                }
            ],
            "firstPage": True,
            "lastPage": True,
            "number": 0,
            "numberOfElements": 1,
            "size": 20,
            "sort": None,
            "totalElements": 1,
            "totalPages": 1,
        }
    ]


def _wire(payload: object, *, pretty: bool = False) -> bytes:
    return (
        "null("
        + json.dumps(
            payload,
            ensure_ascii=False,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
        )
        + ")"
    ).encode()


def _clock() -> Callable[[], datetime]:
    values = iter((STARTED, RECEIVED))
    return lambda: next(values)


def _source(handler: httpx.MockTransport) -> BseInstrumentReferenceSource:
    return BseInstrumentReferenceSource(transport=handler, clock=_clock())


def test_fetches_exact_current_bse_reference_with_auditable_hashes() -> None:
    raw = _wire(_payload())

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == BSE_LISTED_COMPANY_URL
        assert request.method == "POST"
        assert b"xxzqdm=920001" in request.content
        assert request.headers["referer"].startswith("https://www.bse.cn/")
        return httpx.Response(200, content=raw, request=request)

    with _source(httpx.MockTransport(handler)) as source:
        result = source.fetch("920001")

    instrument = result.instrument
    assert instrument.instrument_id == InstrumentId("920001.BJ")
    assert instrument.name == "纬达光电"
    assert instrument.listing_date == date(2022, 12, 27)
    assert instrument.listing_date_semantics == LISTING_DATE_SEMANTICS
    assert instrument.delisting_date is None
    assert instrument.board is Board.BSE
    assert instrument.status is BseListingStatus.LISTED
    assert instrument.trading_state is BseTradingState.NORMAL
    assert instrument.suspension_state is BseSuspensionState.TRADING
    assert instrument.provider_reference_date == date(2026, 8, 28)
    assert instrument.provider_record_update_time.isoformat() == "08:30:00"
    assert instrument.observed_at == RECEIVED
    assert result.audit.raw_response_sha256 == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert result.audit.request_started_at == STARTED
    assert result.audit.response_received_at == RECEIVED
    assert result.audit.waf_cookie_retry is False
    assert result.coverage["exactCodeMatch"] is True
    assert result.coverage["listingDate"] == {
        "status": "complete",
        "sourceField": "xxgprq",
        "semantics": LISTING_DATE_SEMANTICS,
        "note": (
            "the official field is the security-master listing date; "
            "for companies migrated from the former Select Layer it can predate "
            "the opening of the Beijing Stock Exchange"
        ),
    }
    assert result.coverage["bseVenueAdmissionDate"] == {
        "status": "unsupported",
        "reason": (
            "the current listed-company endpoint does not separately publish the Beijing "
            "Stock Exchange venue-admission date for migrated securities"
        ),
    }
    assert result.coverage["providerReferenceDate"] == {
        "value": "2026-08-28",
        "sourceField": "xxjsrq",
        "semantics": PROVIDER_DATE_SEMANTICS,
    }
    assert result.coverage["delistingDate"] == {
        "status": "unsupported",
        "reason": "official endpoint publishes the current listed universe only",
    }


def test_accepts_lowercase_canonical_bj_symbol() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_wire(_payload()), request=request)

    with _source(httpx.MockTransport(handler)) as source:
        result = source.fetch("920001.bj")

    assert result.instrument.instrument_id == InstrumentId("920001.BJ")


def test_numeric_source_update_time_restores_omitted_leading_zero() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_wire(_payload(update_time=83000)), request=request)

    with _source(httpx.MockTransport(handler)) as source:
        result = source.fetch("920001.BJ")

    assert result.instrument.provider_record_update_time.hour == 8
    assert result.instrument.provider_record_update_time.minute == 30


def test_retries_one_official_same_location_cookie_challenge() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                307,
                headers={
                    "Location": BSE_LISTED_COMPANY_URL,
                    "Set-Cookie": "C3VK=fixture; Max-Age=300; Path=/",
                },
                request=request,
            )
        assert "C3VK=fixture" in request.headers.get("cookie", "")
        return httpx.Response(200, content=_wire(_payload()), request=request)

    with _source(httpx.MockTransport(handler)) as source:
        result = source.fetch("920001.BJ")

    assert len(requests) == 2
    assert result.audit.waf_cookie_retry is True


def test_repeated_cookie_challenge_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            307,
            headers={
                "Location": BSE_LISTED_COMPANY_URL,
                "Set-Cookie": "C3VK=fixture; Max-Age=300; Path=/",
            },
            request=request,
        )

    with (
        _source(httpx.MockTransport(handler)) as source,
        pytest.raises(BseInstrumentReferenceError, match="repeated"),
    ):
        source.fetch("920001.BJ")


def test_empty_current_universe_result_is_not_assumed_delisted() -> None:
    empty = _payload()
    empty[0]["content"] = []
    empty[0]["numberOfElements"] = 0
    empty[0]["totalElements"] = 0

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_wire(empty), request=request)

    with (
        _source(httpx.MockTransport(handler)) as source,
        pytest.raises(BseInstrumentNotFoundError, match="current official"),
    ):
        source.fetch("920001.BJ")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("number", 1),
        ("numberOfElements", 2),
        ("firstPage", False),
        ("lastPage", False),
        ("totalElements", 2),
        ("totalPages", 2),
    ],
)
def test_exact_query_pagination_mismatch_fails_closed(field: str, value: object) -> None:
    payload = _payload()
    payload[0][field] = value

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_wire(payload), request=request)

    with (
        _source(httpx.MockTransport(handler)) as source,
        pytest.raises(BseInstrumentReferenceError, match="exactly one"),
    ):
        source.fetch("920001.BJ")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("xxzqdm", "920002", "does not match"),
        ("xxzqjb", "B", "not a listed-company stock"),
        ("xxfcbj", "1", "outside the listed market"),
        ("xxhbzl", "02", "not CNY"),
        ("xxgprq", "2026-99-99", "YYYYMMDD"),
        ("xxzrzt", "I", "trading state is unsupported"),
        ("xxtpbz", "X", "suspension state is unsupported"),
    ],
)
def test_schema_or_scope_mismatch_fails_closed(
    field: str,
    value: str,
    message: str,
) -> None:
    payload = _payload()
    payload[0]["content"][0][field] = value

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_wire(payload), request=request)

    with (
        _source(httpx.MockTransport(handler)) as source,
        pytest.raises(BseInstrumentReferenceError, match=message),
    ):
        source.fetch("920001.BJ")


def test_non_bse_and_unproven_legacy_aliases_fail_closed() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=_wire(_payload(code="920799")), request=request)

    with (
        _source(httpx.MockTransport(handler)) as source,
        pytest.raises(BseInstrumentReferenceError, match="Beijing"),
    ):
        source.fetch("300059.SZ")
    assert calls == 0

    with (
        _source(httpx.MockTransport(handler)) as source,
        pytest.raises(BseInstrumentReferenceError, match="does not match"),
    ):
        source.fetch("830799.BJ")
    assert calls == 1


def test_canonical_payload_hash_ignores_wire_whitespace_but_raw_hash_does_not() -> None:
    payload = _payload()
    raw_compact = _wire(payload)
    raw_pretty = _wire(deepcopy(payload), pretty=True)

    def fetch(raw: bytes) -> BseInstrumentReferenceResult:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=raw, request=request)

        with _source(httpx.MockTransport(handler)) as source:
            return source.fetch("920001.BJ")

    compact = fetch(raw_compact)
    pretty = fetch(raw_pretty)

    assert compact.audit.raw_response_sha256 != pretty.audit.raw_response_sha256
    assert compact.audit.canonical_payload_sha256 == pretty.audit.canonical_payload_sha256


def test_json_payload_keeps_delisting_limitation_explicit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_wire(_payload()), request=request)

    with _source(httpx.MockTransport(handler)) as source:
        payload = to_instrument_reference_payload(source.fetch("920001.BJ"))

    assert payload["schemaVersion"] == "ashare.instrument-reference.v1"
    instrument = cast(dict[str, object], payload["instrument"])
    assert instrument["delisting_date"] is None
    assert instrument["listing_date_semantics"] == LISTING_DATE_SEMANTICS
    assert instrument["provider_reference_date_semantics"] == PROVIDER_DATE_SEMANTICS
    coverage = cast(dict[str, object], payload["coverage"])
    assert cast(dict[str, object], coverage["delistingDate"])["status"] == "unsupported"
