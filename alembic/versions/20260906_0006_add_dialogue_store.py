"""Adopt or create durable dialogue state without replacing existing history.

Revision ID: 20260906_0006
Revises: 20260831_0005
Create Date: 2026-09-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext

from alembic import context, op

revision: str = "20260906_0006"
down_revision: str | None = "20260831_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APPEND_ONLY_TABLES = (
    "dialogue_draft_revisions", "dialogue_idempotency", "dialogue_backtest_reviews",
)


def _metadata() -> sa.MetaData:
    """Frozen revision definitions; never import mutable application metadata."""
    metadata = sa.MetaData()
    sa.Table(
        "dialogue_drafts", metadata,
        sa.Column("draft_id", sa.String(36), primary_key=True),
        sa.Column("latest_revision", sa.Integer(), nullable=False),
        sa.Column("storage_version", sa.Integer(), nullable=False),
        sa.Column("turns_json", sa.Text(), nullable=False),
    )
    sa.Table(
        "dialogue_draft_revisions", metadata,
        sa.Column("draft_id", sa.String(36), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
    )
    sa.Table(
        "dialogue_idempotency", metadata,
        sa.Column("scope", sa.String(64), primary_key=True),
        sa.Column("key", sa.String(255), primary_key=True),
        sa.Column("request_hash", sa.String(128), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
    )
    sa.Table(
        "dialogue_backtest_reviews", metadata,
        sa.Column("run_id", sa.String(128), primary_key=True),
        sa.Column("response_hash", sa.String(71), primary_key=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
    )
    return metadata


def _preflight(bind: sa.Connection, metadata: sa.MetaData) -> set[str]:
    """Reject incompatible adoption before any DDL, including on SQLite."""
    inspector = sa.inspect(bind)
    actual_tables = set(inspector.get_table_names())
    existing = sa.MetaData()
    for table in metadata.sorted_tables:
        if table.name not in actual_tables:
            if inspector.has_table(table.name):
                raise RuntimeError(f"dialogue schema name is not a table: {table.name}")
            continue
        columns = {column["name"]: column for column in inspector.get_columns(table.name)}
        if set(columns) != set(table.columns.keys()) or any(
            not isinstance(columns[column.name]["type"], (sa.Integer, sa.String))
            or columns[column.name]["type"].compile(dialect=bind.dialect)
            != column.type.compile(dialect=bind.dialect)
            for column in table.columns
        ):
            # Autogenerate alone ignores unspecified VARCHAR lengths and NullType.
            raise RuntimeError(f"existing dialogue schema is incompatible: {table.name}")
        primary_key = inspector.get_pk_constraint(table.name)["constrained_columns"]
        if list(table.primary_key.columns.keys()) != primary_key or any((
            inspector.get_foreign_keys(table.name),
            inspector.get_unique_constraints(table.name),
            inspector.get_check_constraints(table.name),
            inspector.get_indexes(table.name),
        )):
            raise RuntimeError(f"existing dialogue schema is incompatible: {table.name}")
        table.to_metadata(existing)
    if existing.tables:
        def include_name(name: str | None, type_: str, parents: object) -> bool:
            return type_ != "table" or name in existing.tables

        migration_context = MigrationContext.configure(bind, opts={
            "compare_type": True, "compare_server_default": True,
            "include_name": include_name,
        })
        if compare_metadata(migration_context, existing):
            raise RuntimeError("existing dialogue schema is incompatible; no changes applied")
    return actual_tables


def upgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("dialogue migration requires online schema preflight")
    bind = op.get_bind()
    statements = _immutability_ddl(bind.dialect.name)
    metadata = _metadata()
    actual_tables = _preflight(bind, metadata)
    for table in metadata.sorted_tables:
        if table.name not in actual_tables:
            table.create(bind)
    _preflight(bind, metadata)
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError(
        "dialogue downgrade is disabled to preserve history; use a reviewed forward migration"
    )


def _immutability_ddl(dialect: str) -> tuple[str, ...]:
    """Keep append-only history guards separate from the mutable draft head."""
    if dialect == "sqlite":
        statements: list[str] = []
        for table in _APPEND_ONLY_TABLES:
            for action in ("UPDATE", "DELETE"):
                name = f"trg_{table}_no_{action.lower()}"
                statements.extend((
                    f"DROP TRIGGER IF EXISTS {name}",
                    f"""CREATE TRIGGER {name} BEFORE {action} ON {table}
                    BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END""",
                ))
        return tuple(statements)
    if dialect == "postgresql":
        statements = [
            """CREATE OR REPLACE FUNCTION reject_dialogue_history_mutation()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'dialogue history is append-only'; END; $$"""
        ]
        for table in _APPEND_ONLY_TABLES:
            statements.extend((
                f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {table}",
                f"""CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION reject_dialogue_history_mutation()""",
            ))
        return tuple(statements)
    raise ValueError(f"unsupported dialogue persistence dialect: {dialect}")
