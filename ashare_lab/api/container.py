"""Explicit dependency container for the HTTP edge."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol, cast

from fastapi import Request

from ashare_lab.application.backtest_submission import BacktestRunConfig
from ashare_lab.application.compile_strategy import StrategyCompiler
from ashare_lab.domain.catalog import CatalogSnapshot, CoverageCatalogSnapshot
from ashare_lab.domain.strategy import StrategySpec
from ashare_lab.ports.backtest_runs import BacktestRunStore, CreateRunResult

from .store import InMemoryDraftStore


class BacktestSubmitter(Protocol):
    def submit(self, strategy: StrategySpec, config: BacktestRunConfig) -> CreateRunResult: ...


@dataclass(frozen=True, slots=True)
class ApiContainer:
    compiler: StrategyCompiler
    catalog: CatalogSnapshot
    coverage_catalog: CoverageCatalogSnapshot
    drafts: InMemoryDraftStore
    service_version: str
    max_body_bytes: int
    event_backtest_probe: Callable[[], bool]
    event_backtest_codes_probe: Callable[[], frozenset[str]]
    event_preparable_codes_probe: Callable[[], frozenset[str]]
    event_document_text_backtest_codes_probe: Callable[[], frozenset[str]]
    event_document_text_preparable_codes_probe: Callable[[], frozenset[str]]
    backtest_submission: BacktestSubmitter | None = None
    run_store: BacktestRunStore | None = None
    readiness_probe: Callable[[], Mapping[str, bool]] | None = None

    @property
    def backtest_execution_available(self) -> bool:
        return self.backtest_submission is not None and self.run_store is not None

    @property
    def event_backtest_available(self) -> bool:
        return (
            self.backtest_execution_available
            and self.event_backtest_probe()
            and bool(self.event_backtest_codes)
        )

    @property
    def event_backtest_codes(self) -> frozenset[str]:
        """Event codes proven runnable by the currently pinned snapshot.

        The published event catalog answers a different question: whether the
        DSL/compiler knows a definition.  Runtime availability is fail-closed
        and must be backed by code-level acquisition coverage.
        """

        if not self.backtest_execution_available or not self.event_backtest_probe():
            return frozenset()
        return self.event_backtest_codes_probe()

    @property
    def event_preparable_codes(self) -> frozenset[str]:
        """Codes an on-demand runtime can prepare for a concrete request.

        This is deliberately separate from ``event_backtest_codes``: provider
        readiness cannot prove that an instrument-and-period snapshot already
        exists or is valid.
        """

        if not self.backtest_execution_available:
            return frozenset()
        return self.event_preparable_codes_probe()

    @property
    def event_preparation_available(self) -> bool:
        return bool(self.event_preparable_codes)

    @property
    def event_document_text_backtest_codes(self) -> frozenset[str]:
        """Pinned event lanes independently proven to carry complete text."""

        if not self.backtest_execution_available or not self.event_backtest_probe():
            return frozenset()
        return self.event_document_text_backtest_codes_probe() & self.event_backtest_codes

    @property
    def event_document_text_preparable_codes(self) -> frozenset[str]:
        """Event lanes whose opt-in full-document preparation path is ready."""

        if not self.backtest_execution_available:
            return frozenset()
        return self.event_document_text_preparable_codes_probe() & self.event_preparable_codes

    @property
    def event_availability_scope(
        self,
    ) -> Literal["pinned_snapshot", "request_preparation", "unavailable"]:
        if self.event_backtest_codes:
            return "pinned_snapshot"
        if self.event_preparable_codes:
            return "request_preparation"
        return "unavailable"

    def event_codes_are_available(self, event_codes: frozenset[str]) -> bool:
        return bool(event_codes) and event_codes.issubset(self.event_backtest_codes)

    def event_codes_are_preparable(self, event_codes: frozenset[str]) -> bool:
        return bool(event_codes) and event_codes.issubset(self.event_preparable_codes)


def get_container(request: Request) -> ApiContainer:
    return cast(ApiContainer, request.app.state.container)
