#!/usr/bin/env python3
"""Apply and verify the PostgreSQL persistence boundary for a candidate release."""

from __future__ import annotations

import argparse
import json
import os
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg import sql
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

_ROLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_APPEND_ONLY_TABLES = (
    "strategy_draft_revisions_v2",
    "strategy_executable_plans_v2",
    "strategy_validation_receipts_v2",
    "backtest_run_manifests_v2",
    "backtest_run_results_v2",
    "dialogue_draft_revisions",
    "dialogue_idempotency",
    "dialogue_backtest_reviews",
)
_RUN_TABLE = "backtest_runs"
_DIALOGUE_HEAD_TABLE = "dialogue_drafts"
_ALEMBIC_TABLE = "alembic_version"


class ProductionDatabaseError(RuntimeError):
    """Raised when production persistence is mutable, stale or misconfigured."""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("provision-role", "apply-acl", "verify"):
        command = subparsers.add_parser(name)
        command.add_argument("--runtime-role", required=True)
        command.add_argument("--alembic-ini", type=Path, default=Path("alembic.ini"))
    args = parser.parse_args()
    runtime_role = validate_role_name(args.runtime_role)
    admin_url = _required_postgres_url("DATABASE_ADMIN_URL")
    if args.command == "provision-role":
        provision_runtime_role(
            admin_url=admin_url,
            runtime_role=runtime_role,
            password=_required_runtime_password(),
        )
        result: dict[str, object] = {
            "status": "runtime_role_provisioned",
            "runtimeRole": runtime_role,
        }
    elif args.command == "apply-acl":
        expected_revision = expected_alembic_head(args.alembic_ini)
        apply_runtime_acl(
            admin_url=admin_url,
            runtime_role=runtime_role,
            expected_revision=expected_revision,
        )
        result = {
            "status": "acl_applied",
            "alembicRevision": expected_revision,
            "runtimeRole": runtime_role,
        }
    else:
        expected_revision = expected_alembic_head(args.alembic_ini)
        runtime_url = _required_postgres_url("DATABASE_URL")
        verify_production_database(
            admin_url=admin_url,
            runtime_url=runtime_url,
            runtime_role=runtime_role,
            expected_revision=expected_revision,
        )
        result = {
            "status": "verified",
            "alembicRevision": expected_revision,
            "runtimeRole": runtime_role,
            "tamperProbe": "rejected_and_rolled_back",
        }
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


def provision_runtime_role(*, admin_url: str, runtime_role: str, password: str) -> None:
    """Create one non-privileged LOGIN role without exposing its password."""

    with psycopg.connect(admin_url) as connection:
        row = connection.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s",
            (runtime_role,),
        ).fetchone()
        if row is None:
            statement = sql.SQL(
                "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOREPLICATION NOBYPASSRLS PASSWORD {}"
            ).format(sql.Identifier(runtime_role), sql.Literal(password))
            connection.execute(statement)
            connection.commit()
        _require_safe_role(connection, runtime_role)


def _required_runtime_password() -> str:
    value = os.environ.get("RUNTIME_DATABASE_PASSWORD", "")
    if len(value) < 24:
        raise ProductionDatabaseError(
            "RUNTIME_DATABASE_PASSWORD must be injected as a secret with at least 24 characters"
        )
    return value


def validate_role_name(value: str) -> str:
    if _ROLE.fullmatch(value) is None:
        raise ProductionDatabaseError("runtime role must be a plain PostgreSQL identifier")
    return value


def expected_alembic_head(config_path: Path) -> str:
    if not config_path.is_file():
        raise ProductionDatabaseError("alembic.ini is missing from the release bundle")
    heads = ScriptDirectory.from_config(Config(str(config_path))).get_heads()
    if len(heads) != 1:
        raise ProductionDatabaseError("release must have exactly one Alembic head")
    return heads[0]


def apply_runtime_acl(
    *,
    admin_url: str,
    runtime_role: str,
    expected_revision: str,
) -> None:
    """Grant only runtime operations; migrations stay on a separate admin URL."""

    with psycopg.connect(admin_url) as connection:
        _require_migration_head(connection, expected_revision)
        _require_safe_role(connection, runtime_role)
        _require_not_object_owner(connection, runtime_role)
        role = sql.Identifier(runtime_role)
        public = sql.Identifier("public")
        with connection.transaction():
            connection.execute(sql.SQL("REVOKE CREATE ON SCHEMA {} FROM PUBLIC").format(public))
            connection.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM {}").format(public, role))
            connection.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(public, role))
            for table in (*_APPEND_ONLY_TABLES, _RUN_TABLE, _DIALOGUE_HEAD_TABLE, _ALEMBIC_TABLE):
                identifier = sql.Identifier(table)
                connection.execute(
                    sql.SQL("REVOKE ALL PRIVILEGES ON TABLE {} FROM PUBLIC").format(identifier)
                )
                connection.execute(
                    sql.SQL("REVOKE ALL PRIVILEGES ON TABLE {} FROM {}").format(identifier, role)
                )
            for table in _APPEND_ONLY_TABLES:
                connection.execute(
                    sql.SQL("GRANT SELECT, INSERT ON TABLE {} TO {}").format(
                        sql.Identifier(table), role
                    )
                )
            for table in (_RUN_TABLE, _DIALOGUE_HEAD_TABLE):
                connection.execute(
                    sql.SQL("GRANT SELECT, INSERT, UPDATE ON TABLE {} TO {}").format(
                        sql.Identifier(table), role
                    )
                )
            connection.execute(
                sql.SQL("GRANT SELECT ON TABLE {} TO {}").format(
                    sql.Identifier(_ALEMBIC_TABLE), role
                )
            )
            # New tables created by this migration owner must not silently
            # inherit PUBLIC access on the next release.
            connection.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES IN SCHEMA {} "
                    "REVOKE ALL PRIVILEGES ON TABLES FROM PUBLIC"
                ).format(public)
            )


def verify_production_database(
    *,
    admin_url: str,
    runtime_url: str,
    runtime_role: str,
    expected_revision: str,
) -> None:
    """Verify migration, ownership, ACLs, triggers and real mutation rejection."""

    with psycopg.connect(admin_url) as connection:
        _require_migration_head(connection, expected_revision)
        _require_safe_role(connection, runtime_role)
        _require_not_object_owner(connection, runtime_role)
        _require_triggers(connection)
        _require_acl(connection, runtime_role)
    with psycopg.connect(runtime_url) as connection:
        actual_role = connection.execute("SELECT current_user").fetchone()
        if actual_role is None or actual_role[0] != runtime_role:
            raise ProductionDatabaseError("DATABASE_URL does not authenticate as the runtime role")
        _run_rollback_only_tamper_probe(connection)


def _required_postgres_url(variable: str) -> str:
    value = os.environ.get(variable, "").strip()
    if not value:
        raise ProductionDatabaseError(f"{variable} must be supplied as a secret")
    try:
        backend = make_url(value).get_backend_name()
    except ArgumentError as exc:
        raise ProductionDatabaseError(f"{variable} is not a valid SQLAlchemy URL") from exc
    if backend != "postgresql":
        raise ProductionDatabaseError(f"{variable} must use PostgreSQL")
    return value


def _require_migration_head(connection: psycopg.Connection[Any], expected: str) -> None:
    row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    if row is None or row[0] != expected:
        raise ProductionDatabaseError("database is not at the release Alembic head")


def _require_safe_role(connection: psycopg.Connection[Any], role: str) -> None:
    row = connection.execute(
        "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, "
        "rolcanlogin "
        "FROM pg_roles WHERE rolname = %s",
        (role,),
    ).fetchone()
    if row is None:
        raise ProductionDatabaseError("runtime role does not exist")
    if any(bool(item) for item in row[:5]):
        raise ProductionDatabaseError("runtime role has privileged PostgreSQL attributes")
    if row[5] is not True:
        raise ProductionDatabaseError("runtime role must be a dedicated LOGIN role")
    current = connection.execute("SELECT current_user").fetchone()
    if current is not None and current[0] == role:
        raise ProductionDatabaseError("migration admin and runtime role must be different")


def _require_not_object_owner(connection: psycopg.Connection[Any], role: str) -> None:
    rows = connection.execute(
        "SELECT c.relname FROM pg_class AS c "
        "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
        "JOIN pg_roles AS r ON r.oid = c.relowner "
        "WHERE n.nspname = 'public' AND r.rolname = %s AND c.relname = ANY(%s)",
        (role, list((*_APPEND_ONLY_TABLES, _RUN_TABLE, _DIALOGUE_HEAD_TABLE, _ALEMBIC_TABLE))),
    ).fetchall()
    if rows:
        raise ProductionDatabaseError("runtime role must not own application tables")


def _require_triggers(connection: psycopg.Connection[Any]) -> None:
    expected = {
        *(f"trg_{table}_immutable" for table in _APPEND_ONLY_TABLES),
        "trg_backtest_runs_terminal_immutable",
    }
    rows = connection.execute(
        "SELECT t.tgname, t.tgenabled FROM pg_trigger AS t "
        "JOIN pg_class AS c ON c.oid = t.tgrelid "
        "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND NOT t.tgisinternal AND t.tgname = ANY(%s)",
        (list(expected),),
    ).fetchall()
    enabled = {str(name) for name, state in rows if state in {"O", "A"}}
    if enabled != expected:
        raise ProductionDatabaseError("append-only PostgreSQL triggers are missing or disabled")


def _require_acl(connection: psycopg.Connection[Any], role: str) -> None:
    schema = connection.execute(
        "SELECT has_schema_privilege(%s, 'public', 'USAGE'), "
        "has_schema_privilege(%s, 'public', 'CREATE')",
        (role, role),
    ).fetchone()
    if schema != (True, False):
        raise ProductionDatabaseError("runtime schema privileges are not minimal")
    for table in _APPEND_ONLY_TABLES:
        _expect_table_privileges(
            connection,
            role=role,
            table=table,
            expected=(True, True, False, False),
        )
    for table in (_RUN_TABLE, _DIALOGUE_HEAD_TABLE):
        _expect_table_privileges(
            connection,
            role=role,
            table=table,
            expected=(True, True, True, False),
        )
    _expect_table_privileges(
        connection,
        role=role,
        table=_ALEMBIC_TABLE,
        expected=(True, False, False, False),
    )


def _expect_table_privileges(
    connection: psycopg.Connection[Any],
    *,
    role: str,
    table: str,
    expected: tuple[bool, bool, bool, bool],
) -> None:
    row = connection.execute(
        "SELECT has_table_privilege(%s, %s, 'SELECT'), "
        "has_table_privilege(%s, %s, 'INSERT'), "
        "has_table_privilege(%s, %s, 'UPDATE'), "
        "has_table_privilege(%s, %s, 'DELETE')",
        (role, table, role, table, role, table, role, table),
    ).fetchone()
    if row != expected:
        raise ProductionDatabaseError(f"runtime table privileges are unsafe: {table}")


def _run_rollback_only_tamper_probe(connection: psycopg.Connection[Any]) -> None:
    """Insert disposable rows, prove mutation fails, then roll the transaction back."""

    suffix = uuid.uuid4().hex
    now = datetime.now(UTC)
    draft_id = f"draft:acl-probe-{suffix}"
    plan_id = "sha256:" + uuid.uuid4().hex * 2
    receipt_id = "receipt:" + uuid.uuid4().hex * 2
    run_id = f"run:acl-probe-{suffix}"
    manifest_hash = "sha256:" + uuid.uuid4().hex * 2
    result_hash = "sha256:" + uuid.uuid4().hex * 2
    fingerprint = "sha256:" + uuid.uuid4().hex * 2
    dialogue_draft_id = str(uuid.uuid4())
    dialogue_scope = f"acl-probe-{suffix}"
    try:
        # A preceding identity query may already have opened an implicit
        # transaction.  Always reset before starting the disposable probe.
        connection.rollback()
        connection.execute(
            "INSERT INTO strategy_draft_revisions_v2 "
            "(draft_id, revision, original_input, provider, created_at, artifact_json) "
            "VALUES (%s, 1, 'acl probe', 'server', %s, '{}')",
            (draft_id, now),
        )
        connection.execute(
            "INSERT INTO strategy_executable_plans_v2 "
            "(plan_id, draft_id, revision, strategy_hash, created_at, artifact_json) "
            "VALUES (%s, %s, 1, %s, %s, '{}')",
            (plan_id, draft_id, fingerprint, now),
        )
        connection.execute(
            "INSERT INTO strategy_validation_receipts_v2 "
            "(receipt_id, plan_id, issued_at, expires_at, artifact_json) "
            "VALUES (%s, %s, %s, %s, '{}')",
            (receipt_id, plan_id, now, now + timedelta(minutes=5)),
        )
        connection.execute(
            "INSERT INTO backtest_run_manifests_v2 "
            "(run_id, draft_id, revision, receipt_id, manifest_hash, created_at, artifact_json) "
            "VALUES (%s, %s, 1, %s, %s, %s, '{}')",
            (run_id, draft_id, receipt_id, manifest_hash, now),
        )
        connection.execute(
            "INSERT INTO backtest_run_results_v2 "
            "(run_id, manifest_hash, result_hash, created_at, artifact_json) "
            "VALUES (%s, %s, %s, %s, '{}')",
            (run_id, manifest_hash, result_hash, now),
        )
        connection.execute(
            "INSERT INTO backtest_runs "
            "(run_id, fingerprint, strategy_json, manifest_json, config_json, state, "
            "progress_percent, progress_label, created_at, updated_at, result_json, "
            "error_code, version, result_integrity_policy) "
            "VALUES (%s, %s, '{}', '{}', '{}', 'succeeded', 100, 'probe', %s, %s, "
            "'{}', NULL, 1, 'bundle_hash_v1')",
            (run_id + "-legacy", fingerprint, now, now),
        )
        connection.execute(
            "INSERT INTO dialogue_drafts "
            "(draft_id, latest_revision, storage_version, turns_json) VALUES (%s, 1, 1, '[]')",
            (dialogue_draft_id,),
        )
        connection.execute(
            "INSERT INTO dialogue_draft_revisions "
            "(draft_id, revision, payload_json) VALUES (%s, 1, '{}')",
            (dialogue_draft_id,),
        )
        connection.execute(
            "INSERT INTO dialogue_idempotency "
            "(scope, key, request_hash, payload_json) VALUES (%s, %s, %s, '{}')",
            (dialogue_scope, suffix, fingerprint),
        )
        connection.execute(
            "INSERT INTO dialogue_backtest_reviews "
            "(run_id, response_hash, payload_json) VALUES (%s, %s, '{}')",
            (run_id + "-legacy", result_hash),
        )
        # Only the draft head is mutable: the store advances its CAS version
        # for revisions and bounded dialogue history; frozen payloads stay insert-only.
        head = connection.execute(
            "UPDATE dialogue_drafts SET storage_version=storage_version+1 "
            "WHERE draft_id=%s AND storage_version=1 RETURNING storage_version",
            (dialogue_draft_id,),
        ).fetchone()
        if head != (2,):
            raise ProductionDatabaseError("runtime cannot advance the dialogue draft head")
        _expect_mutation_rejected(
            connection, table=_DIALOGUE_HEAD_TABLE, key_column="draft_id",
            key=dialogue_draft_id, operation="delete",
        )
        for table, key_column, key in (
            ("strategy_draft_revisions_v2", "draft_id", draft_id),
            ("strategy_executable_plans_v2", "plan_id", plan_id),
            ("strategy_validation_receipts_v2", "receipt_id", receipt_id),
            ("backtest_run_manifests_v2", "run_id", run_id),
            ("backtest_run_results_v2", "run_id", run_id),
            ("backtest_runs", "run_id", run_id + "-legacy"),
            ("dialogue_draft_revisions", "draft_id", dialogue_draft_id),
            ("dialogue_idempotency", "scope", dialogue_scope),
            ("dialogue_backtest_reviews", "run_id", run_id + "-legacy"),
        ):
            _expect_mutation_rejected(
                connection,
                table=table,
                key_column=key_column,
                key=key,
                operation="update",
            )
            _expect_mutation_rejected(
                connection,
                table=table,
                key_column=key_column,
                key=key,
                operation="delete",
            )
    finally:
        connection.rollback()


def _expect_mutation_rejected(
    connection: psycopg.Connection[Any],
    *,
    table: str,
    key_column: str,
    key: str,
    operation: str,
) -> None:
    connection.execute("SAVEPOINT mutation_probe")
    if operation == "update":
        statement = sql.SQL("UPDATE {} SET {} = {} WHERE {} = %s").format(
            sql.Identifier(table),
            sql.Identifier(key_column),
            sql.Identifier(key_column),
            sql.Identifier(key_column),
        )
    elif operation == "delete":
        statement = sql.SQL("DELETE FROM {} WHERE {} = %s").format(
            sql.Identifier(table),
            sql.Identifier(key_column),
        )
    else:  # pragma: no cover - internal programmer contract
        raise AssertionError(f"unsupported mutation probe: {operation}")
    try:
        connection.execute(statement, (key,))
    except psycopg.Error:
        connection.execute("ROLLBACK TO SAVEPOINT mutation_probe")
        connection.execute("RELEASE SAVEPOINT mutation_probe")
        return
    connection.execute("ROLLBACK TO SAVEPOINT mutation_probe")
    connection.execute("RELEASE SAVEPOINT mutation_probe")
    raise ProductionDatabaseError(f"runtime {operation} unexpectedly succeeded: {table}")


if __name__ == "__main__":
    raise SystemExit(main())
