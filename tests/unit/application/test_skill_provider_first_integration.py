"""Synthetic provider-protocol acceptance; not evidence of live Skill coverage."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from ashare_lab.adapters.market_data.mx_daily_history import (
    MxDailyHistory,
    MxDailyHistoryClient,
    MxDailyRow,
)
from ashare_lab.adapters.market_data.mx_saas import (
    MxSaasMarketDataClient,
    MxSaasProviderAuthError,
    MxSaasProviderDataError,
    MxSaasProviderUnavailableError,
)
from ashare_lab.adapters.market_data.provider_indicator_cache import (
    FileCachedHistoricalIndicatorData,
    ProviderIndicatorCacheMissError,
)
from ashare_lab.adapters.persistence.backtest_runs import InMemoryBacktestRunStore
from ashare_lab.application import skill_backtest_service as service_module
from ashare_lab.application.skill_backtest_service import (
    SkillBacktestService,
    SkillCandidatePreparationError,
)
from ashare_lab.application.skill_indicator_routes import build_skill_indicator_routes
from ashare_lab.domain.shared import RunId
from ashare_lab.domain.signals.provider_catalog import provider_binding_for_condition
from ashare_lab.domain.strategy import FirstOfExit, IndicatorCondition, canonical_hash
from ashare_lab.domain.strategy.models import JsonScalar
from ashare_lab.ports.backtest_runs import BacktestJobState
from ashare_lab.ports.provider_indicator_data import (
    HistoricalIndicatorData,
    ProviderIndicatorPoint,
    ProviderIndicatorSeries,
    ProviderIndicatorValue,
)
from tests.unit.application.test_skill_backtest import (
    START,
    SYMBOL,
    _config,  # pyright: ignore[reportPrivateUsage]
    _history,  # pyright: ignore[reportPrivateUsage]
    _row,  # pyright: ignore[reportPrivateUsage]
    _strategy,  # pyright: ignore[reportPrivateUsage]
)
from tests.unit.application.test_skill_numeric_integration import CATALOG, COVERAGE


@pytest.fixture(scope="module", name="history")
def provider_history() -> MxDailyHistory:
    rows: list[MxDailyRow] = []
    day = START
    while len(rows) < 80:
        if day.weekday() < 5:
            close = str(20 + len(rows) % 10)
            rows.append(_row(day, raw_open=close, raw_close=close))
        day += timedelta(days=1)
    return _history(tuple(rows))


def _manual_enqueue(_run: RunId) -> str:
    return "fixture"


@pytest.mark.parametrize("indicator_id", ["technical.ma", "technical.macd"])
@pytest.mark.parametrize("wrong_parameters", [False, True])
def test_provider_first_preserves_valid_values_and_falls_back_only_for_wrong_fields(
    history: MxDailyHistory, indicator_id: str, wrong_parameters: bool,
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    channels: list[str] = []
    params: dict[str, JsonScalar]
    if indicator_id == "technical.ma":
        params = {"period": 3, "price_field": "close"}
        triggers = ("price_above", "price_below")
        fields = [
            {"returnCode": "price", "returnSourceCode": "CLOSE", "returnName": "收盘价",
             "fixedParamValue": "AdjustFlag=2,CurType=1,Period=1", "unitName": "元"},
            {"returnCode": "line", "returnSourceCode": "MAJDYDPJ3",
             "returnName": "3日MA简单移动平均", "unitName": "元",
             "fixedParamValue": f"N={5 if wrong_parameters else 3},AdjustFlag=2,Period=1"},
        ]
    else:
        params = {"fast": 12, "slow": 26, "signal": 9}
        triggers = ("golden_cross", "death_cross")
        fields = [
            {"returnCode": code, "returnSourceCode": f"MACD_{name}",
             "returnName": f"MACD({name}值)", "unitName": "元",
             "fixedParamValue": (
                 f"N1={5 if wrong_parameters else 12},N2=26,M=9,AdjustFlag=2,Period=1"
             )}
            for code, name in (("price", "DIF"), ("line", "DEA"))
        ]

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        channels.append(request.url.path.rsplit("/", 1)[-1])
        if channels[-1] == "selectSecurity":
            return httpx.Response(200, json={"code": 200, "data": {"allResults": {
                "result": {"columns": [], "dataList": []},
            }}})
        return httpx.Response(200, json={"code": 200, "dataTableDTOList": [{
            "entityCode": SYMBOL, "dateGranularity": "DAY", "fieldSet": fields,
            "rawTable": {
                "headName": [row.session_date.isoformat() for row in history.rows],
                "price": [-1 if i % 4 < 2 else 1 for i in range(len(history.rows))],
                "line": [0] * len(history.rows),
            },
        }]})

    def no_python(*_args: object, **_kwargs: object) -> None:
        pytest.fail("provider-first route must not calculate a local indicator fallback")

    original_derive = service_module._skill_derived_timeline  # pyright: ignore[reportPrivateUsage]
    derive = Mock(wraps=original_derive if wrong_parameters else no_python)
    monkeypatch.setattr(service_module, "_skill_derived_timeline", derive)
    client = MxSaasMarketDataClient(
        api_key="fixture-key", strict_indicator_contracts=True,
        transport=httpx.MockTransport(handler),
    )
    cache = FileCachedHistoricalIndicatorData(client, root=tmp_path)
    store = InMemoryBacktestRunStore()
    create = Mock(wraps=store.create_or_get)
    monkeypatch.setattr(store, "create_or_get", create)
    service = SkillBacktestService(
        history=cast(MxDailyHistoryClient, SimpleNamespace(load=AsyncMock(return_value=history))),
        indicators=cache, store=store,
        indicator_routes=build_skill_indicator_routes(CATALOG, COVERAGE),
    )
    monkeypatch.setattr(service.queue, "enqueue", _manual_enqueue)
    entry = IndicatorCondition(
        indicator_id=indicator_id, definition_version="1.0.0", params=params, trigger=triggers[0],
    )
    strategy = _strategy((history.rows[40].session_date, history.end)).model_copy(update={
        "entry": entry,
        "exit": FirstOfExit(children=(entry.model_copy(update={"trigger": triggers[1]}),)),
    })
    config = _config(run_robustness=False)
    try:
        prepared = asyncio.run(service.prepare_candidate(strategy, config))
        create.assert_not_called()
        if wrong_parameters:
            # Invalid provider fields are not admitted or stored. Each condition
            # is evaluated with its requested parameters on the separate Skill bars.
            # This fixture fails its first supplemental operand: one initial
            # finance call, one operand retry, then one alternate screener call.
            assert channels == ["searchData", "searchData", "selectSecurity"]
            assert not tuple(tmp_path.glob("*.json"))
            assert prepared.indicator_series == ()
            formula_route = replace(service.indicator_routes[indicator_id],
                                    source="skill_ohlcv_python", fallback_source=None)
            exit_condition = strategy.exit.children[0]
            assert isinstance(exit_condition, IndicatorCondition)
            assert prepared.entry_timeline == original_derive(entry, history, route=formula_route)
            assert prepared.exit_timeline == original_derive(
                exit_condition, history, route=formula_route,
            )
            assert prepared.derived_condition_hashes == frozenset({
                canonical_hash(entry), canonical_hash(exit_condition),
            })
            created = service.submit(strategy, config, prepared_inputs=True)
            result = service.execute(created.record.run_id)
            assert result.state is BacktestJobState.SUCCEEDED, result.progress_label
            assert result.result_json is not None
            provenance = json.loads(result.result_json)["summary"]["dataProvenance"]
            assert provenance["indicatorSeries"] == 0
            assert len(provenance["derivedIndicatorEvidence"]) == 2
            assert all(item["source"] == "local_formula_on_eastmoney_skill_ohlcv"
                       for item in provenance["derivedIndicatorEvidence"])
            # Preparation reused by execution: the failed provider is not repeatedly
            # called and the provenance survives the prepared-input cache.
            assert channels == ["searchData", "searchData", "selectSecurity"]
            assert derive.call_count == 2
            return
        derive.assert_not_called()
        assert not prepared.derived_condition_hashes
        assert len(prepared.indicator_series) == 1
        created = service.submit(strategy, config, prepared_inputs=True)
        result = service.execute(created.record.run_id)
        assert result.state is BacktestJobState.SUCCEEDED, result.progress_label
        assert result.result_json is not None
        bundle = json.loads(result.result_json)
        assert bundle["summary"]["tradeCount"] > 0
        provenance = bundle["summary"]["dataProvenance"]
        assert not provenance.get("derivedIndicatorEvidence")
        assert provenance["indicatorSeries"] == 1
        assert channels == ["searchData"]
        assert all(point.values[0].field_code == "price"
                   for point in prepared.indicator_series[0].points)
        cached_files = tuple(tmp_path.glob("*.json"))
        assert len(cached_files) == 1
        stored = json.loads(cached_files[0].read_text())
        if indicator_id == "technical.macd":
            assert stored["request"]["conditionParameters"] == params
            assert "快线周期为12个交易日" in str(calls[0]["query"])
        assert stored["series"]["responseSha256"] == prepared.indicator_series[0].response_sha256
        recipe = provider_binding_for_condition(entry)
        reloaded_cache = FileCachedHistoricalIndicatorData(client, root=tmp_path)
        cached = asyncio.run(reloaded_cache.query_indicator_history(
            instrument_id=SYMBOL, indicator_id=indicator_id,
            provider_indicator_name=recipe.provider_indicator_name, value_names=recipe.value_names,
            start=history.start, end=history.end,
            condition_params=params if indicator_id == "technical.macd" else None,
            expected_session_dates=tuple(row.session_date for row in history.rows),
        ))
        assert cached.cache_status == "disk" and len(calls) == 1
        assert cached.response_sha256 == prepared.indicator_series[0].response_sha256
    finally:
        service.shutdown()


@pytest.mark.parametrize("failure", [
    MxSaasProviderAuthError("credential rejected"),
    MxSaasProviderDataError("historical indicator value is not finite"),
    MxSaasProviderDataError("historical indicator value count does not match session dates"),
    MxSaasProviderDataError("real-time financial provider returned a partial business result"),
], ids=["auth", "nonfinite", "value-count", "partial-result"])
def test_data_corruption_and_auth_are_not_swallowed_by_formula_fallback(
    history: MxDailyHistory, failure: Exception, monkeypatch: pytest.MonkeyPatch,
) -> None:
    derive = Mock(side_effect=AssertionError("unsafe failures must not invoke a formula"))
    monkeypatch.setattr(service_module, "_skill_derived_timeline", derive)
    query = AsyncMock(side_effect=failure)
    service = SkillBacktestService(
        history=cast(MxDailyHistoryClient, SimpleNamespace(load=AsyncMock(return_value=history))),
        indicators=cast(HistoricalIndicatorData, SimpleNamespace(query_indicator_history=query)),
        store=InMemoryBacktestRunStore(),
        indicator_routes=build_skill_indicator_routes(CATALOG, COVERAGE),
    )
    strategy = _strategy((history.rows[40].session_date, history.end), holding_sessions=2)
    try:
        with pytest.raises(type(failure), match=str(failure)):
            asyncio.run(service.prepare_candidate(strategy, _config(run_robustness=False)))
        derive.assert_not_called()
        assert not service._preparation_cache  # pyright: ignore[reportPrivateUsage]
    finally:
        service.shutdown()


@pytest.mark.parametrize("message", [
    "historical indicator response has a different date window",
    "historical indicator response contains duplicate dates",
])
def test_macd_bad_indicator_dates_recover_only_from_independent_verified_history(history, message):
    query = AsyncMock(side_effect=MxSaasProviderDataError(message))
    routes = build_skill_indicator_routes(CATALOG, COVERAGE)
    service = SkillBacktestService(
        history=SimpleNamespace(load=AsyncMock(return_value=history)),
        indicators=SimpleNamespace(query_indicator_history=query),
        store=InMemoryBacktestRunStore(), indicator_routes=routes,
    )
    entry = IndicatorCondition(indicator_id="technical.macd", definition_version="1.0.0",
        params={"fast": 12, "slow": 26, "signal": 9}, trigger="golden_cross")
    exit_rule = entry.model_copy(update={"trigger": "death_cross"})
    strategy = _strategy((history.rows[40].session_date, history.end)).model_copy(update={
        "entry": entry, "exit": FirstOfExit(children=(exit_rule,)),
    })
    try:
        prepared = asyncio.run(service.prepare_candidate(strategy, _config(run_robustness=False)))
        route = replace(routes["technical.macd"], source="skill_ohlcv_python", fallback_source=None)
        assert prepared.indicator_series == ()
        assert prepared.entry_timeline == service_module._skill_derived_timeline(entry, history, route=route)
        assert prepared.exit_timeline == service_module._skill_derived_timeline(exit_rule, history, route=route)
        assert prepared.derived_condition_hashes == frozenset({canonical_hash(entry), canonical_hash(exit_rule)})
    finally:
        service.shutdown()


@pytest.mark.parametrize("period", [6, 14, 21])
def test_rsi_parameter_mismatch_recovers_catalog_wilder_without_substituting_period(history, period):
    query = AsyncMock(side_effect=MxSaasProviderDataError("historical indicator field binding mismatch"))
    routes = build_skill_indicator_routes(CATALOG, COVERAGE)
    service = SkillBacktestService(
        history=SimpleNamespace(load=AsyncMock(return_value=history)),
        indicators=SimpleNamespace(query_indicator_history=query),
        store=InMemoryBacktestRunStore(), indicator_routes=routes,
    )
    entry = IndicatorCondition(indicator_id="technical.rsi", definition_version="1.0.0",
        params={"period": period}, trigger="below", value=Decimal(50))
    exit_rule = entry.model_copy(update={"trigger": "above", "value": 70.0})
    strategy = _strategy((history.rows[40].session_date, history.end)).model_copy(update={
        "entry": entry, "exit": FirstOfExit(children=(exit_rule,)),
    })
    try:
        prepared = asyncio.run(service.prepare_candidate(strategy, _config(run_robustness=False)))
        route = replace(routes["technical.rsi"], source="skill_ohlcv_python", fallback_source=None)
        assert prepared.indicator_series == ()
        assert prepared.entry_timeline == service_module._skill_derived_timeline(entry, history, route=route)
        assert prepared.exit_timeline == service_module._skill_derived_timeline(exit_rule, history, route=route)
        assert prepared.derived_condition_hashes == frozenset({canonical_hash(entry), canonical_hash(exit_rule)})
        assert entry.params["period"] == period
    finally:
        service.shutdown()


def test_consecutive_up_missing_unit_recovers_from_closes_not_unlabelled_values(
    history: MxDailyHistory, tmp_path: Path,
) -> None:
    channels: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        channel = request.url.path.rsplit("/", 1)[-1]
        channels.append(channel)
        if channel == "selectSecurity":
            return httpx.Response(200, json={"code": 200, "data": {"allResults": {
                "result": {"columns": [], "dataList": []},
            }}})
        return httpx.Response(200, json={"code": 200, "dataTableDTOList": [{
            "entityCode": SYMBOL, "dateGranularity": "DAY",
            "fieldSet": [{"returnCode": "streak", "returnSourceCode": "LZTS",
                          "returnName": "连涨天数", "unit": "0"}],
            "rawTable": {
                "headName": [row.session_date.isoformat() for row in history.rows],
                "streak": [999] * len(history.rows),
            },
        }]})

    provider = MxSaasMarketDataClient(
        api_key="fixture-key", strict_indicator_contracts=True,
        transport=httpx.MockTransport(handler),
    )
    service = SkillBacktestService(
        history=cast(MxDailyHistoryClient, SimpleNamespace(load=AsyncMock(return_value=history))),
        indicators=FileCachedHistoricalIndicatorData(provider, root=tmp_path),
        store=InMemoryBacktestRunStore(),
        indicator_routes=build_skill_indicator_routes(CATALOG, COVERAGE),
    )
    condition = IndicatorCondition(
        indicator_id="price.consecutive_up", definition_version="1.0.0",
        params={"days": 3}, trigger="at_least",
    )
    strategy = _strategy(
        (history.rows[40].session_date, history.end), holding_sessions=2,
    ).model_copy(update={"entry": condition})
    try:
        prepared = asyncio.run(service.prepare_candidate(strategy, _config(run_robustness=False)))
        assert channels == ["searchData", "selectSecurity"]
        assert prepared.indicator_series == ()
        assert not tuple(tmp_path.glob("*.json"))
        assert prepared.derived_condition_hashes == frozenset({canonical_hash(condition)})
        assert prepared.entry_timeline[:3] == (None,) * 3
        # Fixture closes rise for nine days, then reset. The rejected all-999
        # response must never make every day a buy signal.
        for index, fact in enumerate(prepared.entry_timeline[3:], start=3):
            assert fact is not None
            assert fact.triggered == (index % 10 >= 3)
            assert fact.evidence[0].validation_status == "local_formula_on_provider_ohlcv"
    finally:
        service.shutdown()


@pytest.mark.parametrize("failure", [
    MxSaasProviderUnavailableError("request unavailable"),
    MxSaasProviderDataError(
        "historical indicator response does not bind exactly one requested security",
    ),
    ProviderIndicatorCacheMissError("no cached provider series"),
    MxSaasProviderDataError("historical indicator fields are all missing"),
    MxSaasProviderDataError("historical indicator response contains only non-daily observations"),
    MxSaasProviderDataError("historical indicator unit is unconfirmed or incompatible"),
], ids=["unavailable", "wrong-stock-discarded", "cache-miss", "missing-fields",
        "non-daily-summary", "unconfirmed-unit"])
@pytest.mark.parametrize("period", [20, 1000])
def test_recoverable_provider_failure_still_respects_formula_warmup(
    history: MxDailyHistory, failure: Exception, period: int,
) -> None:
    query = AsyncMock(side_effect=failure)
    load = AsyncMock(return_value=history)
    service = SkillBacktestService(
        history=cast(MxDailyHistoryClient, SimpleNamespace(load=load)),
        indicators=cast(HistoricalIndicatorData, SimpleNamespace(query_indicator_history=query)),
        store=InMemoryBacktestRunStore(),
        indicator_routes=build_skill_indicator_routes(CATALOG, COVERAGE),
    )
    condition = IndicatorCondition(
        indicator_id="technical.ma", definition_version="1.0.0",
        params={"period": period, "price_field": "close"}, trigger="price_above",
    )
    strategy = _strategy(
        (history.rows[40].session_date, history.end), holding_sessions=2,
    ).model_copy(update={"entry": condition})
    try:
        if period > len(history.rows):
            with pytest.raises(SkillCandidatePreparationError) as caught:
                asyncio.run(service.prepare_candidate(strategy, _config(run_robustness=False)))
            assert caught.value.code == "skill_indicator_history_not_ready"
            assert not service._preparation_cache  # pyright: ignore[reportPrivateUsage]
        else:
            prepared = asyncio.run(service.prepare_candidate(
                strategy, _config(run_robustness=False),
            ))
            assert prepared.entry_timeline[:period - 1] == (None,) * (period - 1)
            assert all(fact is not None for fact in prepared.entry_timeline[period - 1:])
            assert prepared.derived_condition_hashes == frozenset({canonical_hash(condition)})
            assert prepared.indicator_series == ()  # No unverified supplier values are adopted.
        assert load.await_args is not None
        assert load.await_args.kwargs["start"] < strategy.backtest.start
    finally:
        service.shutdown()


@pytest.mark.parametrize("indicator_id", ["price.rolling_high", "volume.relative"])
@pytest.mark.parametrize("period", [15, 20])
def test_fallback_uses_requested_prior_window_not_provider_default(
    history: MxDailyHistory, indicator_id: str, period: int,
) -> None:
    rows = tuple(
        replace(
            _row(row.session_date, raw_open="20" if i < 5 else "11" if i == 20 else "10"),
            volume=3_000_000 if i < 5 else 1_600_000 if i == 20 else 1_000_000,
        ) for i, row in enumerate(history.rows[:23])
    )
    daily = _history(rows)
    query = AsyncMock(side_effect=MxSaasProviderDataError(
        "historical indicator field does not match requested parameters",
    ))
    condition = IndicatorCondition(
        indicator_id=indicator_id, definition_version="1.0.0",
        params={"period": period, "price_field": "close"}
        if indicator_id == "price.rolling_high"
        else {"baseline_period": period, "consecutive_days": 1},
        trigger="new_high" if indicator_id == "price.rolling_high" else "gt_multiple",
        value=None if indicator_id == "price.rolling_high" else 1.5,
    )
    strategy = _strategy(
        (daily.rows[20].session_date, daily.end), holding_sessions=2,
    ).model_copy(update={"entry": condition})
    service = SkillBacktestService(
        history=cast(MxDailyHistoryClient, SimpleNamespace(load=AsyncMock(return_value=daily))),
        indicators=cast(HistoricalIndicatorData, SimpleNamespace(query_indicator_history=query)),
        store=InMemoryBacktestRunStore(),
        indicator_routes=build_skill_indicator_routes(CATALOG, COVERAGE),
    )
    try:
        prepared = asyncio.run(service.prepare_candidate(strategy, _config(run_robustness=False)))
        fact = prepared.entry_timeline[20]
        assert fact is not None and fact.triggered is (period == 15)
        if indicator_id == "price.rolling_high":
            assert fact.left_value == Decimal("11")
            assert fact.right_value == Decimal("10" if period == 15 else "20")
        else:
            expected = Decimal("1.6") if period == 15 else Decimal("1.6") / Decimal("1.5")
            assert fact.left_value == pytest.approx(expected, abs=Decimal("1e-26"))
            assert fact.right_value == Decimal("1.5")
        assert fact.evidence[0].evidence_type == "skill_ohlcv_derived_indicator"
        assert not prepared.indicator_series
    finally:
        service.shutdown()


def test_mixed_provider_and_formula_provenance_is_per_condition_and_refreshable(
    history: MxDailyHistory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_fixed = False
    calls: list[tuple[str, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = str(json.loads(request.content)["query"])
        period = 5 if "5日MA" in query else 3
        channel = request.url.path.rsplit("/", 1)[-1]
        calls.append((channel, period))
        if channel == "selectSecurity":
            return httpx.Response(200, json={"code": 200, "data": {"allResults": {
                "result": {"columns": [], "dataList": []},
            }}})
        actual_period = 9 if period == 5 and not provider_fixed else period
        return httpx.Response(200, json={"code": 200, "dataTableDTOList": [{
            "entityCode": SYMBOL, "dateGranularity": "DAY",
            "fieldSet": [
                {"returnCode": "price", "returnSourceCode": "CLOSE", "returnName": "收盘价",
                 "fixedParamValue": "AdjustFlag=2,CurType=1,Period=1", "unitName": "元"},
                {"returnCode": "line", "returnSourceCode": f"MAJDYDPJ{period}",
                 "returnName": f"{period}日MA简单移动平均", "unitName": "元",
                 "fixedParamValue": f"N={actual_period},AdjustFlag=2,Period=1"},
            ],
            "rawTable": {
                "headName": [row.session_date.isoformat() for row in history.rows],
                "price": [-1 if i % 4 < 2 else 1 for i in range(len(history.rows))],
                "line": [0] * len(history.rows),
            },
        }]})

    client = MxSaasMarketDataClient(
        api_key="fixture-key", strict_indicator_contracts=True,
        transport=httpx.MockTransport(handler),
    )
    service = SkillBacktestService(
        history=cast(MxDailyHistoryClient, SimpleNamespace(load=AsyncMock(return_value=history))),
        indicators=FileCachedHistoricalIndicatorData(client, root=tmp_path),
        store=InMemoryBacktestRunStore(),
        indicator_routes=build_skill_indicator_routes(CATALOG, COVERAGE),
    )
    monkeypatch.setattr(service.queue, "enqueue", _manual_enqueue)
    entry = IndicatorCondition(
        indicator_id="technical.ma", definition_version="1.0.0",
        params={"period": 3, "price_field": "close"}, trigger="price_above",
    )
    exit_condition = entry.model_copy(update={
        "params": {"period": 5, "price_field": "close"}, "trigger": "price_below",
    })
    strategy = _strategy((history.rows[40].session_date, history.end)).model_copy(update={
        "entry": entry, "exit": FirstOfExit(children=(exit_condition,)),
    })
    config = _config(run_robustness=False)
    try:
        prepared = asyncio.run(service.prepare_candidate(strategy, config))
        assert prepared.derived_condition_hashes == frozenset({canonical_hash(exit_condition)})
        assert len(prepared.indicator_series) == 1
        assert Counter(calls) == {
            ("searchData", 3): 1, ("searchData", 5): 2, ("selectSecurity", 5): 1,
        }
        assert len(tuple(tmp_path.glob("*.json"))) == 1
        created = service.submit(strategy, config, prepared_inputs=True)
        result = service.execute(created.record.run_id)
        assert result.state is BacktestJobState.SUCCEEDED and result.result_json
        evidence = json.loads(result.result_json)["summary"]["dataProvenance"]
        assert evidence["indicatorSeries"] == 1
        assert len(evidence["derivedIndicatorEvidence"]) == 1
        assert json.loads(evidence["derivedIndicatorEvidence"][0]["parameters"])["period"] == 5
        provider_fixed = True
        refreshed = asyncio.run(service.prepare_candidate(
            strategy, replace(config, refresh_data=True),
        ))
        assert not refreshed.derived_condition_hashes
        assert len(refreshed.indicator_series) == 2
        assert refreshed.data_version != prepared.data_version
        assert Counter(calls) == {
            ("searchData", 3): 2, ("searchData", 5): 3, ("selectSecurity", 5): 1,
        }
        assert len(tuple(tmp_path.glob("*.json"))) == 2
    finally:
        service.shutdown()


@pytest.mark.parametrize("provider", ["eastmoney_mx_screener", "unexpected_source"])
def test_screener_indicator_source_survives_preparation_execution_and_disk_cache(
    history: MxDailyHistory, tmp_path: Path, provider: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A typed, already-bound provider result isolates the service boundary.
    # Actual screener field decoding is covered separately, not claimed here.
    entry = IndicatorCondition(
        indicator_id="technical.ma", definition_version="1.0.0",
        params={"period": 3, "price_field": "close"}, trigger="price_above",
    )
    binding = provider_binding_for_condition(entry)
    points = tuple(ProviderIndicatorPoint(
        session_date=row.session_date,
        observed_at=datetime.combine(row.session_date, time(7), tzinfo=UTC),
        first_available_at=datetime.combine(row.session_date, time(7), tzinfo=UTC),
        values=(
            ProviderIndicatorValue("price", binding.value_names[0],
                                   Decimal("9" if index % 4 < 2 else "11"), unit="元"),
            ProviderIndicatorValue("line", binding.value_names[1], Decimal("10"), unit="元",
                                   source_parameters="N=3,AdjustFlag=2,Period=1"),
        ),
    ) for index, row in enumerate(history.rows))
    series = ProviderIndicatorSeries(
        provider=provider, instrument_id=SYMBOL, indicator_id=entry.indicator_id,
        requested_start=history.start, requested_end=history.end, points=points,
        response_sha256="sha256:" + "c" * 64, retrieved_at=history.retrieved_at,
        schema_version="fixture.bound_screener_history.v1", query="fixture: dated screener output",
    )
    query = AsyncMock(return_value=series)
    cache = FileCachedHistoricalIndicatorData(
        cast(HistoricalIndicatorData, SimpleNamespace(query_indicator_history=query)),
        root=tmp_path,
    )
    service = SkillBacktestService(
        history=cast(MxDailyHistoryClient, SimpleNamespace(load=AsyncMock(return_value=history))),
        indicators=cache, store=InMemoryBacktestRunStore(),
        indicator_routes=build_skill_indicator_routes(CATALOG, COVERAGE),
    )
    monkeypatch.setattr(service.queue, "enqueue", _manual_enqueue)
    derive = Mock(side_effect=AssertionError("bound screener series must not call local formulas"))
    monkeypatch.setattr(service_module, "_skill_derived_timeline", derive)
    strategy = _strategy((history.rows[40].session_date, history.end)).model_copy(update={
        "entry": entry,
        "exit": FirstOfExit(children=(entry.model_copy(update={"trigger": "price_below"}),)),
    })
    config = _config(run_robustness=False)
    try:
        if provider == "unexpected_source":
            with pytest.raises(ValueError, match="unexpected indicator provider"):
                asyncio.run(service.prepare_candidate(strategy, config))
            return
        prepared = asyncio.run(service.prepare_candidate(strategy, config))
        assert prepared.indicator_series[0].provider == "eastmoney_mx_screener"
        assert not prepared.derived_condition_hashes
        created = service.submit(strategy, config, prepared_inputs=True)
        result = service.execute(created.record.run_id)
        assert result.state is BacktestJobState.SUCCEEDED and result.result_json
        bundle = json.loads(result.result_json)
        provenance = bundle["summary"]["dataProvenance"]
        assert provenance["provider"] == history.provider
        assert {item["provider"] for item in provenance["indicatorFieldEvidence"]} == {provider}
        assert {item["responseSha256"] for item in provenance["indicatorFieldEvidence"]} == {
            series.response_sha256,
        }
        signal_sources = [evidence for activity in bundle["activities"]
                          for evidence in activity["evidence"]]
        assert signal_sources and {item["provider"] for item in signal_sources} == {provider}
        assert not provenance["derivedIndicatorEvidence"]
        stored = json.loads(next(tmp_path.glob("*.json")).read_text())
        assert stored["series"]["provider"] == provider
        cached = FileCachedHistoricalIndicatorData(None, root=tmp_path)
        reloaded = asyncio.run(cached.query_indicator_history(
            instrument_id=SYMBOL, indicator_id=entry.indicator_id,
            provider_indicator_name=binding.provider_indicator_name,
            value_names=binding.value_names,
            start=history.start, end=history.end,
            expected_session_dates=tuple(row.session_date for row in history.rows),
        ))
        assert reloaded.provider == provider and reloaded.cache_status == "disk"
        assert reloaded.response_sha256 == series.response_sha256
        query.assert_awaited_once()
        derive.assert_not_called()
    finally:
        service.shutdown()


def test_ma_cross_price_basis_reaches_skill_and_cannot_reuse_close_basis_cache(
    history: MxDailyHistory, tmp_path: Path,
) -> None:
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = str(json.loads(request.content)["query"])
        queries.append(query)
        basis = "open" if "PriceField=open" in query else "close"
        return httpx.Response(200, json={"code": 200, "dataTableDTOList": [{
            "entityCode": SYMBOL, "dateGranularity": "DAY",
            "fieldSet": [{
                "returnCode": str(period), "returnSourceCode": f"MAJDYDPJ{period}",
                "returnName": f"{period}日MA简单移动平均", "unitName": "元",
                "fixedParamValue": f"N={period},AdjustFlag=2,Period=1,PriceField={basis}",
            } for period in (5, 20)],
            "rawTable": {
                "headName": [row.session_date.isoformat() for row in history.rows],
                "5": [12] * len(history.rows), "20": [10] * len(history.rows),
            },
        }]})

    client = MxSaasMarketDataClient(
        api_key="fixture-key", strict_indicator_contracts=True,
        transport=httpx.MockTransport(handler),
    )
    service = SkillBacktestService(
        history=cast(MxDailyHistoryClient, SimpleNamespace(load=AsyncMock(return_value=history))),
        indicators=FileCachedHistoricalIndicatorData(client, root=tmp_path),
        store=InMemoryBacktestRunStore(),
        indicator_routes=build_skill_indicator_routes(CATALOG, COVERAGE),
    )
    try:
        for basis in ("close", "open"):
            condition = IndicatorCondition(
                indicator_id="technical.ma_cross", definition_version="1.0.0",
                params={"fast_period": 5, "slow_period": 20, "price_field": basis},
                trigger="fast_above_slow",
            )
            strategy = _strategy(
                (history.rows[40].session_date, history.end), holding_sessions=2,
            ).model_copy(update={
                "entry": condition,
            })
            prepared = asyncio.run(service.prepare_candidate(
                strategy, _config(run_robustness=False),
            ))
            assert f"PriceField={basis}" in (
                prepared.indicator_series[0].points[0].values[0].source_parameters or ""
            )
        assert len(queries) == 2
        assert "PriceField=open" not in queries[0] and "PriceField=open" in queries[1]
        stored = [json.loads(path.read_text()) for path in tmp_path.glob("*.json")]
        assert len(stored) == 2
        assert {str(item["request"].get("conditionParameters", {}).get("price_field"))
                for item in stored} == {"None", "open"}
    finally:
        service.shutdown()
