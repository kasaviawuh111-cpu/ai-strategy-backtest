"""Create the durable backtest run table.

Revision ID: 20260828_0001
Revises:
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260828_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "backtest_runs",
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("fingerprint", sa.String(length=71), nullable=False),
        sa.Column("strategy_json", sa.Text(), nullable=False),
        sa.Column("manifest_json", sa.Text(), nullable=False),
        sa.Column("config_json", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("progress_percent", sa.Integer(), nullable=False),
        sa.Column("progress_label", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "progress_percent >= 0 AND progress_percent <= 100",
            name="ck_backtest_runs_progress",
        ),
        sa.CheckConstraint("version >= 1", name="ck_backtest_runs_version"),
        sa.PrimaryKeyConstraint("run_id"),
        sa.UniqueConstraint("fingerprint", name="uq_backtest_runs_fingerprint"),
    )
    op.create_index(
        "ix_backtest_runs_fingerprint",
        "backtest_runs",
        ["fingerprint"],
        unique=False,
    )
    op.create_index(
        "ix_backtest_runs_state",
        "backtest_runs",
        ["state"],
        unique=False,
    )
    op.create_index(
        "ix_backtest_runs_updated_at",
        "backtest_runs",
        ["updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_backtest_runs_updated_at", table_name="backtest_runs")
    op.drop_index("ix_backtest_runs_state", table_name="backtest_runs")
    op.drop_index("ix_backtest_runs_fingerprint", table_name="backtest_runs")
    op.drop_table("backtest_runs")
