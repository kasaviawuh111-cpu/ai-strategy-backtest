from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import cast
from zoneinfo import ZoneInfo

import httpx
import pytest

from ashare_lab.adapters.host_http import HostThrottle
from ashare_lab.adapters.market_data.eastmoney_daily import (
    AKSHARE_REQUEST_TOKEN,
    AKSHARE_SOURCE_COMMIT,
    DAILY_REQUEST_FIELD_CODES,
    EASTMONEY_DAILY_URL,
    EASTMONEY_MIN_REQUEST_INTERVAL_SECONDS,
    VOLUME_LOT_SIZE_SHARES,
    VOLUME_RESOLUTION_SHARES,
    EastmoneyDailyResearchSource,
    EastmoneyDailySourceError,
)
from ashare_lab.domain.market_data import PriceBasis
from ashare_lab.domain.shared import InstrumentId

SHANGHAI = ZoneInfo("Asia/Shanghai")
START = date(2026, 8, 27)
END = date(2026, 8, 28)
REQUEST_STARTED_AT = datetime(2026, 8, 29, 16, 29, 59, tzinfo=SHANGHAI)
RESPONSE_RECEIVED_AT = datetime(2026, 8, 29, 16, 30, tzinfo=SHANGHAI)


def _row(
    trade_date: str = "2026-08-28",
    *,
    open_: str = "25.00",
    close: str = "25.18",
    high: str = "25.30",
    low: str = "24.80",
    volume_lots: str = "12345",
    amount: str = "31000000",
    amplitude: str = "2.00",
    change_pct: str = "2.03",
    change: str = "0.50",
    turnover_rate: str = "1.50",
) -> str:
    return ",".join(
        (
            trade_date,
            open_,
            close,
            high,
            low,
            volume_lots,
            amount,
            amplitude,
            change_pct,
            change,
            turnover_rate,
        )
    )


def _payload(
    klines: object,
    *,
    code: str = "300059",
    market: object = 0,
    name: object = "东方财富",
    rc: object = 0,
) -> dict[str, object]:
    return {
        "rc": rc,
        "rt": 6,
        "data": {
            "code": code,
            "market": market,
            "name": name,
            "klines": klines,
        },
    }


def _body(*args: object, **kwargs: object) -> bytes:
    return json.dumps(
        _payload(*args, **kwargs),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _clock(*values: datetime) -> Callable[[], datetime]:
    iterator = iter(values)
    return lambda: next(iterator)


def _source(
    body: bytes,
    requests: list[httpx.Request] | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
    status_code: int = 200,
    max_attempts: int = 1,
    sleeper: Callable[[float], None] = lambda _seconds: None,
) -> EastmoneyDailyResearchSource:
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        return httpx.Response(
            status_code,
            content=body,
            headers={"Content-Type": "application/json"},
            request=request,
        )

    return EastmoneyDailyResearchSource(
        transport=httpx.MockTransport(handler),
        clock=clock or _clock(REQUEST_STARTED_AT, RESPONSE_RECEIVED_AT),
        max_attempts=max_attempts,
        sleeper=sleeper,
    )


def test_fetch_unadjusted_sz_rows_preserves_request_raw_hashes_and_lot_resolution() -> None:
    payload = _payload(
        [
            _row("2026-08-28"),
            _row(
                "2026-08-27",
                open_="24.00",
                close="24.68",
                high="24.80",
                low="23.90",
                volume_lots="10000",
                amount="24500000",
            ),
        ]
    )
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode()
    requests: list[httpx.Request] = []

    with _source(body, requests) as source:
        result = source.fetch(
            instrument_id=InstrumentId("300059.SZ"),
            start=START,
            end=END,
            price_basis=PriceBasis.UNADJUSTED,
        )

    assert len(requests) == 1
    request = requests[0]
    assert str(request.url).startswith(EASTMONEY_DAILY_URL)
    assert request.url.params["secid"] == "0.300059"
    assert request.url.params["klt"] == "101"
    assert request.url.params["fqt"] == "0"
    assert request.url.params["beg"] == "20260827"
    assert request.url.params["end"] == "20260828"
    assert request.url.params["fields2"] == ",".join(DAILY_REQUEST_FIELD_CODES)
    assert request.url.params["ut"] == AKSHARE_REQUEST_TOKEN
    assert AKSHARE_SOURCE_COMMIT == "8e95744b79ae22326308ccd2b4e62650c5b53c55"
    assert result.request_params == dict(request.url.params)
    assert result.request_started_at == REQUEST_STARTED_AT
    assert result.response_received_at == RESPONSE_RECEIVED_AT
    assert result.retrieved_at == RESPONSE_RECEIVED_AT
    assert result.raw_wire_sha256 == hashlib.sha256(body).hexdigest()
    expected_canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    assert result.raw_payload_canonical_sha256 == hashlib.sha256(expected_canonical).hexdigest()
    assert result.provider_name == "eastmoney_push2his_public"
    assert result.dataset_name == "stock_daily_kline"
    assert result.price_basis is PriceBasis.UNADJUSTED
    assert result.attempt_count == 1
    assert result.transient_errors == ()
    assert [row.date for row in result.rows] == [START, END]
    latest = result.rows[-1]
    assert latest.open == Decimal("25.00")
    assert latest.high == Decimal("25.30")
    assert latest.low == Decimal("24.80")
    assert latest.close == Decimal("25.18")
    assert latest.volume_lots == 12_345
    assert latest.volume_shares == 1_234_500
    assert latest.amount == Decimal("31000000")
    assert result.source_volume_unit == "lot"
    assert result.volume_lot_size_shares == VOLUME_LOT_SIZE_SHARES == 100
    assert result.volume_resolution_shares == VOLUME_RESOLUTION_SHARES == 100
    assert latest.as_snapshot_row(InstrumentId("300059.SZ")) == {
        "stock_code": "300059.SZ",
        "date": "2026-08-28",
        "open": Decimal("25.00"),
        "high": Decimal("25.30"),
        "low": Decimal("24.80"),
        "close": Decimal("25.18"),
        "volume": 1_234_500,
        "amount": Decimal("31000000"),
    }


def test_fetch_back_adjusted_sh_uses_market_one_and_fqt_two() -> None:
    requests: list[httpx.Request] = []
    with _source(_body([_row()], code="600519", market=1, name="贵州茅台"), requests) as source:
        result = source.fetch(
            instrument_id=InstrumentId("600519.SH"),
            start=START,
            end=END,
            price_basis=PriceBasis.BACK_ADJUSTED,
        )

    assert requests[0].url.params["secid"] == "1.600519"
    assert requests[0].url.params["fqt"] == "2"
    assert result.instrument_id == InstrumentId("600519.SH")
    assert result.price_basis is PriceBasis.BACK_ADJUSTED


def test_back_adjusted_prices_do_not_claim_turnover_unit_identity() -> None:
    adjusted = _row(
        open_="100.00",
        close="101.00",
        high="102.00",
        low="99.00",
        volume_lots="100",
        amount="250000",
    )
    with _source(_body([adjusted])) as source:
        result = source.fetch(
            instrument_id=InstrumentId("300059.SZ"),
            start=START,
            end=END,
            price_basis=PriceBasis.BACK_ADJUSTED,
        )

    assert result.rows[0].volume_shares == 10_000
    assert result.rows[0].amount == Decimal("250000")


@pytest.mark.parametrize(
    ("instrument", "message"),
    [
        ("300059.SH", "canonical mainland"),
        ("300059.sz", "must be canonical"),
        ("430047.BJ", "SH and SZ"),
        ("AAPL.US", "canonical mainland"),
    ],
)
def test_noncanonical_wrong_suffix_or_unsupported_market_is_rejected(
    instrument: str,
    message: str,
) -> None:
    with (
        _source(_body([_row()])) as source,
        pytest.raises(ValueError, match=message),
    ):
        source.fetch(
            instrument_id=InstrumentId(instrument),
            start=START,
            end=END,
            price_basis=PriceBasis.UNADJUSTED,
        )


def test_date_range_and_price_basis_are_strict() -> None:
    with (
        _source(_body([_row()])) as source,
        pytest.raises(ValueError, match="start must not be later"),
    ):
        source.fetch(
            instrument_id=InstrumentId("300059.SZ"),
            start=END,
            end=START,
            price_basis=PriceBasis.UNADJUSTED,
        )
    with (
        _source(_body([_row()])) as source,
        pytest.raises(TypeError, match="date values"),
    ):
        source.fetch(
            instrument_id=InstrumentId("300059.SZ"),
            start=cast(date, datetime(2026, 8, 27)),
            end=END,
            price_basis=PriceBasis.UNADJUSTED,
        )
    with (
        _source(_body([_row()])) as source,
        pytest.raises(ValueError, match="price_basis"),
    ):
        source.fetch(
            instrument_id=InstrumentId("300059.SZ"),
            start=START,
            end=END,
            price_basis=cast(PriceBasis, "unadjusted"),
        )


@pytest.mark.parametrize("klines", [None, []])
def test_empty_or_null_rows_fail_closed(klines: object) -> None:
    with (
        _source(_body(klines)) as source,
        pytest.raises(EastmoneyDailySourceError, match="non-empty array"),
    ):
        source.fetch(
            instrument_id=InstrumentId("300059.SZ"),
            start=START,
            end=END,
            price_basis=PriceBasis.UNADJUSTED,
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"rc": 1}, "rc is not zero"),
        ({"rc": False}, "rc is not zero"),
        ({"code": "600519"}, "stock code"),
        ({"market": 1}, "response market"),
        ({"market": False}, "response market"),
        ({"name": ""}, "data.name"),
    ],
)
def test_response_status_and_identity_drift_fail_closed(
    overrides: dict[str, object],
    message: str,
) -> None:
    with (
        _source(_body([_row()], **overrides)) as source,
        pytest.raises(EastmoneyDailySourceError, match=message),
    ):
        source.fetch(
            instrument_id=InstrumentId("300059.SZ"),
            start=START,
            end=END,
            price_basis=PriceBasis.UNADJUSTED,
        )


def test_field_count_drift_and_non_text_rows_fail_closed() -> None:
    short_row = ",".join(_row().split(",")[:-1])
    with (
        _source(_body([short_row])) as source,
        pytest.raises(EastmoneyDailySourceError, match="expected 11 for f51-f61"),
    ):
        source.fetch(
            instrument_id=InstrumentId("300059.SZ"),
            start=START,
            end=END,
            price_basis=PriceBasis.UNADJUSTED,
        )
    with (
        _source(_body([{"date": "2026-08-28"}])) as source,
        pytest.raises(EastmoneyDailySourceError, match="must be text"),
    ):
        source.fetch(
            instrument_id=InstrumentId("300059.SZ"),
            start=START,
            end=END,
            price_basis=PriceBasis.UNADJUSTED,
        )


def test_duplicate_and_out_of_range_dates_fail_closed() -> None:
    with (
        _source(_body([_row(), _row()])) as source,
        pytest.raises(EastmoneyDailySourceError, match="duplicate trade dates"),
    ):
        source.fetch(
            instrument_id=InstrumentId("300059.SZ"),
            start=START,
            end=END,
            price_basis=PriceBasis.UNADJUSTED,
        )
    with (
        _source(_body([_row("2026-08-26")])) as source,
        pytest.raises(EastmoneyDailySourceError, match="outside the requested range"),
    ):
        source.fetch(
            instrument_id=InstrumentId("300059.SZ"),
            start=START,
            end=END,
            price_basis=PriceBasis.UNADJUSTED,
        )


def test_current_session_is_rejected_until_the_1605_stability_marker() -> None:
    started = datetime(2026, 8, 28, 16, 4, 58, tzinfo=SHANGHAI)
    received = datetime(2026, 8, 28, 16, 4, 59, tzinfo=SHANGHAI)
    with (
        _source(_body([_row()]), clock=_clock(started, received)) as source,
        pytest.raises(EastmoneyDailySourceError, match="stability marker"),
    ):
        source.fetch(
            instrument_id=InstrumentId("300059.SZ"),
            start=START,
            end=END,
            price_basis=PriceBasis.UNADJUSTED,
        )


def test_historical_row_does_not_inherit_the_current_session_marker() -> None:
    started = datetime(2026, 8, 29, 10, 0, tzinfo=SHANGHAI)
    received = datetime(2026, 8, 29, 10, 0, 1, tzinfo=SHANGHAI)
    with _source(_body([_row()]), clock=_clock(started, received)) as source:
        result = source.fetch(
            instrument_id=InstrumentId("300059.SZ"),
            start=START,
            end=END,
            price_basis=PriceBasis.UNADJUSTED,
        )

    assert result.rows[0].date == END
    assert result.retrieved_at == received


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (_row(open_="0"), "OHLC prices must be positive"),
        (_row(close="-1"), "OHLC prices must be positive"),
        (_row(high="24.99"), "OHLC values are inconsistent"),
        (_row(low="25.01"), "OHLC values are inconsistent"),
        (_row(volume_lots="1.5"), "whole lots"),
        (_row(volume_lots="-1"), "whole lots"),
        (_row(amount="-1"), "turnover must be non-negative"),
        (_row(volume_lots="0", amount="1"), "zero volume must have zero turnover"),
        (_row(volume_lots="1", amount="1"), "volume and turnover units are inconsistent"),
        (_row(amplitude="-0.1"), "amplitude must be non-negative"),
        (_row(turnover_rate="-0.1"), "turnover rate must be non-negative"),
        (_row(change_pct="NaN"), "must be finite"),
    ],
)
def test_invalid_price_liquidity_or_numeric_fields_fail_closed(
    row: str,
    message: str,
) -> None:
    with (
        _source(_body([row])) as source,
        pytest.raises(EastmoneyDailySourceError, match=message),
    ):
        source.fetch(
            instrument_id=InstrumentId("300059.SZ"),
            start=START,
            end=END,
            price_basis=PriceBasis.UNADJUSTED,
        )


def test_future_row_clock_and_http_failures_fail_closed() -> None:
    received_before_row = datetime(2026, 8, 27, 16, 30, tzinfo=SHANGHAI)
    with (
        _source(
            _body([_row()]),
            clock=_clock(received_before_row - timedelta(seconds=1), received_before_row),
        ) as source,
        pytest.raises(EastmoneyDailySourceError, match="later than response_received_at"),
    ):
        source.fetch(
            instrument_id=InstrumentId("300059.SZ"),
            start=START,
            end=END,
            price_basis=PriceBasis.UNADJUSTED,
        )
    with (
        _source(_body([_row()]), status_code=503) as source,
        pytest.raises(EastmoneyDailySourceError, match="daily request failed"),
    ):
        source.fetch(
            instrument_id=InstrumentId("300059.SZ"),
            start=START,
            end=END,
            price_basis=PriceBasis.UNADJUSTED,
        )


def test_transient_transport_error_retries_with_bounded_audit() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ConnectError("temporary disconnect", request=request)
        return httpx.Response(200, content=_body([_row()]), request=request)

    sleeps: list[float] = []
    second_started = REQUEST_STARTED_AT + timedelta(seconds=1)
    second_received = second_started + timedelta(seconds=1)
    with EastmoneyDailyResearchSource(
        transport=httpx.MockTransport(handler),
        clock=_clock(REQUEST_STARTED_AT, second_started, second_received),
        max_attempts=3,
        sleeper=sleeps.append,
    ) as source:
        result = source.fetch(
            instrument_id=InstrumentId("300059.SZ"),
            start=START,
            end=END,
            price_basis=PriceBasis.UNADJUSTED,
        )

    assert len(requests) == 2
    assert result.attempt_count == 2
    assert result.request_started_at == second_started
    assert result.transient_errors == ("ConnectError: temporary disconnect",)
    assert sleeps == [0.25]


def test_each_retry_attempt_reserves_a_push2_host_slot() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ConnectError("temporary disconnect", request=request)
        return httpx.Response(200, content=_body([_row()]), request=request)

    request_sleeps: list[float] = []
    retry_sleeps: list[float] = []
    second_started = REQUEST_STARTED_AT + timedelta(seconds=2)
    second_received = second_started + timedelta(seconds=1)
    request_throttle = HostThrottle(
        monotonic=lambda: 0.0,
        sleeper=request_sleeps.append,
        jitter=lambda: 0.0,
    )
    with EastmoneyDailyResearchSource(
        transport=httpx.MockTransport(handler),
        clock=_clock(REQUEST_STARTED_AT, second_started, second_received),
        max_attempts=2,
        sleeper=retry_sleeps.append,
        request_throttle=request_throttle,
    ) as source:
        result = source.fetch(
            instrument_id=InstrumentId("300059.SZ"),
            start=START,
            end=END,
            price_basis=PriceBasis.UNADJUSTED,
        )

    assert len(requests) == 2
    assert result.attempt_count == 2
    assert retry_sleeps == [0.25]
    assert request_sleeps == [EASTMONEY_MIN_REQUEST_INTERVAL_SECONDS]


def test_empty_success_response_is_retried_but_schema_drift_is_not() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        body = _body([]) if attempts == 1 else _body([_row()])
        return httpx.Response(200, content=body, request=request)

    second_started = REQUEST_STARTED_AT + timedelta(seconds=2)
    second_received = second_started + timedelta(seconds=1)
    with EastmoneyDailyResearchSource(
        transport=httpx.MockTransport(handler),
        clock=_clock(
            REQUEST_STARTED_AT,
            RESPONSE_RECEIVED_AT,
            second_started,
            second_received,
        ),
        max_attempts=3,
        sleeper=lambda _seconds: None,
    ) as source:
        result = source.fetch(
            instrument_id=InstrumentId("300059.SZ"),
            start=START,
            end=END,
            price_basis=PriceBasis.UNADJUSTED,
        )

    assert attempts == 2
    assert result.attempt_count == 2
    assert result.transient_errors == (
        "EastmoneyDailySourceError: data.klines must be a non-empty array",
    )

    malformed_attempts: list[httpx.Request] = []
    with (
        _source(_body(["schema,drift"]), malformed_attempts, max_attempts=3) as source,
        pytest.raises(EastmoneyDailySourceError, match="expected 11"),
    ):
        source.fetch(
            instrument_id=InstrumentId("300059.SZ"),
            start=START,
            end=END,
            price_basis=PriceBasis.UNADJUSTED,
        )
    assert len(malformed_attempts) == 1


def test_retry_configuration_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        EastmoneyDailyResearchSource(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request)),
            max_attempts=0,
        )


def test_adapter_clock_must_be_aware_and_monotonic() -> None:
    naive = datetime(2026, 8, 29, 16, 30)
    with (
        _source(_body([_row()]), clock=_clock(naive)) as source,
        pytest.raises(ValueError, match="explicit timezone"),
    ):
        source.fetch(
            instrument_id=InstrumentId("300059.SZ"),
            start=START,
            end=END,
            price_basis=PriceBasis.UNADJUSTED,
        )
    with (
        _source(
            _body([_row()]),
            clock=_clock(RESPONSE_RECEIVED_AT, RESPONSE_RECEIVED_AT - timedelta(seconds=1)),
        ) as source,
        pytest.raises(EastmoneyDailySourceError, match="precedes request_started_at"),
    ):
        source.fetch(
            instrument_id=InstrumentId("300059.SZ"),
            start=START,
            end=END,
            price_basis=PriceBasis.UNADJUSTED,
        )


@pytest.mark.parametrize("body", [b"not-json", b"[]", b'{"rc":0,"data":null}'])
def test_malformed_payload_fails_closed(body: bytes) -> None:
    with _source(body) as source, pytest.raises(EastmoneyDailySourceError):
        source.fetch(
            instrument_id=InstrumentId("300059.SZ"),
            start=START,
            end=END,
            price_basis=PriceBasis.UNADJUSTED,
        )


def test_raw_payload_remains_json_serializable() -> None:
    with _source(_body([_row()])) as source:
        result = source.fetch(
            instrument_id=InstrumentId("300059.SZ"),
            start=START,
            end=END,
            price_basis=PriceBasis.UNADJUSTED,
        )

    decoded = cast(dict[str, object], json.loads(json.dumps(result.raw_payload)))
    assert decoded["rc"] == 0
