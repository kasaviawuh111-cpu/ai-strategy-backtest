from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from threading import Event
from time import monotonic, sleep
from typing import cast

from fastapi.testclient import TestClient

from ashare_lab.adapters.persistence import InMemoryBacktestRunStore
from ashare_lab.api import create_app
from ashare_lab.application.async_backtest_submission import (
    AsyncBacktestSubmissionCoordinator,
)
from ashare_lab.application.backtest_submission import (
    BacktestDataNotYetAvailableError,
    BacktestRunConfig,
    BacktestSubmissionService,
)
from ashare_lab.domain.shared import RunId
from ashare_lab.domain.strategy import StrategySpec, canonical_hash, canonical_json
from ashare_lab.ports.backtest_runs import (
    BacktestJobState,
    BacktestRunRecord,
    CreateRunResult,
)

ROOT = Path(__file__).parents[3]
NOW = datetime(2026, 9, 4, 12, tzinfo=UTC)


def _strategy() -> StrategySpec:
    return StrategySpec.model_validate_json(
        (ROOT / "contracts/examples/strategy.macd-volume.daily.v1.json").read_text(
            encoding="utf-8"
        )
    )


class _BlockingSubmission:
    def __init__(self, store: InMemoryBacktestRunStore) -> None:
        self.store = store
        self.started = Event()
        self.release = Event()
        self.done = Event()
        self.received_run_id: RunId | None = None

    def submit(
        self,
        strategy: StrategySpec,
        config: BacktestRunConfig,
        *,
        run_id: RunId | None = None,
    ) -> CreateRunResult:
        assert run_id is not None
        self.received_run_id = run_id
        self.started.set()
        assert self.release.wait(2), "test did not release snapshot preparation"
        record = BacktestRunRecord(
            run_id=run_id,
            fingerprint=canonical_hash(
                {
                    "strategy": strategy.model_dump(mode="json"),
                    "allocation": str(config.allocation_ratio),
                    "snapshot": "composite:test",
                }
            ),
            strategy_json=canonical_json(strategy),
            manifest_json=canonical_json({"run_id": str(run_id), "snapshot": "composite:test"}),
            config_json=canonical_json({"allocation_ratio": str(config.allocation_ratio)}),
            state=BacktestJobState.QUEUED,
            progress_percent=0,
            progress_label="等待计算资源",
            created_at=NOW,
            updated_at=NOW,
        )
        result = self.store.create_or_get(record)
        self.done.set()
        return result


class _FailingSubmission(_BlockingSubmission):
    def submit(
        self,
        strategy: StrategySpec,
        config: BacktestRunConfig,
        *,
        run_id: RunId | None = None,
    ) -> CreateRunResult:
        del strategy, config
        assert run_id is not None
        self.received_run_id = run_id
        self.started.set()
        assert self.release.wait(2), "test did not release snapshot preparation"
        self.done.set()
        raise BacktestDataNotYetAvailableError("latest stable data has not arrived")


def _coordinator(
    inner: _BlockingSubmission,
    store: InMemoryBacktestRunStore,
) -> AsyncBacktestSubmissionCoordinator:
    return AsyncBacktestSubmissionCoordinator(
        submission=cast(BacktestSubmissionService, inner),
        run_store=store,
        clock=lambda: NOW,
    )


def test_submit_returns_preparing_run_before_snapshot_provider_finishes() -> None:
    store = InMemoryBacktestRunStore(clock=lambda: NOW)
    inner = _BlockingSubmission(store)
    coordinator = _coordinator(inner, store)
    try:
        first = coordinator.submit(_strategy(), BacktestRunConfig())
        replay = coordinator.submit(_strategy(), BacktestRunConfig())

        assert inner.started.wait(1)
        assert first.record.state is BacktestJobState.RUNNING_DATA
        assert first.record.progress_label == "准备历史数据"
        assert first.replayed is False
        assert replay.replayed is True
        assert replay.record.run_id == first.record.run_id
        assert inner.received_run_id == first.record.run_id

        inner.release.set()
        assert inner.done.wait(1)
        materialized = coordinator.get_preparation(first.record.run_id)
        assert materialized is not None
        assert materialized.run_id == first.record.run_id
        assert materialized.state is BacktestJobState.QUEUED
        assert store.get(first.record.run_id) == materialized
    finally:
        inner.release.set()
        coordinator.shutdown()


def test_cancel_waiting_preparation_skips_provider_and_allows_explicit_retry() -> None:
    store = InMemoryBacktestRunStore(clock=lambda: NOW)
    inner = _BlockingSubmission(store)
    coordinator = _coordinator(inner, store)
    try:
        first = coordinator.submit(_strategy(), BacktestRunConfig())
        assert inner.started.wait(1)
        config = BacktestRunConfig(allocation_ratio=Decimal("0.5"))
        waiting = coordinator.submit(_strategy(), config)
        app = create_app(backtest_submission=coordinator, run_store=store)
        with TestClient(app) as client:
            response = client.post(f"/api/v1/backtest-runs/{waiting.record.run_id}/cancel")
            assert response.status_code == 202
            assert response.json()["state"] == "cancelled"
            polled = client.get(f"/api/v1/backtest-runs/{waiting.record.run_id}")
            assert polled.status_code == 200
            assert polled.json()["state"] == "cancelled"
        cancelled = coordinator.get_preparation(waiting.record.run_id)
        assert cancelled is not None and cancelled.state is BacktestJobState.CANCELLED
        assert coordinator.get_preparation(waiting.record.run_id) == cancelled
        assert coordinator.request_cancel_preparation(waiting.record.run_id) == cancelled
        assert store.get(waiting.record.run_id) is None
        assert inner.received_run_id == first.record.run_id
        retry = coordinator.submit(_strategy(), config)
        assert not retry.replayed and retry.record.run_id != waiting.record.run_id
        inner.release.set()
        coordinator.shutdown()
        assert inner.received_run_id == retry.record.run_id
        assert store.get(waiting.record.run_id) is None
        assert coordinator.get_preparation(waiting.record.run_id) == cancelled
    finally:
        inner.release.set()
        coordinator.shutdown()


def test_replayed_preparation_tracks_current_canonical_run_state() -> None:
    store = InMemoryBacktestRunStore(clock=lambda: NOW)
    inner = _BlockingSubmission(store)
    inner.release.set()
    canonical = inner.submit(
        _strategy(), BacktestRunConfig(), run_id=RunId("run:canonical")
    ).record
    coordinator = _coordinator(inner, store)
    temporary = coordinator.submit(_strategy(), BacktestRunConfig()).record
    coordinator.shutdown()  # Wait until the temporary ID resolves to the replay.

    assert temporary.run_id != canonical.run_id
    assert coordinator.get_preparation(temporary.run_id) == canonical
    assert store.get(temporary.run_id) is None
    app = create_app(backtest_submission=coordinator, run_store=store)
    with TestClient(app) as client:
        response = client.post(f"/api/v1/backtest-runs/{temporary.run_id}/cancel")
        assert response.status_code == 202
        assert response.json()["id"] == str(canonical.run_id)
        assert response.json()["state"] == "cancel_requested"
        polled = client.get(f"/api/v1/backtest-runs/{temporary.run_id}")
        assert polled.status_code == 200
        assert polled.json()["state"] == "cancel_requested"
    cancelled = store.get(canonical.run_id)
    assert cancelled is not None
    assert cancelled.state is BacktestJobState.CANCEL_REQUESTED
    assert coordinator.get_preparation(temporary.run_id) == cancelled
    terminal = store.transition(
        canonical.run_id,
        expected=(BacktestJobState.CANCEL_REQUESTED,),
        target=BacktestJobState.CANCELLED,
        progress_percent=100,
        progress_label="已取消",
    )
    assert coordinator.get_preparation(temporary.run_id) == terminal
    replay = coordinator.submit(_strategy(), BacktestRunConfig())
    assert replay.replayed is True
    assert replay.record == terminal


def test_background_preparation_failure_is_pollable_without_fake_run_data() -> None:
    store = InMemoryBacktestRunStore(clock=lambda: NOW)
    inner = _FailingSubmission(store)
    coordinator = _coordinator(inner, store)
    try:
        created = coordinator.submit(_strategy(), BacktestRunConfig())
        assert created.record.state is BacktestJobState.RUNNING_DATA
        inner.release.set()
        assert inner.done.wait(1)

        failed = coordinator.get_preparation(created.record.run_id)
        assert failed is not None
        assert failed.state is BacktestJobState.FAILED
        assert failed.error_code == "backtest_data_not_yet_available"
        assert store.get(created.record.run_id) is None
        assert "historical_snapshot_preparation" in failed.manifest_json
    finally:
        inner.release.set()
        coordinator.shutdown()


def test_http_post_returns_202_while_historical_preparation_is_blocked() -> None:
    store = InMemoryBacktestRunStore(clock=lambda: NOW)
    inner = _BlockingSubmission(store)
    coordinator = _coordinator(inner, store)
    app = create_app(backtest_submission=coordinator, run_store=store)
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/backtest-runs",
                json={"strategy": _strategy().model_dump(mode="json")},
            )
            assert response.status_code == 202
            payload = response.json()
            assert payload["state"] == "running:data"
            assert payload["progressLabel"] == "准备历史数据"

            polled = client.get(f"/api/v1/backtest-runs/{payload['id']}")
            assert polled.status_code == 200
            assert polled.json()["state"] == "running:data"

            inner.release.set()
            assert inner.done.wait(1)
            materialized = client.get(f"/api/v1/backtest-runs/{payload['id']}")
            assert materialized.status_code == 200
            assert materialized.json()["state"] == "queued"
    finally:
        inner.release.set()
        coordinator.shutdown()


def test_explicit_retry_after_preparation_failure_requeries_and_keeps_old_failure() -> None:
    class FailOnce(_BlockingSubmission):
        calls = 0

        def submit(
            self, strategy: StrategySpec, config: BacktestRunConfig,
            *, run_id: RunId | None = None,
        ) -> CreateRunResult:
            self.calls += 1
            if self.calls == 1:
                raise BacktestDataNotYetAvailableError("data not ready yet")
            return super().submit(strategy, config, run_id=run_id)

    store = InMemoryBacktestRunStore(clock=lambda: NOW)
    inner = FailOnce(store)
    coordinator = _coordinator(inner, store)
    try:
        first = coordinator.submit(_strategy(), BacktestRunConfig())
        deadline = monotonic() + 2
        failed = None
        while monotonic() < deadline:
            failed = coordinator.get_preparation(first.record.run_id)
            if failed is not None and failed.state is BacktestJobState.FAILED:
                break
            sleep(0.005)
        assert failed is not None and failed.state is BacktestJobState.FAILED
        retry = coordinator.submit(_strategy(), BacktestRunConfig())
        assert not retry.replayed
        assert retry.record.run_id != first.record.run_id
        assert inner.started.wait(1)
        assert inner.calls == 2
        duplicate = coordinator.submit(_strategy(), BacktestRunConfig())
        assert duplicate.replayed and duplicate.record.run_id == retry.record.run_id
        assert coordinator.get_preparation(first.record.run_id) == failed
        assert store.get(first.record.run_id) is None
        inner.release.set()
        assert inner.done.wait(1)
        materialized = coordinator.get_preparation(retry.record.run_id)
        assert materialized is not None and materialized.state is BacktestJobState.QUEUED
        assert store.get(retry.record.run_id) == materialized
    finally:
        inner.release.set()
        coordinator.shutdown()
