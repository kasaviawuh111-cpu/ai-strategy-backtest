from __future__ import annotations

import pytest

from ashare_lab.adapters.market_data.baostock_security_search import (
    BaoStockSecuritySearchUnavailableError,
)
from ashare_lab.adapters.market_data.choice_security_search import (
    ChoiceSecurityCandidateSearch,
    ChoiceSecuritySearchError,
    ChoiceSecuritySearchIntegrityError,
    ChoiceSecuritySearchUnavailableError,
)
from ashare_lab.adapters.market_data.eastmoney_security_search import (
    ChoiceBaoStockEastmoneyCandidateSearch,
    EastmoneySecurityCandidateSearch,
    EastmoneySecuritySearchIntegrityError,
)


class _Login:
    def __init__(self, error_code: object = 0) -> None:
        self.ErrorCode = error_code


class _Client:
    def __init__(self, response: tuple[object, object]) -> None:
        self.response = response
        self.calls: list[tuple[object, ...]] = []

    def start(
        self,
        options: str = "",
        logcallback: object | None = None,
        mainCallBack: object | None = None,
    ) -> _Login:
        del logcallback, mainCallBack
        self.calls.append(("start", options))
        return _Login()

    def secucode(
        self,
        content: str,
        typeCodes: str,
        options: str = "",
    ) -> tuple[object, object]:
        self.calls.append(("secucode", content, typeCodes, options))
        return self.response

    def stop(self) -> object:
        self.calls.append(("stop",))
        return None


def test_secucode_returns_deduplicated_canonical_candidates_only() -> None:
    client = _Client(
        (
            0,
            "候选 300059.SZ; 噪声300059; 重复300059.sz; 510300.SH",
        )
    )

    result = ChoiceSecurityCandidateSearch(client)("东方财富")

    assert result == ("300059.SZ", "510300.SH")
    assert client.calls == [
        ("start", "ForceLogin=0,RecordLoginInfo=0"),
        ("secucode", "买入东方财富", "002", ""),
        ("stop",),
    ]


def test_choice_failure_is_explicit_and_session_is_closed() -> None:
    client = _Client((10001017, "provider unavailable"))

    with pytest.raises(ChoiceSecuritySearchError, match="request failed"):
        ChoiceSecurityCandidateSearch(client)("300059")

    assert client.calls[-1] == ("stop",)


def test_provider_chain_uses_choice_then_baostock_then_eastmoney() -> None:
    calls: list[str] = []

    def choice(_: str) -> tuple[str, ...]:
        calls.append("choice")
        raise ChoiceSecuritySearchUnavailableError("offline")

    def baostock(_: str) -> tuple[str, ...]:
        calls.append("baostock")
        return ("510300.SH",)

    def eastmoney(_: str) -> tuple[str, ...]:
        calls.append("eastmoney")
        return ("300059.SZ",)

    result = ChoiceBaoStockEastmoneyCandidateSearch(
        choice=choice,
        baostock=baostock,
        eastmoney=eastmoney,
    )("沪深300ETF")

    assert result == ("510300.SH",)
    assert calls == ["choice", "baostock"]


def test_provider_chain_falls_through_true_empty_and_availability_only() -> None:
    calls: list[str] = []

    def choice(_: str) -> tuple[str, ...]:
        calls.append("choice")
        return ()

    def baostock(_: str) -> tuple[str, ...]:
        calls.append("baostock")
        raise BaoStockSecuritySearchUnavailableError("offline")

    def eastmoney(_: str) -> tuple[str, ...]:
        calls.append("eastmoney")
        return ("300059.SZ",)

    result = ChoiceBaoStockEastmoneyCandidateSearch(
        choice=choice,
        baostock=baostock,
        eastmoney=eastmoney,
    )("东方财富")

    assert result == ("300059.SZ",)
    assert calls == ["choice", "baostock", "eastmoney"]


def test_provider_chain_never_hides_integrity_failure() -> None:
    def choice(_: str) -> tuple[str, ...]:
        raise ChoiceSecuritySearchIntegrityError("malformed")

    with pytest.raises(ChoiceSecuritySearchIntegrityError, match="malformed"):
        ChoiceBaoStockEastmoneyCandidateSearch(
            choice=choice,
            baostock=lambda _: ("510300.SH",),
            eastmoney=lambda _: ("300059.SZ",),
        )("东方财富")


def test_eastmoney_search_parses_only_canonical_a_share_candidates() -> None:
    calls: list[str] = []

    def request(endpoint: str, params: object) -> object:
        del params
        calls.append(endpoint)
        return {
            "QuotationCodeTable": {
                "Data": [
                    {"Code": "510300", "MktNum": "1", "QuoteID": "1.510300"},
                    {"Code": "300059", "MktNum": "0", "QuoteID": "0.300059"},
                    {"Code": "00700", "MktNum": "116", "QuoteID": "116.00700"},
                ]
            }
        }

    result = EastmoneySecurityCandidateSearch(request)("沪深300ETF")

    assert result == ("300059.SZ", "510300.SH")
    assert len(calls) == 1


def test_eastmoney_reachable_malformed_response_fails_without_endpoint_retry() -> None:
    calls: list[str] = []

    def request(endpoint: str, params: object) -> object:
        del params
        calls.append(endpoint)
        return {"unexpected": []}

    with pytest.raises(EastmoneySecuritySearchIntegrityError, match="quotation table"):
        EastmoneySecurityCandidateSearch(request)("东方财富")

    assert len(calls) == 1
