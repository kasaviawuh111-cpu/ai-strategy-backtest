"""Fault-injection progress tests; these are not live-data backtest evidence."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import date
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from ashare_lab.adapters.market_data.mx_daily_history import MxDailyHistoryClient
from ashare_lab.adapters.market_data.mx_saas import MxSaasMarketDataClient
from ashare_lab.adapters.persistence.backtest_runs import (
    BacktestRunConflictError,
    InMemoryBacktestRunStore,
)
from ashare_lab.application.backtest_submission import BacktestRunConfig
from ashare_lab.application.skill_backtest_service import SkillBacktestService
from ashare_lab.domain.strategy import (
    BacktestConfig,
    CatalogRef,
    FirstOfExit,
    HoldingPeriodExit,
    IndicatorCondition,
    Instrument,
    StrategySpec,
)
from ashare_lab.ports.backtest_runs import BacktestJobState, BacktestRunRecord
from ashare_lab.ports.provider_indicator_data import HistoricalIndicatorData

_QUERY = "查询东方财富300059.SZ 2024-09-01至2026-09-04每个交易日数据"
_INDICATORS = "不复权收盘价、成交量、成交额"
_PRIVATE_KEY = "unit-test-only-private-provider-key"
_TABLE = {"title": "离线进度测试夹具", "rawTable": {"headers": ["值"], "data": [["1"]]}}
_SUCCESS = {"code": 200, "data": {"searchDataResultDTO": {"dataTableDTOList": [_TABLE]}}}


class _InterruptedBody(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b'{"code":200,"data":'
        raise httpx.ReadError("private body detail " + _PRIVATE_KEY)


def _create_service(
    monkeypatch: pytest.MonkeyPatch, *, history: object,
) -> tuple[SkillBacktestService, InMemoryBacktestRunStore, BacktestRunRecord]:
    store = InMemoryBacktestRunStore()
    service = SkillBacktestService(
        history=cast(MxDailyHistoryClient, history),
        indicators=cast(HistoricalIndicatorData, object()),
        store=store,
    )
    monkeypatch.setattr(service.queue, "enqueue", lambda _run_id: "manual-offline-test")
    strategy = StrategySpec(
        catalog=CatalogRef(catalog_id="cn_a.signals", release_version="2026.08.28"),
        instrument=Instrument(symbol="300059.SZ"),
        entry=IndicatorCondition(
            indicator_id="technical.ma", definition_version="1.0.0",
            params={"period": 20, "price_field": "close"}, trigger="price_crosses_above",
        ),
        exit=FirstOfExit(children=(HoldingPeriodExit(sessions=10),)),
        backtest=BacktestConfig(
            start=date(2025, 1, 2), end=date(2025, 1, 17), initial_cash_cny=10_000,
        ),
    )
    record = service.submit(strategy, BacktestRunConfig()).record
    return service, store, record


def _assert_original_work_item(actual: BacktestRunRecord, original: BacktestRunRecord) -> None:
    assert actual.run_id == original.run_id
    assert actual.strategy_json == original.strategy_json
    assert actual.config_json == original.config_json
    assert actual.manifest_json == original.manifest_json
    assert actual.fingerprint == original.fingerprint
    assert _PRIVATE_KEY not in actual.progress_label
    assert "private body detail" not in actual.progress_label


@pytest.mark.parametrize(
    ("phase", "percent"),
    [(BacktestJobState.RUNNING_DATA, 10), (BacktestJobState.RUNNING_SIGNAL, 45)],
)
def test_real_adapter_retry_updates_original_running_record_then_recovers_in_same_phase(
    phase: BacktestJobState, percent: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store, original = _create_service(monkeypatch, history=object())
    observed: list[BacktestRunRecord] = []
    requests: list[bytes] = []

    async def sleeper(delay: float) -> None:
        assert delay == 1.0
        snapshot = store.get(original.run_id)
        assert snapshot is not None
        observed.append(snapshot)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.content)
        if len(requests) == 1:
            return httpx.Response(200, stream=_InterruptedBody())
        return httpx.Response(200, json=_SUCCESS)

    mx = MxSaasMarketDataClient(
        api_key=_PRIVATE_KEY, transport=httpx.MockTransport(handler), sleeper=sleeper,
    )
    try:
        service._stage(original.run_id, BacktestJobState.RUNNING_DATA, 10, "读取原区间")
        if phase is BacktestJobState.RUNNING_SIGNAL:
            service._stage(original.run_id, phase, percent, "读取原指标")
        result = asyncio.run(service._with_retry_progress(
            original.run_id, mx.query_finance(query=_QUERY, indicators=_INDICATORS),
        ))
        recovered = store.get(original.run_id)
        assert recovered is not None
        assert len(observed) == 1
        retry = observed[0]
        assert "自动重试（1/2）" in retry.progress_label
        assert "原方案已保留" in retry.progress_label
        assert "已恢复" in recovered.progress_label
        assert "继续" in recovered.progress_label
        assert result.tables == (_TABLE,)
        assert requests[0] == requests[1]
        assert json.loads(requests[0])["query"] == f"{_QUERY}；获取{_INDICATORS}"
        for snapshot in (retry, recovered):
            assert snapshot.state is phase
            assert not snapshot.state.is_terminal
            assert snapshot.progress_percent == percent
            assert snapshot.result_json is None
            assert snapshot.error_code is None
            _assert_original_work_item(snapshot, original)
    finally:
        service.shutdown()


def test_three_read_interruptions_fail_only_after_two_visible_retries_on_original_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[bytes] = []
    observed: list[BacktestRunRecord] = []
    delays: list[float] = []

    async def sleeper(delay: float) -> None:
        delays.append(delay)
        snapshot = store.get(original.run_id)
        assert snapshot is not None
        observed.append(snapshot)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.content)
        return httpx.Response(200, stream=_InterruptedBody())

    mx = MxSaasMarketDataClient(
        api_key=_PRIVATE_KEY, transport=httpx.MockTransport(handler), sleeper=sleeper,
    )

    async def fail_history(**_kwargs: object) -> None:
        # Inject an actual HTTPX response-body failure; no market data or engine runs.
        await mx.query_finance(query=_QUERY, indicators=_INDICATORS)
        pytest.fail("three interrupted attempts must not produce history")

    service, store, original = _create_service(
        monkeypatch, history=SimpleNamespace(load=fail_history),
    )
    try:
        failed = service.execute(original.run_id)
        assert len(requests) == 3
        assert len(set(requests)) == 1
        assert delays == [1.0, 2.0]
        assert len(observed) == 2
        for retry_number, snapshot in enumerate(observed, start=1):
            assert f"自动重试（{retry_number}/2）" in snapshot.progress_label
            assert snapshot.state is BacktestJobState.RUNNING_DATA
            assert snapshot.progress_percent == 10
            assert snapshot.error_code is None
            assert snapshot.result_json is None
            _assert_original_work_item(snapshot, original)
        assert store.get(original.run_id) == failed
        assert failed.state is BacktestJobState.FAILED
        assert failed.progress_percent == 10
        assert failed.error_code == "skill_mx_transport_error"
        assert failed.result_json is None
        _assert_original_work_item(failed, original)
    finally:
        service.shutdown()


def test_parallel_read_recovery_does_not_hide_another_pending_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store, original = _create_service(monkeypatch, history=object())
    after_first_recovery: list[BacktestRunRecord] = []

    async def run_parallel_requests() -> None:
        attempts: dict[str, int] = {}
        allow_second_recovery = asyncio.Event()

        async def sleeper(_delay: float) -> None:
            # Both gather children emit their first retry before either recovers.
            await asyncio.sleep(0)

        async def handler(request: httpx.Request) -> httpx.Response:
            query = json.loads(request.content)["query"]
            attempts[query] = attempts.get(query, 0) + 1
            if attempts[query] == 1:
                return httpx.Response(200, stream=_InterruptedBody())
            if query == "second-pending-read":
                await allow_second_recovery.wait()
            return httpx.Response(200, json=_SUCCESS)

        mx = MxSaasMarketDataClient(
            api_key=_PRIVATE_KEY, transport=httpx.MockTransport(handler), sleeper=sleeper,
        )

        async def first_read() -> None:
            try:
                await mx.query_finance(query="first-read", indicators=None)
                snapshot = store.get(original.run_id)
                assert snapshot is not None
                after_first_recovery.append(snapshot)
            finally:
                allow_second_recovery.set()

        async def both_reads() -> None:
            await asyncio.gather(
                first_read(), mx.query_finance(query="second-pending-read", indicators=None),
            )

        await asyncio.wait_for(service._with_retry_progress(original.run_id, both_reads()), 2)
        assert attempts == {"first-read": 2, "second-pending-read": 2}

    try:
        service._stage(original.run_id, BacktestJobState.RUNNING_DATA, 10, "读取原区间")
        asyncio.run(run_parallel_requests())
        assert len(after_first_recovery) == 1
        pending = after_first_recovery[0]
        assert "自动重试" in pending.progress_label
        assert "已恢复" not in pending.progress_label
        recovered = store.get(original.run_id)
        assert recovered is not None
        assert "已恢复" in recovered.progress_label
        for snapshot in (pending, recovered):
            assert snapshot.state is BacktestJobState.RUNNING_DATA
            assert snapshot.progress_percent == 10
            assert snapshot.error_code is None
            _assert_original_work_item(snapshot, original)
    finally:
        service.shutdown()


@pytest.mark.parametrize("concurrent_cancel", [False, True])
def test_display_update_cas_conflict_never_marks_run_failed(
    concurrent_cancel: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[bytes] = []
    conflict_injected = False

    async def sleeper(_delay: float) -> None:
        return None

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.content)
        if len(requests) == 1:
            return httpx.Response(200, stream=_InterruptedBody())
        return httpx.Response(200, json=_SUCCESS)

    mx = MxSaasMarketDataClient(
        api_key=_PRIVATE_KEY, transport=httpx.MockTransport(handler), sleeper=sleeper,
    )

    async def history_read(**_kwargs: object) -> None:
        await mx.query_finance(query=_QUERY, indicators=_INDICATORS)
        pytest.fail("concurrent cancellation must stop this fixture before returning history")

    service, store, original = _create_service(
        monkeypatch, history=SimpleNamespace(load=history_read),
    )
    real_transition = store.transition

    def conflicting_transition(*args: Any, **kwargs: Any) -> BacktestRunRecord:
        nonlocal conflict_injected
        if kwargs.get("expected_version") is not None and not conflict_injected:
            conflict_injected = True
            if concurrent_cancel:
                store.request_cancel(original.run_id)
            else:
                # Another display writer advances the actual store version first.
                current = store.get(original.run_id)
                assert current is not None
                real_transition(
                    original.run_id, expected=(current.state,), target=current.state,
                    progress_percent=current.progress_percent, progress_label="并发显示更新",
                )
            raise BacktestRunConflictError("synthetic display-only version race")
        return real_transition(*args, **kwargs)

    monkeypatch.setattr(store, "transition", conflicting_transition)
    try:
        if concurrent_cancel:
            result = service.execute(original.run_id)
            assert result.state is BacktestJobState.CANCELLED
            assert result.progress_label == "已取消"
            assert len(requests) == 1
        else:
            service._stage(original.run_id, BacktestJobState.RUNNING_DATA, 10, "读取原区间")
            asyncio.run(service._with_retry_progress(
                original.run_id, mx.query_finance(query=_QUERY, indicators=_INDICATORS),
            ))
            result = store.get(original.run_id)
            assert result is not None
            assert result.state is BacktestJobState.RUNNING_DATA
            assert "已恢复" in result.progress_label
            assert len(requests) == 2
        assert conflict_injected
        assert result.progress_percent == 10
        assert result.error_code is None
        assert result.result_json is None
        _assert_original_work_item(result, original)
    finally:
        service.shutdown()
