from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest

from ashare_lab.adapters.market_data.eastmoney_minute import (
    EastmoneyMinuteResearchSource,
    EastmoneyMinuteSourceError,
    calibrate_session_timestamp_semantics,
    decode_minute_rows,
)

WIRE = "2025-01-02 09:31,10,10.1,10.2,9.9,123,123456,10.04"


def test_minute_units_keep_prices_and_lots_separate():
    rows = decode_minute_rows([WIRE, WIRE], received_at=datetime(2025, 1, 3, tzinfo=UTC))
    assert len(rows) == 1
    assert rows[0].open == Decimal("10")
    assert rows[0].volume_shares == 12300
    assert str(rows[0].timestamp) == "2025-01-02 09:31:00+08:00"


@pytest.mark.parametrize("wire", [WIRE.replace("10.2", "9.5"),
                                  WIRE.replace("09:31", "12:00"),
                                  WIRE.replace("2025-01-02", "2025-01-04"),
                                  WIRE.replace("10.1", "NaN"), "broken"])
def test_invalid_rows_are_not_promoted_to_prices(wire):
    with pytest.raises(EastmoneyMinuteSourceError, match="不一致"):
        decode_minute_rows([wire], received_at=datetime(2025, 1, 6, tzinfo=UTC))


def test_forming_bar_is_excluded_but_far_future_is_rejected():
    received = datetime(2025, 1, 2, 1, 31, 30, tzinfo=UTC)  # 09:31:30 Asia/Shanghai
    forming = "2025-01-02 09:32,10,10.1,10.2,9.9,123,123456,10.04"
    rows = decode_minute_rows([WIRE, forming], received_at=received, period_minutes=1)
    assert [row.timestamp.strftime("%H:%M") for row in rows] == ["09:31"]
    with pytest.raises(EastmoneyMinuteSourceError, match="不一致"):
        decode_minute_rows([WIRE, forming.replace("09:32", "09:35")],
                           received_at=received, period_minutes=1)


def test_complete_session_calibration_requires_an_exact_endpoint_grid():
    session = date(2025, 1, 2)
    start_rows = decode_minute_rows(
        [f"{stamp.strftime('%Y-%m-%d %H:%M')},10,10,10,10,1,1000,10"
         for stamp in _session_grid(session, period_minutes=1, semantics="bar_start")],
        received_at=datetime(2025, 1, 3, tzinfo=UTC),
    )
    end_rows = decode_minute_rows(
        [f"{stamp.strftime('%Y-%m-%d %H:%M')},10,10,10,10,1,1000,10"
         for stamp in _session_grid(session, period_minutes=5, semantics="bar_end")],
        received_at=datetime(2025, 1, 3, tzinfo=UTC), period_minutes=5,
    )
    assert calibrate_session_timestamp_semantics(
        start_rows, session=session, period_minutes=1,
    )["semantics"] == "bar_start"
    assert calibrate_session_timestamp_semantics(
        end_rows, session=session, period_minutes=5,
    )["semantics"] == "bar_end"
    incomplete = calibrate_session_timestamp_semantics(
        start_rows[:-1], session=session, period_minutes=1,
    )
    assert incomplete["status"] == "unverified"
    assert incomplete["reason"] == "not_a_complete_normal_session_grid"


def _session_grid(session: date, *, period_minutes: int, semantics: str):
    from ashare_lab.adapters.market_data.eastmoney_minute import _normal_session_grid
    return _normal_session_grid(session, period_minutes=period_minutes, semantics=semantics)  # type: ignore[arg-type]


def test_acquisition_retries_connection_then_binds_symbol_and_recent_window():
    requests = []

    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ConnectError("unavailable", request=request)
        return httpx.Response(200, json={"rc": 0, "data": {
            "code": "300059", "market": 0, "trends": [WIRE],
        }})

    with EastmoneyMinuteResearchSource(transport=httpx.MockTransport(handle)) as source:
        result = source.fetch(instrument_id="300059.SZ", start=date(2025, 1, 2),
                              end=date(2025, 1, 2), period=1)
    assert result.attempts == 2
    assert requests[0].url.params["ndays"] == "5"
    assert result.manifest()["coverage"]["complete_requested_range_verified"] is False
    assert result.response_sha256.startswith("sha256:")


def test_unavailable_history_is_not_empty_success_or_retried_as_network():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"rc": 0, "data": {
            "code": "300059", "market": 0, "trends": [WIRE],
        }})

    with (
        EastmoneyMinuteResearchSource(transport=httpx.MockTransport(handle)) as source,
        pytest.raises(EastmoneyMinuteSourceError) as error,
    ):
        source.fetch(instrument_id="300059.SZ", start=date(2024, 1, 2),
                     end=date(2024, 1, 2), period=1)
    assert error.value.code == "range_unavailable"
    assert len(calls) == 1


def test_permission_denial_is_not_retried_or_disguised_as_network():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(403)

    with (
        EastmoneyMinuteResearchSource(transport=httpx.MockTransport(handle)) as source,
        pytest.raises(EastmoneyMinuteSourceError) as error,
    ):
        source.fetch(instrument_id="300059.SZ", start=date(2025, 1, 2),
                     end=date(2025, 1, 2))
    assert error.value.code == "access_denied"
    assert len(calls) == 1
