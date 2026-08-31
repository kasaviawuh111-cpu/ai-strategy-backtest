"""Fixture-level runtime-path gate for every stable Catalog trigger.

This matrix proves that Catalog defaults reach the deterministic daily signal
runtime without registry drift or an unimplemented branch.  Synthetic bars do
not prove formula accuracy, live data coverage, trading performance, or
production readiness.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ashare_lab.domain.catalog import (
    IndicatorDefinition,
    TriggerDefinition,
    load_catalog_directory,
)
from ashare_lab.domain.market_data import DailyBar
from ashare_lab.domain.shared import InstrumentId, Price, Quantity
from ashare_lab.domain.signals import SignalRuntime
from ashare_lab.domain.signals.runtime import validate_stable_indicator_evaluator_catalog
from ashare_lab.domain.strategy import (
    BacktestConfig,
    CatalogRef,
    FirstOfExit,
    IndicatorCondition,
    Instrument,
    StrategySpec,
    validate_strategy_against_catalog,
)
from ashare_lab.domain.strategy.models import JsonScalar

ROOT = Path(__file__).resolve().parents[2]
SHANGHAI = ZoneInfo("Asia/Shanghai")
CATALOG = load_catalog_directory(ROOT / "catalogs")
STABLE_DEFINITIONS = tuple(
    definition for definition in CATALOG.indicators if definition.status == "stable"
)
TRIGGER_CASES = tuple(
    (definition, trigger) for definition in STABLE_DEFINITIONS for trigger in definition.triggers
)


def _synthetic_a_share_daily_bars(count: int = 420) -> tuple[DailyBar, ...]:
    """Build positive-volume, CNY, single-A-share fixture bars with varied regimes."""

    sessions: list[date] = []
    session = date(2022, 1, 4)
    while len(sessions) < count:
        if session.weekday() < 5:
            sessions.append(session)
        session += timedelta(days=1)

    bars: list[DailyBar] = []
    for index, session_date in enumerate(sessions):
        regime = Decimal((index // 70) % 4 - 1)
        cycle = Decimal((index % 19) - 9) * Decimal("0.11")
        close = Decimal("30") + Decimal(index) * Decimal("0.025") + regime + cycle
        open_ = close + Decimal((index % 5) - 2) * Decimal("0.04")
        high = max(open_, close) + Decimal("0.45") + Decimal(index % 3) * Decimal("0.02")
        low = min(open_, close) - Decimal("0.43") - Decimal(index % 4) * Decimal("0.02")
        volume = 100_000 + (index % 23) * 4_000 + (index // 50) * 100
        bars.append(
            DailyBar(
                instrument_id=InstrumentId("300059.SZ"),
                session_date=session_date,
                open=Price(open_),
                high=Price(high),
                low=Price(low),
                close=Price(close),
                volume=Quantity(volume),
                turnover=close * Decimal(volume),
                available_at=datetime.combine(
                    session_date,
                    time(hour=15, minute=1),
                    tzinfo=SHANGHAI,
                ),
            )
        )
    return tuple(bars)


FIXTURE_BARS = _synthetic_a_share_daily_bars()


def _default_parameters(definition: IndicatorDefinition) -> dict[str, JsonScalar]:
    params: dict[str, JsonScalar] = {}
    for parameter in definition.parameters:
        assert parameter.default is not None, (
            f"stable indicator {definition.id} parameter {parameter.name} has no Catalog default"
        )
        params[parameter.name] = parameter.default
    return params


def _legal_trigger_value(trigger: TriggerDefinition) -> float | None:
    if trigger.value_requirement == "forbidden":
        return None
    if trigger.minimum is not None and trigger.maximum is not None:
        if trigger.minimum == trigger.maximum:
            return trigger.minimum
        return (trigger.minimum + trigger.maximum) / 2
    if trigger.minimum is not None:
        return trigger.minimum + 1 if trigger.exclusive_minimum else trigger.minimum
    if trigger.maximum is not None:
        return trigger.maximum - 1 if trigger.exclusive_maximum else trigger.maximum
    return 0.0


def _condition(
    definition: IndicatorDefinition,
    trigger: TriggerDefinition,
) -> IndicatorCondition:
    return IndicatorCondition(
        indicator_id=definition.id,
        definition_version=definition.version,
        params=_default_parameters(definition),
        trigger=trigger.id,
        value=_legal_trigger_value(trigger),
    )


def _strategy_for(condition: IndicatorCondition) -> StrategySpec:
    owner = next(
        manifest
        for manifest in CATALOG.manifests
        if any(definition.id == condition.indicator_id for definition in manifest.indicators)
    )
    return StrategySpec(
        catalog=CatalogRef(
            catalog_id=owner.catalog_id,
            release_version=owner.release_version,
        ),
        instrument=Instrument(symbol="300059.SZ"),
        entry=condition,
        exit=FirstOfExit(children=(condition,)),
        backtest=BacktestConfig(
            start=FIXTURE_BARS[0].session_date,
            end=FIXTURE_BARS[-1].session_date,
            initial_cash_cny=1_000_000,
        ),
    )


def test_stable_catalog_and_daily_evaluator_registry_are_identical() -> None:
    assert len(STABLE_DEFINITIONS) == 35
    assert len(TRIGGER_CASES) == 126
    validate_stable_indicator_evaluator_catalog(CATALOG)


@pytest.mark.parametrize(
    ("definition", "trigger"),
    TRIGGER_CASES,
    ids=lambda value: value.id,
)
def test_each_stable_catalog_trigger_reaches_the_fixture_daily_runtime(
    definition: IndicatorDefinition,
    trigger: TriggerDefinition,
) -> None:
    condition = _condition(definition, trigger)
    validate_strategy_against_catalog(_strategy_for(condition), CATALOG)

    timeline = SignalRuntime().evaluate_aligned(condition, FIXTURE_BARS)

    assert len(timeline) == len(FIXTURE_BARS)
    facts = tuple(fact for fact in timeline if fact is not None)
    assert facts, f"{definition.id}:{trigger.id} never left warmup on the fixture"
    assert all(
        fact.condition_ref == f"{definition.id}@{definition.version}:{trigger.id}" for fact in facts
    )
