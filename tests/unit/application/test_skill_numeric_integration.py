"""Synthetic service integration, not real provider or market acceptance evidence."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest

from ashare_lab.adapters.market_data.mx_daily_history import (
    MxDailyHistory,
    MxDailyHistoryClient,
    MxDailyRow,
)
from ashare_lab.adapters.market_data.mx_finance_history_format import MxFinanceHistoryDecoder
from ashare_lab.adapters.persistence.backtest_runs import InMemoryBacktestRunStore
from ashare_lab.api.result_schemas import BacktestResultBundle
from ashare_lab.application.skill_backtest_service import SkillBacktestService
from ashare_lab.application.skill_indicator_routes import build_skill_indicator_routes
from ashare_lab.application.skill_numeric_catalog import extend_skill_numeric_catalogs
from ashare_lab.application.skill_numeric_history import SkillNumericHistoryError
from ashare_lab.domain.catalog import load_catalog_directory, load_coverage_catalog_directory
from ashare_lab.domain.shared import InstrumentId, RunId
from ashare_lab.domain.signals import SignalFact
from ashare_lab.domain.strategy import (
    AllCondition,
    CatalogRef,
    FirstOfExit,
    IndicatorCondition,
    StrategySpec,
    validate_strategy_against_catalog,
)
from ashare_lab.ports.backtest_runs import BacktestJobState
from ashare_lab.ports.live_market_data import LiveFinanceDataResult, LiveMarketDataProvenance
from ashare_lab.ports.provider_indicator_data import (
    HistoricalIndicatorData,
    ProviderIndicatorSeries,
)
from tests.unit.application.test_skill_backtest import (
    START,
    SYMBOL,
    _config,  # pyright: ignore[reportPrivateUsage]
    _history,  # pyright: ignore[reportPrivateUsage]
    _row,  # pyright: ignore[reportPrivateUsage]
    _strategy,  # pyright: ignore[reportPrivateUsage]
)

ROOT = Path(__file__).resolve().parents[3] / "catalogs"
CATALOG, COVERAGE = extend_skill_numeric_catalogs(
    load_catalog_directory(ROOT), load_coverage_catalog_directory(ROOT / "coverage"),
)
# Keep this suite's explicit mixed Python-formula profile; provider-first is
# independently covered with an actual provider-protocol fixture.
ROUTES = build_skill_indicator_routes(CATALOG, COVERAGE, provider_first=False)
QUERY = "市盈率 TTM"
RIGHT_QUERY = "另一估值口径"


@pytest.fixture(scope="module")
def history() -> MxDailyHistory:
    rows: list[MxDailyRow] = []
    day = START
    while len(rows) < 80:
        if day.weekday() < 5:
            close = str(20 + len(rows) % 10)
            rows.append(_row(day, raw_open=close, raw_close=close))
        day += timedelta(days=1)
    return _history(tuple(rows))


def _pe(index: int) -> Decimal:
    return Decimal(10 + index % 10 + (10 if index % 10 >= 5 else 0))


class _Finance:
    """Real response/table protocol shape with entirely synthetic values."""

    def __init__(self, history: MxDailyHistory) -> None:
        self.history = history
        self.calls: list[tuple[str, str | None]] = []

    async def query_finance(self, *, query: str, indicators: str | None) -> LiveFinanceDataResult:
        self.calls.append((query, indicators))
        is_right = indicators is not None and indicators.startswith(RIGHT_QUERY)
        return LiveFinanceDataResult(
            provider="eastmoney_mx_finance_data", query=query, indicators=indicators,
            tables=({
                "entityCode": SYMBOL, "dateGranularity": "DAY",
                "fieldSet": [{
                    "returnCode": "328773", "returnSourceCode": "RIGHT" if is_right else "PETTM",
                    "returnName": RIGHT_QUERY if is_right else QUERY,
                    "unit": "1", "unitName": "倍", "unitDesc": "倍",
                    "fixedParamValue": "Period=1", "dateGranularity": "DAY",
                }],
                "rawTable": {
                    "headName": [row.session_date.isoformat() for row in self.history.rows],
                    "328773": [str(15 if is_right else _pe(index))
                               for index in range(len(self.history.rows))],
                },
            },),
            provenance=LiveMarketDataProvenance(
                response_sha256="sha256:" + ("b" if is_right else "a") * 64,
                retrieved_at=datetime(2026, 9, 7, tzinfo=UTC), schema_version="fixture.v1",
            ),
        )


@pytest.mark.parametrize("with_ma", [False, True], ids=["shared-pe", "pe-and-ma"])
@pytest.mark.parametrize("series_compare", [False, True], ids=["threshold", "two-series"])
def test_numeric_history_runs_with_shared_lookup_and_private_audit(
    history: MxDailyHistory, with_ma: bool, series_compare: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    numeric = IndicatorCondition(
        indicator_id="provider.numeric", definition_version="1.0.0",
        params={"metric_query": QUERY, "unit": "倍"}, trigger="below", value=15,
    )
    if series_compare:
        numeric = numeric.model_copy(update={
            "indicator_id": "provider.series_compare", "value": None,
            "params": {"left_metric_query": QUERY, "right_metric_query": RIGHT_QUERY, "unit": "倍"},
        })
    ma = IndicatorCondition(
        indicator_id="technical.ma", definition_version="1.0.0",
        params={"period": 3, "price_field": "close"}, trigger="price_above",
    )
    manifest = next(item for item in CATALOG.manifests if item.catalog_id == "cn_a.signals")
    strategy = _strategy((history.rows[40].session_date, history.rows[-1].session_date)).model_copy(
        update={
            "catalog": CatalogRef(
                catalog_id=manifest.catalog_id, release_version=manifest.release_version,
            ),
            "entry": AllCondition(children=(numeric, ma)) if with_ma else numeric,
            "exit": FirstOfExit(children=(numeric.model_copy(update={
                "trigger": "above", "value": None if series_compare else 20.0,
            }),)),
        },
    )
    validate_strategy_against_catalog(strategy, CATALOG)
    finance = _Finance(history)
    load = AsyncMock(return_value=history)
    finished_indicator_query = AsyncMock(side_effect=AssertionError("wrong data route"))
    store = InMemoryBacktestRunStore()
    service = SkillBacktestService(
        history=cast(MxDailyHistoryClient, SimpleNamespace(load=load)),
        indicators=cast(HistoricalIndicatorData, SimpleNamespace(
            query_indicator_history=finished_indicator_query,
        )),
        store=store, indicator_routes=ROUTES, finance=finance,
        finance_decoder=MxFinanceHistoryDecoder(),
    )
    captured: list[tuple[tuple[SignalFact | None, ...], tuple[SignalFact | None, ...]]] = []
    original_signals = service._signals  # pyright: ignore[reportPrivateUsage]

    async def capture_signals(
        requested: StrategySpec, daily: MxDailyHistory, *, force_refresh: bool,
        require_ready: bool = False,
        derived_condition_hashes: set[str] | None = None,
        price_rebases: tuple | None = None,
    ) -> tuple[
        tuple[SignalFact | None, ...], tuple[SignalFact | None, ...],
        tuple[ProviderIndicatorSeries, ...],
    ]:
        result = await original_signals(requested, daily, force_refresh=force_refresh,
                                        require_ready=require_ready,
                                        price_rebases=price_rebases,
                                        derived_condition_hashes=derived_condition_hashes)
        captured.append((result[0], result[1]))
        return result

    def manual_enqueue(_run_id: RunId) -> str:
        return "manual-fixture"

    monkeypatch.setattr(service.queue, "enqueue", manual_enqueue)
    monkeypatch.setattr(service, "_signals", capture_signals)
    create_record = Mock(wraps=store.create_or_get)
    monkeypatch.setattr(store, "create_or_get", create_record)
    try:
        config = _config(run_robustness=False)
        prepared = asyncio.run(service.prepare_candidate(strategy, config))
        create_record.assert_not_called()
        assert len(prepared.indicator_series) == 1
        assert asyncio.run(service.prepare_candidate(strategy, config)) is prepared
        created = service.submit(strategy, config, prepared_inputs=True)
        assert created.record.state is BacktestJobState.QUEUED
        finished = service.execute(created.record.run_id)
        assert finished.state is BacktestJobState.SUCCEEDED, (
            finished.error_code, finished.progress_label,
        )
        assert finished.result_json is not None
        assert store.get(finished.run_id) == finished
        assert StrategySpec.model_validate_json(finished.strategy_json) == strategy
        bundle = BacktestResultBundle.model_validate_json(finished.result_json)
        assert bundle.summary.run_id == str(finished.run_id)
        assert bundle.summary.trade_count > 0
        assert len(bundle.series) == 41
        assert {item.side for item in bundle.activities if item.kind == "fill"} == {"buy", "sell"}
        assert bundle.audit.result_hash is not None
        assert len(bundle.audit.skill_numeric_sources) == 1
        source = json.loads(bundle.audit.skill_numeric_sources[0])
        assert source["instrument_id"] == SYMBOL
        assert source["indicator_id"] == numeric.indicator_id
        assert len(source["points"]) == len(history.rows)
        value = source["points"][0]["values"][0]
        assert value["field_code"] == "328773"
        assert value["field_name"] == ("left_value" if series_compare else "value")
        parameters = json.loads(value["source_parameters"])
        assert parameters["assumedTime"] is True
        assert parameters["returnCode"] == "328773"
        assert parameters["fieldMetadata"]["returnSourceCode"] == "PETTM"
        assert parameters["bindingHash"].startswith("sha256:")
        if series_compare:
            right_value = source["points"][0]["values"][1]
            assert right_value["field_name"] == "right_value"
            right_parameters = json.loads(right_value["source_parameters"])
            assert right_parameters["fieldMetadata"]["returnSourceCode"] == "RIGHT"
            assert right_parameters["sourceSeriesResponseHash"] == "sha256:" + "b" * 64
            assert parameters["sourceSeriesResponseHash"] == "sha256:" + "a" * 64
        public_summary = bundle.summary.model_dump_json(by_alias=True)
        assert "可得时间未验证" not in public_summary
        assert "assumedTime" not in public_summary
        provenance = bundle.summary.data_provenance
        assert provenance is not None and provenance.indicator_series == 1
        assert provenance.indicator_field_evidence[0]["sourceFieldCode"] == "328773"
        assert len(provenance.derived_indicator_evidence) == int(with_ma)
        assert len(finance.calls) == (2 if series_compare else 1)
        assert finance.calls[0][1] == (
            f"{QUERY}，{history.start}至{history.end}，逐日历史数值"
        )
        load.assert_awaited_once()
        finished_indicator_query.assert_not_awaited()
        assert len(captured) == 1
        entries, exits = captured[0]
        for index in range(40, len(history.rows)):
            entry, exit_ = entries[index], exits[index]
            assert entry is not None and exit_ is not None
            expected_entry = _pe(index) < 15
            if with_ma:
                expected_entry &= history.rows[index].adjusted_close > sum(
                    (row.adjusted_close for row in history.rows[index - 2:index + 1]),
                    start=Decimal(0),
                ) / 3
            assert entry.triggered is expected_entry
            assert exit_.triggered is (_pe(index) > (15 if series_compare else 20))
            assert entry.condition_ref != exit_.condition_ref
            assert entry.instrument_id == exit_.instrument_id == InstrumentId(SYMBOL)
    finally:
        service.shutdown()


def test_comparison_catalog_requires_both_queries_and_forbids_a_scalar_threshold() -> None:
    definition = CATALOG.resolve_indicator("provider.series_compare")
    assert definition is not None
    assert {item.name for item in definition.parameters} == {
        "left_metric_query", "right_metric_query", "unit",
    }
    assert all(item.required for item in definition.parameters)
    assert len(definition.triggers) == 6
    assert all(item.value_requirement == "forbidden" for item in definition.triggers)
    route = ROUTES["provider.series_compare"]
    assert route.source == "skill_numeric_history"
    assert set(route.required_fields) == {
        "instrument_id", "session_date", "left_value", "right_value", "unit",
    }


def test_comparison_missing_right_history_stops_preflight_before_creating_run(
    history: MxDailyHistory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MissingRightFinance(_Finance):
        async def query_finance(
            self, *, query: str, indicators: str | None,
        ) -> LiveFinanceDataResult:
            result = await super().query_finance(query=query, indicators=indicators)
            if indicators is not None and indicators.startswith(RIGHT_QUERY):
                table = dict(result.tables[0])
                table["rawTable"] = {
                    "headName": [history.end.isoformat()], "328773": [15],
                }
                return replace(result, tables=(table,))
            return result

    condition = IndicatorCondition(
        indicator_id="provider.series_compare", definition_version="1.0.0", trigger="below",
        params={"left_metric_query": QUERY, "right_metric_query": RIGHT_QUERY, "unit": "倍"},
    )
    strategy = _strategy((history.rows[40].session_date, history.end)).model_copy(update={
        "entry": condition,
    })
    finance = MissingRightFinance(history)
    store = InMemoryBacktestRunStore()
    create = Mock(wraps=store.create_or_get)
    monkeypatch.setattr(store, "create_or_get", create)
    service = SkillBacktestService(
        history=cast(MxDailyHistoryClient, SimpleNamespace(load=AsyncMock(return_value=history))),
        indicators=cast(HistoricalIndicatorData, object()),
        store=store, indicator_routes=ROUTES, finance=finance,
        finance_decoder=MxFinanceHistoryDecoder(),
    )
    try:
        with pytest.raises(SkillNumericHistoryError) as caught:
            asyncio.run(service.prepare_candidate(strategy, _config(run_robustness=False)))
        assert caught.value.code == "history_unavailable_after_query_retry"
        assert caught.value.metric_query == RIGHT_QUERY
        assert caught.value.indicator_id == "provider.series_compare"
        assert caught.value.condition_path == "entry.$"
        create.assert_not_called()
        assert len(finance.calls) == 3
        assert strategy.entry == condition
    finally:
        service.shutdown()
