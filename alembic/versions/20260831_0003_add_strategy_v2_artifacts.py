"""Add immutable Strategy v2 draft, plan, receipt and manifest artifacts.

Revision ID: 20260831_0003
Revises: 20260830_0002
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260831_0003"
down_revision: str | None = "20260830_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "strategy_draft_revisions_v2",
        sa.Column("draft_id", sa.String(length=160), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("original_input", sa.Text(), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("artifact_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("draft_id", "revision"),
    )
    op.create_table(
        "strategy_executable_plans_v2",
        sa.Column("plan_id", sa.String(length=71), nullable=False),
        sa.Column("draft_id", sa.String(length=160), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("strategy_hash", sa.String(length=71), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("artifact_json", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["draft_id", "revision"],
            ["strategy_draft_revisions_v2.draft_id", "strategy_draft_revisions_v2.revision"],
            name="fk_strategy_plans_v2_draft_revision",
        ),
        sa.PrimaryKeyConstraint("plan_id"),
    )
    op.create_index(
        "ix_strategy_executable_plans_v2_draft_id",
        "strategy_executable_plans_v2",
        ["draft_id"],
        unique=False,
    )
    op.create_index(
        "ix_strategy_executable_plans_v2_strategy_hash",
        "strategy_executable_plans_v2",
        ["strategy_hash"],
        unique=False,
    )
    op.create_table(
        "strategy_validation_receipts_v2",
        sa.Column("receipt_id", sa.String(length=72), nullable=False),
        sa.Column("plan_id", sa.String(length=71), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("artifact_json", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["plan_id"], ["strategy_executable_plans_v2.plan_id"]),
        sa.PrimaryKeyConstraint("receipt_id"),
        sa.UniqueConstraint("plan_id", name="uq_strategy_validation_receipts_v2_plan"),
    )
    op.create_index(
        "ix_strategy_validation_receipts_v2_expires_at",
        "strategy_validation_receipts_v2",
        ["expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_strategy_validation_receipts_v2_plan_id",
        "strategy_validation_receipts_v2",
        ["plan_id"],
        unique=False,
    )
    op.create_table(
        "backtest_run_manifests_v2",
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("draft_id", sa.String(length=160), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("receipt_id", sa.String(length=72), nullable=False),
        sa.Column("manifest_hash", sa.String(length=71), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("artifact_json", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["receipt_id"],
            ["strategy_validation_receipts_v2.receipt_id"],
        ),
        sa.PrimaryKeyConstraint("run_id"),
        sa.UniqueConstraint("manifest_hash"),
    )
    op.create_index(
        "ix_backtest_run_manifests_v2_draft_id",
        "backtest_run_manifests_v2",
        ["draft_id"],
        unique=False,
    )
    op.create_index(
        "ix_backtest_run_manifests_v2_receipt_id",
        "backtest_run_manifests_v2",
        ["receipt_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_backtest_run_manifests_v2_receipt_id",
        table_name="backtest_run_manifests_v2",
    )
    op.drop_index(
        "ix_backtest_run_manifests_v2_draft_id",
        table_name="backtest_run_manifests_v2",
    )
    op.drop_table("backtest_run_manifests_v2")
    op.drop_index(
        "ix_strategy_validation_receipts_v2_plan_id",
        table_name="strategy_validation_receipts_v2",
    )
    op.drop_index(
        "ix_strategy_validation_receipts_v2_expires_at",
        table_name="strategy_validation_receipts_v2",
    )
    op.drop_table("strategy_validation_receipts_v2")
    op.drop_index(
        "ix_strategy_executable_plans_v2_strategy_hash",
        table_name="strategy_executable_plans_v2",
    )
    op.drop_index(
        "ix_strategy_executable_plans_v2_draft_id",
        table_name="strategy_executable_plans_v2",
    )
    op.drop_table("strategy_executable_plans_v2")
    op.drop_table("strategy_draft_revisions_v2")
