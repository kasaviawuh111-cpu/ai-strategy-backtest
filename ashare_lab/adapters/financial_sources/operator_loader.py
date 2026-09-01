"""Pin Eastmoney ``操盘必读`` valuation facts for deterministic replay."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from ashare_lab.domain.financials import (
    FIRST_FINANCIAL_METRIC_CATALOG,
    FinancialDataKind,
    FinancialMetricId,
)
from ashare_lab.domain.shared import require_aware
from ashare_lab.domain.strategy import StrategySpec, canonical_hash, iter_financial_conditions
from ashare_lab.ports.financial_data import PinnedFinancialFacts
from ashare_lab.ports.market_data import DateRange

from .eastmoney_operator import (
    OPERATOR_READING_PROVIDER,
    OPERATOR_READING_SCHEMA_VERSION,
    EastmoneyOperatorReadingSource,
    OperatorReadingBatch,
)
from .operator_normalize import normalize_valuation_facts

_EXECUTABLE_VALUATION_METRICS = frozenset(
    {
        FinancialMetricId.PE,
        FinancialMetricId.PB,
        FinancialMetricId.PS,
        FinancialMetricId.PCF,
    }
)
_SHANGHAI = ZoneInfo("Asia/Shanghai")


class OperatorFinancialDataUnavailableError(RuntimeError):
    """The direct-value provider cannot safely cover the requested replay."""


class _OperatorSource(Protocol):
    def fetch_valuation_trends(
        self,
        instrument_id: str,
        *,
        retrieved_at: datetime,
        statistics_cycle: int = 4,
    ) -> tuple[OperatorReadingBatch, ...]: ...

    def close(self) -> None: ...


class EastmoneyOperatorFinancialFactLoader:
    """Fetch direct historical valuation values; never derive statement history."""

    def __init__(
        self,
        source_factory: Callable[[], _OperatorSource] = EastmoneyOperatorReadingSource,
    ) -> None:
        self._source_factory = source_factory

    def load(
        self,
        strategy: StrategySpec,
        period: DateRange,
        *,
        retrieved_at: datetime,
    ) -> PinnedFinancialFacts:
        require_aware(retrieved_at, "retrieved_at")
        provider_retrieved_at = retrieved_at.astimezone(_SHANGHAI)
        conditions = tuple(iter_financial_conditions(strategy))
        requested = frozenset(item.metric_id for item in conditions)
        if not requested:
            raise OperatorFinancialDataUnavailableError("strategy_has_no_financial_condition")
        unsupported = requested - _EXECUTABLE_VALUATION_METRICS
        if unsupported:
            # The statement endpoints expose current/restated values without a
            # historical revision timeline.  Backdating those values would be
            # look-ahead, so this release fails closed.
            if any(
                FIRST_FINANCIAL_METRIC_CATALOG.definition_for(item).data_kind
                is FinancialDataKind.STATEMENT
                for item in unsupported
            ):
                raise OperatorFinancialDataUnavailableError(
                    "financial_statement_revision_history_unavailable"
                )
            raise OperatorFinancialDataUnavailableError(
                "financial_metric_not_available_from_operator_reading"
            )

        source = self._source_factory()
        try:
            batches = source.fetch_valuation_trends(
                strategy.instrument.symbol,
                retrieved_at=provider_retrieved_at,
                statistics_cycle=4,
            )
        finally:
            source.close()
        selected_batches = tuple(
            batch
            for batch in batches
            if FinancialMetricId(f"valuation.{_indicator_name(batch.indicator_type)}") in requested
        )
        identity_basis: dict[str, object] = {
            "instrument_id": strategy.instrument.symbol,
            "provider": OPERATOR_READING_PROVIDER,
            "schema_version": OPERATOR_READING_SCHEMA_VERSION,
            "series": [
                {
                    "canonical_rows_sha256": batch.canonical_rows_sha256,
                    "dataset": batch.dataset.value,
                    "indicator_type": batch.indicator_type,
                    "page_hashes": [item.raw_response_sha256 for item in batch.requests],
                    "statistics_cycle": batch.statistics_cycle,
                }
                for batch in selected_batches
            ],
        }
        checksum = canonical_hash(identity_basis)
        snapshot_id = "financial:" + checksum.removeprefix("sha256:")
        normalized = normalize_valuation_facts(
            selected_batches,
            snapshot_id=snapshot_id,
        )
        facts = tuple(
            fact
            for fact in normalized
            if fact.metric_id in requested
            and fact.availability.observed_at is not None
            and fact.availability.observed_at.date() <= period.end
        )
        if not facts or set(requested) != {item.metric_id for item in facts}:
            raise OperatorFinancialDataUnavailableError("financial_valuation_history_incomplete")
        observed_dates = tuple(
            item.availability.observed_at.date()
            for item in facts
            if item.availability.observed_at is not None
        )
        return PinnedFinancialFacts(
            snapshot_id=snapshot_id,
            checksum=checksum,
            provider=OPERATOR_READING_PROVIDER,
            schema_version=OPERATOR_READING_SCHEMA_VERSION,
            coverage_start=min(observed_dates),
            coverage_end=max(observed_dates),
            identity_basis=identity_basis,
            facts=facts,
        )


def _indicator_name(value: int | None) -> str:
    names: Mapping[int, str] = {1: "pe", 2: "pb", 3: "ps", 4: "pcf"}
    if value is None:
        raise OperatorFinancialDataUnavailableError("operator_valuation_indicator_identity_invalid")
    try:
        return names[value]
    except KeyError as exc:
        raise OperatorFinancialDataUnavailableError(
            "operator_valuation_indicator_identity_invalid"
        ) from exc


__all__ = [
    "EastmoneyOperatorFinancialFactLoader",
    "OperatorFinancialDataUnavailableError",
]
