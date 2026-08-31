"""Add an external result-integrity generation identity.

Revision ID: 20260830_0002
Revises: 20260828_0001
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260830_0002"
down_revision: str | None = "20260828_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMN = "result_integrity_policy"
_LEGACY = "legacy_unverified"


def upgrade() -> None:
    op.add_column(
        "backtest_runs",
        sa.Column(
            _COLUMN,
            sa.String(length=32),
            nullable=False,
            server_default=_LEGACY,
        ),
    )
    with op.batch_alter_table("backtest_runs") as batch_op:
        batch_op.alter_column(
            _COLUMN,
            existing_type=sa.String(length=32),
            nullable=False,
            server_default=None,
        )
        batch_op.create_check_constraint(
            "ck_backtest_runs_result_integrity_policy",
            "result_integrity_policy IN ('bundle_hash_v1', 'legacy_unverified')",
        )


def downgrade() -> None:
    with op.batch_alter_table("backtest_runs") as batch_op:
        batch_op.drop_constraint(
            "ck_backtest_runs_result_integrity_policy",
            type_="check",
        )
        batch_op.drop_column(_COLUMN)
