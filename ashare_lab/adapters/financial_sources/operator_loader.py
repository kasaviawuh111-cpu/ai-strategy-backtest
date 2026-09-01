"""Pin Eastmoney ``操盘必读`` valuation facts for deterministic replay."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from ashare_lab.adapters.event_sources.collector import EventFetchBatch
from ashare_lab.adapters.event_sources.eastmoney import EastmoneyAnnouncementSource
from ashare_lab.domain.financials import (
    FinancialMetricId,
    FinancialPublicationEvidence,
)
from ashare_lab.domain.shared import InstrumentId, require_aware
from ashare_lab.domain.strategy import StrategySpec, canonical_hash, iter_financial_conditions
from ashare_lab.ports.financial_data import PinnedFinancialFacts
from ashare_lab.ports.market_data import DateRange

from .eastmoney_operator import (
    OPERATOR_READING_PROVIDER,
    OPERATOR_READING_SCHEMA_VERSION,
    EastmoneyOperatorReadingSource,
    OperatorReadingBatch,
)
from .operator_normalize import normalize_main_financial_facts, normalize_valuation_facts
from .publication_resolver import (
    FinancialPublicationResolutionError,
    resolve_financial_publication_evidence,
)

_EXECUTABLE_VALUATION_METRICS = frozenset(
    {
        FinancialMetricId.PE,
        FinancialMetricId.PB,
        FinancialMetricId.PS,
        FinancialMetricId.PCF,
    }
)
_EXECUTABLE_STATEMENT_METRICS = frozenset(
    {
        FinancialMetricId.BASIC_EPS,
        FinancialMetricId.BOOK_VALUE_PER_SHARE,
        FinancialMetricId.CAPITAL_RESERVE_PER_SHARE,
        FinancialMetricId.UNASSIGNED_PROFIT_PER_SHARE,
        FinancialMetricId.OPERATING_CASH_FLOW_PER_SHARE,
        FinancialMetricId.REVENUE,
        FinancialMetricId.REVENUE_YOY,
        FinancialMetricId.NET_PROFIT_PARENT,
        FinancialMetricId.NET_PROFIT_PARENT_YOY,
        FinancialMetricId.ROE,
        FinancialMetricId.GROSS_MARGIN,
        FinancialMetricId.NET_MARGIN,
        FinancialMetricId.OPERATING_CASH_FLOW,
        FinancialMetricId.DEDUCTED_NET_PROFIT,
    }
)
_STATEMENT_LOOKBACK_DAYS = 400
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_PERIODIC_FINANCIAL_EVENT_CODES = (
    "event.financial_results.annual_report",
    "event.financial_results.quarterly_report",
    "event.financial_results.semiannual_report",
)


class OperatorFinancialDataUnavailableError(RuntimeError):
    """The direct-value provider cannot safely cover the requested replay."""


class _OperatorSource(Protocol):
    def fetch_main_financial_data(
        self,
        instrument_id: str,
        *,
        retrieved_at: datetime,
    ) -> OperatorReadingBatch: ...

    def fetch_valuation_trends(
        self,
        instrument_id: str,
        *,
        retrieved_at: datetime,
        statistics_cycle: int = 4,
    ) -> tuple[OperatorReadingBatch, ...]: ...

    def close(self) -> None: ...


class _AnnouncementSource(Protocol):
    def fetch_batch(
        self,
        *,
        instrument_id: InstrumentId,
        start: date,
        end: date,
        retrieved_at: datetime,
        requested_event_codes: Sequence[str] | None = None,
        requested_provider_column_codes: Sequence[str] | None = None,
    ) -> EventFetchBatch: ...

    def close(self) -> None: ...


class EastmoneyOperatorFinancialFactLoader:
    """Pin direct provider facts behind an auditable financial-PIT join."""

    def __init__(
        self,
        source_factory: Callable[[], _OperatorSource] = EastmoneyOperatorReadingSource,
        announcement_source_factory: Callable[[], _AnnouncementSource] = (
            EastmoneyAnnouncementSource
        ),
    ) -> None:
        self._source_factory = source_factory
        self._announcement_source_factory = announcement_source_factory

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
        unsupported = requested - _EXECUTABLE_VALUATION_METRICS - _EXECUTABLE_STATEMENT_METRICS
        if unsupported:
            raise OperatorFinancialDataUnavailableError(
                "financial_metric_direct_provider_field_unavailable"
            )

        requested_valuations = requested & _EXECUTABLE_VALUATION_METRICS
        requested_statements = requested & _EXECUTABLE_STATEMENT_METRICS

        source = self._source_factory()
        try:
            valuation_batches = (
                source.fetch_valuation_trends(
                    strategy.instrument.symbol,
                    retrieved_at=provider_retrieved_at,
                    statistics_cycle=4,
                )
                if requested_valuations
                else ()
            )
            main_financial_batch = (
                source.fetch_main_financial_data(
                    strategy.instrument.symbol,
                    retrieved_at=provider_retrieved_at,
                )
                if requested_statements
                else None
            )
        finally:
            source.close()
        selected_valuation_batches = tuple(
            batch
            for batch in valuation_batches
            if FinancialMetricId(f"valuation.{_indicator_name(batch.indicator_type)}")
            in requested_valuations
        )
        selected_statement_batch: OperatorReadingBatch | None = None
        publication_evidence: dict[date, FinancialPublicationEvidence] = {}
        statement_exclusions: tuple[dict[str, str], ...] = ()
        if main_financial_batch is not None:
            selected_statement_batch, statement_exclusions = _select_statement_rows(
                main_financial_batch,
                period=period,
            )
            if not selected_statement_batch.rows:
                raise OperatorFinancialDataUnavailableError(
                    "financial_statement_revision_history_unavailable"
                )
            announcement_source = self._announcement_source_factory()
            try:
                announcement_batch = announcement_source.fetch_batch(
                    instrument_id=InstrumentId(strategy.instrument.symbol),
                    start=_min_notice_date(selected_statement_batch.rows),
                    end=_max_notice_date(selected_statement_batch.rows),
                    retrieved_at=provider_retrieved_at,
                    requested_event_codes=_PERIODIC_FINANCIAL_EVENT_CODES,
                )
            finally:
                announcement_source.close()
            try:
                publication_evidence = {
                    _row_date(row, "REPORT_DATE"): resolve_financial_publication_evidence(
                        instrument_id=InstrumentId(strategy.instrument.symbol),
                        f10_row=row,
                        observations=announcement_batch.observations,
                    )
                    for row in selected_statement_batch.rows
                }
            except FinancialPublicationResolutionError as exc:
                raise OperatorFinancialDataUnavailableError(str(exc)) from exc
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
                for batch in selected_valuation_batches
            ],
        }
        if selected_statement_batch is not None:
            identity_basis["statement_series"] = {
                "canonical_rows_sha256": selected_statement_batch.canonical_rows_sha256,
                "dataset": selected_statement_batch.dataset.value,
                "page_hashes": [
                    item.raw_response_sha256 for item in selected_statement_batch.requests
                ],
            }
            identity_basis["publication_bindings"] = [
                {
                    "binding_hash": evidence.binding_hash,
                    "document_sha256": evidence.document_sha256,
                    "event_code": evidence.event_code,
                    "event_provider": evidence.event_provider,
                    "event_revision_no": evidence.event_revision_no,
                    "first_available_at": evidence.first_available_at.isoformat(),
                    "notice_date": evidence.notice_date.isoformat(),
                    "provider_event_id": evidence.provider_event_id,
                    "raw_response_sha256": evidence.raw_response_sha256,
                    "report_period": evidence.report_period.isoformat(),
                    "time_quality": evidence.time_quality.value,
                    "update_date": evidence.update_date.isoformat(),
                }
                for evidence in sorted(
                    publication_evidence.values(), key=lambda item: item.report_period
                )
            ]
            identity_basis["statement_exclusions"] = list(statement_exclusions)
        checksum = canonical_hash(identity_basis)
        snapshot_id = "financial:" + checksum.removeprefix("sha256:")
        normalized = tuple(
            [
                *(
                    normalize_valuation_facts(
                        selected_valuation_batches,
                        snapshot_id=snapshot_id,
                    )
                    if selected_valuation_batches
                    else ()
                ),
                *(
                    normalize_main_financial_facts(
                        selected_statement_batch,
                        publication_evidence=publication_evidence,
                        snapshot_id=snapshot_id,
                    )
                    if selected_statement_batch is not None
                    else ()
                ),
            ]
        )
        facts = tuple(
            sorted(
                (
                    fact
                    for fact in normalized
                    if fact.metric_id in requested
                    and fact.availability.observed_at is not None
                    and fact.availability.observed_at.date() <= period.end
                ),
                key=lambda item: (
                    item.metric_id.value,
                    item.availability.observed_at,
                    item.revision_id,
                    item.source_field,
                ),
            )
        )
        if not facts or set(requested) != {item.metric_id for item in facts}:
            raise OperatorFinancialDataUnavailableError("financial_direct_history_incomplete")
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


def _select_statement_rows(
    batch: OperatorReadingBatch,
    *,
    period: DateRange,
) -> tuple[OperatorReadingBatch, tuple[dict[str, str], ...]]:
    """Keep only PIT-eligible direct rows relevant to this replay period.

    A row whose F10 ``UPDATE_DATE`` differs from ``NOTICE_DATE`` is preserved
    in snapshot provenance as an explicit exclusion.  We do not silently use
    a possibly revised current value at an old report's publication time.
    """

    earliest_notice = period.start - timedelta(days=_STATEMENT_LOOKBACK_DAYS)
    selected_indices: list[int] = []
    exclusions: list[dict[str, str]] = []
    for index, row in enumerate(batch.rows):
        notice_date = _row_date(row, "NOTICE_DATE")
        if not earliest_notice <= notice_date <= period.end:
            continue
        update_date = _row_date(row, "UPDATE_DATE")
        if update_date != notice_date:
            exclusions.append(
                {
                    "notice_date": notice_date.isoformat(),
                    "raw_response_sha256": batch.row_raw_response_sha256[index],
                    "reason": "f10_update_date_differs_from_notice_date",
                    "report_period": _row_date(row, "REPORT_DATE").isoformat(),
                    "update_date": update_date.isoformat(),
                }
            )
            continue
        selected_indices.append(index)
    selected_rows = tuple(batch.rows[index] for index in selected_indices)
    selected_hash = canonical_hash(list(selected_rows))
    return (
        OperatorReadingBatch(
            dataset=batch.dataset,
            instrument_id=batch.instrument_id,
            rows=selected_rows,
            row_raw_response_sha256=tuple(
                batch.row_raw_response_sha256[index] for index in selected_indices
            ),
            row_retrieved_at=tuple(batch.row_retrieved_at[index] for index in selected_indices),
            requests=batch.requests,
            canonical_rows_sha256=selected_hash,
            indicator_type=batch.indicator_type,
            statistics_cycle=batch.statistics_cycle,
        ),
        tuple(sorted(exclusions, key=lambda item: item["report_period"])),
    )


def _row_date(row: Mapping[str, object], field_name: str) -> date:
    raw_value = row.get(field_name)
    if type(raw_value) is not str or not raw_value:
        raise OperatorFinancialDataUnavailableError(f"{field_name} is missing from F10 row")
    try:
        return datetime.fromisoformat(raw_value).date()
    except ValueError as exc:
        raise OperatorFinancialDataUnavailableError(f"{field_name} is invalid in F10 row") from exc


def _min_notice_date(rows: Sequence[Mapping[str, object]]) -> date:
    if not rows:
        raise OperatorFinancialDataUnavailableError("no_statement_rows_selected")
    return min(_row_date(row, "NOTICE_DATE") for row in rows)


def _max_notice_date(rows: Sequence[Mapping[str, object]]) -> date:
    if not rows:
        raise OperatorFinancialDataUnavailableError("no_statement_rows_selected")
    return max(_row_date(row, "NOTICE_DATE") for row in rows)


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
