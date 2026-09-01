"""Migration drift gate for the durable backtest run store."""

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DatabaseError

from alembic import command
from ashare_lab.adapters.persistence import BACKTEST_RUN_METADATA, PERSISTENCE_METADATA

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_HEAD_REVISION = "20260831_0005"


def test_fresh_database_migration_matches_run_store_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "fresh.db"
    database_url = f"sqlite+pysqlite:///{database_path}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config(str(_REPOSITORY_ROOT / "alembic.ini"))

    command.upgrade(config, "head")
    command.check(config)

    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            current_revision = MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()

    assert current_revision == _HEAD_REVISION
    assert set(BACKTEST_RUN_METADATA.tables) == {"backtest_runs"}
    assert set(PERSISTENCE_METADATA.tables) == {
        "backtest_runs",
        "backtest_run_manifests_v2",
        "backtest_run_results_v2",
        "strategy_draft_revisions_v2",
        "strategy_executable_plans_v2",
        "strategy_validation_receipts_v2",
    }


def test_existing_rows_are_explicitly_migrated_as_legacy_unverified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "legacy.db"
    database_url = f"sqlite+pysqlite:///{database_path}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config(str(_REPOSITORY_ROOT / "alembic.ini"))
    command.upgrade(config, "20260828_0001")

    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO backtest_runs (
                        run_id, fingerprint, strategy_json, manifest_json, config_json,
                        state, progress_percent, progress_label, created_at, updated_at,
                        result_json, error_code, version
                    ) VALUES (
                        :run_id, :fingerprint, '{}', '{}', '{}',
                        'succeeded', 100, 'succeeded', :created_at, :updated_at,
                        '{}', NULL, 1
                    )
                    """
                ),
                {
                    "run_id": "run:migrated-legacy",
                    "fingerprint": "sha256:" + "a" * 64,
                    "created_at": "2026-08-28 00:00:00",
                    "updated_at": "2026-08-28 00:00:00",
                },
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")

    migrated_engine = create_engine(database_url)
    try:
        with migrated_engine.connect() as connection:
            policy = connection.execute(
                text(
                    "SELECT result_integrity_policy FROM backtest_runs "
                    "WHERE run_id = 'run:migrated-legacy'"
                )
            ).scalar_one()
        with (
            migrated_engine.begin() as connection,
            pytest.raises(
                DatabaseError,
                match="terminal",
            ),
        ):
            connection.execute(
                text(
                    "UPDATE backtest_runs SET progress_label='tampered' "
                    "WHERE run_id='run:migrated-legacy'"
                )
            )
    finally:
        migrated_engine.dispose()

    assert policy == "legacy_unverified"


def test_receipt_plan_uniqueness_migration_round_trips_without_losing_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'receipt-migration.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config(str(_REPOSITORY_ROOT / "alembic.ini"))

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    try:
        constraints = inspect(engine).get_unique_constraints("strategy_validation_receipts_v2")
        assert all(
            item["name"] != "uq_strategy_validation_receipts_v2_plan" for item in constraints
        )
        with engine.connect() as connection:
            trigger_count = connection.execute(
                text(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
                    "AND name LIKE 'trg_strategy_validation_receipts_v2_no_%'"
                )
            ).scalar_one()
        assert trigger_count == 2
    finally:
        engine.dispose()

    command.downgrade(config, "20260831_0004")
    downgraded = create_engine(database_url)
    try:
        constraints = inspect(downgraded).get_unique_constraints("strategy_validation_receipts_v2")
        assert any(
            item["name"] == "uq_strategy_validation_receipts_v2_plan" for item in constraints
        )
        with downgraded.connect() as connection:
            trigger_count = connection.execute(
                text(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
                    "AND name LIKE 'trg_strategy_validation_receipts_v2_no_%'"
                )
            ).scalar_one()
        assert trigger_count == 2
    finally:
        downgraded.dispose()

    command.upgrade(config, "head")
