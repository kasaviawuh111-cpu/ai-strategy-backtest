"""Choice-first, Eastmoney-fallback security candidate discovery.

Both providers return discovery candidates only.  A caller must still confirm
every candidate against an authoritative security master before execution.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import cast

import httpx

from .baostock_security_search import BaoStockSecuritySearchUnavailableError
from .choice_security_search import ChoiceSecuritySearchUnavailableError

_ENDPOINTS = (
    "https://searchapi.eastmoney.com/api/suggest/get",
    "https://searchadapter.eastmoney.com/api/suggest/get",
)
_TOKEN = "D43BF722C8E33BDC906FB84D85E326E8"


class EastmoneySecuritySearchError(RuntimeError):
    """Base failure for Eastmoney candidate discovery."""


class EastmoneySecuritySearchUnavailableError(EastmoneySecuritySearchError):
    """All approved Eastmoney search endpoints were unavailable."""


class EastmoneySecuritySearchIntegrityError(EastmoneySecuritySearchError):
    """A reachable endpoint returned an unsafe response shape."""


class EastmoneySecurityCandidateSearch:
    """Parse only SH/SZ canonical candidates from Eastmoney suggestions."""

    def __init__(
        self,
        request_json: Callable[[str, Mapping[str, str]], object] | None = None,
    ) -> None:
        self._request_json = request_json or _request_json

    def __call__(self, identifier: str) -> tuple[str, ...]:
        raw = identifier.strip()
        if not raw or len(raw) > 128:
            raise EastmoneySecuritySearchIntegrityError(
                "Eastmoney security search identifier is invalid"
            )
        params = {
            "input": raw,
            "type": "14",
            "token": _TOKEN,
            "count": "20",
        }
        last_network_error: Exception | None = None
        payload: object | None = None
        for endpoint in _ENDPOINTS:
            try:
                payload = self._request_json(endpoint, params)
            except (httpx.HTTPError, OSError, TimeoutError) as error:
                last_network_error = error
                continue
            break
        if payload is None:
            raise EastmoneySecuritySearchUnavailableError(
                "Eastmoney security search endpoints are unavailable"
            ) from last_network_error
        if not isinstance(payload, Mapping):
            raise EastmoneySecuritySearchIntegrityError(
                "Eastmoney security search response must be an object"
            )
        table = cast(Mapping[object, object], payload).get("QuotationCodeTable")
        if not isinstance(table, Mapping):
            raise EastmoneySecuritySearchIntegrityError(
                "Eastmoney security search response has no quotation table"
            )
        rows = cast(Mapping[object, object], table).get("Data")
        if not isinstance(rows, list):
            raise EastmoneySecuritySearchIntegrityError(
                "Eastmoney security search response has invalid candidate rows"
            )
        candidates: set[str] = set()
        for value in cast(list[object], rows):
            if not isinstance(value, Mapping):
                raise EastmoneySecuritySearchIntegrityError(
                    "Eastmoney security search candidate must be an object"
                )
            row = cast(Mapping[object, object], value)
            code = row.get("Code")
            market = row.get("MktNum")
            quote_id = row.get("QuoteID")
            if not isinstance(code, str) or len(code) != 6 or not code.isdigit():
                continue
            normalized_market = str(market)
            if isinstance(quote_id, str) and "." in quote_id:
                normalized_market = quote_id.split(".", maxsplit=1)[0]
            suffix = {"1": "SH", "0": "SZ"}.get(normalized_market)
            if suffix is not None:
                candidates.add(f"{code}.{suffix}")
        return tuple(sorted(candidates))


class ChoiceFirstSecurityCandidateSearch:
    """Use Eastmoney only for explicit Choice availability failures."""

    def __init__(
        self,
        *,
        choice: Callable[[str], tuple[str, ...]],
        eastmoney: Callable[[str], tuple[str, ...]],
    ) -> None:
        self._choice = choice
        self._eastmoney = eastmoney

    def __call__(self, identifier: str) -> tuple[str, ...]:
        try:
            return self._choice(identifier)
        except ChoiceSecuritySearchUnavailableError:
            return self._eastmoney(identifier)


class ChoiceBaoStockEastmoneyCandidateSearch:
    """Use three bounded discovery sources without weakening integrity gates.

    Availability failures and true empty results may advance to the next
    provider.  A successful but malformed response is never hidden by a
    fallback.  Returned symbols still have no execution authority until the
    caller reopens each one through exact BaoStock security-master resolution.
    """

    def __init__(
        self,
        *,
        choice: Callable[[str], tuple[str, ...]],
        baostock: Callable[[str], tuple[str, ...]],
        eastmoney: Callable[[str], tuple[str, ...]],
    ) -> None:
        self._choice = choice
        self._baostock = baostock
        self._eastmoney = eastmoney

    def __call__(self, identifier: str) -> tuple[str, ...]:
        try:
            choice_candidates = self._choice(identifier)
        except ChoiceSecuritySearchUnavailableError:
            choice_candidates = ()
        if choice_candidates:
            return choice_candidates
        try:
            baostock_candidates = self._baostock(identifier)
        except BaoStockSecuritySearchUnavailableError:
            baostock_candidates = ()
        if baostock_candidates:
            return baostock_candidates
        return self._eastmoney(identifier)


def _request_json(endpoint: str, params: Mapping[str, str]) -> object:
    with httpx.Client(
        timeout=8,
        follow_redirects=False,
        headers={
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://quote.eastmoney.com/",
        },
    ) as client:
        response = client.get(endpoint, params=params)
        response.raise_for_status()
        return response.json()


__all__ = [
    "ChoiceBaoStockEastmoneyCandidateSearch",
    "ChoiceFirstSecurityCandidateSearch",
    "EastmoneySecurityCandidateSearch",
    "EastmoneySecuritySearchError",
    "EastmoneySecuritySearchIntegrityError",
    "EastmoneySecuritySearchUnavailableError",
]
