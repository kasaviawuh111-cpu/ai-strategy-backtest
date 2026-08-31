from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import cast
from zoneinfo import ZoneInfo

import httpx
import pytest

from ashare_lab.adapters.fund_flow import (
    DAILY_FIELD_CODES,
    EASTMONEY_FUND_FLOW_URL,
    EastmoneyFundFlowResearchSource,
    FundFlowSourceError,
)
from ashare_lab.domain.shared import InstrumentId

SHANGHAI = ZoneInfo("Asia/Shanghai")
REQUEST_STARTED_AT = datetime(2026, 8, 29, 16, 29, 59, tzinfo=SHANGHAI)
RESPONSE_RECEIVED_AT = datetime(2026, 8, 29, 16, 30, tzinfo=SHANGHAI)


def _row(
    trade_date: str = "2026-08-28",
    *,
    main: str = "100.5",
    large: str = "60",
    extra_large: str = "40",
) -> str:
    return ",".join(
        (
            trade_date,
            main,
            "-70",
            "-30",
            large,
            extra_large,
            "1.25",
            "-0.70",
            "-0.30",
            "0.60",
            "0.40",
            "25.18",
            "2.03",
        )
    )


def _body(klines: object, *, code: str = "300059", market: int = 0) -> bytes:
    return json.dumps(
        {
            "rc": 0,
            "rt": 6,
            "data": {
                "code": code,
                "market": market,
                "name": "东方财富",
                "klines": klines,
            },
        },
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
):
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        return httpx.Response(
            200,
            content=body,
            headers={"Content-Type": "application/json"},
            request=request,
        )

    return EastmoneyFundFlowResearchSource(
        transport=httpx.MockTransport(handler),
        clock=clock or _clock(REQUEST_STARTED_AT, RESPONSE_RECEIVED_AT),
    )


def test_fetch_parses_f51_through_f63_and_preserves_provenance() -> None:
    body = _body([_row("2026-08-28"), _row("2026-08-27", main="100", extra_large="40")])
    requests: list[httpx.Request] = []

    with _source(body, requests) as source:
        result = source.fetch(
            instrument_id=InstrumentId("300059.SZ"),
            limit=120,
        )

    assert len(requests) == 1
    request = requests[0]
    assert str(request.url).startswith(EASTMONEY_FUND_FLOW_URL)
    assert request.url.params["secid"] == "0.300059"
    assert request.url.params["klt"] == "101"
    assert request.url.params["lmt"] == "120"
    assert request.url.params["fields2"] == ",".join(DAILY_FIELD_CODES)
    assert result.request_started_at == REQUEST_STARTED_AT
    assert result.response_received_at == RESPONSE_RECEIVED_AT
    assert result.retrieved_at == RESPONSE_RECEIVED_AT
    assert result.raw_wire_sha256 == hashlib.sha256(body).hexdigest()
    assert result.raw_payload_canonical_sha256 == hashlib.sha256(body).hexdigest()
    assert result.provider_name == "eastmoney_push2his_public"
    assert [row.trade_date.isoformat() for row in result.rows] == [
        "2026-08-27",
        "2026-08-28",
    ]
    latest = result.rows[-1]
    assert latest.main_net_inflow_cny == Decimal("100.5")
    assert latest.large_net_inflow_cny == Decimal("60")
    assert latest.extra_large_net_inflow_cny == Decimal("40")
    assert not hasattr(latest, "policy_available_at")


@pytest.mark.parametrize("klines", [None, []])
def test_empty_or_null_rows_fail_closed(klines: object) -> None:
    with (
        _source(_body(klines)) as source,
        pytest.raises(FundFlowSourceError, match="non-empty array"),
    ):
        source.fetch(
            instrument_id=InstrumentId("300059.SZ"),
        )


def test_field_count_drift_fails_closed() -> None:
    short_row = ",".join(_row().split(",")[:-1])
    with (
        _source(_body([short_row])) as source,
        pytest.raises(FundFlowSourceError, match="expected 13 for f51-f63"),
    ):
        source.fetch(
            instrument_id=InstrumentId("300059.SZ"),
        )


def test_main_component_identity_uses_a_small_rounding_tolerance() -> None:
    with (
        _source(_body([_row(main="101.01")])) as source,
        pytest.raises(FundFlowSourceError, match=r"f52 ~= f55 \+ f56"),
    ):
        source.fetch(
            instrument_id=InstrumentId("300059.SZ"),
        )


def test_current_session_row_before_the_1605_stability_marker_fails_closed() -> None:
    started = datetime(2026, 8, 28, 16, 4, 58, tzinfo=SHANGHAI)
    received = datetime(2026, 8, 28, 16, 4, 59, tzinfo=SHANGHAI)
    with (
        _source(_body([_row()]), clock=_clock(started, received)) as source,
        pytest.raises(FundFlowSourceError, match="stability marker"),
    ):
        source.fetch(
            instrument_id=InstrumentId("300059.SZ"),
        )


def test_1605_marker_is_not_applied_as_a_historical_known_time() -> None:
    started = datetime(2026, 8, 29, 9, 59, 59, tzinfo=SHANGHAI)
    received = datetime(2026, 8, 29, 10, 0, tzinfo=SHANGHAI)
    with _source(_body([_row()]), clock=_clock(started, received)) as source:
        result = source.fetch(instrument_id=InstrumentId("300059.SZ"))

    assert result.retrieved_at == received
    assert result.rows[0].trade_date.isoformat() == "2026-08-28"


def test_response_identity_drift_is_rejected() -> None:
    with (
        _source(_body([_row()], code="600519", market=1)) as source,
        pytest.raises(FundFlowSourceError, match="stock code"),
    ):
        source.fetch(
            instrument_id=InstrumentId("300059.SZ"),
        )


def test_adapter_clock_must_be_aware_and_monotonic() -> None:
    naive = datetime(2026, 8, 29, 16, 30)
    with (
        _source(_body([_row()]), clock=_clock(naive)) as source,
        pytest.raises(ValueError, match="explicit timezone"),
    ):
        source.fetch(instrument_id=InstrumentId("300059.SZ"))

    with (
        _source(
            _body([_row()]),
            clock=_clock(RESPONSE_RECEIVED_AT, RESPONSE_RECEIVED_AT - timedelta(seconds=1)),
        ) as source,
        pytest.raises(FundFlowSourceError, match="precedes request_started_at"),
    ):
        source.fetch(instrument_id=InstrumentId("300059.SZ"))


def test_raw_payload_remains_json_serializable() -> None:
    body = _body([_row()])
    with _source(body) as source:
        result = source.fetch(
            instrument_id=InstrumentId("300059.SZ"),
        )
    decoded = cast(dict[str, object], json.loads(json.dumps(result.raw_payload)))
    assert decoded["rc"] == 0
