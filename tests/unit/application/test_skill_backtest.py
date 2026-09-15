from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Literal, cast
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from ashare_lab.adapters.market_data.mx_daily_history import (
    MX_BACK_ADJUSTMENT,
    MX_DAILY_HISTORY_PROVIDER,
    MX_LISTING_NO_LIMIT_SOURCE,
    MxDailyHistory,
    MxDailyHistoryBeforeListingError,
    MxDailyHistoryClient,
    MxDailyHistoryFieldsMissingError,
    MxDailyRow,
    MxQueryEvidence,
)
from ashare_lab.adapters.market_data.mx_saas import (
    MxSaasProviderDataError,
    MxSaasProviderUnavailableError,
)
from ashare_lab.adapters.persistence.backtest_runs import InMemoryBacktestRunStore
from ashare_lab.api.result_schemas import BacktestResultBundle
from ashare_lab.application.backtest_submission import (
    BacktestRunConfig,
    _effective_warmup_calendar_days,
)
from ashare_lab.application.skill_backtest import run_skill_backtest
from ashare_lab.application.skill_backtest_service import (
    SkillBacktestService,
    SkillCandidatePreparationError,
    _skill_derived_timeline,
)
from ashare_lab.domain.execution import CapacityMode, LimitHandling
from ashare_lab.domain.market_data import Board, TradingStatus
from ashare_lab.domain.shared import InstrumentId, RunId
from ashare_lab.domain.signals import SignalFact
from ashare_lab.domain.strategy import (
    AllCondition,
    BacktestConfig,
    CatalogRef,
    FirstOfExit,
    HoldingPeriodExit,
    IndicatorCondition,
    Instrument,
    StrategySpec,
)
from ashare_lab.ports.backtest_runs import BacktestJobState
from ashare_lab.ports.provider_indicator_data import (
    HistoricalIndicatorData,
    ProviderIndicatorPoint,
    ProviderIndicatorSeries,
    ProviderIndicatorValue,
)

TZ = ZoneInfo("Asia/Shanghai")
SYMBOL = "300059.SZ"
START = date(2025, 1, 2)


def test_prelisting_range_is_actionable_and_does_not_retry_or_modify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    load = AsyncMock(side_effect=MxDailyHistoryBeforeListingError(
        start=date(2020, 3, 10), listing_date=date(2021, 4, 9),
    ))
    service = SkillBacktestService(
        history=cast(MxDailyHistoryClient, SimpleNamespace(load=load)),
        indicators=cast(HistoricalIndicatorData, object()), store=InMemoryBacktestRunStore(),
    )
    monkeypatch.setattr(service.queue, "enqueue", lambda _run_id: "manual-test")
    try:
        strategy = _strategy((date(2020, 9, 6), date(2026, 9, 6)))
        created = service.submit(strategy, BacktestRunConfig(slippage_bps=Decimal("7")))
        result = service.execute(created.record.run_id)
        assert result.error_code == "skill_history_before_listing"
        assert "2021-04-09" in result.progress_label
        assert "2020-09-06" in result.progress_label
        assert "不会自动缩短区间" in result.progress_label
        assert result.strategy_json == created.record.strategy_json
        assert result.config_json == created.record.config_json
        assert load.await_count == 1
        assert result.result_json is None
    finally:
        service.shutdown()


def test_warmup_only_prelisting_keeps_requested_range(monkeypatch: pytest.MonkeyPatch) -> None:
    marker = object()
    listing = START - timedelta(days=30)
    load = AsyncMock(side_effect=[
        MxDailyHistoryBeforeListingError(start=START - timedelta(days=180), listing_date=listing),
        marker,
    ])
    service = SkillBacktestService(
        history=cast(MxDailyHistoryClient, SimpleNamespace(load=load)),
        indicators=cast(HistoricalIndicatorData, object()), store=InMemoryBacktestRunStore(),
    )
    monkeypatch.setattr(service.queue, "enqueue", lambda _run_id: "manual-test")
    try:
        import asyncio

        strategy = _strategy((START, START + timedelta(days=10)))
        config = BacktestRunConfig()
        created = service.submit(strategy, config)
        result = asyncio.run(service._load_history(created.record.run_id, strategy, config, 180))
        assert result is marker
        assert load.await_args_list[1].kwargs["start"] == listing
        assert load.await_args_list[1].kwargs["end"] == strategy.backtest.end
        assert strategy.backtest.start == START
        assert load.await_count == 2
    finally:
        service.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(("refresh", "prepared_inputs", "read_count"), [
    (False, True, 1), (True, True, 1), (True, False, 2),
])
async def test_candidate_preparation_is_runless_allows_zero_signals_and_execution_reuses_it(
    monkeypatch: pytest.MonkeyPatch, refresh: bool, prepared_inputs: bool, read_count: int,
) -> None:
    rows = tuple(_row(START + timedelta(days=i), raw_open="10") for i in range(8))
    strategy = _strategy((START, rows[-1].session_date), holding_sessions=2).model_copy(update={
        "entry": _condition(trigger="price_crosses_above").model_copy(
            update={"params": {"period": 2, "price_field": "close"}},
        ),
    })
    load = AsyncMock(return_value=_history(rows))
    query = AsyncMock(side_effect=AssertionError("no provider exit series needed"))
    store = InMemoryBacktestRunStore()
    service = SkillBacktestService(
        history=cast(MxDailyHistoryClient, SimpleNamespace(load=load)),
        indicators=cast(HistoricalIndicatorData, SimpleNamespace(query_indicator_history=query)),
        store=store,
        market_calendar_loader=lambda: (tuple(row.session_date for row in rows), {}, b"fixture-calendar"),
    )
    monkeypatch.setattr(service.queue, "enqueue", lambda _run_id: "manual-test")
    created_records: list[object] = []
    original_create = store.create_or_get

    def create(record: object) -> object:
        created_records.append(record)
        return original_create(record)  # type: ignore[arg-type]

    monkeypatch.setattr(store, "create_or_get", create)
    try:
        config = _config(run_robustness=False, refresh_data=refresh)
        prepared = await service.prepare_candidate(strategy, config)
        assert created_records == []
        assert prepared is await service.prepare_candidate(
            strategy, replace(config, refresh_data=False),
        )
        assert prepared.entry_timeline[:2] == (None, None)
        assert all(not item.triggered for item in prepared.entry_timeline if item is not None)
        assert prepared.exit_timeline == (None,) * len(rows)
        assert prepared.data_version.startswith("sha256:")
        created = service.submit(strategy, config, prepared_inputs=True) if prepared_inputs \
            else service.submit(strategy, config)
        assert json.loads(created.record.config_json)["refresh_data"] is refresh
        manifest = json.loads(created.record.manifest_json)
        assert manifest["preparedInputs"] is prepared_inputs
        assert manifest["refreshRequested"] is refresh
        assert manifest["refreshConsumed"] is (prepared_inputs and refresh)
        record = await asyncio.to_thread(service.execute, created.record.run_id)
        assert record.state is BacktestJobState.SUCCEEDED, record.progress_label
        assert len(created_records) == 1
        assert load.await_count == read_count
        assert load.await_args is not None
        assert load.await_args.kwargs["force_refresh"] is refresh
        assert record.result_json is not None
        provenance = json.loads(record.result_json)["summary"]["dataProvenance"]
        assert provenance.get("refreshRequested", False) is refresh
        from hashlib import sha256
        calendar = provenance["holdingCalendar"]
        assert calendar["fileSha256"] == sha256(b"fixture-calendar").hexdigest()
        assert calendar["sessionCount"] == len(rows)
        assert calendar["sessionsSha256"] == sha256("\n".join(
            row.session_date.isoformat() for row in rows
        ).encode()).hexdigest()
        assert calendar["holdingPeriodConvention"] == "buy_session_D0_target_D_plus_N_open"
        query.assert_not_awaited()
    finally:
        service.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["hidden_unknown_leaf", "last_day_only", "no_next_trading_day"])
async def test_candidate_preparation_requires_each_leaf_and_next_session(case: str) -> None:
    rows = tuple(_row(START + timedelta(days=i), raw_open="10") for i in range(5))
    short = _condition(trigger="price_crosses_above").model_copy(
        update={"params": {"period": 2, "price_field": "close"}},
    )
    if case == "hidden_unknown_leaf":
        entry = AllCondition(children=(short, short.model_copy(
            update={"params": {"period": 20, "price_field": "close"}},
        )))
    elif case == "last_day_only":
        entry = short.model_copy(update={"params": {"period": 4, "price_field": "close"}})
    else:
        entry = short
        rows = (rows[0], *tuple(replace(
            row, trading_status=TradingStatus.SUSPENDED, volume=0, amount=Decimal("0"),
            upper_limit=None, lower_limit=None, limit_source="not_applicable_suspended",
        ) for row in rows[1:]))
    strategy = _strategy((START, rows[-1].session_date), holding_sessions=2).model_copy(
        update={"entry": entry},
    )
    service = SkillBacktestService(
        history=cast(MxDailyHistoryClient, SimpleNamespace(
            load=AsyncMock(return_value=_history(rows)),
        )),
        indicators=cast(HistoricalIndicatorData, object()), store=InMemoryBacktestRunStore(),
    )
    try:
        with pytest.raises(SkillCandidatePreparationError) as failure:
            await service.prepare_candidate(strategy, _config())
        assert failure.value.code == (
            "skill_history_no_execution_session" if case == "no_next_trading_day"
            else "skill_indicator_history_not_ready"
        )
        if case == "hidden_unknown_leaf":
            assert failure.value.condition_path == "entry.$.children[1]"
            assert failure.value.indicator_id == "technical.ma"
        assert not service._preparation_cache
    finally:
        service.shutdown()


@pytest.mark.asyncio
async def test_candidate_preparation_clamps_only_warmup_and_keeps_user_period() -> None:
    rows = tuple(_row(START + timedelta(days=i), raw_open="10") for i in range(8))
    history = replace(_history(rows), listing_date=START)
    strategy = _strategy((START + timedelta(days=1), rows[-1].session_date), holding_sessions=2)
    strategy = strategy.model_copy(update={"entry": strategy.entry.model_copy(
        update={"params": {"period": 2, "price_field": "close"}},
    )})
    original = strategy.model_dump_json()
    load = AsyncMock(side_effect=[
        MxDailyHistoryBeforeListingError(start=START - timedelta(days=180), listing_date=START),
        history,
    ])
    service = SkillBacktestService(
        history=cast(MxDailyHistoryClient, SimpleNamespace(load=load)),
        indicators=cast(HistoricalIndicatorData, object()), store=InMemoryBacktestRunStore(),
    )
    try:
        prepared = await service.prepare_candidate(strategy, _config())
        assert prepared.history is history
        assert load.await_count == 2
        assert load.await_args_list[1].kwargs["start"] == START
        assert strategy.model_dump_json() == original
    finally:
        service.shutdown()


@pytest.mark.asyncio
async def test_candidate_success_cache_expires_is_bounded_and_keys_all_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ashare_lab.application import skill_backtest_service as module

    now = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: now[0])
    monkeypatch.setattr(module, "_PREPARATION_CACHE_MAX_ENTRIES", 2)
    rows = tuple(_row(START + timedelta(days=i), raw_open="10") for i in range(8))
    history = _history(rows)

    async def load_history(**kwargs: object) -> MxDailyHistory:
        return replace(history, instrument_id=str(kwargs["instrument_id"]))

    load = AsyncMock(side_effect=load_history)
    service = SkillBacktestService(
        history=cast(MxDailyHistoryClient, SimpleNamespace(load=load)),
        indicators=cast(HistoricalIndicatorData, object()), store=InMemoryBacktestRunStore(),
    )
    strategy = _strategy((START, rows[-1].session_date), holding_sessions=2).model_copy(update={
        "entry": _condition(trigger="price_crosses_above").model_copy(
            update={"params": {"period": 2, "price_field": "close"}},
        ),
    })
    config = _config()
    try:
        first = await service.prepare_candidate(strategy, config)
        assert first is await service.prepare_candidate(strategy, config)
        assert load.await_count == 1
        changes = (
            (strategy.model_copy(update={"instrument": Instrument(symbol="600519.SH")}), config),
            (strategy.model_copy(update={"entry": strategy.entry.model_copy(
                update={"params": {"period": 3, "price_field": "close"}},
            )}), config),
            (strategy.model_copy(update={"backtest": strategy.backtest.model_copy(
                update={"start": START + timedelta(days=1)},
            )}), config),
            (strategy, replace(config, slippage_bps=Decimal("9"))),
        )
        for changed_strategy, changed_config in changes:
            await service.prepare_candidate(changed_strategy, changed_config)
            assert len(service._preparation_cache) == 2
        assert load.await_count == 5
        await service.prepare_candidate(strategy, config)
        assert load.await_count == 6  # First request was evicted.
        now[0] += module._PREPARATION_CACHE_TTL_SECONDS + 1
        await service.prepare_candidate(strategy, config)
        assert load.await_count == 7
        forced = await service.prepare_candidate(strategy, replace(config, refresh_data=True))
        assert load.await_args is not None
        assert load.await_args.kwargs["force_refresh"] is True
        assert forced is await service.prepare_candidate(strategy, config)
        assert load.await_count == 8
        service.indicator_routes = {**service.indicator_routes, "technical.ma": replace(
            service.indicator_routes["technical.ma"], formula_summary="new formula version",
        )}
        await service.prepare_candidate(strategy, config)
        assert load.await_count == 9
    finally:
        service.shutdown()


@pytest.mark.asyncio
async def test_candidate_temporary_failure_is_not_cached_or_global_unavailability() -> None:
    rows = tuple(_row(START + timedelta(days=i), raw_open="10") for i in range(25))
    load = AsyncMock(side_effect=[
        MxDailyHistoryFieldsMissingError(("收盘价",), start=START, end=rows[-1].session_date),
        _history(rows),
    ])
    service = SkillBacktestService(
        history=cast(MxDailyHistoryClient, SimpleNamespace(load=load)),
        indicators=cast(HistoricalIndicatorData, object()), store=InMemoryBacktestRunStore(),
    )
    strategy = _strategy((START, rows[-1].session_date))
    availability = dict(service.indicator_unavailable_reasons)
    try:
        with pytest.raises(MxDailyHistoryFieldsMissingError):
            await service.prepare_candidate(strategy, _config())
        assert not service._preparation_cache
        await service.prepare_candidate(strategy, _config())
        assert load.await_count == 2
        assert service.indicator_unavailable_reasons == availability
    finally:
        service.shutdown()


@pytest.mark.asyncio
async def test_candidate_force_refresh_cannot_be_overwritten_by_older_inflight() -> None:
    rows = tuple(_row(START + timedelta(days=i), raw_open="10") for i in range(25))
    old_history = _history(rows)
    new_history = _history(tuple(_row(row.session_date, raw_open="11") for row in rows))
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def load_history(**_kwargs: object) -> MxDailyHistory:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()
            return old_history
        return new_history

    service = SkillBacktestService(
        history=cast(MxDailyHistoryClient, SimpleNamespace(load=load_history)),
        indicators=cast(HistoricalIndicatorData, object()), store=InMemoryBacktestRunStore(),
    )
    strategy, config = _strategy((START, rows[-1].session_date)), _config()
    old_task = asyncio.create_task(service.prepare_candidate(strategy, config))
    try:
        await started.wait()
        refreshed = await service.prepare_candidate(strategy, replace(config, refresh_data=True))
        release.set()
        old = await old_task
        assert old.data_version != refreshed.data_version
        assert refreshed is await service.prepare_candidate(strategy, config)
        assert calls == 2
    finally:
        release.set()
        await asyncio.gather(old_task, return_exceptions=True)
        service.shutdown()


@pytest.mark.asyncio
async def test_candidate_provider_series_are_reused_and_current_only_is_not_ready() -> None:
    rows = tuple(_row(START + timedelta(days=i), raw_open="10") for i in range(5))
    history = _history(rows)
    current_only = False

    async def query_history(**kwargs: object) -> ProviderIndicatorSeries:
        points = tuple(
            ProviderIndicatorPoint(
                session_date=row.session_date,
                observed_at=datetime.combine(row.session_date, datetime.min.time(), tzinfo=TZ),
                first_available_at=datetime.combine(
                    row.session_date, datetime.min.time(), tzinfo=TZ,
                ),
                values=tuple(ProviderIndicatorValue(name, name, Decimal("50"))
                             for name in cast(tuple[str, ...], kwargs["value_names"])),
            ) for row in (rows[-1:] if current_only else rows[1:])
        )
        return ProviderIndicatorSeries(
            provider=MX_DAILY_HISTORY_PROVIDER, instrument_id=SYMBOL, indicator_id="technical.rsi",
            requested_start=history.start, requested_end=history.end, points=points,
            response_sha256="sha256:" + "b" * 64, retrieved_at=history.retrieved_at,
            schema_version="test.provider.v1", query="controlled fixture, not live evidence",
        )

    query = AsyncMock(side_effect=query_history)
    service = SkillBacktestService(
        history=cast(MxDailyHistoryClient, SimpleNamespace(load=AsyncMock(return_value=history))),
        indicators=cast(HistoricalIndicatorData, SimpleNamespace(query_indicator_history=query)),
        store=InMemoryBacktestRunStore(),
    )
    strategy = _strategy((START, rows[-1].session_date), holding_sessions=2).model_copy(update={
        "entry": IndicatorCondition(indicator_id="technical.rsi", definition_version="1.0.0",
                                    params={"period": 14}, trigger="above", value=70),
    })
    try:
        prepared = await service.prepare_candidate(strategy, _config())
        assert len(prepared.indicator_series) == 1
        assert prepared.entry_timeline[0] is None
        assert prepared is await service.prepare_candidate(strategy, _config())
        assert query.await_count == 1
        current_only = True
        with pytest.raises(SkillCandidatePreparationError, match="历史指标值"):
            await service.prepare_candidate(strategy, replace(_config(), refresh_data=True))
        assert not service._preparation_cache
        assert query.await_count == 2
        assert "technical.rsi" in service.available_indicator_ids
    finally:
        service.shutdown()


def test_candidate_execution_cancellation_during_preparation_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = tuple(_row(START + timedelta(days=i), raw_open="10") for i in range(25))
    store = InMemoryBacktestRunStore()
    run_ids: list[RunId] = []

    async def load_history(**_kwargs: object) -> MxDailyHistory:
        store.transition(
            run_ids[0], expected=(BacktestJobState.RUNNING_DATA,),
            target=BacktestJobState.CANCEL_REQUESTED, progress_percent=10, progress_label="取消中",
        )
        return _history(rows)

    service = SkillBacktestService(
        history=cast(MxDailyHistoryClient, SimpleNamespace(
            load=AsyncMock(side_effect=load_history),
        )),
        indicators=cast(HistoricalIndicatorData, object()), store=store,
    )
    monkeypatch.setattr(service.queue, "enqueue", lambda _run_id: "manual-test")
    try:
        created = service.submit(_strategy((START, rows[-1].session_date)), _config())
        run_ids.append(created.record.run_id)
        record = service.execute(created.record.run_id)
        assert record.state is BacktestJobState.CANCELLED
        assert not service._preparation_cache
    finally:
        service.shutdown()


@pytest.mark.parametrize(
    ("failure", "expected_code", "expected_label"),
    [
        (
            MxSaasProviderUnavailableError("private", tool="searchData", reason="read_timeout"),
            "skill_mx_read_timeout",
            "等待东方财富查数 Skill响应超时",
        ),
        (
            MxSaasProviderUnavailableError(
                "private",
                tool="selectSecurity",
                reason="connect_timeout",
            ),
            "skill_mx_connect_timeout",
            "连接东方财富选股 Skill超时",
        ),
        (
            MxSaasProviderUnavailableError(
                "private",
                tool="searchData",
                reason="http_error",
                http_status=503,
            ),
            "skill_mx_http_error",
            "东方财富查数 Skill返回服务异常（HTTP 503）",
        ),
    ],
)
def test_skill_fetch_failure_reports_exact_step_without_leaking_provider_text(
    failure: MxSaasProviderUnavailableError,
    expected_code: str,
    expected_label: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Fault-injection regression only; real-data acceptance is a separate run.
    service = SkillBacktestService(
        history=cast(MxDailyHistoryClient, SimpleNamespace(load=AsyncMock(side_effect=failure))),
        indicators=cast(HistoricalIndicatorData, object()),
        store=InMemoryBacktestRunStore(),
    )
    monkeypatch.setattr(service.queue, "enqueue", lambda _run_id: "manual-test")
    try:
        created = service.submit(
            _strategy((START, START + timedelta(days=10))),
            BacktestRunConfig(),
        )
        result = service.execute(created.record.run_id)
        assert result.error_code == expected_code
        assert expected_label in result.progress_label
        assert result.progress_percent == 10
        assert result.result_json is None
        assert "private" not in result.progress_label + caplog.text
    finally:
        service.shutdown()


@pytest.mark.parametrize(("message", "reason", "label"), [
    ("historical indicator response omitted rawTable",
     "protocol_raw_table_missing", "缺少逐日原始表"),
    ("historical indicator response contains duplicate dates",
     "data_dates_mismatch", "日期未与本次查询对齐"),
    ("real-time market-data provider rejected the request",
     "provider_query_rejected", "未接受本次数据查询"),
    ("https://private.invalid/?token=secret", "data_validation_failed", "未通过完整性校验"),
])
def test_data_failure_keeps_protocol_and_validation_diagnostics_separate(
    message: str, reason: str, label: str,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    failure = MxSaasProviderDataError(message, tool="searchData")
    load = AsyncMock(side_effect=failure)
    service = SkillBacktestService(
        history=cast(MxDailyHistoryClient, SimpleNamespace(load=load)),
        indicators=cast(HistoricalIndicatorData, object()), store=InMemoryBacktestRunStore(),
    )
    monkeypatch.setattr(service.queue, "enqueue", lambda _run_id: "manual-test")
    try:
        created = service.submit(
            _strategy((START, START + timedelta(days=10))), BacktestRunConfig(),
        )
        result = service.execute(created.record.run_id)
        assert result.error_code == "skill_MxSaasProviderDataError"
        assert label in result.progress_label
        assert f"reason={reason}" in caplog.text
        assert result.result_json is None
        assert result.strategy_json == created.record.strategy_json
        assert result.config_json == created.record.config_json
        assert load.await_count == 1
        assert "private.invalid" not in result.progress_label + caplog.text
        assert "token=secret" not in result.progress_label + caplog.text
    finally:
        service.shutdown()


@pytest.mark.parametrize("with_range", [True, False])
def test_missing_history_explains_fetch_range_without_changing_strategy_or_config(
    with_range: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = MxDailyHistoryFieldsMissingError(
        ("涨停价", "跌停价"),
        start=date(2010, 3, 9) if with_range else None,
        end=date(2012, 3, 8) if with_range else None,
    )
    service = SkillBacktestService(
        history=cast(MxDailyHistoryClient, SimpleNamespace(load=AsyncMock(side_effect=failure))),
        indicators=cast(HistoricalIndicatorData, object()),
        store=InMemoryBacktestRunStore(),
    )
    monkeypatch.setattr(service.queue, "enqueue", lambda _run_id: "manual-test")
    strategy = _strategy((date(2010, 9, 5), date(2026, 9, 5))).model_copy(
        update={
            "instrument": Instrument(symbol="600519.SH"),
        }
    )
    try:
        created = service.submit(strategy, BacktestRunConfig(slippage_bps=Decimal("0")))
        result = service.execute(created.record.run_id)
        assert result.error_code == "skill_history_fields_missing"
        expected_range = "2010-03-09 至 2012-03-08" if with_range else "本次区间"
        assert f"查询 {expected_range} 的历史数据时" in result.progress_label
        assert "东方财富未返回涨停价、跌停价" in result.progress_label
        assert "原区间和规则已保留" in result.progress_label
        assert "可修改区间或稍后重新读取" in result.progress_label
        assert result.strategy_json == created.record.strategy_json
        assert result.config_json == created.record.config_json
        assert result.result_json is None
    finally:
        service.shutdown()


def _condition(*, trigger: str) -> IndicatorCondition:
    return IndicatorCondition(
        indicator_id="technical.ma",
        definition_version="1.0.0",
        params={"period": 20, "price_field": "close"},
        trigger=trigger,
    )


def _strategy(
    dates: tuple[date, ...],
    *,
    holding_sessions: int | None = None,
) -> StrategySpec:
    exits = (
        (HoldingPeriodExit(sessions=holding_sessions),)
        if holding_sessions is not None
        else (_condition(trigger="price_crosses_below"),)
    )
    return StrategySpec(
        catalog=CatalogRef(catalog_id="cn_a.signals", release_version="2026.08.28"),
        instrument=Instrument(symbol=SYMBOL),
        entry=_condition(trigger="price_crosses_above"),
        exit=FirstOfExit(children=exits),
        backtest=BacktestConfig(
            start=dates[0],
            end=dates[-1],
            initial_cash_cny=10_000,
        ),
    )


def test_fixed_slippage_is_added_to_raw_price_before_adjustment():
    from ashare_lab.application.skill_backtest import _adjusted_execution_price
    row = _row(date(2025, 1, 2), raw_open="10", adjusted_scale="2")
    config = BacktestRunConfig(slippage_bps=Decimal(0), slippage_cny=Decimal("0.02"))
    assert _adjusted_execution_price(row, config, side="buy") == Decimal("20.04")
    assert _adjusted_execution_price(row, config, side="sell") == Decimal("19.96")


def test_fixed_slippage_cannot_turn_negative_sale_price_into_limit_down_fill():
    from ashare_lab.application.skill_backtest import _adjusted_execution_price
    row = _row(date(2025, 1, 2), raw_open="10")
    config = BacktestRunConfig(slippage_bps=Decimal(0), slippage_cny=Decimal("11"))
    assert _adjusted_execution_price(row, config, side="sell") <= 0


def test_research_execution_uses_shared_bar_bounds_and_zero_volume_gate():
    from ashare_lab.application.skill_backtest import _adjusted_execution_price, _execution_capacity
    from ashare_lab.domain.execution import CapacityMode
    row = _row(date(2025, 1, 2), raw_open="10", adjusted_scale="2")
    narrow = replace(row, raw_high=Decimal("10.01"), raw_low=Decimal("9.99"),
                     adjusted_high=Decimal("20.02"), adjusted_low=Decimal("19.98"))
    config = BacktestRunConfig(slippage_bps=Decimal(5), slippage_cny=Decimal(".10"),
                              capacity_mode=CapacityMode.UNLIMITED)
    assert _adjusted_execution_price(narrow, config, side="buy") == Decimal("20.02")
    assert _adjusted_execution_price(narrow, config, side="sell") == Decimal("19.98")
    no_trades = replace(row, volume=0, amount=Decimal(0))
    for side in ("buy", "sell"):
        assert _execution_capacity(row=no_trades, previous_row=row, side=side, config=config) == (
            "no_market_trades", Decimal(0))


def _row(
    session_date: date,
    *,
    raw_open: str,
    raw_close: str | None = None,
    adjusted_scale: str = "1",
    status: TradingStatus = TradingStatus.TRADING,
    at_limit: Literal["up", "down"] | None = None,
) -> MxDailyRow:
    open_value = Decimal(raw_open)
    close_value = Decimal(raw_close or raw_open)
    high = max(open_value, close_value) + Decimal("1")
    low = max(Decimal("0.01"), min(open_value, close_value) - Decimal("1"))
    scale = Decimal(adjusted_scale)
    if status is TradingStatus.SUSPENDED:
        upper = lower = None
        volume = 0
        amount = Decimal("0")
        limit_source: Literal["eastmoney_mx_finance_data", "not_applicable_suspended"] = (
            "not_applicable_suspended"
        )
    else:
        upper = open_value if at_limit == "up" else max(high + Decimal("1"), open_value)
        lower = (
            open_value
            if at_limit == "down"
            else max(Decimal("0.01"), min(low - Decimal("0.5"), open_value - Decimal("0.01")))
        )
        volume = 1_000_000
        amount = open_value * volume
        limit_source = "eastmoney_mx_finance_data"
    return MxDailyRow(
        session_date=session_date,
        raw_open=open_value,
        raw_high=high,
        raw_low=low,
        raw_close=close_value,
        raw_preclose=open_value,
        adjusted_open=open_value * scale,
        adjusted_high=high * scale,
        adjusted_low=low * scale,
        adjusted_close=close_value * scale,
        volume=volume,
        amount=amount,
        trading_status=status,
        is_st=False,
        upper_limit=upper,
        lower_limit=lower,
        limit_source=limit_source,
    )


def _history(rows: tuple[MxDailyRow, ...]) -> MxDailyHistory:
    retrieved_at = datetime(2025, 2, 1, tzinfo=UTC)
    return MxDailyHistory(
        instrument_id=SYMBOL,
        board=Board.CHINEXT,
        listing_date=date(2010, 1, 1),
        start=rows[0].session_date,
        end=rows[-1].session_date,
        retrieved_at=retrieved_at,
        provider=MX_DAILY_HISTORY_PROVIDER,
        adjustment=MX_BACK_ADJUSTMENT,
        rows=rows,
        query_evidence=(
            MxQueryEvidence(
                purpose="unit-test-fixture",
                provider=MX_DAILY_HISTORY_PROVIDER,
                query="fixture only; not live acceptance evidence",
                retrieved_at=retrieved_at,
                response_sha256=f"sha256:{'0' * 64}",
                row_count=len(rows),
            ),
        ),
    )


def _fact(session_date: date, *, triggered: bool, ref: str) -> SignalFact:
    observed_at = datetime.combine(session_date, datetime.min.time().replace(hour=15), tzinfo=TZ)
    return SignalFact(
        instrument_id=InstrumentId(SYMBOL),
        session_date=session_date,
        condition_ref=ref,
        triggered=triggered,
        observed_at=observed_at,
        available_at=observed_at,
        reason=f"{ref}:{'true' if triggered else 'false'}",
    )


@pytest.mark.asyncio
async def test_price_threshold_recovers_from_bad_indicator_using_verified_raw_closes():
    from ashare_lab.adapters.market_data.mx_saas import _MismatchedIndicatorFieldError
    rows = tuple(_row(START + timedelta(days=i), raw_open=value, adjusted_scale="10")
                 for i, value in enumerate(("17", "19", "21")))
    entry = IndicatorCondition(indicator_id="price.close", definition_version="1.0.0",
                               params={}, trigger="below", value=18)
    strategy = _strategy((START, rows[-1].session_date)).model_copy(update={
        "entry": entry,
        "exit": FirstOfExit(children=(entry.model_copy(update={"trigger": "above", "value": 20}),)),
    })
    query = AsyncMock(side_effect=_MismatchedIndicatorFieldError("historical indicator field mismatch"))
    service = SkillBacktestService(history=cast(MxDailyHistoryClient, SimpleNamespace()),
        indicators=cast(HistoricalIndicatorData, SimpleNamespace(query_indicator_history=query)),
        store=InMemoryBacktestRunStore())
    derived: set[str] = set()
    try:
        entries, exits, series = await service._signals(strategy, _history(rows),
                                                       derived_condition_hashes=derived)
        assert [fact.triggered for fact in entries] == [True, False, False]
        assert [fact.triggered for fact in exits] == [False, False, True]
        assert series == () and len(derived) == 2
        assert all(fact.evidence[0].provider == MX_DAILY_HISTORY_PROVIDER for fact in entries)
    finally:
        service.shutdown()


@pytest.mark.parametrize("indicator_id", ["price.rolling_high", "volume.relative"])
def test_skill_derived_window_is_causal_and_preserves_source(indicator_id: str) -> None:
    # Formula-boundary fixture only; live model/data acceptance is separate.
    rows = tuple(_row(START + timedelta(days=i), raw_open="10") for i in range(20))
    suspended = _row(START + timedelta(days=20), raw_open="10", status=TradingStatus.SUSPENDED)
    breakout = replace(
        _row(START + timedelta(days=21), raw_open="11"),
        volume=1_500_000,
    )
    future = _row(START + timedelta(days=22), raw_open="999")
    condition = IndicatorCondition(
        indicator_id=indicator_id,
        definition_version="1.0.0",
        params={"period": 20, "price_field": "close"}
        if indicator_id == "price.rolling_high"
        else {"baseline_period": 20, "consecutive_days": 1},
        trigger="new_high" if indicator_id == "price.rolling_high" else "gte_multiple",
        value=None if indicator_id == "price.rolling_high" else Decimal("1.5"),
    )
    assert indicator_id in SkillBacktestService.available_indicator_ids
    assert indicator_id not in SkillBacktestService.indicator_unavailable_reasons
    timeline = _skill_derived_timeline(condition, _history((*rows, suspended, breakout)))
    assert timeline[:21] == (None,) * 21
    fact = timeline[-1]
    assert fact is not None and fact.triggered
    expected = ("11", "10") if indicator_id == "price.rolling_high" else ("1.5", "1.5")
    assert (fact.left_value, fact.right_value) == tuple(Decimal(value) for value in expected)
    assert fact.evidence[0].provider == MX_DAILY_HISTORY_PROVIDER
    assert fact.evidence[0].evidence_type == "skill_ohlcv_derived_indicator"
    extended = _skill_derived_timeline(condition, _history((*rows, suspended, breakout, future)))
    assert extended[:-1] == timeline


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["complete", "long_holiday", "suspended_prior",
                                 "missing_prior", "missing_evidence", "conflicting_overlap"])
async def test_signal_boundary_ex_date_loads_prior_close_as_context_only(case):
    from ashare_lab.adapters.market_data.local_price_plan_actions import source_backed_price_rebases
    from ashare_lab.application.minute_replay_input import MinuteReplayDataError
    from ashare_lab.domain.market_data import CorporateActionKind
    from tests.unit.portfolio.test_corporate_actions import _action

    previous_day = date(2025, 9, 30) if case == "long_holiday" else date(2025, 1, 2)
    first_day = date(2025, 10, 9) if case == "long_holiday" else date(2025, 1, 3)
    days = tuple(first_day + timedelta(days=i) for i in range(4))
    rows = tuple(_row(day, raw_open=price)
                 for day, price in zip(days, ("9.5", "10", "11", "12")))
    original = _history(rows)
    original = replace(original, query_evidence=(replace(
        original.query_evidence[0], purpose="raw_prices"),))
    prior = _row(previous_day, raw_open="10", status=(
        TradingStatus.SUSPENDED if case == "suspended_prior" else TradingStatus.TRADING))
    supplement = _history((prior, rows[0]))
    supplement = replace(supplement, query_evidence=(replace(
        supplement.query_evidence[0], purpose="raw_prices", response_sha256="sha256:" + "b" * 64),))
    if case == "missing_prior":
        supplement = replace(supplement, rows=(rows[0],))
    elif case == "missing_evidence":
        supplement = replace(supplement, query_evidence=(replace(
            supplement.query_evidence[0], purpose="adjusted_prices"),))
    elif case == "conflicting_overlap":
        supplement = replace(supplement, rows=(prior, replace(rows[0], raw_close=Decimal("9.4"))))
    load = AsyncMock(side_effect=(original, supplement))
    action = _action(CorporateActionKind.CASH_DIVIDEND, cash_per_share=Decimal(".5"),
                     pay_date=date(2025, 1, 6))
    action = replace(action, record_date=previous_day, ex_date=days[0], cash_pay_date=days[1])
    action_result = SimpleNamespace(instrument_id=InstrumentId(SYMBOL), start=days[0],
                                   end=days[-1], corporate_actions=(action,))
    corporate_calls = []

    def corporate_loader(strategy, history, *, required_start):
        corporate_calls.append((history, required_start))
        rebases = source_backed_price_rebases(action_result, history)
        return SimpleNamespace(price_rebases=rebases, evidence={"factorSource": rebases[0].source_sha256})

    condition = _condition(trigger="price_crosses_above").model_copy(
        update={"params": {"period": 2, "price_field": "close"}})
    strategy = _strategy(days, holding_sessions=1).model_copy(update={"entry": condition})
    original_strategy = strategy.model_dump_json()
    service = SkillBacktestService(history=SimpleNamespace(load=load), indicators=object(),
        store=InMemoryBacktestRunStore(), signal_corporate_loader=corporate_loader,
        market_calendar_loader=lambda: ((previous_day, *days), {}, b"fixture-calendar"))
    try:
        if case in {"missing_prior", "missing_evidence", "conflicting_overlap"}:
            with pytest.raises(MinuteReplayDataError, match="corporate_action_prior_close_missing"):
                await service.prepare_candidate(strategy, BacktestRunConfig())
            assert len(corporate_calls) == 1
            return
        prepared = await service.prepare_candidate(strategy, BacktestRunConfig())
        assert load.await_count == 2
        assert load.await_args.kwargs == dict(instrument_id=SYMBOL, start=previous_day,
                                             end=days[0], force_refresh=False)
        assert prepared.history is original
        assert strategy.model_dump_json() == original_strategy
        assert [start for _, start in corporate_calls] == [days[0], days[0]]
        context = corporate_calls[-1][0]
        assert context.rows == (prior, *original.rows)
        assert supplement.query_evidence[0] in context.query_evidence
        rebases = source_backed_price_rebases(action_result, context)
        assert rebases[0].factor == Decimal(".95")
        assert prepared.entry_timeline == _skill_derived_timeline(condition, original, price_rebases=rebases)
        changed_evidence = replace(context, query_evidence=(*original.query_evidence, replace(
            supplement.query_evidence[0], response_sha256="sha256:" + "c" * 64)))
        assert source_backed_price_rebases(action_result, changed_evidence)[0].source_sha256 != rebases[0].source_sha256
    finally:
        service.shutdown()


def test_dynamic_front_adjustment_rebases_each_prefix_without_future_events():
    from ashare_lab.application.minute_price_rebase import MinutePriceRebase
    days = tuple(START + timedelta(days=i) for i in range(5))
    rows = tuple(_row(day, raw_open=price, adjusted_scale="999")
                 for day, price in zip(days, ("10", "10", "5", "6", "3")))
    first = MinutePriceRebase(InstrumentId(SYMBOL), days[2], Decimal(".5"),
                             datetime.combine(days[2], time(9), TZ), "a" * 64)
    future = MinutePriceRebase(InstrumentId(SYMBOL), days[4], Decimal(".5"),
                              datetime.combine(days[4], time(9), TZ), "b" * 64)
    condition = IndicatorCondition(indicator_id="price.rolling_high", definition_version="1.0.0",
                                   params={"period": 2, "price_field": "close"}, trigger="new_high")
    short = _skill_derived_timeline(condition, _history(rows[:4]), price_rebases=(first,))
    long = _skill_derived_timeline(condition, _history(rows), price_rebases=(first, future))
    assert long[:4] == short
    assert short[2] is not None and not short[2].triggered
    assert short[3].triggered
    assert (short[3].left_value, short[3].right_value) == (Decimal(6), Decimal(5))
    assert short[3].evidence[0].validation_status == "local_formula_on_point_in_time_rebases"
    from unittest.mock import Mock
    source = Mock(return_value=SimpleNamespace(price_rebases=(first,), evidence={"provider": "fixture", "fileSha256": "a" * 64}))
    from ashare_lab.application.skill_indicator_routes import default_skill_indicator_routes
    routes = dict(default_skill_indicator_routes())
    routes[condition.indicator_id] = replace(routes[condition.indicator_id], source="provider_indicator", fallback_source="skill_ohlcv_python")
    service = SkillBacktestService(history=SimpleNamespace(load=AsyncMock(return_value=_history(rows[:4]))),
        indicators=object(), store=InMemoryBacktestRunStore(), signal_corporate_loader=source, indicator_routes=routes)
    try:
        strategy = _strategy(days[:4], holding_sessions=1).model_copy(update={"entry": condition})
        prepared = asyncio.run(service.prepare_candidate(strategy, BacktestRunConfig()))
        assert prepared.entry_timeline == short
        assert source.call_args.kwargs["required_start"] == days[0]
        assert json.loads(prepared.signal_adjustment_source)["fileSha256"] == "a" * 64
        source.return_value = SimpleNamespace(price_rebases=(first,), evidence={"provider": "fixture", "fileSha256": "b" * 64})
        refreshed = asyncio.run(service.prepare_candidate(strategy, BacktestRunConfig()))
        assert refreshed.data_version != prepared.data_version
        assert source.call_count == 2  # A changed source cannot reuse stale prepared signals.
        from ashare_lab.application.skill_backtest_service import _result_bundle
        result = run_skill_backtest(strategy=strategy, history=prepared.history,
            entry_timeline=prepared.entry_timeline, exit_timeline=prepared.exit_timeline,
            config=BacktestRunConfig())
        bundle = _result_bundle(RunId("dynamic-source"), strategy, BacktestRunConfig(), prepared.history,
            prepared.indicator_series, result, None, indicator_routes=routes,
            derived_condition_hashes=prepared.derived_condition_hashes,
            signal_adjustment_source=prepared.signal_adjustment_source)
        document = bundle.model_dump(mode="json", by_alias=True)
        assert document["summary"]["dataProvenance"]["derivedIndicatorEvidence"][0]["priceBasis"] == "dynamic_front_adjusted"
        assert json.loads(document["audit"]["signalAdjustmentSource"])["fileSha256"] == "a" * 64
        assert not any(text.startswith("本路径未逐日重建") for text in document["summary"]["warnings"])
        from ashare_lab.api.routes.backtest_runs import _verified_review_facts
        review = _verified_review_facts(SimpleNamespace(fingerprint="fixture", config_json="{}"), bundle)
        assert "signalAdjustmentSource" not in review["audit"]
        assert review["summary"]["dataProvenance"]["derivedIndicatorEvidence"][0]["priceBasis"] == "dynamic_front_adjusted"
    finally:
        service.shutdown()


def test_skill_derived_donchian_compares_close_to_prior_high_not_prior_close() -> None:
    rows = tuple(
        replace(_row(START + timedelta(days=i), raw_open="10"), adjusted_high=Decimal("12"))
        for i in range(3)
    )
    not_breakout = replace(
        _row(START + timedelta(days=3), raw_open="11"), adjusted_high=Decimal("12"),
    )
    breakout = replace(
        _row(START + timedelta(days=4), raw_open="13"), adjusted_high=Decimal("15"),
    )
    condition = IndicatorCondition(
        indicator_id="technical.donchian", definition_version="1.0.0",
        params={"period": 2}, trigger="price_crosses_above_upper",
    )
    assert condition.indicator_id in SkillBacktestService.available_indicator_ids
    timeline = _skill_derived_timeline(condition, _history((*rows, not_breakout, breakout)))
    assert timeline[-2] is not None and not timeline[-2].triggered
    assert timeline[-1] is not None and timeline[-1].triggered
    assert (timeline[-1].left_value, timeline[-1].right_value) == (Decimal(13), Decimal(12))
    future = _row(START + timedelta(days=5), raw_open="999")
    assert _skill_derived_timeline(
        condition, _history((*rows, not_breakout, breakout, future)),
    )[:-1] == timeline


def test_skill_derived_ma20_cross_uses_exact_window() -> None:
    rows = tuple(_row(START + timedelta(days=i), raw_open="10") for i in range(20))
    rising = _row(START + timedelta(days=20), raw_open="11")
    falling = _row(START + timedelta(days=21), raw_open="9")
    timeline = _skill_derived_timeline(
        _condition(trigger="price_crosses_below"),
        _history((*rows, rising, falling)),
    )
    assert timeline[:19] == (None,) * 19
    assert timeline[-2] is not None and not timeline[-2].triggered
    assert timeline[-1] is not None and timeline[-1].triggered
    assert timeline[-1].left_value == Decimal("9")
    assert timeline[-1].right_value == Decimal("10")


def _macd_condition(trigger: str) -> IndicatorCondition:
    return IndicatorCondition(
        indicator_id="technical.macd", definition_version="1.0.0",
        params={"fast": 12, "slow": 26, "signal": 9}, trigger=trigger,
    )


@pytest.mark.parametrize(("trigger", "cross_index"), [("golden_cross", 35), ("death_cross", 36)])
def test_skill_derived_macd_cross_is_causal_and_skips_suspended_sessions(
    trigger: str, cross_index: int,
) -> None:
    # A constant warmup makes the first nonzero DIF/DEA independently calculable.
    rows = tuple(_row(START + timedelta(days=i), raw_open="10") for i in range(34))
    rows += (
        _row(START + timedelta(days=34), raw_open="999", status=TradingStatus.SUSPENDED),
        _row(START + timedelta(days=35), raw_open="12"),
        _row(START + timedelta(days=36), raw_open="6"),
    )
    condition = _macd_condition(trigger)
    timeline = _skill_derived_timeline(condition, _history(rows))
    assert timeline[:35] == (None,) * 35
    assert [index for index, fact in enumerate(timeline) if fact and fact.triggered] == [
        cross_index,
    ]
    rise = timeline[35]
    assert rise is not None
    assert rise.left_value is not None and rise.right_value is not None
    assert abs(rise.left_value - Decimal(56) / Decimal(351)) < Decimal("1e-25")
    assert abs(rise.right_value - rise.left_value / 5) < Decimal("1e-25")
    assert rise.evidence[0].validation_status == "local_formula_on_provider_ohlcv"
    future = _row(START + timedelta(days=37), raw_open="999")
    assert _skill_derived_timeline(condition, _history((*rows, future)))[:-1] == timeline
    assert _skill_derived_timeline(condition, _history(rows[:-1])) == timeline[:-1]


def test_skill_macd_runs_without_provider_indicator_fields_and_reports_its_formula(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = tuple(_row(START + timedelta(days=i), raw_open="10") for i in range(34)) + tuple(
        _row(START + timedelta(days=i), raw_open=price)
        for i, price in enumerate(("12", "12", "6", "6"), start=34)
    )
    strategy = _strategy((rows[0].session_date, rows[-1].session_date)).model_copy(update={
        "entry": _macd_condition("golden_cross"),
        "exit": FirstOfExit(children=(_macd_condition("death_cross"),)),
    })
    load = AsyncMock(return_value=_history(rows))
    query = AsyncMock(side_effect=AssertionError("MACD must not query provider indicator fields"))
    service = SkillBacktestService(
        history=cast(MxDailyHistoryClient, SimpleNamespace(load=load)),
        indicators=cast(HistoricalIndicatorData, SimpleNamespace(query_indicator_history=query)),
        store=InMemoryBacktestRunStore(),
    )
    monkeypatch.setattr(service.queue, "enqueue", lambda _run_id: "manual-test")
    try:
        config = _config(run_robustness=False)
        assert _effective_warmup_calendar_days(strategy, config) == 180
        assert _effective_warmup_calendar_days(
            strategy, replace(config, warmup_calendar_days=0),
        ) == 70  # ceil((26 + 9) * 8 / 5) + 14 calendar days, even without configured warmup.
        created = service.submit(strategy, config)
        record = service.execute(created.record.run_id)
        assert record.state is BacktestJobState.SUCCEEDED, record.progress_label
        assert record.result_json is not None
        bundle = BacktestResultBundle.model_validate_json(record.result_json)
        source = bundle.summary.data_provenance
        assert source is not None
        assert source.provider == MX_DAILY_HISTORY_PROVIDER
        assert source.price_basis == "provider_back_adjusted"
        assert source.indicator_series == 0 and not source.indicator_field_evidence
        assert len(source.derived_indicator_evidence) == 2
        for evidence in source.derived_indicator_evidence:
            assert evidence["indicatorId"] == "technical.macd"
            assert evidence["source"] == "local_formula_on_eastmoney_skill_ohlcv"
            assert evidence["sessionPolicy"] == "positive_volume_sessions_including_current"
            assert "first_value_seeded_ema" in evidence["warmupPolicy"]
            assert "DIF=EMA" in evidence["formula"] and "DEA=EMA(DIF,signal)" in evidence["formula"]
        assert any("不是 Skill 直接提供的成品指标" in text for text in bundle.summary.warnings)
        assert any("递推初值" in text for text in bundle.summary.warnings)
        assert load.await_args.kwargs["start"] == strategy.backtest.start - timedelta(days=180)
        query.assert_not_awaited()
    finally:
        service.shutdown()


def _config(**overrides: object) -> BacktestRunConfig:
    values: dict[str, object] = {
        "capacity_mode": CapacityMode.UNLIMITED,
        "slippage_bps": Decimal("0"),
        "commission_rate": Decimal("0.001"),
        "minimum_commission_cny": Decimal("5"),
    }
    values.update(overrides)
    return BacktestRunConfig(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(("operator", "sell_index"), [("all", 5), ("first_of", 3)])
def test_combined_exit_holding_maturity_waits_for_same_close_signal(
    operator: str, sell_index: int
) -> None:
    dates = tuple(START + timedelta(days=i) for i in range(6))
    strategy = _strategy(dates).model_copy(
        update={
            "exit": FirstOfExit.model_validate(
                {
                    "op": operator,
                    "children": [
                        HoldingPeriodExit(sessions=2),
                        _condition(trigger="price_crosses_below"),
                    ],
                }
            )
        }
    )
    result = run_skill_backtest(
        strategy=strategy,
        history=_history(tuple(_row(day, raw_open="10") for day in dates)),
        entry_timeline=tuple(
            _fact(day, triggered=i == 0, ref="entry") for i, day in enumerate(dates)
        ),
        # True before maturity must not latch forever. At maturity it is false,
        # then becomes true after maturity; ALL sells at the following open.
        exit_timeline=tuple(
            _fact(day, triggered=i in (2, 4), ref="exit") for i, day in enumerate(dates)
        ),
        config=_config(),
        market_sessions=dates,
    )
    fills = [item for item in result.activities if item.kind == "fill"]
    assert [(item.side, item.occurred_at.date()) for item in fills] == [
        ("buy", dates[1]),
        ("sell", dates[sell_index]),
    ]


@pytest.mark.parametrize("calendar_problem", ["missing_security", "empty", "short", "unordered"])
def test_holding_calendar_does_not_count_missing_security_rows_as_holidays(calendar_problem):
    from ashare_lab.application.skill_backtest import SkillBacktestInputError

    calendar = tuple(START + timedelta(days=i) for i in range(6))
    dates = tuple(day for i, day in enumerate(calendar) if i != 3)
    strategy = _strategy(dates, holding_sessions=2)
    supplied = {"missing_security": calendar, "empty": (), "short": calendar[:-1],
                "unordered": tuple(reversed(calendar))}[calendar_problem]
    error = ("holding target market session has no security data" if calendar_problem == "missing_security"
             else "holding market calendar does not cover backtest end")
    with pytest.raises(SkillBacktestInputError, match=error):
        run_skill_backtest(
            strategy=strategy,
            history=_history(tuple(_row(day, raw_open="10") for day in dates)),
            entry_timeline=tuple(_fact(day, triggered=i == 0, ref="entry") for i, day in enumerate(dates)),
            exit_timeline=tuple(None for _ in dates),
            config=_config(), market_sessions=supplied,
        )


@pytest.mark.parametrize("position_rule", ["stop_loss", "trailing_drawdown"])
def test_all_exit_does_not_trigger_on_position_rule_alone(position_rule: str) -> None:
    from ashare_lab.domain.strategy import PositionReturnExit, TrailingDrawdownExit

    dates = tuple(START + timedelta(days=i) for i in range(6))
    risk_rule = (
        PositionReturnExit(trigger="stop_loss", threshold_pct=5)
        if position_rule == "stop_loss"
        else TrailingDrawdownExit(threshold_pct=5)
    )
    strategy = _strategy(dates).model_copy(
        update={
            "exit": FirstOfExit(
                op="all",
                children=(_condition(trigger="price_crosses_below"), risk_rule),
            )
        }
    )
    # Position threshold holds on day 2, but market evidence is unknown/false.
    # Only the day-4 close has both facts; no early exit is allowed.
    result = run_skill_backtest(
        strategy=strategy,
        history=_history(
            tuple(
                _row(day, raw_open="10", raw_close="9" if i >= 2 else "10")
                for i, day in enumerate(dates)
            )
        ),
        entry_timeline=tuple(
            _fact(day, triggered=i == 0, ref="entry") for i, day in enumerate(dates)
        ),
        exit_timeline=tuple(
            None if i == 2 else _fact(day, triggered=i == 4, ref="exit")
            for i, day in enumerate(dates)
        ),
        config=_config(),
    )
    sells = [item for item in result.activities if item.kind == "fill" and item.side == "sell"]
    assert [item.occurred_at.date() for item in sells] == [dates[5]]


def test_all_exit_market_timeline_combines_indicators_with_and() -> None:
    from ashare_lab.application.skill_backtest_service import _exit_condition
    from ashare_lab.domain.strategy import AllCondition

    dates = (START, START + timedelta(days=1))
    strategy = _strategy(dates).model_copy(
        update={
            "exit": FirstOfExit(
                op="all",
                children=(
                    _condition(trigger="price_crosses_above"),
                    _condition(trigger="price_crosses_below"),
                ),
            )
        }
    )
    assert isinstance(_exit_condition(strategy), AllCondition)


def test_verified_ipo_no_limit_sessions_reach_actual_next_open_matching() -> None:
    dates = (START, START + timedelta(days=1), START + timedelta(days=4))
    ordinary = tuple(
        _row(day, raw_open=price) for day, price in zip(dates, ("10", "100", "50"), strict=True)
    )
    rows = tuple(replace(
        row, upper_limit=None, lower_limit=None, raw_preclose=Decimal("10"),
        limit_source=MX_LISTING_NO_LIMIT_SOURCE, listing_session_number=number,
    ) for number, row in enumerate(ordinary, start=1))
    history = replace(_history(ordinary), listing_date=START, rows=rows)
    result = run_skill_backtest(
        strategy=_strategy(dates), history=history, config=_config(),
        entry_timeline=tuple(
            _fact(day, triggered=index == 0, ref="entry") for index, day in enumerate(dates)
        ),
        exit_timeline=tuple(
            _fact(day, triggered=index == 1, ref="exit") for index, day in enumerate(dates)
        ),
    )
    fills = [item for item in result.activities if item.kind in {"fill", "partial_fill"}]
    assert [(item.side, item.occurred_at.date()) for item in fills] == [
        ("buy", dates[1]), ("sell", dates[2]),
    ]
    assert result.metrics.trade_count == 1
    assert not any("limit" in item.reason for item in result.activities if item.kind == "unfilled")


@pytest.mark.parametrize("state", [False, True])
def test_new_entry_policy_adds_only_new_occurrences_in_return_fallback(state):
    from ashare_lab.domain.strategy import DailyExecutionPolicy
    dates = tuple(START + timedelta(days=offset) for offset in range(7))
    spec = _strategy(dates)
    spec = spec.model_copy(update={"execution": DailyExecutionPolicy(position_policy="accumulate_on_new_entry_signal")})
    if state:
        spec = spec.model_copy(update={"entry": spec.entry.model_copy(update={"trigger": "price_above"})})
    result = run_skill_backtest(strategy=spec, history=_history(tuple(_row(day, raw_open="10") for day in dates)),
        entry_timeline=tuple(_fact(day, ref="entry", triggered=i in ((0, 1, 3, 4) if state else (0, 3)))
                             for i, day in enumerate(dates)),
        exit_timeline=tuple(_fact(day, ref="exit", triggered=i == 5) for i, day in enumerate(dates)),
        config=replace(_config(), allocation_ratio=Decimal("0.5")))
    fills = [item for item in result.activities if item.kind in {"fill", "partial_fill"}]
    assert [(item.side, item.occurred_at.date()) for item in fills] == [
        ("buy", dates[1]), ("buy", dates[4]), ("sell", dates[6])]
    assert not result.has_open_position
    assert len(result.round_trips) == 1


@pytest.mark.parametrize("holding_sessions", [1, 3])
@pytest.mark.parametrize("initial_market_exit", [False, True])
def test_accumulation_checks_complete_all_exit_before_suppressing_entry(
    holding_sessions, initial_market_exit,
):
    from ashare_lab.domain.strategy import DailyExecutionPolicy

    dates = tuple(START + timedelta(days=offset) for offset in range(6))
    spec = _strategy(dates).model_copy(update={
        "execution": DailyExecutionPolicy(position_policy="accumulate_on_new_entry_signal"),
        "exit": FirstOfExit(op="all", children=(
            HoldingPeriodExit(sessions=holding_sessions),
            _condition(trigger="price_crosses_below"),
        )),
    })
    result = run_skill_backtest(
        strategy=spec,
        history=_history(tuple(_row(day, raw_open="10") for day in dates)),
        entry_timeline=tuple(_fact(day, ref="entry", triggered=index in (0, 2))
                             for index, day in enumerate(dates)),
        exit_timeline=tuple(_fact(day, ref="exit", triggered=(
            index == 2 or initial_market_exit and index == 0))
            for index, day in enumerate(dates)),
        config=_config(allocation_ratio=Decimal("0.5")),
        market_sessions=dates,
    )

    fills = [item for item in result.activities if item.kind in {"fill", "partial_fill"}]
    # With no position, an account-rule conjunction cannot suppress the first
    # entry. Later, the same market fact only blocks an add if maturity is true.
    assert [(item.side, item.occurred_at.date()) for item in fills] == [
        ("buy", dates[1]),
        ("sell" if holding_sessions == 1 else "buy", dates[3]),
    ]
    assert result.has_open_position is (holding_sessions == 3)
    buy_signals = [item.occurred_at.date() for item in result.activities
                   if item.kind == "signal" and item.side == "buy"]
    assert buy_signals == ([dates[0]] if holding_sessions == 1 else [dates[0], dates[2]])


@pytest.mark.parametrize("market_exit_index", [2, 3])
def test_pending_accumulation_is_cancelled_only_by_complete_all_exit(market_exit_index):
    from ashare_lab.domain.strategy import DailyExecutionPolicy

    dates = tuple(START + timedelta(days=offset) for offset in range(6))
    spec = _strategy(dates).model_copy(update={
        "execution": DailyExecutionPolicy(position_policy="accumulate_on_new_entry_signal"),
        "exit": FirstOfExit(op="all", children=(
            HoldingPeriodExit(sessions=2),
            _condition(trigger="price_crosses_below"),
        )),
    })
    result = run_skill_backtest(
        strategy=spec,
        history=_history(tuple(_row(day, raw_open="10",
            at_limit="up" if index in (2, 3) else None)
            for index, day in enumerate(dates))),
        entry_timeline=tuple(_fact(day, ref="entry", triggered=index in (0, 1))
                             for index, day in enumerate(dates)),
        exit_timeline=tuple(_fact(day, ref="exit", triggered=index == market_exit_index)
                            for index, day in enumerate(dates)),
        config=_config(allocation_ratio=Decimal("0.5"), edge_entry_validity_sessions=3),
        market_sessions=dates,
    )

    fills = [item for item in result.activities if item.kind in {"fill", "partial_fill"}]
    assert [(item.side, item.occurred_at.date()) for item in fills] == [
        ("buy", dates[1]), ("sell" if market_exit_index == 3 else "buy", dates[4]),
    ]


def test_pending_accumulation_is_cancelled_when_holding_exit_activates():
    from ashare_lab.domain.strategy import DailyExecutionPolicy

    dates = tuple(START + timedelta(days=offset) for offset in range(5))
    rows = tuple(
        _row(day, raw_open="10", at_limit="down" if index == 2 else None)
        for index, day in enumerate(dates)
    )
    spec = _strategy(dates, holding_sessions=1).model_copy(
        update={
            "execution": DailyExecutionPolicy(
                position_policy="accumulate_on_new_entry_signal"
            )
        }
    )
    result = run_skill_backtest(
        strategy=spec,
        history=_history(rows),
        entry_timeline=tuple(
            _fact(day, ref="entry", triggered=index in (0, 1))
            for index, day in enumerate(dates)
        ),
        exit_timeline=(None,) * len(dates),
        config=_config(
            allocation_ratio=Decimal("0.5"),
            edge_entry_validity_sessions=3,
            retry_unfilled_exits=False,
            limit_handling=LimitHandling.WAIT_FOR_UNLOCK,
        ),
    )

    buy_fills = [
        item
        for item in result.activities
        if item.kind in {"fill", "partial_fill"} and item.side == "buy"
    ]
    assert [item.occurred_at.date() for item in buy_fills] == [dates[1]]
    assert any(
        item.kind == "unfilled"
        and item.side == "sell"
        and item.occurred_at.date() == dates[2]
        for item in result.activities
    )


def test_close_signals_execute_next_open_with_configured_fees() -> None:
    dates = tuple(START + timedelta(days=offset) for offset in range(4))
    rows = tuple(
        _row(day, raw_open=open_, raw_close=close)
        for day, open_, close in zip(
            dates,
            ("10", "10", "12", "12"),
            ("10", "11", "12", "12"),
            strict=True,
        )
    )
    entries = tuple(
        _fact(day, triggered=index == 0, ref="entry") for index, day in enumerate(dates)
    )
    exits = tuple(_fact(day, triggered=index == 1, ref="exit") for index, day in enumerate(dates))

    result = run_skill_backtest(
        strategy=_strategy(dates),
        history=_history(rows),
        entry_timeline=entries,
        exit_timeline=exits,
        config=_config(),
    )

    fills = [item for item in result.activities if item.kind in {"fill", "partial_fill"}]
    assert [(item.side, item.occurred_at.date()) for item in fills] == [
        ("buy", dates[1]),
        ("sell", dates[2]),
    ]
    for side in ("buy", "sell"):
        signal = next(
            item for item in result.activities if item.kind == "signal" and item.side == side
        )
        order = next(
            item for item in result.activities if item.kind == "order" and item.side == side
        )
        fill = next(item for item in fills if item.side == side)
        assert signal.chain_id == signal.decision_id == signal.origin_signal_id == signal.id
        assert order.chain_id == order.decision_id == signal.id
        assert order.origin_signal_id == order.parent_id == signal.id
        assert order.order_id == order.id
        assert fill.chain_id == fill.decision_id == signal.id
        assert fill.origin_signal_id == signal.id
        assert fill.order_id == fill.parent_id == order.id
        assert fill.fill_id == fill.id
    assert all(item.quantity is None for item in fills)
    assert all(item.notional_cny is not None for item in fills)
    assert result.round_trips[0].entry_date == dates[1]
    assert result.round_trips[0].exit_date == dates[2]
    assert result.metrics.trade_count == 1
    assert result.has_open_position is False
    assert result.final_cash_cny == pytest.approx(Decimal("11976.02397602397602397602398"))
    assert "fees_and_slippage_from_backtest_run_config" in result.assumptions


@pytest.mark.parametrize("failure", ["suspended", "limit"])
def test_default_market_sell_does_not_retry_old_signal_in_return_fallback(failure):
    dates = tuple(START + timedelta(days=offset) for offset in range(6))
    rows = tuple(_row(day, raw_open="10",
        status=TradingStatus.SUSPENDED if failure == "suspended" and i == 3 else TradingStatus.TRADING,
        at_limit="down" if failure == "limit" and i == 3 else None)
        for i, day in enumerate(dates))
    result = run_skill_backtest(strategy=_strategy(dates), history=_history(rows),
        entry_timeline=tuple(_fact(day, triggered=i == 0, ref="entry") for i, day in enumerate(dates)),
        exit_timeline=tuple(_fact(day, triggered=i == 2, ref="exit") for i, day in enumerate(dates)),
        config=_config())
    assert not [item for item in result.activities if item.kind == "fill" and item.side == "sell"]
    assert result.has_open_position


@pytest.mark.parametrize("ordinary_retry_budget", [1, 3])
def test_entry_validity_and_exit_retry_cover_suspension_and_price_limits(ordinary_retry_budget) -> None:
    dates = tuple(START + timedelta(days=offset) for offset in range(7))
    rows = (
        _row(dates[0], raw_open="10"),
        _row(dates[1], raw_open="10", status=TradingStatus.SUSPENDED),
        _row(dates[2], raw_open="11", at_limit="up"),
        _row(dates[3], raw_open="10"),
        _row(dates[4], raw_open="9", at_limit="down"),
        _row(dates[5], raw_open="12"),
        _row(dates[6], raw_open="12"),
    )
    entries = tuple(
        _fact(day, triggered=index == 0, ref="entry") for index, day in enumerate(dates)
    )
    exits = tuple(None for _ in dates)

    result = run_skill_backtest(
        strategy=_strategy(dates, holding_sessions=1),
        history=_history(rows),
        entry_timeline=entries,
        exit_timeline=exits,
        config=_config(
            edge_entry_validity_sessions=3,
            max_exit_attempts=ordinary_retry_budget,
            retry_unfilled_exits=True,
            limit_handling=LimitHandling.WAIT_FOR_UNLOCK,
        ),
    )

    unfilled_reasons = [item.reason for item in result.activities if item.kind == "unfilled"]
    assert "security_suspended" in unfilled_reasons
    assert "daily_buy_limit_open_unlock_unknown" in unfilled_reasons
    assert "daily_sell_limit_open_unlock_unknown" in unfilled_reasons
    fills = [item for item in result.activities if item.kind == "fill"]
    assert [(item.side, item.occurred_at.date()) for item in fills] == [
        ("buy", dates[3]),
        ("sell", dates[5]),
    ]
    assert result.round_trips[0].entry_date == dates[3]
    assert result.round_trips[0].exit_date == dates[5]
    orders = [item for item in result.activities if item.kind == "order"]
    assert [item.attempt_no for item in orders if item.side == "buy"] == [1, 2, 3]
    assert [item.attempt_no for item in orders if item.side == "sell"] == [1, 2]
    assert len({item.order_id for item in orders}) == len(orders)
    for order in orders:
        outcomes = [
            item
            for item in result.activities
            if item.kind in {"fill", "partial_fill", "unfilled"} and item.order_id == order.order_id
        ]
        assert len(outcomes) == 1
        assert outcomes[0].parent_id == order.id
        assert outcomes[0].attempt_no == order.attempt_no


def test_adjusted_scale_is_return_invariant_and_period_end_does_not_liquidate() -> None:
    dates = tuple(START + timedelta(days=offset) for offset in range(4))
    base_rows = tuple(
        _row(day, raw_open=value, raw_close=value)
        for day, value in zip(dates, ("10", "10", "11", "12"), strict=True)
    )
    scaled_rows = tuple(
        _row(day, raw_open=value, raw_close=value, adjusted_scale="100")
        for day, value in zip(dates, ("10", "10", "11", "12"), strict=True)
    )
    entries = (
        _fact(dates[0], triggered=True, ref="entry"),
        None,
        None,
        None,
    )
    exits = tuple(None for _ in dates)
    config = _config(slippage_bps=Decimal("5"))
    strategy = _strategy(dates)

    base = run_skill_backtest(
        strategy=strategy,
        history=_history(base_rows),
        entry_timeline=entries,
        exit_timeline=exits,
        config=config,
    )
    scaled = run_skill_backtest(
        strategy=strategy,
        history=_history(scaled_rows),
        entry_timeline=entries,
        exit_timeline=exits,
        config=config,
    )

    assert base.has_open_position is True
    assert base.round_trips == ()
    assert base.open_position_notional_cny == pytest.approx(
        scaled.open_position_notional_cny,
        abs=Decimal("1e-20"),
    )
    assert tuple(point.equity for point in base.equity_curve) == pytest.approx(
        tuple(point.equity for point in scaled.equity_curve),
        abs=Decimal("1e-20"),
    )
    assert base.metrics.total_return == pytest.approx(scaled.metrics.total_return)
    assert base.unknown_entry_sessions == dates[1:]
    assert base.unknown_exit_sessions == dates
    assert any("不是证券账户逐笔股份账本" in item for item in base.limitations)
    assert any("动态前复权" in item and "尚未验收" in item for item in base.limitations)

    final_session_signal = run_skill_backtest(
        strategy=strategy,
        history=_history(base_rows),
        entry_timeline=tuple(
            _fact(day, triggered=index == len(dates) - 1, ref="entry")
            for index, day in enumerate(dates)
        ),
        exit_timeline=exits,
        config=config,
    )
    assert [item.kind for item in final_session_signal.activities] == ["signal"]
    pending_signal = final_session_signal.activities[0]
    assert pending_signal.origin_signal_id == pending_signal.id
    assert pending_signal.order_id is None


def test_ma_cross_recovers_bad_indicator_dates_from_verified_bars():
    from ashare_lab.adapters.market_data.mx_saas import _UnexpectedIndicatorSessionsError
    rows = tuple(_row(START + timedelta(days=i), raw_open=str(10 + i % 7)) for i in range(40))
    history = _history(rows)
    leaf = IndicatorCondition(indicator_id='technical.ma_cross', definition_version='1.0.0',
        params={'fast_period': 5, 'slow_period': 20, 'price_field': 'close'}, trigger='golden_cross')
    query = AsyncMock(side_effect=_UnexpectedIndicatorSessionsError(
        'historical indicator response contains rows outside the trusted session axis'))
    service = SkillBacktestService(history=SimpleNamespace(load=AsyncMock(return_value=history)),
        indicators=SimpleNamespace(query_indicator_history=query), store=InMemoryBacktestRunStore())
    try:
        strategy = _strategy(tuple(r.session_date for r in rows), holding_sessions=1).model_copy(update={'entry': leaf})
        prepared = asyncio.run(service.prepare_candidate(strategy, BacktestRunConfig()))
        assert query.await_count == 1
        assert prepared.indicator_series == ()
        assert prepared.entry_timeline == _skill_derived_timeline(leaf, history,
            route=replace(service.indicator_routes[leaf.indicator_id], source='skill_ohlcv_python', fallback_source=None))
        assert prepared.derived_condition_hashes
    finally:
        service.shutdown()
