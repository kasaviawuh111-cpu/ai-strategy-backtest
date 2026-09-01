"""Persist completed v2 results and enforce database immutability.

Revision ID: 20260831_0004
Revises: 20260831_0003
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260831_0004"
down_revision: str | None = "20260831_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ARTIFACT_TABLES = (
    "strategy_draft_revisions_v2",
    "strategy_executable_plans_v2",
    "strategy_validation_receipts_v2",
    "backtest_run_manifests_v2",
    "backtest_run_results_v2",
)
_TERMINAL_STATES = "'succeeded', 'failed', 'cancelled'"


def upgrade() -> None:
    op.create_table(
        "backtest_run_results_v2",
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("manifest_hash", sa.String(length=71), nullable=False),
        sa.Column("result_hash", sa.String(length=71), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("artifact_json", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["backtest_run_manifests_v2.run_id"]),
        sa.PrimaryKeyConstraint("run_id"),
        sa.UniqueConstraint("result_hash"),
    )
    op.create_index(
        "ix_backtest_run_results_v2_manifest_hash",
        "backtest_run_results_v2",
        ["manifest_hash"],
        unique=False,
    )
    for statement in _immutability_ddl(op.get_bind().dialect.name):
        op.execute(statement)


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for table in (
            "strategy_draft_revisions_v2",
            "strategy_executable_plans_v2",
            "strategy_validation_receipts_v2",
            "backtest_run_manifests_v2",
            "backtest_run_results_v2",
        ):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_update")
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_delete")
        op.execute("DROP TRIGGER IF EXISTS trg_backtest_runs_terminal_no_update")
        op.execute("DROP TRIGGER IF EXISTS trg_backtest_runs_terminal_no_delete")
    elif dialect == "postgresql":
        for table in (
            "strategy_draft_revisions_v2",
            "strategy_executable_plans_v2",
            "strategy_validation_receipts_v2",
            "backtest_run_manifests_v2",
            "backtest_run_results_v2",
        ):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {table}")
        op.execute("DROP TRIGGER IF EXISTS trg_backtest_runs_terminal_immutable ON backtest_runs")
        op.execute("DROP FUNCTION IF EXISTS reject_strategy_v2_artifact_mutation()")
        op.execute("DROP FUNCTION IF EXISTS reject_terminal_backtest_run_mutation()")
    else:
        raise ValueError(f"unsupported immutability dialect: {dialect}")
    op.drop_index(
        "ix_backtest_run_results_v2_manifest_hash",
        table_name="backtest_run_results_v2",
    )
    op.drop_table("backtest_run_results_v2")


def _immutability_ddl(dialect: str) -> tuple[str, ...]:
    """Frozen migration DDL; do not import mutable application helpers here."""

    if dialect == "sqlite":
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
    if dialect == "postgresql":
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
    raise ValueError(f"unsupported immutability dialect: {dialect}")
