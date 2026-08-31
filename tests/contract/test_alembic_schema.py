"""Migration drift gate for the durable backtest run store."""

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, text

from alembic import command
from ashare_lab.adapters.persistence import BACKTEST_RUN_METADATA

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_HEAD_REVISION = "20260830_0002"


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
    finally:
        migrated_engine.dispose()

    assert policy == "legacy_unverified"
