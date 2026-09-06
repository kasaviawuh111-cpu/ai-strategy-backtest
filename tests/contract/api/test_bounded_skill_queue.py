"""Offline admission tests; no Skill data or backtest calculations are run."""

import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from ashare_lab.adapters.market_data.mx_daily_history import MxDailyHistoryClient
from ashare_lab.adapters.persistence.backtest_runs import InMemoryBacktestRunStore
from ashare_lab.api import create_app
from ashare_lab.application.backtest_submission import BacktestRunConfig
from ashare_lab.application.skill_backtest_service import SkillBacktestService
from ashare_lab.domain.shared import RunId
from ashare_lab.domain.strategy import StrategySpec
from ashare_lab.ports.backtest_runs import BacktestJobState, BacktestRunRecord, CreateRunResult
from ashare_lab.ports.provider_indicator_data import HistoricalIndicatorData

ROOT = Path(__file__).parents[3]


class _TrackingStore(InMemoryBacktestRunStore):
    def __init__(self) -> None:
        super().__init__()
        self.created: list[RunId] = []

    def create_or_get(self, record: BacktestRunRecord) -> CreateRunResult:
        result = super().create_or_get(record)
        self.created.append(result.record.run_id)
        return result


def _strategy() -> StrategySpec:
    payload = StrategySpec.model_validate_json(
        (ROOT / "contracts/examples/strategy.macd-volume.daily.v1.json").read_text(),
    ).model_dump(mode="json")
    payload["entry"] = {
        "type": "indicator_condition", "indicator_id": "price.rolling_high",
        "definition_version": "1.0.0",
        "trigger": "new_high", "params": {"period": 20, "price_field": "close"},
    }
    payload["exit"] = {"op": "first_of", "children": [
        {"type": "holding_period_exit", "sessions": 10},
    ]}
    return StrategySpec.model_validate(payload)


def test_full_skill_queue_is_retryable_http_failure_and_never_strands_a_queued_run() -> None:
    started, release, completed = Event(), Event(), Event()
    seen: list[RunId] = []
    store = _TrackingStore()
    load = AsyncMock(side_effect=AssertionError("data lookup is forbidden in admission tests"))

    class BlockedSkillService(SkillBacktestService):
        def execute(self, run_id: RunId) -> BacktestRunRecord:
            seen.append(run_id)
            self.store.transition(
                run_id, expected=(BacktestJobState.QUEUED,), target=BacktestJobState.RUNNING_DATA,
                progress_percent=1, progress_label="offline admission fixture",
            )
            started.set()
            assert release.wait(timeout=5)
            return self.store.transition(
                run_id, expected=(BacktestJobState.RUNNING_DATA,), target=BacktestJobState.FAILED,
                progress_percent=1, progress_label="offline fixture finished",
                error_code="offline_fixture_finished",
            )

    service = BlockedSkillService(
        history=cast(MxDailyHistoryClient, SimpleNamespace(load=load)),
        indicators=cast(HistoricalIndicatorData, object()), store=store,
        max_workers=1, max_pending=2,
    )
    payload = {
        "strategy": _strategy().model_dump(mode="json"),
        "config": {"slippageBps": "0", "commissionRate": "0", "minimumCommissionCny": "0"},
    }
    try:
        with TestClient(create_app(
            backtest_submission=service, run_store=store, include_portfolio_review=False,
        )) as client:
            assert client.post("/api/v1/backtest-runs", json=payload).status_code == 202
            assert started.wait(timeout=2)
            for _ in range(2):
                assert client.post("/api/v1/backtest-runs", json=payload).status_code == 202
            response = client.post("/api/v1/backtest-runs", json=payload)
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "backtest_queue_full"
            assert "本次未开始取数或回测，请稍后重试" in response.json()["error"]["message"]
            rejected = store.get(store.created[-1])
            assert rejected is not None and rejected.state is BacktestJobState.FAILED
            assert rejected.error_code == "backtest_queue_full" and rejected.progress_percent == 0
            assert rejected.run_id not in seen
            assert json.loads(rejected.strategy_json) == payload["strategy"]
            config = json.loads(rejected.config_json)
            assert config["slippage_bps"] == config["commission_rate"] == "0"
            assert config["minimum_commission_cny"] == "0"
            next(iter(service.queue._futures.values())).add_done_callback(
                lambda future: completed.set(),
            )
            release.set()
            assert completed.wait(timeout=2)
            assert client.post("/api/v1/backtest-runs", json=payload).status_code == 202
    finally:
        release.set()
        service.shutdown()
    assert len(store.created) == 5 and len(seen) == 4
    assert all((record := store.get(run_id)) is not None and record.state.is_terminal
               for run_id in store.created)
    load.assert_not_called()


def test_unknown_enqueue_failure_keeps_existing_failure_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _TrackingStore()
    service = SkillBacktestService(
        history=cast(MxDailyHistoryClient, object()),
        indicators=cast(HistoricalIndicatorData, object()), store=store,
    )

    def unavailable(run_id: RunId) -> str:
        raise RuntimeError("offline executor unavailable")

    monkeypatch.setattr(service.queue, "enqueue", unavailable)
    try:
        with pytest.raises(RuntimeError, match="offline executor unavailable"):
            service.submit(_strategy(), BacktestRunConfig())
        record = store.get(store.created[0])
        assert record is not None and record.state is BacktestJobState.FAILED
        assert record.error_code == "queue_unavailable" and record.progress_label == "回测排队失败"
    finally:
        service.shutdown()
