from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from ashare_lab.adapters.market_data.baostock_reference import (
    BaoStockListingStatus,
    BaoStockReferenceAdapter,
    BaoStockReferenceError,
    to_choice_snapshot_payload,
)
from ashare_lab.domain.market_data import Board, CorporateActionKind, TimeQuality

BASIC_FIELDS = ("code", "code_name", "ipoDate", "outDate", "type", "status")
DIVIDEND_FIELDS = (
    "code",
    "dividPreNoticeDate",
    "dividAgmPumDate",
    "dividPlanAnnounceDate",
    "dividPlanDate",
    "dividRegistDate",
    "dividOperateDate",
    "dividPayDate",
    "dividStockMarketDate",
    "dividCashPsBeforeTax",
    "dividCashPsAfterTax",
    "dividStocksPs",
    "dividCashStock",
    "dividReserveToStockPs",
)
HISTORY_FIELDS = ("date", "preclose", "tradestatus", "isST")
CAPTURED_AT = datetime(2024, 6, 1, 8, tzinfo=UTC)


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
        *,
        basic: FakeResult,
        dividends: dict[str, FakeResult],
        histories: dict[str, FakeResult],
    ) -> None:
        self._basic = basic
        self._dividends = dividends
        self._histories = histories
        self.calls: list[tuple[object, ...]] = []

    def query_stock_basic(self, *, code: str) -> FakeResult:
        self.calls.append(("query_stock_basic", code, None))
        return self._basic

    def query_dividend_data(
        self,
        *,
        code: str,
        year: str,
        yearType: str,
    ) -> FakeResult:
        self.calls.append(("query_dividend_data", year, yearType))
        return self._dividends[year]

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
            (
                "query_history_k_data_plus",
                code,
                fields,
                start_date,
                end_date,
                frequency,
                adjustflag,
            )
        )
        return self._histories.get(start_date[:4], FakeResult(HISTORY_FIELDS, []))


def _basic_result(
    *,
    code: str = "sz.300059",
    name: str = "东方财富",
    ipo_date: str = "2010-03-19",
    out_date: str = "",
    stock_type: str = "1",
    status: str = "1",
    fields: Sequence[str] = BASIC_FIELDS,
) -> FakeResult:
    values = {
        "code": code,
        "code_name": name,
        "ipoDate": ipo_date,
        "outDate": out_date,
        "type": stock_type,
        "status": status,
    }
    return FakeResult(fields, [[values[field] for field in fields]])


def _dividend_row(**overrides: str) -> list[str]:
    values = {
        "code": "sz.300059",
        "dividPreNoticeDate": "2024-03-15",
        "dividAgmPumDate": "2024-04-20",
        "dividPlanAnnounceDate": "2024-03-15",
        "dividPlanDate": "2024-04-26",
        "dividRegistDate": "2024-05-09",
        "dividOperateDate": "2024-05-10",
        "dividPayDate": "2024-05-10",
        "dividStockMarketDate": "2024-05-10",
        "dividCashPsBeforeTax": "0.04",
        "dividCashPsAfterTax": "0.036",
        "dividStocksPs": "0.2",
        "dividCashStock": "每10股派0.4元送2股转增1股",
        "dividReserveToStockPs": "0.1",
    }
    values.update(overrides)
    return [values[field] for field in DIVIDEND_FIELDS]


def _history_row(**overrides: str) -> list[str]:
    values = {
        "date": "2024-01-02",
        "preclose": "25.8200",
        "tradestatus": "1",
        "isST": "0",
    }
    values.update(overrides)
    return [values[field] for field in HISTORY_FIELDS]


def _client(
    *,
    basic: FakeResult | None = None,
    dividends: dict[str, FakeResult] | None = None,
    histories: dict[str, FakeResult] | None = None,
) -> FakeClient:
    return FakeClient(
        basic=basic or _basic_result(),
        dividends=dividends or {"2024": FakeResult(DIVIDEND_FIELDS, [_dividend_row()])},
        histories=histories or {"2024": FakeResult(HISTORY_FIELDS, [_history_row()])},
    )


def test_prepares_metadata_cash_and_share_actions_with_auditable_coverage() -> None:
    client = _client()

    result = BaoStockReferenceAdapter(client).prepare(
        symbol="300059.SZ",
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        captured_at=CAPTURED_AT,
    )

    assert result.instrument.name == "东方财富"
    assert result.instrument.listing_date == date(2010, 3, 19)
    assert result.instrument.delisting_date is None
    assert result.instrument.status is BaoStockListingStatus.LISTED
    assert result.instrument.board is Board.CHINEXT
    assert client.calls == [
        ("query_stock_basic", "sz.300059", None),
        ("query_dividend_data", "2024", "operate"),
        (
            "query_history_k_data_plus",
            "sz.300059",
            "date,preclose,tradestatus,isST",
            "2024-01-01",
            "2024-12-31",
            "d",
            "3",
        ),
    ]

    cash, shares = result.corporate_actions
    assert cash.action_type is CorporateActionKind.CASH_DIVIDEND
    assert cash.gross_cash_per_share == Decimal("0.04")
    assert cash.cash_pay_date == date(2024, 5, 10)
    assert shares.action_type is CorporateActionKind.SHARE_DISTRIBUTION
    assert shares.share_multiplier == Decimal("1.3")
    assert shares.share_credit_date == shares.share_sellable_date == date(2024, 5, 10)
    for action in result.corporate_actions:
        assert action.time_quality is TimeQuality.DATE_ONLY_CONSERVATIVE
        assert action.source_released_at is not None
        assert action.source_released_at.isoformat() == "2024-04-26T15:00:00+08:00"
        assert action.vendor_first_available_at is None
        assert action.replay_available_at == action.source_released_at
        assert action.raw_response_sha256 == result.query_audits[1].normalized_response_sha256

    assert result.coverage["status"] == "complete"
    assert result.coverage["rowCount"] == 2
    assert result.coverage["zeroResult"] is False
    assert result.coverage["supportedCategories"] == [
        "cash_dividend",
        "share_distribution",
    ]
    assert result.coverage["unsupportedCategories"] == [
        "rights_issue",
        "stock_split",
        "reverse_split",
    ]
    assert result.coverage["hashSemantics"] == (
        "sha256_of_canonical_normalized_results_not_raw_wire_bytes"
    )
    assert result.historical_sessions[0].session_date == date(2024, 1, 2)
    assert result.historical_sessions[0].previous_close == Decimal("25.8200")
    assert result.historical_sessions[0].trade_status == "1"
    assert result.historical_sessions[0].is_st is False
    assert result.historical_session_coverage["status"] == "complete"
    assert result.historical_session_coverage["rowCount"] == 1
    assert result.historical_session_coverage["frequency"] == "d"
    assert result.historical_session_coverage["adjustFlag"] == "3"
    assert result.historical_session_coverage["paginationPolicy"] == (
        "annual_queries_bounded_to_at_most_366_calendar_days"
    )

    payload = to_choice_snapshot_payload(result)
    assert payload["schemaVersion"] == "baostock.internal-demo-reference.v2"
    assert payload["actions"][0]["gross_cash_per_share"] == "0.04"  # type: ignore[index]
    assert payload["coverage"] == result.coverage
    assert payload["historicalSessions"]["rows"] == [  # type: ignore[index]
        {
            "date": "2024-01-02",
            "preclose": "25.8200",
            "tradestatus": "1",
            "isST": "0",
        }
    ]
    json.dumps(payload, ensure_ascii=False)


def test_after_tax_display_variants_do_not_replace_the_gross_cash_policy() -> None:
    client = _client(
        dividends={
            "2024": FakeResult(
                DIVIDEND_FIELDS,
                [_dividend_row(dividCashPsAfterTax="0.032,0.036,0.04")],
            )
        }
    )

    result = BaoStockReferenceAdapter(client).prepare(
        symbol="300059.SZ",
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        captured_at=CAPTURED_AT,
    )

    assert result.corporate_actions[0].gross_cash_per_share == Decimal("0.04")


@pytest.mark.parametrize(
    ("symbol", "provider_code", "board"),
    [
        ("600000.SH", "sh.600000", Board.MAIN),
        ("sh.688001", "sh.688001", Board.STAR),
        ("689001.SH", "sh.689001", Board.STAR),
        ("300059", "sz.300059", Board.CHINEXT),
    ],
)
def test_validates_exchange_and_derives_board(
    symbol: str,
    provider_code: str,
    board: Board,
) -> None:
    basic = _basic_result(
        code=provider_code,
        ipo_date="2000-01-01",
        name="测试股份",
    )
    client = _client(
        basic=basic,
        dividends={"2024": FakeResult(DIVIDEND_FIELDS, [])},
    )

    result = BaoStockReferenceAdapter(client).prepare(
        symbol=symbol,
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        captured_at=CAPTURED_AT,
    )

    assert result.instrument.provider_code == provider_code
    assert result.instrument.board is board


@pytest.mark.parametrize("symbol", ["200001.SZ", "900901.SH", "688001.SZ", "430047.BJ"])
def test_rejects_non_cny_stock_or_exchange_mismatch_before_query(symbol: str) -> None:
    client = _client()

    with pytest.raises(BaoStockReferenceError, match=r"supported|prefix"):
        BaoStockReferenceAdapter(client).prepare(
            symbol=symbol,
            start=date(2024, 1, 1),
            end=date(2024, 12, 31),
            captured_at=CAPTURED_AT,
        )

    assert client.calls == []


def test_requires_type_one_and_consistent_delisting_metadata() -> None:
    type_client = _client(basic=_basic_result(stock_type="2"))
    with pytest.raises(BaoStockReferenceError, match="type=1") as type_error:
        BaoStockReferenceAdapter(type_client).prepare(
            symbol="300059.SZ",
            start=date(2024, 1, 1),
            end=date(2024, 12, 31),
            captured_at=CAPTURED_AT,
        )
    assert len(type_error.value.query_audits) == 1

    delisted_client = _client(
        basic=_basic_result(
            status="0",
            out_date="2025-02-01",
        ),
        dividends={"2024": FakeResult(DIVIDEND_FIELDS, [])},
    )
    result = BaoStockReferenceAdapter(delisted_client).prepare(
        symbol="300059.SZ",
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        captured_at=CAPTURED_AT,
    )
    assert result.instrument.status is BaoStockListingStatus.DELISTED
    assert result.instrument.delisting_date == date(2025, 2, 1)


def test_all_requested_years_must_succeed_even_for_complete_zero_result() -> None:
    client = _client(
        dividends={year: FakeResult(DIVIDEND_FIELDS, []) for year in ("2022", "2023", "2024")}
    )

    result = BaoStockReferenceAdapter(client).prepare(
        symbol="300059.SZ",
        start=date(2022, 1, 1),
        end=date(2024, 12, 31),
        captured_at=CAPTURED_AT,
    )

    assert result.corporate_actions == ()
    assert result.coverage["zeroResult"] is True
    assert result.coverage["rowCount"] == 0
    assert result.coverage["sourceRowCount"] == 0
    assert result.coverage["requestedYears"] == [2022, 2023, 2024]
    assert len(result.query_audits) == 7
    assert all(audit.error_code == "0" for audit in result.query_audits)


def test_any_annual_failure_aborts_the_whole_result_with_query_audit() -> None:
    client = _client(
        dividends={
            "2023": FakeResult(DIVIDEND_FIELDS, []),
            "2024": FakeResult(
                DIVIDEND_FIELDS,
                [],
                error_code="10002007",
                error_msg="network receive error",
            ),
            "2025": FakeResult(DIVIDEND_FIELDS, []),
        }
    )

    with pytest.raises(BaoStockReferenceError, match="10002007") as caught:
        BaoStockReferenceAdapter(client).prepare(
            symbol="300059.SZ",
            start=date(2023, 1, 1),
            end=date(2025, 12, 31),
            captured_at=CAPTURED_AT,
        )

    assert [audit.method for audit in caught.value.query_audits] == [
        "query_stock_basic",
        "query_dividend_data",
        "query_dividend_data",
    ]
    assert caught.value.query_audits[-1].zero_result is True
    assert ("query_dividend_data", "2025", "operate") not in client.calls


def test_failure_after_returned_rows_is_not_misclassified_as_empty_or_complete() -> None:
    client = _client(
        dividends={
            "2024": FakeResult(
                DIVIDEND_FIELDS,
                [_dividend_row()],
                fail_after_rows=True,
            )
        }
    )

    with pytest.raises(BaoStockReferenceError, match="10002007") as caught:
        BaoStockReferenceAdapter(client).prepare(
            symbol="300059.SZ",
            start=date(2024, 1, 1),
            end=date(2024, 12, 31),
            captured_at=CAPTURED_AT,
        )

    failed = caught.value.query_audits[-1]
    assert failed.row_count == 1
    assert failed.zero_result is False
    assert failed.error_code == "10002007"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"dividPlanDate": "2024-05-10"},
            "implementation announcement date is after record date",
        ),
        ({"dividRegistDate": "2024-05-10"}, "record date must precede ex-date"),
        ({"dividPayDate": ""}, "dividPayDate is required"),
        ({"dividPayDate": "2024-05-09"}, "cash pay date cannot precede ex-date"),
        ({"dividStockMarketDate": ""}, "dividStockMarketDate is required"),
        (
            {"dividStockMarketDate": "2024-05-09"},
            "share settlement date cannot precede ex-date",
        ),
    ],
)
def test_rejects_incomplete_or_temporally_invalid_action_rows(
    overrides: dict[str, str],
    message: str,
) -> None:
    client = _client(dividends={"2024": FakeResult(DIVIDEND_FIELDS, [_dividend_row(**overrides)])})

    with pytest.raises(BaoStockReferenceError, match=message) as caught:
        BaoStockReferenceAdapter(client).prepare(
            symbol="300059.SZ",
            start=date(2024, 1, 1),
            end=date(2024, 12, 31),
            captured_at=CAPTURED_AT,
        )

    assert len(caught.value.query_audits) == 2


def test_normalized_hash_is_stable_across_field_and_row_order() -> None:
    first_row = _dividend_row(
        dividOperateDate="2024-05-10",
        dividCashPsBeforeTax="",
        dividCashPsAfterTax="",
        dividStocksPs="",
        dividReserveToStockPs="",
    )
    second_row = _dividend_row(
        dividOperateDate="2024-11-10",
        dividCashPsBeforeTax="",
        dividCashPsAfterTax="",
        dividStocksPs="",
        dividReserveToStockPs="",
    )
    reversed_fields = tuple(reversed(DIVIDEND_FIELDS))
    first_maps = [dict(zip(DIVIDEND_FIELDS, row, strict=True)) for row in (first_row, second_row)]
    reversed_rows = [[row[field] for field in reversed_fields] for row in reversed(first_maps)]
    first = BaoStockReferenceAdapter(
        _client(dividends={"2024": FakeResult(DIVIDEND_FIELDS, [first_row, second_row])})
    ).prepare(
        symbol="300059.SZ",
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        captured_at=CAPTURED_AT,
    )
    second = BaoStockReferenceAdapter(
        _client(dividends={"2024": FakeResult(reversed_fields, reversed_rows)})
    ).prepare(
        symbol="300059.SZ",
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        captured_at=CAPTURED_AT,
    )

    assert first.query_audits[1].normalized_response_sha256 == (
        second.query_audits[1].normalized_response_sha256
    )
    assert first.coverage["normalizedResponseSha256"] == second.coverage["normalizedResponseSha256"]


def test_preserves_suspended_and_st_facts_from_real_response_shape() -> None:
    client = _client(
        histories={
            "2024": FakeResult(
                HISTORY_FIELDS,
                [
                    _history_row(
                        date="2024-01-02",
                        preclose="4.6400",
                        tradestatus="0",
                        isST="1",
                    ),
                    _history_row(
                        date="2024-01-03",
                        preclose="4.6400",
                        tradestatus="1",
                        isST="1",
                    ),
                ],
            )
        }
    )

    result = BaoStockReferenceAdapter(client).prepare(
        symbol="300059.SZ",
        start=date(2024, 1, 2),
        end=date(2024, 1, 3),
        captured_at=CAPTURED_AT,
    )

    suspended, resumed = result.historical_sessions
    assert suspended.trade_status == "0"
    assert suspended.is_st is True
    assert resumed.trade_status == "1"
    assert resumed.is_st is True
    assert result.historical_session_coverage["intervals"] == [
        {
            "start": "2024-01-02",
            "end": "2024-01-03",
            "rowCount": 2,
            "zeroResult": False,
            "normalizedResponseSha256": result.query_audits[-1].normalized_response_sha256,
        }
    ]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"preclose": ""}, "preclose must be positive"),
        ({"preclose": "nan"}, "finite and non-negative"),
        ({"tradestatus": "2"}, "tradestatus must be BaoStock 0 or 1"),
        ({"isST": ""}, "isST must be BaoStock 0 or 1"),
        ({"date": "2025-01-02"}, "outside its query interval"),
    ],
)
def test_rejects_malformed_historical_session_fields(
    overrides: dict[str, str],
    message: str,
) -> None:
    client = _client(
        histories={
            "2024": FakeResult(HISTORY_FIELDS, [_history_row(**overrides)]),
        }
    )

    with pytest.raises(BaoStockReferenceError, match=message) as caught:
        BaoStockReferenceAdapter(client).prepare(
            symbol="300059.SZ",
            start=date(2024, 1, 1),
            end=date(2024, 12, 31),
            captured_at=CAPTURED_AT,
        )

    assert caught.value.query_audits[-1].method == "query_history_k_data_plus"


def test_rejects_duplicate_historical_session_dates() -> None:
    duplicate = _history_row()
    client = _client(
        histories={"2024": FakeResult(HISTORY_FIELDS, [duplicate, duplicate])},
    )

    with pytest.raises(BaoStockReferenceError, match="duplicate dates"):
        BaoStockReferenceAdapter(client).prepare(
            symbol="300059.SZ",
            start=date(2024, 1, 1),
            end=date(2024, 12, 31),
            captured_at=CAPTURED_AT,
        )


def test_historical_queries_are_bounded_by_calendar_year() -> None:
    client = _client(
        dividends={year: FakeResult(DIVIDEND_FIELDS, []) for year in ("2023", "2024", "2025")},
        histories={year: FakeResult(HISTORY_FIELDS, []) for year in ("2023", "2024", "2025")},
    )

    result = BaoStockReferenceAdapter(client).prepare(
        symbol="300059.SZ",
        start=date(2023, 6, 1),
        end=date(2025, 2, 1),
        captured_at=CAPTURED_AT,
    )

    history_calls = [call for call in client.calls if call[0] == "query_history_k_data_plus"]
    assert [(call[3], call[4]) for call in history_calls] == [
        ("2023-06-01", "2023-12-31"),
        ("2024-01-01", "2024-12-31"),
        ("2025-01-01", "2025-02-01"),
    ]
    assert result.historical_session_coverage["zeroResult"] is True


def test_history_provider_failure_aborts_with_all_query_audits() -> None:
    client = _client(
        histories={
            "2024": FakeResult(
                HISTORY_FIELDS,
                [],
                error_code="10002007",
                error_msg="network receive error",
            )
        }
    )

    with pytest.raises(BaoStockReferenceError, match="10002007") as caught:
        BaoStockReferenceAdapter(client).prepare(
            symbol="300059.SZ",
            start=date(2024, 1, 1),
            end=date(2024, 12, 31),
            captured_at=CAPTURED_AT,
        )

    assert [audit.method for audit in caught.value.query_audits] == [
        "query_stock_basic",
        "query_dividend_data",
        "query_history_k_data_plus",
    ]
