"""Allow append-only receipt re-signing for one deterministic plan.

Revision ID: 20260831_0005
Revises: 20260831_0004
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260831_0005"
down_revision: str | None = "20260831_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "strategy_validation_receipts_v2"
_CONSTRAINT = "uq_strategy_validation_receipts_v2_plan"


def upgrade() -> None:
    _set_plan_uniqueness(unique=False)


def downgrade() -> None:
    # This is intentionally non-destructive: if multiple receipts have already
    # been issued for one plan, the database refuses the downgrade instead of
    # deleting immutable audit history.
    _set_plan_uniqueness(unique=True)


def _set_plan_uniqueness(*, unique: bool) -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        _drop_sqlite_receipt_guards()
    with op.batch_alter_table(_TABLE) as batch_op:
        if unique:
            batch_op.create_unique_constraint(_CONSTRAINT, ["plan_id"])
        else:
            batch_op.drop_constraint(_CONSTRAINT, type_="unique")
    if dialect == "sqlite":
        for statement in _sqlite_receipt_guard_ddl():
            op.execute(statement)
    elif dialect != "postgresql":
        raise ValueError(f"unsupported receipt persistence dialect: {dialect}")


def _drop_sqlite_receipt_guards() -> None:
    op.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_{_TABLE}_no_update"))
    op.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_{_TABLE}_no_delete"))


def _sqlite_receipt_guard_ddl() -> tuple[str, str]:
    """Frozen trigger definitions for this historical migration."""

    return (
        f"""CREATE TRIGGER IF NOT EXISTS trg_{_TABLE}_no_update
        BEFORE UPDATE ON {_TABLE}
        BEGIN SELECT RAISE(ABORT, '{_TABLE} is append-only'); END""",
        f"""CREATE TRIGGER IF NOT EXISTS trg_{_TABLE}_no_delete
        BEFORE DELETE ON {_TABLE}
        BEGIN SELECT RAISE(ABORT, '{_TABLE} is append-only'); END""",
    )
