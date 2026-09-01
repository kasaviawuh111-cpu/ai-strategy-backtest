"""Bounded Choice candidate discovery for security names/local codes.

``secucode`` is used only to discover canonical candidates.  Its output never
becomes an executable identity until an independent security-master adapter
confirms symbol, name, exchange, and asset type.
"""

from __future__ import annotations

import re
from typing import Protocol

_CANONICAL_SYMBOL = re.compile(r"(?<![0-9])([0-9]{6}\.(?:SH|SZ|BJ))(?![A-Z0-9])", re.I)


class ChoiceSecuritySearchError(RuntimeError):
    """Choice candidate discovery was unavailable or malformed."""


class ChoiceSecuritySearchUnavailableError(ChoiceSecuritySearchError):
    """Choice could not be imported, authenticated, or queried."""


class ChoiceSecuritySearchIntegrityError(ChoiceSecuritySearchError):
    """Choice returned a successful but structurally unsafe response."""


class _ChoiceLoginResult(Protocol):
    ErrorCode: object


class ChoiceSecuritySearchClient(Protocol):
    def start(
        self,
        options: str = "",
        logcallback: object | None = None,
        mainCallBack: object | None = None,
    ) -> _ChoiceLoginResult: ...

    def stop(self) -> object: ...

    def secucode(
        self,
        content: str,
        typeCodes: str,
        options: str = "",
    ) -> tuple[object, object]: ...


class ChoiceSecurityCandidateSearch:
    """Return only canonical candidates from the documented ``002`` route."""

    def __init__(self, client: ChoiceSecuritySearchClient) -> None:
        self._client = client

    def __call__(self, identifier: str) -> tuple[str, ...]:
        raw = identifier.strip()
        if not raw or len(raw) > 128:
            raise ChoiceSecuritySearchError("security search identifier is invalid")
        login = self._client.start("ForceLogin=0,RecordLoginInfo=0", _quiet_log)
        if str(login.ErrorCode) != "0":
            raise ChoiceSecuritySearchUnavailableError("Choice security search login failed")
        try:
            error_code, response = self._client.secucode(f"买入{raw}", "002", "")
        finally:
            self._client.stop()
        if str(error_code) != "0":
            raise ChoiceSecuritySearchUnavailableError("Choice security search request failed")
        if not isinstance(response, str):
            raise ChoiceSecuritySearchIntegrityError("Choice security search response is invalid")
        if len(response.encode("utf-8")) > 100_000:
            raise ChoiceSecuritySearchIntegrityError("Choice security search response is too large")
        return tuple(sorted({match.upper() for match in _CANONICAL_SYMBOL.findall(response)}))


def _quiet_log(_: bytes) -> int:
    return 1


__all__ = [
    "ChoiceSecurityCandidateSearch",
    "ChoiceSecuritySearchClient",
    "ChoiceSecuritySearchError",
    "ChoiceSecuritySearchIntegrityError",
    "ChoiceSecuritySearchUnavailableError",
]
