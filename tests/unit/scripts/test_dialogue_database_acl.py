"""Offline dialogue ACL contracts; these tests never connect to PostgreSQL or CloudBase."""

from __future__ import annotations

import json
from collections.abc import Sequence
from contextlib import nullcontext
from typing import Any, cast

import psycopg
import pytest
from psycopg import sql

from deploy.cloudbase import production_db, tc3_database_release

APPEND_ONLY = (
    "dialogue_draft_revisions", "dialogue_idempotency", "dialogue_backtest_reviews",
)
HEAD = "dialogue_drafts"


class _Cursor:
    def __init__(
        self, row: tuple[object, ...] | None = None, rows: list[tuple[object, ...]] | None = None,
    ) -> None:
        self.row, self.rows = row, rows or []

    def fetchone(self) -> tuple[object, ...] | None:
        return self.row

    def fetchall(self) -> list[tuple[object, ...]]:
        return self.rows


class _Connection:
    def __init__(self, *, bad_table: str | None = None, head_version: int = 2) -> None:
        self.bad_table, self.head_version = bad_table, head_version
        self.calls: list[tuple[str, Sequence[object] | None]] = []
        self.rollbacks = 0

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def transaction(self):
        return nullcontext()

    def rollback(self) -> None:
        self.rollbacks += 1

    def execute(
        self, statement: str | sql.Composable, params: Sequence[object] | None = None,
    ) -> _Cursor:
        text = statement if isinstance(statement, str) else statement.as_string()
        self.calls.append((text, params))
        if "has_schema_privilege" in text:
            return _Cursor((True, False))
        if "has_table_privilege" in text:
            assert params is not None
            table = str(params[1])
            mutable = table in {HEAD, "backtest_runs"}
            expected = (True, table != "alembic_version", mutable, False)
            actual = (True, True, not mutable, False) if table == self.bad_table else expected
            return _Cursor(actual)
        if text.startswith("UPDATE dialogue_drafts SET storage_version"):
            return _Cursor((self.head_version,))
        if text.startswith(('UPDATE "', 'DELETE FROM "')):
            raise psycopg.errors.InsufficientPrivilege("offline denied")
        return _Cursor()


def _connection(fake: _Connection) -> psycopg.Connection[Any]:
    return cast(psycopg.Connection[Any], fake)


def test_both_acl_paths_grant_only_head_updates(monkeypatch: pytest.MonkeyPatch) -> None:
    assert set(APPEND_ONLY).issubset(production_db._APPEND_ONLY_TABLES)
    assert production_db._APPEND_ONLY_TABLES == tc3_database_release._APPEND_ONLY_TABLES
    assert set((*APPEND_ONLY, HEAD)).issubset(tc3_database_release._APPLICATION_TABLES)
    statements = tc3_database_release._runtime_acl_statements("runtime")
    for table in APPEND_ONLY:
        assert f"GRANT SELECT, INSERT ON TABLE public.{table} TO runtime" in statements
        assert f"GRANT SELECT, INSERT, UPDATE ON TABLE public.{table} TO runtime" not in statements
    assert f"GRANT SELECT, INSERT, UPDATE ON TABLE public.{HEAD} TO runtime" in statements
    assert not any(item.startswith("GRANT") and "DELETE" in item for item in statements)

    fake = _Connection()
    monkeypatch.setattr(production_db.psycopg, "connect", lambda *args: fake)
    for name in ("_require_migration_head", "_require_safe_role", "_require_not_object_owner"):
        monkeypatch.setattr(production_db, name, lambda *args: None)
    production_db.apply_runtime_acl(
        admin_url="offline", runtime_role="runtime", expected_revision="x",
    )
    rendered = [item[0] for item in fake.calls]
    for table in APPEND_ONLY:
        assert f'GRANT SELECT, INSERT ON TABLE "{table}" TO "runtime"' in rendered
    assert f'GRANT SELECT, INSERT, UPDATE ON TABLE "{HEAD}" TO "runtime"' in rendered
    for table in (*APPEND_ONLY, HEAD):
        assert f'REVOKE ALL PRIVILEGES ON TABLE "{table}" FROM PUBLIC' in rendered


@pytest.mark.parametrize("bad_table", (None, HEAD, *APPEND_ONLY))
def test_direct_acl_verification_checks_every_dialogue_table(bad_table: str | None) -> None:
    fake = _Connection(bad_table=bad_table)
    if bad_table is None:
        production_db._require_acl(_connection(fake), "runtime")
        observed = {str(params[1]) for text, params in fake.calls
                    if "has_table_privilege" in text and params is not None}
        assert set((*APPEND_ONLY, HEAD)).issubset(observed)
    else:
        with pytest.raises(production_db.ProductionDatabaseError, match=bad_table):
            production_db._require_acl(_connection(fake), "runtime")


@pytest.mark.parametrize("bad_table", (None, HEAD, *APPEND_ONLY))
def test_tc3_acl_verification_checks_head_and_append_only_payloads(
    monkeypatch: pytest.MonkeyPatch, bad_table: str | None,
) -> None:
    seen: list[str] = []

    def execute(env_id: str, statement: str, role: str | None = None) -> dict[str, object]:
        assert env_id == "offline"
        table = next(table for table in (*production_db._APPEND_ONLY_TABLES, HEAD)
                     if f"public.{table}'" in statement)
        seen.append(table)
        update = table == HEAD
        row = {"s": True, "i": True, "u": not update if table == bad_table else update, "d": False}
        return {"Rows": [json.dumps(row)]}

    monkeypatch.setattr(tc3_database_release, "_execute_sql", execute)
    if bad_table is None:
        tc3_database_release._verify_remote_runtime_role("offline", "runtime")
        assert set((*APPEND_ONLY, HEAD)).issubset(seen)
    else:
        with pytest.raises(tc3_database_release.CloudReleaseGateError, match=bad_table):
            tc3_database_release._verify_remote_runtime_role("offline", "runtime")


@pytest.mark.parametrize("head_version", (1, 2))
def test_local_probe_updates_only_head_and_always_rolls_back(head_version: int) -> None:
    fake = _Connection(head_version=head_version)
    if head_version != 2:
        with pytest.raises(production_db.ProductionDatabaseError, match=r"advance.*head"):
            production_db._run_rollback_only_tamper_probe(_connection(fake))
    else:
        production_db._run_rollback_only_tamper_probe(_connection(fake))
        statements = [item[0] for item in fake.calls]
        for table in (*APPEND_ONLY, HEAD):
            assert any(item.startswith(f"INSERT INTO {table} ") for item in statements)
            assert any(item.startswith(f'DELETE FROM "{table}"') for item in statements)
        for table in APPEND_ONLY:
            assert any(item.startswith(f'UPDATE "{table}"') for item in statements)
        assert not any(item.startswith(f'UPDATE "{HEAD}"') for item in statements)
    assert fake.rollbacks == 2


def test_tc3_probe_uses_real_dialogue_columns_and_checks_head_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed, rejected = [], []

    def execute(env_id: str, statement: str, role: str | None = None) -> dict[str, object]:
        allowed.append(statement)
        if "SELECT version_num" in statement:
            return {"Rows": [json.dumps({"version_num": "head"})]}
        if "FROM pg_trigger" in statement:
            names = [f"trg_{table}_immutable" for table in production_db._APPEND_ONLY_TABLES]
            names.append("trg_backtest_runs_terminal_immutable")
            return {"Rows": [json.dumps({"tgname": name, "tgenabled": "O"}) for name in names]}
        return {}

    monkeypatch.setattr(tc3_database_release, "_execute_sql", execute)
    monkeypatch.setattr(tc3_database_release, "_require_remote_runtime_role", lambda *args: None)
    monkeypatch.setattr(tc3_database_release, "_verify_remote_runtime_role", lambda *args: None)
    monkeypatch.setattr(tc3_database_release, "_expect_sql_rejected",
                        lambda env, statement, role: rejected.append(statement))
    tc3_database_release.verify_database_via_tc3("offline", "runtime", "head")
    for table in APPEND_ONLY:
        assert f"UPDATE public.{table} SET payload_json=payload_json WHERE false" in rejected
    assert "DELETE FROM public.dialogue_drafts WHERE false" in rejected
    assert (
        "UPDATE public.dialogue_drafts SET storage_version=storage_version WHERE false" in allowed
    )


def test_owner_check_covers_all_four_dialogue_tables() -> None:
    fake = _Connection()
    production_db._require_not_object_owner(_connection(fake), "runtime")
    assert len(fake.calls) == 1
    params = fake.calls[0][1]
    assert params is not None
    assert set((*APPEND_ONLY, HEAD)).issubset(cast(list[str], params[1]))


@pytest.mark.parametrize("missing", (None, "trg_dialogue_idempotency_immutable"))
def test_direct_trigger_check_requires_the_new_append_only_guards(missing: str | None) -> None:
    class TriggerConnection(_Connection):
        def execute(
            self, statement: str | sql.Composable, params: Sequence[object] | None = None,
        ) -> _Cursor:
            names = {f"trg_{table}_immutable" for table in production_db._APPEND_ONLY_TABLES}
            names.add("trg_backtest_runs_terminal_immutable")
            assert f"trg_{HEAD}_immutable" not in names
            return _Cursor(rows=[(name, "O") for name in names if name != missing])

    if missing is None:
        production_db._require_triggers(_connection(TriggerConnection()))
    else:
        with pytest.raises(production_db.ProductionDatabaseError, match=r"triggers.*missing"):
            production_db._require_triggers(_connection(TriggerConnection()))
