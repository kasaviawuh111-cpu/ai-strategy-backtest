"""Database-level immutability guards for durable run evidence."""

from __future__ import annotations

from sqlalchemy import Engine

_ARTIFACT_TABLES = (
    "strategy_draft_revisions_v2",
    "strategy_executable_plans_v2",
    "strategy_validation_receipts_v2",
    "backtest_run_manifests_v2",
    "backtest_run_results_v2",
)
_TERMINAL_STATES = "'succeeded', 'failed', 'cancelled'"


def immutability_ddl(dialect_name: str) -> tuple[str, ...]:
    """Return idempotent DDL for SQLite or PostgreSQL."""

    if dialect_name == "sqlite":
        statements: list[str] = []
        for table in _ARTIFACT_TABLES:
            statements.extend(
                (
                    f"""CREATE TRIGGER IF NOT EXISTS trg_{table}_no_update
                    BEFORE UPDATE ON {table}
                    BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END""",
                    f"""CREATE TRIGGER IF NOT EXISTS trg_{table}_no_delete
                    BEFORE DELETE ON {table}
                    BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END""",
                )
            )
        statements.extend(
            (
                f"""CREATE TRIGGER IF NOT EXISTS trg_backtest_runs_terminal_no_update
                BEFORE UPDATE ON backtest_runs
                WHEN OLD.state IN ({_TERMINAL_STATES})
                BEGIN SELECT RAISE(ABORT, 'terminal backtest run is immutable'); END""",
                f"""CREATE TRIGGER IF NOT EXISTS trg_backtest_runs_terminal_no_delete
                BEFORE DELETE ON backtest_runs
                WHEN OLD.state IN ({_TERMINAL_STATES})
                BEGIN SELECT RAISE(ABORT, 'terminal backtest run is immutable'); END""",
            )
        )
        return tuple(statements)
    if dialect_name == "postgresql":
        statements = [
            """CREATE OR REPLACE FUNCTION reject_strategy_v2_artifact_mutation()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'strategy v2 artifact is append-only'; END; $$"""
        ]
        for table in _ARTIFACT_TABLES:
            statements.extend(
                (
                    f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {table}",
                    f"""CREATE TRIGGER trg_{table}_immutable
                    BEFORE UPDATE OR DELETE ON {table}
                    FOR EACH ROW EXECUTE FUNCTION reject_strategy_v2_artifact_mutation()""",
                )
            )
        statements.extend(
            (
                f"""CREATE OR REPLACE FUNCTION reject_terminal_backtest_run_mutation()
                RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    IF OLD.state IN ({_TERMINAL_STATES}) THEN
                        RAISE EXCEPTION 'terminal backtest run is immutable';
                    END IF;
                    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
                    RETURN NEW;
                END; $$""",
                "DROP TRIGGER IF EXISTS trg_backtest_runs_terminal_immutable ON backtest_runs",
                """CREATE TRIGGER trg_backtest_runs_terminal_immutable
                BEFORE UPDATE OR DELETE ON backtest_runs
                FOR EACH ROW EXECUTE FUNCTION reject_terminal_backtest_run_mutation()""",
            )
        )
        return tuple(statements)
    raise ValueError(f"unsupported immutability dialect: {dialect_name}")


def install_artifact_immutability_guards(engine: Engine) -> None:
    statements = immutability_ddl(engine.dialect.name)
    cutoff = len(_ARTIFACT_TABLES) * 2
    if engine.dialect.name == "postgresql":
        cutoff = 1 + len(_ARTIFACT_TABLES) * 2
    _execute(engine, statements[:cutoff])


def install_terminal_run_guard(engine: Engine) -> None:
    statements = immutability_ddl(engine.dialect.name)
    cutoff = len(_ARTIFACT_TABLES) * 2
    if engine.dialect.name == "postgresql":
        cutoff = 1 + len(_ARTIFACT_TABLES) * 2
    _execute(engine, statements[cutoff:])


def _execute(engine: Engine, statements: tuple[str, ...]) -> None:
    with engine.begin() as connection:
        for statement in statements:
            connection.exec_driver_sql(statement)


__all__ = [
    "immutability_ddl",
    "install_artifact_immutability_guards",
    "install_terminal_run_guard",
]
