from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

from ashare_lab.adapters.market_data.mx_daily_history import (
    MxDailyHistory,
    MxDailyHistoryClient,
    MxDailyRow,
)
from ashare_lab.adapters.persistence.backtest_runs import InMemoryBacktestRunStore
from ashare_lab.api.result_schemas import BacktestResultBundle
from ashare_lab.application.skill_backtest_service import SkillBacktestService
from ashare_lab.application.skill_indicator_routes import default_skill_indicator_routes
from ashare_lab.domain.catalog import IndicatorDefinition, load_catalog_directory
from ashare_lab.domain.shared import RunId
from ashare_lab.domain.signals import SignalFact
from ashare_lab.domain.strategy import CatalogRef, IndicatorCondition, StrategySpec
from ashare_lab.domain.strategy.validation import validate_strategy_against_catalog
from ashare_lab.ports.backtest_runs import BacktestJobState
from ashare_lab.ports.provider_indicator_data import (
    HistoricalIndicatorData,
    ProviderIndicatorSeries,
)
from tests.unit.application.test_skill_backtest import (
    START,
    _config,  # pyright: ignore[reportPrivateUsage]
    _history,  # pyright: ignore[reportPrivateUsage]
    _row,  # pyright: ignore[reportPrivateUsage]
    _strategy,  # pyright: ignore[reportPrivateUsage]
)

CATALOG = load_catalog_directory(Path(__file__).resolve().parents[3] / "catalogs")
ROUTES = default_skill_indicator_routes()
DERIVED_DEFINITIONS = tuple(
    definition for definition in CATALOG.indicators
    if ROUTES.get(definition.id) and ROUTES[definition.id].source == "skill_ohlcv_python"
)


@pytest.fixture(scope="module")
def oscillating_history() -> MxDailyHistory:
    """Synthetic valid daily records; never real-data acceptance evidence."""
    rows: list[MxDailyRow] = []
    session = START
    while len(rows) < 180:
        if session.weekday() < 5:
            index = len(rows)
            wave = Decimal(min(index % 30, 30 - index % 30)) / Decimal(2)
            close = Decimal(20) + wave + Decimal(index) / Decimal(100)
            opening = close + Decimal(index % 3 - 1) / Decimal(10)
            volume = 1_000_000 + (index % 11) * 150_000
            rows.append(replace(
                _row(
                    session, raw_open=str(opening), raw_close=str(close), adjusted_scale="1.7",
                ),
                volume=volume,
                amount=close * volume,
            ))
        session += timedelta(days=1)
    return _history(tuple(rows))


def _default_condition(definition: IndicatorDefinition) -> IndicatorCondition:
    # Consume the actual Catalog instead of hand-writing a second indicator spec.
    assert all(parameter.default is not None for parameter in definition.parameters)
    trigger = definition.triggers[0]
    value = None
    if trigger.value_requirement == "required":
        if trigger.minimum is not None and trigger.maximum is not None:
            value = (trigger.minimum + trigger.maximum) / 2
        elif trigger.minimum is not None:
            value = trigger.minimum + int(trigger.exclusive_minimum)
        elif trigger.maximum is not None:
            value = trigger.maximum - int(trigger.exclusive_maximum)
        else:
            value = 0
    return IndicatorCondition(
        indicator_id=definition.id,
        definition_version=definition.version,
        params={
            parameter.name: parameter.default for parameter in definition.parameters
            if parameter.default is not None
        },
        trigger=trigger.id,
        value=value,
    )


@pytest.mark.parametrize("definition", DERIVED_DEFINITIONS, ids=lambda item: item.id)
def test_each_skill_derived_indicator_submits_executes_and_persists_formula_evidence(
    definition: IndicatorDefinition,
    oscillating_history: MxDailyHistory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert len(DERIVED_DEFINITIONS) == 30
    history = oscillating_history
    condition = _default_condition(definition)
    manifest = next(item for item in CATALOG.manifests if definition in item.indicators)
    strategy = _strategy(
        (history.rows[140].session_date, history.rows[-1].session_date), holding_sessions=5,
    ).model_copy(update={
        "entry": condition,
        "catalog": CatalogRef(
            catalog_id=manifest.catalog_id, release_version=manifest.release_version,
        ),
    })
    validate_strategy_against_catalog(strategy, CATALOG)
    original_strategy = strategy.model_dump(mode="json")
    load = AsyncMock(return_value=history)
    query = AsyncMock(side_effect=AssertionError(
        f"{definition.id} must compute on Skill OHLCV, not request a finished indicator",
    ))
    service = SkillBacktestService(
        history=cast(MxDailyHistoryClient, SimpleNamespace(load=load)),
        indicators=cast(HistoricalIndicatorData, SimpleNamespace(query_indicator_history=query)),
        store=InMemoryBacktestRunStore(),
        indicator_routes=ROUTES,  # Legacy formula profile, not live provider-first routing.
        # This synthetic fixture has an explicit weekday-only market calendar;
        # production must still require an independently sourced calendar.
        market_calendar_loader=lambda: (
            tuple(row.session_date for row in history.rows),
            {"provider": "synthetic-test-calendar"}, b"synthetic-test-calendar",
        ),
    )
    def manual_enqueue(_run_id: RunId) -> str:
        return "manual-fixture"

    monkeypatch.setattr(service.queue, "enqueue", manual_enqueue)
    original_signals = service._signals  # pyright: ignore[reportPrivateUsage]
    captured: list[tuple[SignalFact | None, ...]] = []

    async def capture_signals(
        requested: StrategySpec, daily: MxDailyHistory, *, force_refresh: bool,
        require_ready: bool = False,
        derived_condition_hashes: set[str] | None = None,
        price_rebases: tuple | None = None,
    ) -> tuple[
        tuple[SignalFact | None, ...], tuple[SignalFact | None, ...],
        tuple[ProviderIndicatorSeries, ...],
    ]:
        result = await original_signals(
            requested, daily, force_refresh=force_refresh, require_ready=require_ready,
            derived_condition_hashes=derived_condition_hashes,
            price_rebases=price_rebases,
        )
        captured.append(result[0])
        return result

    monkeypatch.setattr(service, "_signals", capture_signals)
    try:
        created = service.submit(strategy, _config(run_robustness=False))
        assert not created.replayed
        assert created.record.state is BacktestJobState.QUEUED
        finished = service.execute(created.record.run_id)

        assert finished.state is BacktestJobState.SUCCEEDED, (
            definition.id, finished.error_code, finished.progress_label,
        )
        assert finished.result_json is not None
        assert StrategySpec.model_validate_json(finished.strategy_json).model_dump(
            mode="json",
        ) == original_strategy
        assert len(captured) == 1 and len(captured[0]) == len(history.rows)
        assert any(fact is not None for fact in captured[0][140:])
        assert all(
            fact is None or fact.evidence[0].validation_status == "local_formula_on_provider_ohlcv"
            for fact in captured[0]
        )
        bundle = BacktestResultBundle.model_validate_json(finished.result_json)
        source = bundle.summary.data_provenance
        assert source is not None
        assert source.indicator_series == 0
        assert not source.indicator_field_evidence
        assert len(source.derived_indicator_evidence) == 1
        evidence = source.derived_indicator_evidence[0]
        assert evidence["indicatorId"] == definition.id
        assert evidence["definitionVersion"] == definition.version
        assert evidence["source"] == "local_formula_on_eastmoney_skill_ohlcv"
        assert evidence["formula"] == ROUTES[definition.id].formula_summary
        assert evidence["implementationRef"] == ROUTES[definition.id].implementation_ref
        assert json.loads(evidence["parameters"]) == condition.params
        assert len(bundle.series) > 1
        load.assert_awaited_once()
        query.assert_not_awaited()
    finally:
        service.shutdown()
