"""Contracts for live, query-time market-data discovery.

These objects deliberately sit outside the historical backtest repository.
They carry an auditable response identity for a *current* provider answer, but
do not make that answer eligible for a historical strategy run.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class LiveMarketDataProvenance:
    """Non-secret identity of one provider response."""

    response_sha256: str
    retrieved_at: datetime
    schema_version: str


@dataclass(frozen=True, slots=True)
class LiveMarketDataResult:
    """A provider-normalised current screening response."""

    provider: str
    query: str
    asset_type: str
    columns: tuple[str, ...]
    rows: tuple[Mapping[str, Any], ...]
    provenance: LiveMarketDataProvenance


@dataclass(frozen=True, slots=True)
class LiveFinanceDataResult:
    """A provider-normalised current financial-data response.

    ``tables`` intentionally preserves the provider's reported fields and
    units.  It is for current research only, not a historical financial
    snapshot or a strategy input.
    """

    provider: str
    query: str
    indicators: str | None
    tables: tuple[Mapping[str, Any], ...]
    provenance: LiveMarketDataProvenance


@dataclass(frozen=True, slots=True)
class LiveSecurityEntity:
    """One provider-selected entity used only for a current lookup."""

    code: str
    name: str | None
    asset_type: str


@dataclass(frozen=True, slots=True)
class LiveScreenedFinanceDataResult:
    """Auditable result of current screening followed by batched lookup.

    The screening response and every finance response keep their own provider
    hash.  This compound result is intentionally ineligible for historical
    signals or point-in-time backtests.
    """

    screen: LiveMarketDataResult
    entities: tuple[LiveSecurityEntity, ...]
    batches: tuple[LiveFinanceDataResult, ...]


class LiveMarketData(Protocol):
    """Server-owned provider for current, all-universe security screening."""

    async def screen(self, *, query: str, asset_type: str) -> LiveMarketDataResult: ...


class LiveFinanceData(Protocol):
    """Server-owned provider for a current query of known financial data."""

    async def query_finance(
        self,
        *,
        query: str,
        indicators: str | None,
    ) -> LiveFinanceDataResult: ...


class LiveScreenedFinanceData(Protocol):
    """Current screener-to-lookup composition; never a backtest repository."""

    async def screen_then_query_finance(
        self,
        *,
        screening_query: str,
        asset_type: str,
        indicators: str,
    ) -> LiveScreenedFinanceDataResult: ...
