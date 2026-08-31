from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ashare_lab.adapters.persistence import (
    BacktestRunConflictError,
    IllegalBacktestRunTransitionError,
    InMemoryBacktestRunStore,
    SQLAlchemyBacktestRunStore,
    create_backtest_run_engine,
    create_backtest_run_schema,
)
from ashare_lab.domain.shared import DomainValidationError, RunId
from ashare_lab.ports.backtest_runs import (
    BacktestJobState,
    BacktestResultIntegrityPolicy,
    BacktestRunRecord,
)

NOW = datetime(2025, 1, 2, 8, 0, tzinfo=UTC)


def record(
    *,
    run_id: str = "run:one",
    fingerprint_character: str = "a",
    strategy_json: str = '{ "schema": "strategy.v1" }',
    result_integrity_policy: BacktestResultIntegrityPolicy = (
        BacktestResultIntegrityPolicy.BUNDLE_HASH_V1
    ),
) -> BacktestRunRecord:
    return BacktestRunRecord(
        run_id=RunId(run_id),
        fingerprint="sha256:" + fingerprint_character * 64,
        strategy_json=strategy_json,
        manifest_json='{ "run_id": "run:one" }',
        config_json='{ "slippage_bps": "5" }',
        state=BacktestJobState.QUEUED,
        progress_percent=0,
        progress_label="queued",
        created_at=NOW,
        updated_at=NOW,
        result_integrity_policy=result_integrity_policy,
    )


@pytest.fixture(params=["memory", "sqlite"])
def run_store(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> InMemoryBacktestRunStore | SQLAlchemyBacktestRunStore:
    ticks = iter(NOW + timedelta(seconds=value) for value in range(1, 100))

    def clock() -> datetime:
        return next(ticks)

    if request.param == "memory":
        return InMemoryBacktestRunStore(clock=clock)
    engine = create_backtest_run_engine(f"sqlite+pysqlite:///{tmp_path / 'runs.db'}")
    create_backtest_run_schema(engine)
    return SQLAlchemyBacktestRunStore(engine, clock=clock)


def test_create_or_get_is_fingerprint_idempotent(
    run_store: InMemoryBacktestRunStore | SQLAlchemyBacktestRunStore,
) -> None:
    first = run_store.create_or_get(record(run_id="run:first"))
    replay = run_store.create_or_get(record(run_id="run:retry"))

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.record.run_id == first.record.run_id


@pytest.mark.parametrize("result_integrity_policy", list(BacktestResultIntegrityPolicy))
def test_result_integrity_policy_survives_store_and_transition(
    run_store: InMemoryBacktestRunStore | SQLAlchemyBacktestRunStore,
    result_integrity_policy: BacktestResultIntegrityPolicy,
) -> None:
    source = record(result_integrity_policy=result_integrity_policy)
    created = run_store.create_or_get(source).record
    running = run_store.transition(
        source.run_id,
        expected=(BacktestJobState.QUEUED,),
        target=BacktestJobState.RUNNING_DATA,
        progress_percent=10,
        progress_label="loading_data",
    )
    restored = run_store.get(source.run_id)

    assert created.result_integrity_policy is result_integrity_policy
    assert running.result_integrity_policy is result_integrity_policy
    assert restored is not None
    assert restored.result_integrity_policy is result_integrity_policy


def test_run_id_collision_with_different_fingerprint_is_a_conflict(
    run_store: InMemoryBacktestRunStore | SQLAlchemyBacktestRunStore,
) -> None:
    run_store.create_or_get(record())

    with pytest.raises(BacktestRunConflictError, match="another fingerprint"):
        run_store.create_or_get(record(fingerprint_character="b"))

    assert run_store.get(RunId("run:one")) == record()


def test_transition_enforces_expected_state_version_graph_and_monotonic_progress(
    run_store: InMemoryBacktestRunStore | SQLAlchemyBacktestRunStore,
) -> None:
    run_store.create_or_get(record())
    running = run_store.transition(
        RunId("run:one"),
        expected=(BacktestJobState.QUEUED,),
        target=BacktestJobState.RUNNING_DATA,
        progress_percent=10,
        progress_label="loading_data",
        expected_version=1,
    )

    assert running.version == 2
    with pytest.raises(BacktestRunConflictError, match="expected one of"):
        run_store.transition(
            RunId("run:one"),
            expected=(BacktestJobState.QUEUED,),
            target=BacktestJobState.RUNNING_DATA,
            progress_percent=11,
            progress_label="loading_data",
        )
    with pytest.raises(BacktestRunConflictError, match="expected version"):
        run_store.transition(
            RunId("run:one"),
            expected=(BacktestJobState.RUNNING_DATA,),
            target=BacktestJobState.RUNNING_DATA,
            progress_percent=11,
            progress_label="loading_data",
            expected_version=1,
        )
    with pytest.raises(IllegalBacktestRunTransitionError, match="cannot transition"):
        run_store.transition(
            RunId("run:one"),
            expected=(BacktestJobState.RUNNING_DATA,),
            target=BacktestJobState.SUCCEEDED,
            progress_percent=100,
            progress_label="done",
            result_json="{}",
        )
    with pytest.raises(IllegalBacktestRunTransitionError, match="cannot decrease"):
        run_store.transition(
            RunId("run:one"),
            expected=(BacktestJobState.RUNNING_DATA,),
            target=BacktestJobState.RUNNING_DATA,
            progress_percent=9,
            progress_label="loading_data",
        )

    unchanged = run_store.get(RunId("run:one"))
    assert unchanged is not None
    assert unchanged.state is BacktestJobState.RUNNING_DATA
    assert unchanged.progress_percent == 10
    assert unchanged.version == 2


def test_invalid_record_result_rolls_back_transition(
    run_store: InMemoryBacktestRunStore | SQLAlchemyBacktestRunStore,
) -> None:
    run_store.create_or_get(record())

    with pytest.raises(DomainValidationError, match="only succeeded"):
        run_store.transition(
            RunId("run:one"),
            expected=(BacktestJobState.QUEUED,),
            target=BacktestJobState.RUNNING_DATA,
            progress_percent=5,
            progress_label="loading_data",
            result_json='{ "must_not_persist": true }',
        )

    assert run_store.get(RunId("run:one")) == record()


def test_request_cancel_is_atomic_and_idempotent(
    run_store: InMemoryBacktestRunStore | SQLAlchemyBacktestRunStore,
) -> None:
    run_store.create_or_get(record())

    requested = run_store.request_cancel(RunId("run:one"))
    replay = run_store.request_cancel(RunId("run:one"))
    cancelled = run_store.transition(
        RunId("run:one"),
        expected=(BacktestJobState.CANCEL_REQUESTED,),
        target=BacktestJobState.CANCELLED,
        progress_percent=requested.progress_percent,
        progress_label="cancelled",
    )

    assert requested.state is BacktestJobState.CANCEL_REQUESTED
    assert requested.version == 2
    assert replay == requested
    assert run_store.request_cancel(RunId("run:one")) == cancelled


def test_completed_run_cannot_be_cancelled(
    run_store: InMemoryBacktestRunStore | SQLAlchemyBacktestRunStore,
) -> None:
    run_store.create_or_get(record())
    stages = (
        (BacktestJobState.QUEUED, BacktestJobState.RUNNING_DATA, 10),
        (BacktestJobState.RUNNING_DATA, BacktestJobState.RUNNING_SIGNAL, 30),
        (BacktestJobState.RUNNING_SIGNAL, BacktestJobState.RUNNING_EXECUTION, 60),
        (BacktestJobState.RUNNING_EXECUTION, BacktestJobState.RUNNING_REPORT, 90),
    )
    for source, target, progress in stages:
        run_store.transition(
            RunId("run:one"),
            expected=(source,),
            target=target,
            progress_percent=progress,
            progress_label=target.value,
        )
    run_store.transition(
        RunId("run:one"),
        expected=(BacktestJobState.RUNNING_REPORT,),
        target=BacktestJobState.SUCCEEDED,
        progress_percent=100,
        progress_label="done",
        result_json='{ "annualized_return": "0.12" }',
    )

    with pytest.raises(IllegalBacktestRunTransitionError, match="cannot cancel"):
        run_store.request_cancel(RunId("run:one"))


def test_sql_store_preserves_json_text_and_survives_reopen(tmp_path: Path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'persistent.db'}"
    first_engine = create_backtest_run_engine(database_url)
    create_backtest_run_schema(first_engine)
    first_store = SQLAlchemyBacktestRunStore(first_engine)
    source = record(strategy_json=' {\n  "spacing":  "is preserved"\n} ')
    first_store.create_or_get(source)
    first_engine.dispose()

    reopened = SQLAlchemyBacktestRunStore(create_backtest_run_engine(database_url))
    restored = reopened.get(source.run_id)

    assert restored == source
    assert restored is not None
    assert restored.strategy_json == source.strategy_json
    assert restored.result_integrity_policy is BacktestResultIntegrityPolicy.BUNDLE_HASH_V1


def test_sql_fingerprint_constraint_is_concurrently_idempotent(tmp_path: Path) -> None:
    engine = create_backtest_run_engine(f"sqlite+pysqlite:///{tmp_path / 'concurrent.db'}")
    create_backtest_run_schema(engine)
    store = SQLAlchemyBacktestRunStore(engine)
    attempts = [record(run_id=f"run:{index}") for index in range(8)]

    with ThreadPoolExecutor(max_workers=len(attempts)) as executor:
        results = list(executor.map(store.create_or_get, attempts))

    assert sum(not item.replayed for item in results) == 1
    assert len({item.record.run_id for item in results}) == 1
