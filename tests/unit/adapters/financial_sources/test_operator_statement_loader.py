from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from ashare_lab.adapters.event_sources.collector import EventFetchBatch
from ashare_lab.adapters.financial_sources.eastmoney_operator import (
    OperatorDataset,
    OperatorReadingBatch,
    OperatorRequestAudit,
)
from ashare_lab.adapters.financial_sources.operator_loader import (
    EastmoneyOperatorFinancialFactLoader,
    OperatorFinancialDataUnavailableError,
)
from ashare_lab.domain.events import EventObservation
from ashare_lab.domain.financials import (
    FinancialMetricId,
    FinancialStatementScope,
    FinancialUnit,
)
from ashare_lab.domain.market_data import TimeQuality
from ashare_lab.domain.shared import InstrumentId
from ashare_lab.domain.strategy import (
    BacktestConfig,
    CatalogRef,
    DailyExecutionPolicy,
    FinancialConditionV1,
    FirstOfExit,
    IndicatorCondition,
    Instrument,
    StrategySpec,
)
from ashare_lab.ports.market_data import DateRange

SHANGHAI = ZoneInfo("Asia/Shanghai")
RETRIEVED_AT = datetime(2026, 9, 1, 12, tzinfo=SHANGHAI)
RAW_HASH = "sha256:" + "a" * 64
EVENT_HASH = "b" * 64


class _FinancialSource:
    def __init__(self, batch: OperatorReadingBatch) -> None:
        self.batch = batch
        self.closed = False

    def fetch_main_financial_data(
        self,
        instrument_id: str,
        *,
        retrieved_at: datetime,
    ) -> OperatorReadingBatch:
        assert instrument_id == "300059.SZ"
        assert retrieved_at == RETRIEVED_AT
        return self.batch

    def close(self) -> None:
        self.closed = True


class _AnnouncementSource:
    def __init__(self, observations: tuple[EventObservation, ...]) -> None:
        self.observations = observations
        self.closed = False

    def fetch_batch(
        self,
        *,
        instrument_id: InstrumentId,
        start: date,
        end: date,
        retrieved_at: datetime,
        requested_event_codes: tuple[str, ...] | None = None,
        requested_provider_column_codes: tuple[str, ...] | None = None,
    ) -> EventFetchBatch:
        assert instrument_id == InstrumentId("300059.SZ")
        assert start <= date(2025, 8, 15) <= end
        assert retrieved_at == RETRIEVED_AT
        assert requested_event_codes == (
            "event.financial_results.annual_report",
            "event.financial_results.quarterly_report",
            "event.financial_results.semiannual_report",
        )
        assert requested_provider_column_codes is None
        return EventFetchBatch(
            observations=self.observations,
            acquisition_evidence={"provider": "test"},
        )

    def close(self) -> None:
        self.closed = True


def _statement_batch() -> OperatorReadingBatch:
    return OperatorReadingBatch(
        dataset=OperatorDataset.MAIN_FINANCIAL_DATA,
        instrument_id="300059.SZ",
        rows=(
            {
                "SECUCODE": "300059.SZ",
                "REPORT_DATE": "2025-06-30 00:00:00",
                "REPORT_TYPE": "中报",
                "NOTICE_DATE": "2025-08-15 00:00:00",
                "UPDATE_DATE": "2025-08-15 00:00:00",
                "ROEJQ": 8.46,
            },
        ),
        row_raw_response_sha256=(RAW_HASH,),
        row_retrieved_at=(RETRIEVED_AT,),
        requests=(
            OperatorRequestAudit(
                dataset=OperatorDataset.MAIN_FINANCIAL_DATA,
                page=1,
                raw_response_sha256=RAW_HASH,
                retrieved_at=RETRIEVED_AT,
                row_count=1,
            ),
        ),
        canonical_rows_sha256="sha256:" + "c" * 64,
    )


def _observation() -> EventObservation:
    return EventObservation(
        provider="eastmoney",
        provider_event_id="AN202508151234567890",
        instrument_id=InstrumentId("300059.SZ"),
        event_code="event.financial_results.semiannual_report",
        title="东方财富：2025年半年度报告",
        occurred_at=None,
        source_released_at=None,
        vendor_first_available_at=datetime(2025, 8, 15, 20, 3, 5, tzinfo=SHANGHAI),
        retrieved_at=RETRIEVED_AT,
        time_quality=TimeQuality.VENDOR_OBSERVED,
        document_sha256=EVENT_HASH,
        raw_response_sha256=EVENT_HASH,
        validation_status="validated",
        attributes={
            "report_type": "semiannual_report",
            "stat_date": "2025-06-30",
            "report_period_quality": "title_exact",
            "document_version_role": "initial_complete",
            "raw_notice_date": "2025-08-15",
        },
        revision_no=0,
    )


def _strategy() -> StrategySpec:
    return StrategySpec(
        catalog=CatalogRef(catalog_id="cn_a.signals", release_version="2026.09.01"),
        instrument=Instrument(symbol="300059.SZ"),
        entry=FinancialConditionV1(
            metric_id=FinancialMetricId.ROE,
            comparator="gt",
            value=Decimal("8"),
            unit=FinancialUnit.PERCENT,
            statement_scope=FinancialStatementScope.CONSOLIDATED,
        ),
        exit=FirstOfExit(
            children=(
                IndicatorCondition(
                    indicator_id="technical.macd",
                    definition_version="1.0.0",
                    params={"fast": 12, "slow": 26, "signal": 9},
                    trigger="death_cross",
                ),
            )
        ),
        execution=DailyExecutionPolicy(
            data_capability="daily_ohlcv_financials",
            evaluation_frequency="financial_available_plus_1d_close",
        ),
        backtest=BacktestConfig(
            start=date(2025, 1, 1),
            end=date(2025, 12, 31),
            initial_cash_cny=1_000_000,
        ),
    )


def test_statement_loader_pins_direct_values_only_after_periodic_announcement_binding() -> None:
    financial_source = _FinancialSource(_statement_batch())
    announcement_source = _AnnouncementSource((_observation(),))
    loader = EastmoneyOperatorFinancialFactLoader(
        source_factory=lambda: financial_source,
        announcement_source_factory=lambda: announcement_source,
    )

    bundle = loader.load(
        _strategy(),
        DateRange(start=date(2024, 12, 1), end=date(2026, 1, 14)),
        retrieved_at=RETRIEVED_AT.astimezone(UTC),
    )

    assert financial_source.closed is True
    assert announcement_source.closed is True
    assert [item.metric_id for item in bundle.facts] == [FinancialMetricId.ROE]
    assert bundle.facts[0].value == Decimal("8.46")
    assert bundle.facts[0].unit is FinancialUnit.PERCENT
    assert bundle.facts[0].availability.first_available_at == datetime(
        2025, 8, 15, 20, 3, 5, tzinfo=SHANGHAI
    )
    binding = bundle.identity_basis["publication_bindings"]
    assert isinstance(binding, list)
    assert binding[0]["provider_event_id"] == "AN202508151234567890"
    assert binding[0]["binding_hash"] in bundle.facts[0].revision_id


def test_statement_loader_rejects_an_unbound_provider_row() -> None:
    loader = EastmoneyOperatorFinancialFactLoader(
        source_factory=lambda: _FinancialSource(_statement_batch()),
        announcement_source_factory=lambda: _AnnouncementSource(()),
    )

    with pytest.raises(OperatorFinancialDataUnavailableError, match="no exact periodic"):
        loader.load(
            _strategy(),
            DateRange(start=date(2024, 12, 1), end=date(2026, 1, 14)),
            retrieved_at=RETRIEVED_AT,
        )
