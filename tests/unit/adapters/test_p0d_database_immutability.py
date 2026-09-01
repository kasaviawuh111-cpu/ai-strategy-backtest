from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from ashare_lab.adapters.persistence import (
    SQLAlchemyBacktestRunStore,
    create_backtest_run_engine,
    create_backtest_run_schema,
)
from ashare_lab.adapters.persistence.immutability import immutability_ddl
from ashare_lab.adapters.persistence.strategy_v2_artifacts import (
    SQLAlchemyStrategyV2ArtifactStore,
    create_strategy_v2_artifact_schema,
)
from ashare_lab.domain.shared import RunId
from ashare_lab.ports.backtest_runs import BacktestJobState, BacktestRunRecord

NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)


def _run() -> BacktestRunRecord:
    return BacktestRunRecord(
        run_id=RunId("run:p0d-terminal"),
        fingerprint="sha256:" + "a" * 64,
        strategy_json="{}",
        manifest_json="{}",
        config_json="{}",
        state=BacktestJobState.QUEUED,
        progress_percent=0,
        progress_label="queued",
        created_at=NOW,
        updated_at=NOW,
    )


def test_sqlite_terminal_run_is_database_immutable_without_breaking_lifecycle(
    tmp_path: Path,
) -> None:
    engine = create_backtest_run_engine(f"sqlite+pysqlite:///{tmp_path / 'runs.db'}")
    create_backtest_run_schema(engine)
    store = SQLAlchemyBacktestRunStore(engine, clock=lambda: NOW)
    run = _run()
    store.create_or_get(run)
    for source, target, progress in (
        (BacktestJobState.QUEUED, BacktestJobState.RUNNING_DATA, 10),
        (BacktestJobState.RUNNING_DATA, BacktestJobState.RUNNING_SIGNAL, 30),
        (BacktestJobState.RUNNING_SIGNAL, BacktestJobState.RUNNING_EXECUTION, 60),
        (BacktestJobState.RUNNING_EXECUTION, BacktestJobState.RUNNING_REPORT, 90),
        (BacktestJobState.RUNNING_REPORT, BacktestJobState.SUCCEEDED, 100),
    ):
        store.transition(
            run.run_id,
            expected=(source,),
            target=target,
            progress_percent=progress,
            progress_label=target.value,
            result_json="{}" if target is BacktestJobState.SUCCEEDED else None,
        )

    with engine.begin() as connection, pytest.raises(DatabaseError, match="terminal"):
        connection.execute(
            text("UPDATE backtest_runs SET progress_label='tampered' WHERE run_id=:run_id"),
            {"run_id": str(run.run_id)},
        )
    with engine.begin() as connection, pytest.raises(DatabaseError, match="terminal"):
        connection.execute(
            text("DELETE FROM backtest_runs WHERE run_id=:run_id"),
            {"run_id": str(run.run_id)},
        )


def test_sqlite_v2_artifact_tables_reject_direct_update_and_delete(tmp_path: Path) -> None:
    engine = create_backtest_run_engine(f"sqlite+pysqlite:///{tmp_path / 'artifacts.db'}")
    create_strategy_v2_artifact_schema(engine)
    SQLAlchemyStrategyV2ArtifactStore(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO strategy_draft_revisions_v2 "
                "(draft_id,revision,original_input,provider,created_at,artifact_json) "
                "VALUES ('draft:guard',1,'x','local',:created_at,'{}')"
            ),
            {"created_at": NOW},
        )
    with engine.begin() as connection, pytest.raises(DatabaseError, match="append-only"):
        connection.execute(
            text(
                "UPDATE strategy_draft_revisions_v2 SET original_input='tampered' "
                "WHERE draft_id='draft:guard' AND revision=1"
            )
        )
    with engine.begin() as connection, pytest.raises(DatabaseError, match="append-only"):
        connection.execute(
            text(
                "DELETE FROM strategy_draft_revisions_v2 "
                "WHERE draft_id='draft:guard' AND revision=1"
            )
        )


def test_postgresql_guard_ddl_covers_all_v2_artifacts_and_terminal_runs() -> None:
    statements = "\n".join(immutability_ddl("postgresql"))
    for table in (
        "strategy_draft_revisions_v2",
        "strategy_executable_plans_v2",
        "strategy_validation_receipts_v2",
        "backtest_run_manifests_v2",
        "backtest_run_results_v2",
    ):
        assert table in statements
    assert "OLD.state IN ('succeeded', 'failed', 'cancelled')" in statements
    assert "BEFORE UPDATE OR DELETE" in statements
