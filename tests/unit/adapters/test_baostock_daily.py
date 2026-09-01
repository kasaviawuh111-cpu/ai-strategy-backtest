from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import cast

import pytest

from ashare_lab.adapters.market_data.baostock_daily import (
    DAILY_FIELD_ORDER,
    DAILY_FIELDS,
    FREQUENCY,
    NORMALIZED_RESPONSE_HASH_SEMANTICS,
    BaoStockDailyCollection,
    BaoStockDailyResearchSource,
    BaoStockDailySourceError,
)
from ashare_lab.domain.market_data import PriceBasis
from ashare_lab.domain.shared import InstrumentId


class FakeResult:
    def __init__(
        self,
        fields: Sequence[object],
        rows: Sequence[Sequence[object]],
        *,
        error_code: object = "0",
        error_msg: object = "success",
        fail_after_rows: bool = False,
    ) -> None:
        self.fields = tuple(fields)
        self.error_code = error_code
        self.error_msg = error_msg
        self._rows = tuple(tuple(row) for row in rows)
        self._index = 0
        self._fail_after_rows = fail_after_rows
        self._failure_emitted = False

    def next(self) -> bool:
        if self._index < len(self._rows):
            return True
        if self._fail_after_rows and not self._failure_emitted:
            self.error_code = "10002007"
            self.error_msg = "network receive error"
            self._failure_emitted = True
        return False

    def get_row_data(self) -> Sequence[object]:
        row = self._rows[self._index]
        self._index += 1
        return row


class FakeClient:
    def __init__(
        self,
        results: dict[str, FakeResult],
        *,
        raise_on_start: str | None = None,
    ) -> None:
        self._results = results
        self._raise_on_start = raise_on_start
        self.calls: list[dict[str, str]] = []

    def query_history_k_data_plus(
        self,
        *,
        code: str,
        fields: str,
        start_date: str,
        end_date: str,
        frequency: str,
        adjustflag: str,
    ) -> FakeResult:
        self.calls.append(
            {
                "code": code,
                "fields": fields,
                "start_date": start_date,
                "end_date": end_date,
                "frequency": frequency,
                "adjustflag": adjustflag,
            }
        )
        if start_date == self._raise_on_start:
            raise ConnectionError("SDK socket disconnected")
        return self._results[start_date]


def _values(**overrides: str) -> dict[str, str]:
    values = {
        "date": "2024-06-03",
        "open": "10.00",
        "high": "12.00",
        "low": "9.50",
        "close": "11.00",
        "preclose": "9.80",
        "volume": "1000",
        "amount": "11000",
        "turn": "1.279500",
        "tradestatus": "1",
        "isST": "0",
    }
    values.update(overrides)
    return values


def _result(
    *rows: dict[str, str],
    fields: Sequence[object] = DAILY_FIELD_ORDER,
    error_code: object = "0",
    error_msg: object = "success",
    fail_after_rows: bool = False,
) -> FakeResult:
    materialized_rows: list[list[object]] = []
    for row in rows:
        materialized_rows.append([row[cast(str, field)] for field in fields])
    return FakeResult(
        fields,
        materialized_rows,
        error_code=error_code,
        error_msg=error_msg,
        fail_after_rows=fail_after_rows,
    )


def _fetch(
    result: FakeResult,
    *,
    instrument: str = "300059.SZ",
    start: date = date(2024, 6, 3),
    end: date = date(2024, 6, 3),
    price_basis: PriceBasis = PriceBasis.UNADJUSTED,
) -> BaoStockDailyCollection:
    client = FakeClient({start.isoformat(): result})
    return BaoStockDailyResearchSource(client).fetch(
        instrument_id=InstrumentId(instrument),
        start=start,
        end=end,
        price_basis=price_basis,
    )


def test_fetch_splits_at_calendar_years_and_preserves_each_query_audit() -> None:
    start = date(2023, 12, 30)
    end = date(2025, 1, 2)
    client = FakeClient(
        {
            "2023-12-30": _result(_values(date="2023-12-31")),
            "2024-01-01": _result(_values(date="2024-06-03", isST="1")),
            "2025-01-01": _result(_values(date="2025-01-02")),
        }
    )

    collection = BaoStockDailyResearchSource(client).fetch(
        instrument_id=InstrumentId("300059.SZ"),
        start=start,
        end=end,
        price_basis=PriceBasis.UNADJUSTED,
    )

    assert [call["start_date"] for call in client.calls] == [
        "2023-12-30",
        "2024-01-01",
        "2025-01-01",
    ]
    assert [call["end_date"] for call in client.calls] == [
        "2023-12-31",
        "2024-12-31",
        "2025-01-02",
    ]
    assert all(call["code"] == "sz.300059" for call in client.calls)
    assert all(call["fields"] == DAILY_FIELDS for call in client.calls)
    assert all(call["frequency"] == FREQUENCY for call in client.calls)
    assert all(call["adjustflag"] == "3" for call in client.calls)
    assert collection.instrument_id == InstrumentId("300059.SZ")
    assert collection.provider_code == "sz.300059"
    assert collection.price_basis is PriceBasis.UNADJUSTED
    assert collection.adjustflag == "3"
    assert collection.turnover_price_range_checked is True
    assert collection.source_volume_unit == "share"
    assert collection.provider_name == "baostock_python_api"
    assert collection.dataset_name == "history_k_data_plus_daily"
    assert [row.date for row in collection.rows] == [
        date(2023, 12, 31),
        date(2024, 6, 3),
        date(2025, 1, 2),
    ]
    row = collection.rows[1]
    assert row.open == Decimal("10.00")
    assert row.high == Decimal("12.00")
    assert row.low == Decimal("9.50")
    assert row.close == Decimal("11.00")
    assert row.preclose == Decimal("9.80")
    assert row.volume == 1000
    assert row.amount == Decimal("11000")
    assert row.turnover_rate_pct == Decimal("1.279500")
    assert row.trade_status == "1"
    assert row.is_st is True
    assert row.as_snapshot_row(collection.instrument_id) == {
        "stock_code": "300059.SZ",
        "date": "2024-06-03",
        "open": Decimal("10.00"),
        "high": Decimal("12.00"),
        "low": Decimal("9.50"),
        "close": Decimal("11.00"),
        "volume": 1000,
        "amount": Decimal("11000"),
        "turnover_rate_pct": Decimal("1.279500"),
        "turnover_rate_provider": "baostock_python_api",
        "turnover_rate_methodology": (
            "baostock.history_k_data_plus.turn.provider_reported_turnover_rate_pct.v1"
        ),
    }

    assert len(collection.query_audits) == 3
    for call, audit in zip(client.calls, collection.query_audits, strict=True):
        assert dict(audit.request_params) == call
        assert audit.error_code == "0"
        assert audit.row_count == 1
        assert audit.zero_result is False
        assert len(audit.normalized_response_sha256) == 64
        assert audit.hash_semantics == NORMALIZED_RESPONSE_HASH_SEMANTICS
        assert audit.as_dict()["rawWireCaptured"] is False
        assert audit.as_dict()["hashSemantics"] == (
            "sha256_of_canonical_normalized_sdk_result_not_raw_wire_bytes"
        )


def test_sh_symbol_maps_to_baostock_provider_code() -> None:
    client = FakeClient({"2024-06-03": _result(_values())})

    collection = BaoStockDailyResearchSource(client).fetch(
        instrument_id=InstrumentId("600519.SH"),
        start=date(2024, 6, 3),
        end=date(2024, 6, 3),
        price_basis=PriceBasis.UNADJUSTED,
    )

    assert collection.provider_code == "sh.600519"
    assert client.calls[0]["code"] == "sh.600519"


def test_provider_reported_turnover_rate_is_preserved_in_percent_points() -> None:
    """The provider's raw ``turn`` field must cross the adapter unchanged.

    It is explicitly not derived from volume or share-capital data: the
    fixture's turnover-rate value has no relationship to its volume/amount.
    """

    collection = _fetch(_result(_values(turn="1.279500")))

    row = collection.rows[0]
    assert row.turnover_rate_pct == Decimal("1.279500")
    assert row.as_snapshot_row(collection.instrument_id)["turnover_rate_pct"] == Decimal("1.279500")
    assert row.as_snapshot_row(collection.instrument_id)["turnover_rate_provider"] == (
        "baostock_python_api"
    )
    assert row.as_snapshot_row(collection.instrument_id)["turnover_rate_methodology"] == (
        "baostock.history_k_data_plus.turn.provider_reported_turnover_rate_pct.v1"
    )


def test_back_adjusted_uses_flag_one_without_false_amount_price_identity() -> None:
    adjusted = _values(
        open="2.00",
        high="3.00",
        low="1.00",
        close="2.50",
        preclose="2.20",
        volume="100",
        amount="25000",
    )
    client = FakeClient({"2024-06-03": _result(adjusted)})

    collection = BaoStockDailyResearchSource(client).fetch(
        instrument_id=InstrumentId("300059.SZ"),
        start=date(2024, 6, 3),
        end=date(2024, 6, 3),
        price_basis=PriceBasis.BACK_ADJUSTED,
    )

    assert client.calls[0]["adjustflag"] == "1"
    assert collection.adjustflag == "1"
    assert collection.turnover_price_range_checked is False
    assert collection.rows[0].amount == Decimal("25000")
    assert collection.rows[0].volume == 100


def test_same_amount_price_mismatch_is_rejected_for_unadjusted_rows() -> None:
    row = _values(volume="100", amount="25000")

    with pytest.raises(BaoStockDailySourceError, match="unadjusted volume and amount"):
        _fetch(_result(row))


def test_normalized_hash_is_stable_across_field_and_row_order_and_is_not_wire_hash() -> None:
    start = date(2024, 6, 3)
    end = date(2024, 6, 4)
    first_row = _values(date="2024-06-03")
    second_row = _values(date="2024-06-04", close="10.50")
    reversed_fields = tuple(reversed(DAILY_FIELD_ORDER))
    first_client = FakeClient({start.isoformat(): _result(second_row, first_row)})
    second_client = FakeClient(
        {start.isoformat(): _result(first_row, second_row, fields=reversed_fields)}
    )

    first = BaoStockDailyResearchSource(first_client).fetch(
        instrument_id=InstrumentId("300059.SZ"),
        start=start,
        end=end,
        price_basis=PriceBasis.UNADJUSTED,
    )
    second = BaoStockDailyResearchSource(second_client).fetch(
        instrument_id=InstrumentId("300059.SZ"),
        start=start,
        end=end,
        price_basis=PriceBasis.UNADJUSTED,
    )

    assert first.query_audits[0].normalized_response_sha256 == (
        second.query_audits[0].normalized_response_sha256
    )
    audit_payload = first.query_audits[0].as_dict()
    encoded = json.dumps(audit_payload, sort_keys=True)
    assert "not_raw_wire_bytes" in encoded
    assert audit_payload["rawWireCaptured"] is False


@pytest.mark.parametrize(
    ("instrument", "message"),
    [
        ("300059", "must be canonical"),
        ("300059.sz", "must be canonical"),
        ("300059.SH", "canonical mainland"),
        ("430047.BJ", "SH and SZ"),
        ("AAPL.US", "canonical mainland"),
    ],
)
def test_symbol_must_be_canonical_sh_or_sz(instrument: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _fetch(_result(_values()), instrument=instrument)


def test_date_range_and_price_basis_are_strict() -> None:
    source = BaoStockDailyResearchSource(FakeClient({}))
    with pytest.raises(TypeError, match="date values"):
        source.fetch(
            instrument_id=InstrumentId("300059.SZ"),
            start=cast(date, datetime(2024, 6, 3)),
            end=date(2024, 6, 3),
            price_basis=PriceBasis.UNADJUSTED,
        )
    with pytest.raises(ValueError, match="start must not be later"):
        source.fetch(
            instrument_id=InstrumentId("300059.SZ"),
            start=date(2024, 6, 4),
            end=date(2024, 6, 3),
            price_basis=PriceBasis.UNADJUSTED,
        )
    with pytest.raises(ValueError, match="price_basis"):
        source.fetch(
            instrument_id=InstrumentId("300059.SZ"),
            start=date(2024, 6, 3),
            end=date(2024, 6, 3),
            price_basis=cast(PriceBasis, "unadjusted"),
        )


def test_empty_annual_result_is_preserved_as_explicit_zero_result() -> None:
    collection = _fetch(_result())

    assert collection.rows == ()
    audit = collection.query_audits[0]
    assert audit.row_count == 0
    assert audit.zero_result is True


def test_provider_failure_stops_before_later_year_and_attaches_audit() -> None:
    client = FakeClient(
        {
            "2023-12-31": FakeResult(DAILY_FIELD_ORDER, [], error_code="10001001"),
            "2024-01-01": _result(_values()),
        }
    )

    with pytest.raises(BaoStockDailySourceError, match="10001001") as caught:
        BaoStockDailyResearchSource(client).fetch(
            instrument_id=InstrumentId("300059.SZ"),
            start=date(2023, 12, 31),
            end=date(2024, 1, 1),
            price_basis=PriceBasis.UNADJUSTED,
        )

    assert len(client.calls) == 1
    assert caught.value.query_audits[0].error_code == "10001001"
    assert dict(caught.value.query_audits[0].request_params)["start_date"] == "2023-12-31"


def test_client_exception_becomes_bounded_audit_without_leaking_message() -> None:
    client = FakeClient({}, raise_on_start="2024-06-03")

    with pytest.raises(BaoStockDailySourceError, match="client_exception") as caught:
        BaoStockDailyResearchSource(client).fetch(
            instrument_id=InstrumentId("300059.SZ"),
            start=date(2024, 6, 3),
            end=date(2024, 6, 3),
            price_basis=PriceBasis.UNADJUSTED,
        )

    audit = caught.value.query_audits[0]
    assert audit.error_message == "ConnectionError"
    assert "SDK socket disconnected" not in json.dumps(audit.as_dict())
    assert audit.hash_semantics == NORMALIZED_RESPONSE_HASH_SEMANTICS


def test_iteration_failure_after_rows_is_not_mistaken_for_success() -> None:
    result = _result(_values(), fail_after_rows=True)

    with pytest.raises(BaoStockDailySourceError, match="10002007") as caught:
        _fetch(result)

    audit = caught.value.query_audits[0]
    assert audit.error_code == "10002007"
    assert audit.row_count == 1


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (
            FakeResult((*DAILY_FIELD_ORDER, "date"), []),
            "adapter_schema_error",
        ),
        (
            FakeResult(DAILY_FIELD_ORDER[:-1], []),
            "fields must exactly match",
        ),
        (
            FakeResult(DAILY_FIELD_ORDER, [[object()] * len(DAILY_FIELD_ORDER)]),
            "adapter_schema_error",
        ),
    ],
)
def test_response_field_or_row_schema_drift_fails_closed(
    result: FakeResult,
    message: str,
) -> None:
    with pytest.raises(BaoStockDailySourceError, match=message):
        _fetch(result)


def test_invalid_and_out_of_interval_dates_fail_closed() -> None:
    with pytest.raises(BaoStockDailySourceError, match="not an ISO date"):
        _fetch(_result(_values(date="2024/06/03")))
    with pytest.raises(BaoStockDailySourceError, match="outside its annual query interval"):
        _fetch(_result(_values(date="2024-06-04")))


def test_duplicate_dates_fail_closed_even_when_rows_differ() -> None:
    with pytest.raises(BaoStockDailySourceError, match="duplicate trade dates"):
        _fetch(_result(_values(), _values(close="10.50")))


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"open": "0"}, "open must be positive"),
        ({"close": "-1"}, "close must be positive"),
        ({"preclose": "NaN"}, "preclose must be finite"),
        ({"high": "10.50", "close": "11.00"}, "OHLC values are inconsistent"),
        ({"low": "10.50", "open": "10.00"}, "OHLC values are inconsistent"),
        ({"volume": "-1"}, "volume must be non-negative"),
        ({"volume": "1.5"}, "whole number of shares"),
        ({"amount": "-1"}, "amount must be non-negative"),
        ({"volume": "0", "amount": "1"}, "both be zero or both be positive"),
        ({"volume": "1", "amount": "0"}, "both be zero or both be positive"),
        ({"tradestatus": "0"}, "suspended status"),
        ({"tradestatus": "2"}, "tradestatus must be BaoStock 0 or 1"),
        ({"isST": "2"}, "isST must be BaoStock 0 or 1"),
    ],
)
def test_invalid_decimal_ohlc_liquidity_or_status_fails_closed(
    overrides: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(BaoStockDailySourceError, match=message):
        _fetch(_result(_values(**overrides)))


def test_suspended_zero_liquidity_row_is_retained_with_explicit_status() -> None:
    collection = _fetch(_result(_values(tradestatus="0", volume="0", amount="0", isST="1")))

    row = collection.rows[0]
    assert row.tradestatus == "0"
    assert row.is_st is True
    assert row.volume == 0
    assert row.amount == 0
