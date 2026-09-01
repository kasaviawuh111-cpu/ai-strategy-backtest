from __future__ import annotations

from collections.abc import Sequence

import pytest

from ashare_lab.adapters.market_data.baostock_security_search import (
    BaoStockSecurityCandidateSearch,
    BaoStockSecuritySearchIntegrityError,
    BaoStockSecuritySearchUnavailableError,
)

FIELDS = ("code", "code_name", "ipoDate", "outDate", "type", "status")


class _Result:
    def __init__(
        self,
        rows: Sequence[Sequence[object]],
        *,
        fields: Sequence[object] = FIELDS,
        error_code: object = "0",
    ) -> None:
        self.fields: Sequence[object] = fields
        self.error_code: object = error_code
        self.error_msg: object = "ok"
        self._rows = tuple(tuple(item) for item in rows)
        self._index = 0

    def next(self) -> bool:
        return self._index < len(self._rows)

    def get_row_data(self) -> Sequence[object]:
        row = self._rows[self._index]
        self._index += 1
        return row


class _Client:
    def __init__(self, result: _Result) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    def query_stock_basic(self, *, code: str = "", code_name: str = "") -> _Result:
        self.calls.append((code, code_name))
        return self.result


def test_baostock_name_search_is_bounded_discovery_only() -> None:
    client = _Client(
        _Result(
            (
                ("sh.510300", "沪深300ETF", "2012-05-28", "", "5", "1"),
                ("sh.000300", "沪深300", "2005-04-08", "", "2", "1"),
                ("sz.300059", "东方财富", "2010-03-19", "", "1", "1"),
                ("bj.430047", "unsupported market", "", "", "1", "1"),
                ("sh.999999", "unknown type", "", "", "9", "1"),
            )
        )
    )

    assert BaoStockSecurityCandidateSearch(client)("沪深300") == (
        "000300.SH",
        "300059.SZ",
        "510300.SH",
    )
    assert client.calls == [("", "沪深300")]


def test_baostock_search_rejects_malformed_results() -> None:
    malformed = _Client(_Result((), fields=("code", "code_name")))
    with pytest.raises(BaoStockSecuritySearchIntegrityError, match="fields"):
        BaoStockSecurityCandidateSearch(malformed)("东方财富")


def test_baostock_search_retains_bounded_candidates_for_ambiguity() -> None:
    oversized = _Client(
        _Result(tuple((f"sh.{index:06d}", "同名", "", "", "1", "1") for index in range(21)))
    )

    candidates = BaoStockSecurityCandidateSearch(oversized)("同名")

    assert len(candidates) == 20
    assert candidates[:2] == ("000000.SH", "000001.SH")
    assert candidates.truncated is True


def test_baostock_search_reports_provider_failure_without_guessing() -> None:
    client = _Client(_Result((), error_code="10002007"))

    with pytest.raises(BaoStockSecuritySearchUnavailableError, match="request failed"):
        BaoStockSecurityCandidateSearch(client)("东方财富")
