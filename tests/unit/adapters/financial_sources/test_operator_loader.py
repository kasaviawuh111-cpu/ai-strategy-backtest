from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from ashare_lab.adapters.financial_sources.eastmoney_operator import (
    OperatorDataset,
    OperatorReadingBatch,
    OperatorRequestAudit,
)
from ashare_lab.adapters.financial_sources.operator_loader import (
    EastmoneyOperatorFinancialFactLoader,
    OperatorFinancialDataUnavailableError,
)
from ashare_lab.domain.financials import (
    FinancialMetricId,
    FinancialPeriodBasis,
    FinancialStatementScope,
    FinancialUnit,
)
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


class FakeSource:
    def __init__(self, batches: tuple[OperatorReadingBatch, ...]) -> None:
        self.batches = batches
        self.closed = False

    def fetch_valuation_trends(
        self,
        instrument_id: str,
        *,
        retrieved_at: datetime,
        statistics_cycle: int = 4,
    ) -> tuple[OperatorReadingBatch, ...]:
        assert instrument_id == "300059.SZ"
        assert retrieved_at == RETRIEVED_AT
        assert retrieved_at.tzinfo == SHANGHAI
        assert statistics_cycle == 4
        return self.batches

    def close(self) -> None:
        self.closed = True


def _batch() -> OperatorReadingBatch:
    rows = tuple(
        {
            "SECUCODE": "300059.SZ",
            "TRADE_DATE": f"{day} 00:00:00",
            "INDICATORTYPE": "1",
            "INDICATOR_VALUE": value,
        }
        for day, value in (("2025-01-02", 22.0), ("2025-08-29", 18.0))
    )
    return OperatorReadingBatch(
        dataset=OperatorDataset.VALUATION_TREND,
        instrument_id="300059.SZ",
        rows=rows,
        row_raw_response_sha256=(RAW_HASH,) * len(rows),
        row_retrieved_at=(RETRIEVED_AT,) * len(rows),
        requests=(
            OperatorRequestAudit(
                dataset=OperatorDataset.VALUATION_TREND,
                page=1,
                raw_response_sha256=RAW_HASH,
                retrieved_at=RETRIEVED_AT,
                row_count=len(rows),
            ),
        ),
        canonical_rows_sha256="sha256:" + "b" * 64,
        indicator_type=1,
        statistics_cycle=4,
    )


def _strategy(metric: FinancialMetricId) -> StrategySpec:
    valuation = metric in {
        FinancialMetricId.PE,
        FinancialMetricId.PB,
        FinancialMetricId.PS,
        FinancialMetricId.PCF,
    }
    return StrategySpec(
        catalog=CatalogRef(catalog_id="cn_a.signals", release_version="2026.08.30"),
        instrument=Instrument(symbol="300059.SZ"),
        entry=FinancialConditionV1(
            metric_id=metric,
            comparator="lt",
            value=Decimal("20"),
            unit=FinancialUnit.TIMES if valuation else FinancialUnit.RATIO,
            period_basis=(FinancialPeriodBasis.POINT_IN_TIME if valuation else None),
            statement_scope=(None if valuation else FinancialStatementScope.CONSOLIDATED),
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


def test_valuation_loader_pins_direct_provider_rows_and_hashes() -> None:
    source = FakeSource((_batch(),))
    loader = EastmoneyOperatorFinancialFactLoader(
        source_factory=lambda: source,
    )

    bundle = loader.load(
        _strategy(FinancialMetricId.PE),
        DateRange(start=date(2024, 12, 1), end=date(2026, 1, 14)),
        retrieved_at=RETRIEVED_AT.astimezone(UTC),
    )

    assert source.closed is True
    assert bundle.snapshot_id == "financial:" + bundle.checksum.removeprefix("sha256:")
    assert bundle.coverage_start == date(2025, 1, 2)
    assert bundle.coverage_end == date(2025, 8, 29)
    assert [item.value for item in bundle.facts] == [Decimal("22.0"), Decimal("18.0")]
    assert all(item.raw_response_sha256 == RAW_HASH for item in bundle.facts)


def test_statement_metric_fails_closed_before_provider_call() -> None:
    loader = EastmoneyOperatorFinancialFactLoader(
        source_factory=lambda: pytest.fail("source must not be called"),
    )

    with pytest.raises(
        OperatorFinancialDataUnavailableError,
        match="revision_history_unavailable",
    ):
        loader.load(
            _strategy(FinancialMetricId.ROE),
            DateRange(start=date(2025, 1, 1), end=date(2025, 12, 31)),
            retrieved_at=RETRIEVED_AT,
        )
