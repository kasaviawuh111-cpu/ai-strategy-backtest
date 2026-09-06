#!/usr/bin/env python3
"""Fail-closed CloudBase management-plane gates for a PostgreSQL release.

The script deliberately shells out to the official ``tcb api`` command.  It
therefore reuses the operator's short-lived/login credential and never accepts,
prints, or writes Tencent Cloud secrets.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

_TCB_API_VERSION = "2018-06-08"
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_MIGRATION_VERSION = re.compile(r"^[0-9]{14}$")
_MIGRATION_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
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
_DIALOGUE_HEAD_TABLE = "dialogue_drafts"
_APPLICATION_TABLES = (
    *_APPEND_ONLY_TABLES, "backtest_runs", _DIALOGUE_HEAD_TABLE, "alembic_version",
)
_TASK_SUCCESS = {"SUCCESS", "SUCCEED", "SUCCEEDED", "FINISHED", "DONE"}
_TASK_FAILURE = {"FAILED", "FAIL", "ERROR", "CANCELED", "CANCELLED"}
_MIGRATION_REHEARSAL_DEFERRED = (
    "cloud PostgreSQL migration rehearsal is deferred: ExecutePGSql accepts one "
    "statement per call and cannot prove a multi-statement rollback transaction"
)


class CloudReleaseGateError(RuntimeError):
    """Raised before a migration, ACL change, or traffic mutation can run."""


Runner = Callable[..., subprocess.CompletedProcess[str]]


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "render-migration":
        migration = render_alembic_migration(
            repository=args.repository,
            version=args.version,
            name=args.name,
        )
        _write_json_new(args.output, migration)
        result: dict[str, object] = _migration_evidence(migration)
    elif args.command == "preview-migration":
        migration = load_migration(args.migration)
        response = preview_migration(args.env_id, migration)
        result = {"status": "preview_verified", **_migration_evidence(migration), **response}
    elif args.command in {"rehearse-migration", "push-migration"}:
        raise CloudReleaseGateError(_MIGRATION_REHEARSAL_DEFERRED)
    elif args.command == "record-preflight":
        _require_confirmation(args.code_revision, args.confirm_code_revision)
        evidence = record_preflight_evidence(
            env_id=args.env_id,
            runtime_role=args.runtime_role,
            code_revision=args.code_revision,
        )
        _write_json_new(args.output, evidence)
        result = evidence
    elif args.command == "apply-runtime-acl":
        _require_confirmation(args.runtime_role, args.confirm_runtime_role)
        baseline = load_preflight_evidence(
            args.preflight_evidence,
            env_id=args.env_id,
            code_revision=args.code_revision,
        )
        apply_runtime_acl_via_tc3(args.env_id, args.runtime_role)
        result = {
            "status": "runtime_acl_applied",
            "runtimeRole": args.runtime_role,
            "preflightEvidenceSha256": baseline["evidenceSha256"],
        }
    else:
        verify_database_via_tc3(args.env_id, args.runtime_role, args.alembic_head)
        result = {
            "status": "database_verified",
            "runtimeRole": args.runtime_role,
            "alembicRevision": args.alembic_head,
            "tamperProbe": "update_delete_rejected",
        }
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    render = commands.add_parser("render-migration")
    render.add_argument("--repository", type=Path, default=Path.cwd())
    render.add_argument("--version", required=True)
    render.add_argument("--name", required=True)
    render.add_argument("--output", type=Path, required=True)

    preview = commands.add_parser("preview-migration")
    _add_environment(preview)
    preview.add_argument("--migration", type=Path, required=True)

    push = commands.add_parser("push-migration")
    _add_environment(push)
    push.add_argument("--migration", type=Path, required=True)
    push.add_argument("--preflight-evidence", type=Path, required=True)
    push.add_argument("--rehearsal-evidence", type=Path, required=True)
    push.add_argument("--code-revision", required=True)
    push.add_argument("--confirm-code-revision", required=True)

    rehearsal = commands.add_parser("rehearse-migration")
    _add_environment(rehearsal)
    rehearsal.add_argument("--migration", type=Path, required=True)
    rehearsal.add_argument("--preflight-evidence", type=Path, required=True)
    rehearsal.add_argument("--runtime-role", required=True)
    rehearsal.add_argument("--code-revision", required=True)
    rehearsal.add_argument("--confirm-code-revision", required=True)
    rehearsal.add_argument("--output", type=Path, required=True)

    preflight = commands.add_parser("record-preflight")
    _add_environment(preflight)
    preflight.add_argument("--runtime-role", required=True)
    preflight.add_argument("--code-revision", required=True)
    preflight.add_argument("--confirm-code-revision", required=True)
    preflight.add_argument("--output", type=Path, required=True)

    acl = commands.add_parser("apply-runtime-acl")
    _add_environment(acl)
    acl.add_argument("--runtime-role", required=True)
    acl.add_argument("--confirm-runtime-role", required=True)
    acl.add_argument("--preflight-evidence", type=Path, required=True)
    acl.add_argument("--code-revision", required=True)

    verify = commands.add_parser("verify-database")
    _add_environment(verify)
    verify.add_argument("--runtime-role", required=True)
    verify.add_argument("--alembic-head", required=True)

    return parser


def _add_environment(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--env-id", required=True)


def render_alembic_migration(*, repository: Path, version: str, name: str) -> dict[str, str]:
    _validate_migration_identity(version, name)
    repository = repository.resolve()
    config = repository / "alembic.ini"
    if not config.is_file() or not (repository / "alembic").is_dir():
        raise CloudReleaseGateError("release bundle does not contain Alembic migrations")
    environment = os.environ.copy()
    environment["DATABASE_URL"] = "postgresql+psycopg://unused:unused@localhost/unused"
    command = (sys.executable, "-m", "alembic", "-c", str(config), "upgrade", "head", "--sql")
    completed = subprocess.run(
        command,
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise CloudReleaseGateError("Alembic could not render the PostgreSQL migration")
    query = _strip_outer_transaction(completed.stdout)
    if not query.strip():
        raise CloudReleaseGateError("Alembic rendered an empty migration")
    return {"Version": version, "Name": name, "Query": query}


def load_migration(path: Path) -> dict[str, str]:
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CloudReleaseGateError("migration JSON is unreadable") from exc
    if not isinstance(raw, dict):
        raise CloudReleaseGateError("migration JSON must be an object")
    decoded = cast(dict[str, object], raw)
    if set(decoded) - {"Version", "Name", "Query", "Rollback"}:
        raise CloudReleaseGateError("migration JSON has unknown fields")
    version, name, query = decoded.get("Version"), decoded.get("Name"), decoded.get("Query")
    if not all(isinstance(item, str) and item for item in (version, name, query)):
        raise CloudReleaseGateError("migration JSON is incomplete")
    _validate_migration_identity(cast(str, version), cast(str, name))
    migration = {
        "Version": cast(str, version),
        "Name": cast(str, name),
        "Query": cast(str, query),
    }
    rollback = decoded.get("Rollback")
    if rollback is not None:
        if not isinstance(rollback, str) or not rollback:
            raise CloudReleaseGateError("migration rollback must be non-empty SQL")
        migration["Rollback"] = rollback
    return migration


def preview_migration(env_id: str, migration: Mapping[str, str]) -> dict[str, object]:
    response = call_cloud_api(
        "tcb",
        "PreviewPGUserMigrations",
        api_version=_TCB_API_VERSION,
        body={"EnvId": env_id, "Migrations": [dict(migration)], "IncludeAll": False},
    )
    conflicts = _object_list(response.get("Conflicts"), "migration conflicts")
    if response.get("Executable") is not True or conflicts:
        raise CloudReleaseGateError("remote migration preview is not executable")
    expected = (migration["Version"], migration["Name"])
    pending_identities = _migration_identities(response.get("Pending"), "pending migrations")
    applied_identities = _migration_identities(response.get("Applied"), "applied migrations")
    identities = pending_identities | applied_identities
    if expected not in identities:
        raise CloudReleaseGateError("remote migration preview omitted the selected migration")
    return {
        "remoteStatus": "pending" if expected in pending_identities else "applied",
        "requestId": response.get("RequestId"),
    }


def _object_list(value: object, label: str) -> list[object]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise CloudReleaseGateError(f"remote {label} are invalid")
    return cast(list[object], value)


def _migration_identities(value: object, label: str) -> set[tuple[str, str]]:
    identities: set[tuple[str, str]] = set()
    for item in _object_list(value, label):
        if not isinstance(item, dict):
            raise CloudReleaseGateError(f"remote {label} contain a non-object")
        plan = cast(dict[str, object], item)
        version, name = plan.get("Version"), plan.get("Name")
        if not isinstance(version, str) or not isinstance(name, str):
            raise CloudReleaseGateError(f"remote {label} contain an invalid identity")
        identities.add((version, name))
    return identities


def push_migration(
    env_id: str,
    migration: Mapping[str, str],
    *,
    poll_interval_seconds: float = 2.0,
    timeout_seconds: float = 300.0,
) -> dict[str, object]:
    preview = preview_migration(env_id, migration)
    if preview["remoteStatus"] == "applied":
        return {"remoteStatus": "already_applied", "requestId": preview["requestId"]}
    response = call_cloud_api(
        "tcb",
        "PushPGUserMigrations",
        api_version=_TCB_API_VERSION,
        body={
            "EnvId": env_id,
            "Migrations": [dict(migration)],
            "LockTimeoutMs": 5000,
            "StatementTimeoutMs": 300000,
            "IncludeAll": False,
        },
    )
    task_id = response.get("TaskId")
    if not isinstance(task_id, str) or not task_id:
        raise CloudReleaseGateError("migration API did not return a task ID")
    task = wait_for_task(
        env_id,
        task_id,
        poll_interval_seconds=poll_interval_seconds,
        timeout_seconds=timeout_seconds,
    )
    final_preview = preview_migration(env_id, migration)
    if final_preview["remoteStatus"] != "applied":
        raise CloudReleaseGateError("migration task completed but preview is not applied")
    return {
        "remoteStatus": "applied",
        "taskId": task_id,
        "taskRequestId": task.get("RequestId"),
        "requestId": response.get("RequestId"),
    }


def wait_for_task(
    env_id: str,
    task_id: str,
    *,
    poll_interval_seconds: float,
    timeout_seconds: float,
) -> dict[str, object]:
    if poll_interval_seconds < 0 or timeout_seconds <= 0:
        raise CloudReleaseGateError("task polling intervals are invalid")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = call_cloud_api(
            "tcb",
            "DescribeTaskResult",
            api_version=_TCB_API_VERSION,
            body={"EnvId": env_id, "TaskId": task_id},
        )
        status = _task_status(response)
        if status in _TASK_SUCCESS:
            return response
        if status in _TASK_FAILURE:
            raise CloudReleaseGateError("remote migration task failed")
        if poll_interval_seconds:
            time.sleep(poll_interval_seconds)
    raise CloudReleaseGateError("remote migration task did not finish before timeout")


def _task_status(response: Mapping[str, object]) -> str:
    for key in ("Status", "Result", "TaskStatus", "State"):
        value = response.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    raise CloudReleaseGateError("DescribeTaskResult returned no recognized task status")


def record_preflight_evidence(
    *, env_id: str, runtime_role: str, code_revision: str
) -> dict[str, object]:
    _validate_role(runtime_role)
    if _GIT_SHA.fullmatch(code_revision) is None:
        raise CloudReleaseGateError("preflight code revision must be a full lowercase Git SHA")
    state = capture_database_state(env_id=env_id, runtime_role=runtime_role)
    body: dict[str, object] = {
        "schemaVersion": "ashare-lab.cloudbase-pg-preflight.v1",
        "environmentId": env_id,
        "runtimeRole": runtime_role,
        "codeRevision": code_revision,
        "recordedAt": datetime.now(UTC).isoformat(),
        "evidenceKind": "logical_preflight_not_provider_physical_backup",
        "databaseState": state,
        "databaseStateSha256": _canonical_sha256(state),
    }
    body["evidenceSha256"] = _canonical_sha256(body)
    return body


def load_preflight_evidence(path: Path, *, env_id: str, code_revision: str) -> dict[str, object]:
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CloudReleaseGateError("database preflight evidence is unreadable") from exc
    if not isinstance(decoded, dict):
        raise CloudReleaseGateError("database preflight evidence must be an object")
    evidence = cast(dict[str, object], decoded)
    checksum = evidence.pop("evidenceSha256", None)
    state_value = evidence.get("databaseState")
    state = cast(dict[str, object], state_value) if isinstance(state_value, dict) else None
    valid = (
        evidence.get("schemaVersion") == "ashare-lab.cloudbase-pg-preflight.v1"
        and evidence.get("environmentId") == env_id
        and evidence.get("codeRevision") == code_revision
        and evidence.get("evidenceKind") == "logical_preflight_not_provider_physical_backup"
        and isinstance(evidence.get("runtimeRole"), str)
        and isinstance(evidence.get("recordedAt"), str)
        and state is not None
        and evidence.get("databaseStateSha256") == _canonical_sha256(state)
        and isinstance(checksum, str)
        and checksum == _canonical_sha256(evidence)
    )
    evidence["evidenceSha256"] = checksum
    if not valid:
        raise CloudReleaseGateError(
            "database preflight evidence is incomplete, mismatched, or tampered"
        )
    return evidence


def load_rehearsal_evidence(
    path: Path,
    *,
    env_id: str,
    code_revision: str,
    migration: Mapping[str, str],
    preflight_evidence_sha256: str,
) -> dict[str, object]:
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CloudReleaseGateError("migration rehearsal evidence is unreadable") from exc
    if not isinstance(decoded, dict):
        raise CloudReleaseGateError("migration rehearsal evidence must be an object")
    evidence = cast(dict[str, object], decoded)
    checksum = evidence.pop("evidenceSha256", None)
    expected_migration = _migration_evidence(migration)
    valid = (
        evidence.get("schemaVersion") == "ashare-lab.cloudbase-pg-rehearsal.v1"
        and evidence.get("status") == "migration_rehearsal_rolled_back"
        and evidence.get("environmentId") == env_id
        and evidence.get("codeRevision") == code_revision
        and evidence.get("preflightEvidenceSha256") == preflight_evidence_sha256
        and evidence.get("migrationVersion") == expected_migration["migrationVersion"]
        and evidence.get("migrationName") == expected_migration["migrationName"]
        and evidence.get("querySha256") == expected_migration["querySha256"]
        and isinstance(evidence.get("databaseStateSha256"), str)
        and isinstance(evidence.get("recordedAt"), str)
        and isinstance(checksum, str)
        and checksum == _canonical_sha256(evidence)
    )
    evidence["evidenceSha256"] = checksum
    if not valid:
        raise CloudReleaseGateError(
            "migration rehearsal evidence is incomplete, mismatched, or tampered"
        )
    return evidence


def _require_logically_recoverable_preflight(  # pyright: ignore[reportUnusedFunction]
    evidence: Mapping[str, object],
) -> None:
    state_value = evidence.get("databaseState")
    if not isinstance(state_value, dict):
        raise CloudReleaseGateError("database preflight state is missing")
    state = cast(dict[str, object], state_value)
    counts_value = state.get("applicationRowCounts")
    if not isinstance(counts_value, dict):
        raise CloudReleaseGateError("database preflight row counts are missing")
    counts = cast(dict[object, object], counts_value)
    nonempty = {
        str(table): value
        for table, value in counts.items()
        if str(value).strip() not in {"0", "0.0"}
    }
    if nonempty:
        raise CloudReleaseGateError(
            "logical preflight cannot recover a non-empty application database; "
            "a verified provider physical backup is required"
        )


def capture_database_state(*, env_id: str, runtime_role: str) -> dict[str, object]:
    role = _validate_role(runtime_role)
    table_rows = _rows(
        _execute_sql(
            env_id,
            "SELECT c.relname, c.relkind, pg_total_relation_size(c.oid)::text AS bytes "
            "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='public' ORDER BY c.relname",
        )
    )
    existing_tables = {
        cast(str, row["relname"])
        for row in table_rows
        if row.get("relkind") in {"r", "p"} and row.get("relname") in _APPLICATION_TABLES
    }
    row_counts = {
        table: _single_value(
            _execute_sql(env_id, f"SELECT count(*)::text AS row_count FROM public.{table}"),
            "row_count",
        )
        for table in sorted(existing_tables)
    }
    role_rows = _rows(
        _execute_sql(
            env_id,
            "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolreplication, "
            "rolbypassrls, rolcanlogin FROM pg_roles WHERE rolname='" + role + "'",
        )
    )
    trigger_rows = _rows(
        _execute_sql(
            env_id,
            "SELECT t.tgname, t.tgenabled, c.relname FROM pg_trigger t "
            "JOIN pg_class c ON c.oid=t.tgrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='public' AND NOT t.tgisinternal "
            "ORDER BY c.relname,t.tgname",
        )
    )
    column_rows = _rows(
        _execute_sql(
            env_id,
            "SELECT table_name, column_name, ordinal_position::text, data_type, is_nullable "
            "FROM information_schema.columns WHERE table_schema='public' "
            "ORDER BY table_name,ordinal_position",
        )
    )
    constraint_rows = _rows(
        _execute_sql(
            env_id,
            "SELECT con.conname, con.contype, cls.relname, pg_get_constraintdef(con.oid) AS def "
            "FROM pg_constraint con JOIN pg_class cls ON cls.oid=con.conrelid "
            "JOIN pg_namespace n ON n.oid=cls.relnamespace "
            "WHERE n.nspname='public' ORDER BY cls.relname,con.conname",
        )
    )
    return {
        "tables": table_rows,
        "columns": column_rows,
        "constraints": constraint_rows,
        "applicationRowCounts": row_counts,
        "runtimeRole": role_rows,
        "triggers": trigger_rows,
    }


def rehearse_migration(
    *,
    env_id: str,
    migration: Mapping[str, str],
    expected_state_sha256: str,
    runtime_role: str,
) -> dict[str, object]:
    del env_id, migration, expected_state_sha256, runtime_role
    raise CloudReleaseGateError(_MIGRATION_REHEARSAL_DEFERRED)


def apply_runtime_acl_via_tc3(env_id: str, runtime_role: str) -> None:
    role = _validate_role(runtime_role)
    _require_remote_runtime_role(env_id, role)
    for statement in _runtime_acl_statements(role):
        _execute_sql(env_id, _wrap_ddl(statement))
    _verify_remote_runtime_role(env_id, role)


def verify_database_via_tc3(env_id: str, runtime_role: str, alembic_head: str) -> None:
    role = _validate_role(runtime_role)
    revision = _single_value(
        _execute_sql(env_id, "SELECT version_num FROM public.alembic_version"),
        "version_num",
    )
    if revision != alembic_head:
        raise CloudReleaseGateError("remote database is not at the selected Alembic head")
    _require_remote_runtime_role(env_id, role)
    trigger_rows = _rows(
        _execute_sql(
            env_id,
            "SELECT t.tgname, t.tgenabled FROM pg_trigger t "
            "JOIN pg_class c ON c.oid=t.tgrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='public' AND NOT t.tgisinternal",
        )
    )
    names = {row.get("tgname") for row in trigger_rows if row.get("tgenabled") in {"O", "A"}}
    expected = {
        *(f"trg_{table}_immutable" for table in _APPEND_ONLY_TABLES),
        "trg_backtest_runs_terminal_immutable",
    }
    if not expected.issubset(names):
        raise CloudReleaseGateError("append-only PostgreSQL triggers are missing or disabled")
    _verify_remote_runtime_role(env_id, role)
    for table in _APPEND_ONLY_TABLES:
        payload = "payload_json" if table.startswith("dialogue_") else "artifact_json"
        _expect_sql_rejected(
            env_id, f"UPDATE public.{table} SET {payload}={payload} WHERE false", role
        )
        _expect_sql_rejected(env_id, f"DELETE FROM public.{table} WHERE false", role)
    # Zero-row statements check permissions only; the single-connection
    # production_db probe proves actual head mutation and rolls all rows back.
    _execute_sql(
        env_id,
        "UPDATE public.dialogue_drafts SET storage_version=storage_version WHERE false",
        role,
    )
    _expect_sql_rejected(env_id, "DELETE FROM public.dialogue_drafts WHERE false", role)


def call_cloud_api(
    service: str,
    action: str,
    *,
    api_version: str,
    body: Mapping[str, object],
    runner: Runner = subprocess.run,
) -> dict[str, object]:
    command = (
        "tcb",
        "api",
        service,
        action,
        "--body",
        json.dumps(body, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
        "--api-version",
        api_version,
        "--json",
    )
    try:
        completed = runner(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise CloudReleaseGateError(
            "CloudBase CLI v3 is unavailable; do not mutate production"
        ) from exc
    if completed.returncode != 0:
        raise CloudReleaseGateError(f"CloudBase API {action} failed; production mutation stopped")
    try:
        raw: object = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise CloudReleaseGateError("CloudBase CLI did not return strict JSON") from exc
    if not isinstance(raw, dict):
        raise CloudReleaseGateError("CloudBase API response envelope is invalid")
    envelope = cast(dict[str, object], raw)
    response_value = envelope.get("Response")
    if not isinstance(response_value, dict):
        raise CloudReleaseGateError("CloudBase API response envelope is invalid")
    response = cast(dict[str, object], response_value)
    if response.get("Error") is not None:
        raise CloudReleaseGateError(f"CloudBase API {action} returned an error")
    return response


def _execute_sql(env_id: str, statement: str, role: str | None = None) -> dict[str, object]:
    body: dict[str, object] = {"EnvId": env_id, "Sql": statement}
    if role is not None:
        body["Role"] = role
    return call_cloud_api("tcb", "ExecutePGSql", api_version=_TCB_API_VERSION, body=body)


def _expect_sql_rejected(env_id: str, statement: str, role: str) -> None:
    try:
        _execute_sql(env_id, statement, role)
    except CloudReleaseGateError:
        return
    raise CloudReleaseGateError("runtime role unexpectedly accepted an immutable-table mutation")


def _require_remote_runtime_role(env_id: str, role: str) -> None:
    response = _execute_sql(
        env_id,
        "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolreplication, "
        "rolbypassrls, rolcanlogin FROM pg_roles WHERE rolname='" + role + "'",
    )
    rows = _rows(response)
    if len(rows) != 1:
        raise CloudReleaseGateError("dedicated runtime PostgreSQL role does not exist")
    row = rows[0]
    if _postgres_bool(row.get("rolcanlogin")) is not True:
        raise CloudReleaseGateError("runtime PostgreSQL role cannot authenticate directly")
    for attribute in ("rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls"):
        if _postgres_bool(row.get(attribute)) is not False:
            raise CloudReleaseGateError("runtime PostgreSQL role has privileged attributes")


def _verify_remote_runtime_role(env_id: str, role: str) -> None:
    for table in (*_APPEND_ONLY_TABLES, _DIALOGUE_HEAD_TABLE):
        response = _execute_sql(
            env_id,
            "SELECT has_table_privilege('" + role + "','public." + table + "','SELECT') AS s, "
            "has_table_privilege('" + role + "','public." + table + "','INSERT') AS i, "
            "has_table_privilege('" + role + "','public." + table + "','UPDATE') AS u, "
            "has_table_privilege('" + role + "','public." + table + "','DELETE') AS d",
        )
        row = _rows(response)
        actual = (
            tuple(_postgres_bool(row[0].get(key)) for key in ("s", "i", "u", "d"))
            if len(row) == 1
            else ()
        )
        expected = (True, True, table == _DIALOGUE_HEAD_TABLE, False)
        if actual != expected:
            raise CloudReleaseGateError(f"runtime PostgreSQL ACL is unsafe: {table}")


def _postgres_bool(value: object) -> bool | None:
    if value is True or value == 1:
        return True
    if value is False or value == 0:
        return False
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"t", "true", "1"}:
            return True
        if normalized in {"f", "false", "0"}:
            return False
    return None


def _runtime_acl_statements(role: str) -> tuple[str, ...]:
    statements = [
        "REVOKE CREATE ON SCHEMA public FROM PUBLIC",
        f"REVOKE ALL ON SCHEMA public FROM {role}",
        f"GRANT USAGE ON SCHEMA public TO {role}",
    ]
    for table in _APPLICATION_TABLES:
        statements.extend(
            (
                f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC",
                f"REVOKE ALL ON TABLE public.{table} FROM {role}",
            )
        )
    statements.extend(
        f"GRANT SELECT, INSERT ON TABLE public.{table} TO {role}" for table in _APPEND_ONLY_TABLES
    )
    statements.extend(
        (
            f"GRANT SELECT, INSERT, UPDATE ON TABLE public.backtest_runs TO {role}",
            f"GRANT SELECT, INSERT, UPDATE ON TABLE public.dialogue_drafts TO {role}",
            f"GRANT SELECT ON TABLE public.alembic_version TO {role}",
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL PRIVILEGES ON TABLES FROM PUBLIC",
        )
    )
    return tuple(statements)


def _wrap_ddl(statement: str) -> str:
    escaped = statement.replace("'", "''")
    return f"DO LANGUAGE plpgsql $$ BEGIN EXECUTE '{escaped}'; END $$;"


def _rows(response: Mapping[str, object]) -> list[dict[str, object]]:
    raw_value = response.get("Rows")
    if raw_value is None:
        raw: list[object] = []
    elif not isinstance(raw_value, list):
        raise CloudReleaseGateError("ExecutePGSql rows are invalid")
    else:
        raw = cast(list[object], raw_value)
    rows: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, str):
            raise CloudReleaseGateError("ExecutePGSql row is not encoded JSON")
        decoded: object = json.loads(item)
        if not isinstance(decoded, dict):
            raise CloudReleaseGateError("ExecutePGSql row is not an object")
        rows.append(cast(dict[str, object], decoded))
    return rows


def _single_value(response: Mapping[str, object], key: str) -> object:
    rows = _rows(response)
    if len(rows) != 1 or key not in rows[0]:
        raise CloudReleaseGateError("ExecutePGSql did not return one expected value")
    return rows[0][key]


def _validate_migration_identity(version: str, name: str) -> None:
    if _MIGRATION_VERSION.fullmatch(version) is None:
        raise CloudReleaseGateError("migration version must be a 14-digit timestamp")
    if _MIGRATION_NAME.fullmatch(name) is None:
        raise CloudReleaseGateError("migration name must be lowercase snake_case")


def _validate_role(value: str) -> str:
    if _ROLE.fullmatch(value) is None:
        raise CloudReleaseGateError("runtime role must be a plain PostgreSQL identifier")
    return value


def _require_confirmation(expected: str, actual: str) -> None:
    if expected != actual:
        raise CloudReleaseGateError(
            "explicit release confirmation does not match the selected target"
        )
    if len(expected) == 40 and _GIT_SHA.fullmatch(expected) is None:
        raise CloudReleaseGateError("release code revision must be a full lowercase Git SHA")


def _strip_outer_transaction(sql_text: str) -> str:
    lines = sql_text.splitlines()
    first = next((index for index, line in enumerate(lines) if line.strip() == "BEGIN;"), None)
    last = next(
        (index for index in range(len(lines) - 1, -1, -1) if lines[index].strip() == "COMMIT;"),
        None,
    )
    if first is None or last is None or first >= last:
        raise CloudReleaseGateError("Alembic offline SQL is missing its outer transaction")
    return "\n".join((*lines[:first], *lines[first + 1 : last], *lines[last + 1 :])).strip() + "\n"


def _migration_evidence(migration: Mapping[str, str]) -> dict[str, object]:
    return {
        "migrationVersion": migration["Version"],
        "migrationName": migration["Name"],
        "querySha256": hashlib.sha256(migration["Query"].encode("utf-8")).hexdigest(),
    }


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _write_json_new(path: Path, value: object) -> None:
    if path.exists():
        raise CloudReleaseGateError(
            "evidence output already exists; immutable evidence is never overwritten"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
