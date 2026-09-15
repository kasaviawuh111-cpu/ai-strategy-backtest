"""Provider format boundary for historical numeric discovery.

Decoding vendor table metadata and classifying its errors belong to the adapter.
The application owns discovery policy and preserves every returned value.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any, Protocol

FinanceFieldMetadata = Mapping[str, tuple[str, frozenset[str], str | None]]


class FinanceHistoryDecoder(Protocol):
    provider_errors: tuple[type[Exception], ...]
    no_data_errors: tuple[type[Exception], ...]
    unavailable_errors: tuple[type[Exception], ...]
    invalid_data_errors: tuple[type[Exception], ...]

    def entity_codes(self, table: Mapping[str, Any]) -> set[str]: ...

    def field_metadata(self, table: Mapping[str, Any]) -> FinanceFieldMetadata: ...

    def session_date(self, value: object) -> date: ...

    def notify_data_retry(self, call_id: str, *, recovered: bool = False) -> None: ...
