"""Bounded BaoStock security-name discovery.

The query is discovery only.  Every returned canonical symbol must still be
reopened through the exact security-master resolver before it can be used by a
Strategy v2 plan.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Protocol

_PROVIDER_CODE = re.compile(r"^(sh|sz)\.([0-9]{6})$", re.IGNORECASE)
_REQUIRED_FIELDS = frozenset({"code", "code_name", "ipoDate", "outDate", "type", "status"})
_DISCOVERABLE_TYPES = frozenset({"1", "2", "5"})  # STOCK, INDEX, ETF


class BaoStockSecuritySearchError(RuntimeError):
    """Base failure for BaoStock candidate discovery."""


class BaoStockSecuritySearchUnavailableError(BaoStockSecuritySearchError):
    """BaoStock could not answer the bounded candidate query."""


class BaoStockSecuritySearchIntegrityError(BaoStockSecuritySearchError):
    """BaoStock returned a successful but structurally unsafe response."""


class BaoStockSecurityCandidates(tuple[str, ...]):
    """Canonical candidates plus a non-authoritative truncation marker."""

    truncated: bool

    def __new__(
        cls,
        values: Sequence[str],
        *,
        truncated: bool,
    ) -> BaoStockSecurityCandidates:
        instance = super().__new__(cls, values)
        instance.truncated = truncated
        return instance


class _BaoStockSearchResult(Protocol):
    fields: Sequence[object]
    error_code: object
    error_msg: object

    def next(self) -> bool: ...

    def get_row_data(self) -> Sequence[object]: ...


class BaoStockSecuritySearchClient(Protocol):
    def query_stock_basic(
        self,
        *,
        code: str = "",
        code_name: str = "",
    ) -> _BaoStockSearchResult: ...


class BaoStockSecurityCandidateSearch:
    """Return a bounded set of canonical SH/SZ discovery candidates.

    BaoStock name queries may match many funds.  We retain only the first
    bounded set so the caller can return an ambiguity/ask-for-code response;
    truncation must never turn a fuzzy name into an executable identity.
    """

    def __init__(
        self,
        client: BaoStockSecuritySearchClient,
        *,
        max_candidates: int = 20,
    ) -> None:
        if not 2 <= max_candidates <= 100:
            raise ValueError("max_candidates must be between 2 and 100")
        self._client = client
        self._max_candidates = max_candidates

    def __call__(self, identifier: str) -> tuple[str, ...]:
        raw = identifier.strip()
        if not raw or len(raw) > 128:
            raise BaoStockSecuritySearchIntegrityError(
                "BaoStock security search identifier is invalid"
            )
        try:
            result = self._client.query_stock_basic(code="", code_name=raw)
        except Exception as error:
            raise BaoStockSecuritySearchUnavailableError(
                "BaoStock security search request failed"
            ) from error
        if str(result.error_code) != "0":
            raise BaoStockSecuritySearchUnavailableError("BaoStock security search request failed")
        fields = tuple(str(value) for value in result.fields)
        if len(fields) != len(set(fields)) or not _REQUIRED_FIELDS.issubset(fields):
            raise BaoStockSecuritySearchIntegrityError(
                "BaoStock security search response fields are invalid"
            )
        positions = {name: fields.index(name) for name in _REQUIRED_FIELDS}
        candidates: list[str] = []
        seen: set[str] = set()
        truncated = False
        try:
            while result.next():
                row = tuple(str(value) for value in result.get_row_data())
                if len(row) != len(fields):
                    raise BaoStockSecuritySearchIntegrityError(
                        "BaoStock security search row width is invalid"
                    )
                provider_code = row[positions["code"]]
                asset_type = row[positions["type"]]
                match = _PROVIDER_CODE.fullmatch(provider_code)
                if match is None or asset_type not in _DISCOVERABLE_TYPES:
                    continue
                suffix = "SH" if match.group(1).lower() == "sh" else "SZ"
                canonical = f"{match.group(2)}.{suffix}"
                if canonical in seen:
                    continue
                seen.add(canonical)
                if len(candidates) < self._max_candidates:
                    candidates.append(canonical)
                else:
                    truncated = True
        except BaoStockSecuritySearchIntegrityError:
            raise
        except Exception as error:
            raise BaoStockSecuritySearchUnavailableError(
                "BaoStock security search stream failed"
            ) from error
        if str(result.error_code) != "0":
            raise BaoStockSecuritySearchUnavailableError("BaoStock security search stream failed")
        return BaoStockSecurityCandidates(
            tuple(sorted(candidates)),
            truncated=truncated,
        )


__all__ = [
    "BaoStockSecurityCandidateSearch",
    "BaoStockSecurityCandidates",
    "BaoStockSecuritySearchClient",
    "BaoStockSecuritySearchError",
    "BaoStockSecuritySearchIntegrityError",
    "BaoStockSecuritySearchUnavailableError",
]
