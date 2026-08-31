from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from ashare_lab.domain.catalog import load_catalog_directory
from ashare_lab.domain.events.catalog import PUBLIC_ANNOUNCEMENT_EVENT_CODES
from ashare_lab.domain.strategy import (
    BacktestConfig,
    CatalogRef,
    DailyExecutionPolicy,
    EventCondition,
    FirstOfExit,
    IndicatorCondition,
    Instrument,
    StrategyCatalogError,
    StrategySpec,
    strategy_requires_events,
    validate_strategy_against_catalog,
)

ROOT = Path(__file__).resolve().parents[3]


def event_strategy(
    *,
    event_code: str,
    version: str = "1.0.0",
    attributes: dict[str, str | int | float | bool] | None = None,
) -> StrategySpec:
    return StrategySpec(
        catalog=CatalogRef(catalog_id="cn_a.signals", release_version="2026.08.30"),
        instrument=Instrument(symbol="300059.SZ"),
        entry=EventCondition(
            event_code=event_code,
            definition_version=version,
            attributes=attributes or {},
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
            data_capability="daily_ohlcv_events",
            evaluation_frequency="event_available_plus_1d_close",
        ),
        backtest=BacktestConfig(
            start=date(2021, 1, 1),
            end=date(2025, 1, 1),
            initial_cash_cny=100_000,
        ),
    )


def test_executable_event_condition_validates_with_indicator_exit() -> None:
    strategy = event_strategy(event_code="event.financial_results.earnings_forecast_published")

    validated = validate_strategy_against_catalog(
        strategy,
        load_catalog_directory(ROOT / "catalogs"),
    )

    assert validated is strategy
    assert strategy_requires_events(strategy)


@pytest.mark.parametrize(
    ("event_code", "attributes"),
    [
        (
            "event.contracts_orders.major_contract_won",
            {
                "award_stage": "formal_winner",
                "materiality_status": "material",
                "is_consortium": False,
            },
        ),
        (
            "event.macro_policy_industry.license_approval",
            {"approval_status": "approved", "license_type": "业务许可"},
        ),
    ],
)
def test_external_web_event_attributes_are_bounded_exact_filters(
    event_code: str,
    attributes: dict[str, str | int | float | bool],
) -> None:
    strategy = event_strategy(event_code=event_code, attributes=attributes)

    validated = validate_strategy_against_catalog(
        strategy,
        load_catalog_directory(ROOT / "catalogs"),
    )

    assert validated is strategy


def test_external_web_event_rejects_undeclared_attribute_filter() -> None:
    strategy = event_strategy(
        event_code="event.contracts_orders.major_contract_won",
        attributes={"amount_gte_cny": 100_000_000},
    )

    with pytest.raises(StrategyCatalogError, match="unknown_event_attribute"):
        validate_strategy_against_catalog(strategy, load_catalog_directory(ROOT / "catalogs"))


def test_public_announcement_release_is_executable_without_unproved_filters() -> None:
    catalog = load_catalog_directory(ROOT / "catalogs")

    for event_code in PUBLIC_ANNOUNCEMENT_EVENT_CODES:
        strategy = event_strategy(event_code=event_code)
        assert validate_strategy_against_catalog(strategy, catalog) is strategy

    filtered = event_strategy(
        event_code="event.repurchase_capital.repurchase_proposal",
        attributes={"amount_cny": 100_000_000},
    )
    with pytest.raises(StrategyCatalogError, match="unknown_event_attribute"):
        validate_strategy_against_catalog(filtered, catalog)


def test_unknown_event_code_and_wrong_version_are_rejected() -> None:
    catalog = load_catalog_directory(ROOT / "catalogs")
    unsupported = event_strategy(event_code="event.unknown.not_released")
    wrong_version = event_strategy(
        event_code="event.financial_results.earnings_forecast_published",
        version="2.0.0",
    )

    with pytest.raises(StrategyCatalogError, match="event_not_executable"):
        validate_strategy_against_catalog(unsupported, catalog)
    with pytest.raises(StrategyCatalogError, match="event_version_mismatch"):
        validate_strategy_against_catalog(wrong_version, catalog)


def test_event_strategy_must_declare_event_data_capability() -> None:
    with pytest.raises(ValueError, match="daily_ohlcv_events"):
        StrategySpec(
            catalog=CatalogRef(catalog_id="cn_a.signals", release_version="2026.08.30"),
            instrument=Instrument(symbol="300059.SZ"),
            entry=EventCondition(
                event_code="event.financial_results.annual_report",
                definition_version="1.0.0",
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
            backtest=BacktestConfig(
                start=date(2021, 1, 1),
                end=date(2025, 1, 1),
                initial_cash_cny=100_000,
            ),
        )
