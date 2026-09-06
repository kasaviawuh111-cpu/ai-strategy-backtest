from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from deploy.cloudbase import (
    deployment_entrypoint,
    production_db,
    runtime_entrypoint,
    tc3_database_release,
)


def _runtime_environment(tmp_path: Path) -> dict[str, str]:
    mount = tmp_path / "provider-mount"
    mount.mkdir()
    storage_id = "cfs:ashare-snapshot-store"
    restart_probe_id = "restart-probe-20260831"
    marker = mount / ".ashare-snapshot-store.json"
    marker.write_text(
        json.dumps(
            {
                "schemaVersion": "ashare-lab.durable-snapshot-store.v1",
                "storageId": storage_id,
                "restartProbeId": restart_probe_id,
                "durable": True,
            }
        ),
        encoding="utf-8",
    )
    roots: dict[str, str] = {}
    for variable, name in (
        ("CHOICE_SNAPSHOT_ROOT", "choice"),
        ("TECHNICAL_SNAPSHOT_ROOT", "technical"),
        ("EVENT_SNAPSHOT_ROOT", "events"),
        ("COMPOSITE_SNAPSHOT_ROOT", "composite"),
        ("SNAPSHOT_PREPARATION_ROOT", "preparations"),
    ):
        path = mount / name
        path.mkdir()
        roots[variable] = str(path)
    return {
        "APP_ENV": "production",
        "DATABASE_URL": "postgresql+psycopg://ashare_runtime:secret@db.internal/ashare",
        "INITIALIZE_SCHEMA": "false",
        "CODE_REVISION": "a" * 40,
        "MARKET_DATA_PROFILE": "on_demand_snapshot",
        "ON_DEMAND_REFRESH_EACH_SUBMISSION": "true",
        "CANDIDATE_PROVIDER_MODE": "openai_compatible",
        "CANDIDATE_PROVIDER_API_KEY": "candidate-test-secret",
        "RESEARCH_PROVIDER_MODE": "deepseek_responses",
        "RESEARCH_PROVIDER_API_KEY": "research-test-secret",
        "MX_SAAS_API_KEY": "mx-test-secret",
        "SNAPSHOT_STORAGE_MODE": "durable_mount",
        "SNAPSHOT_STORAGE_ID": storage_id,
        "SNAPSHOT_STORAGE_RESTART_PROBE_ID": restart_probe_id,
        "SNAPSHOT_STORAGE_MARKER": str(marker),
        **roots,
    }


def test_runtime_requires_postgres_non_admin_and_durable_snapshot_mount(
    tmp_path: Path,
) -> None:
    environment = _runtime_environment(tmp_path)

    runtime_entrypoint.validate_runtime_environment(environment)

    for database_url in (
        "sqlite+pysqlite:///unsafe.db",
        "postgresql+psycopg://cloudbase_admin:secret@db.internal/ashare",
        "postgresql+psycopg://service_role:secret@db.internal/ashare",
    ):
        invalid = {**environment, "DATABASE_URL": database_url}
        with pytest.raises(runtime_entrypoint.ProductionConfigurationError):
            runtime_entrypoint.validate_runtime_environment(invalid)

    without_mount = {**environment, "SNAPSHOT_STORAGE_MODE": "image_layer"}
    with pytest.raises(
        runtime_entrypoint.ProductionConfigurationError,
        match="durable external mount",
    ):
        runtime_entrypoint.validate_runtime_environment(without_mount)


def test_runtime_rejects_snapshot_marker_not_proven_across_restart(tmp_path: Path) -> None:
    environment = _runtime_environment(tmp_path)
    environment["SNAPSHOT_STORAGE_RESTART_PROBE_ID"] = "different-restart-probe"

    with pytest.raises(
        runtime_entrypoint.ProductionConfigurationError,
        match="does not match",
    ):
        runtime_entrypoint.validate_runtime_environment(environment)


def test_runtime_requires_fresh_submission_and_backend_only_provider_secrets(
    tmp_path: Path,
) -> None:
    environment = _runtime_environment(tmp_path)

    with pytest.raises(
        runtime_entrypoint.ProductionConfigurationError,
        match="ON_DEMAND_REFRESH_EACH_SUBMISSION=true",
    ):
        runtime_entrypoint.validate_runtime_environment(
            {**environment, "ON_DEMAND_REFRESH_EACH_SUBMISSION": "false"}
        )

    for variable in (
        "CANDIDATE_PROVIDER_API_KEY",
        "RESEARCH_PROVIDER_API_KEY",
        "MX_SAAS_API_KEY",
    ):
        with pytest.raises(
            runtime_entrypoint.ProductionConfigurationError,
            match="backend-only provider secrets",
        ):
            runtime_entrypoint.validate_runtime_environment({**environment, variable: ""})


def test_deployment_profile_is_explicit_and_strict_keeps_production_entrypoint() -> None:
    with pytest.raises(
        deployment_entrypoint.DeploymentProfileError,
        match="DEPLOYMENT_PROFILE",
    ):
        deployment_entrypoint.command_for_environment({})

    assert deployment_entrypoint.command_for_environment(
        {"DEPLOYMENT_PROFILE": "strict_production"}
    ) == ("python", "deploy/cloudbase/runtime_entrypoint.py")


def test_ephemeral_candidate_is_labeled_and_cannot_masquerade_as_production() -> None:
    environment = {
        "DEPLOYMENT_PROFILE": "ephemeral_candidate",
        "APP_ENV": "staging",
        "DATABASE_URL": "sqlite+pysqlite:////app/var/ephemeral/ashare.db",
        "INITIALIZE_SCHEMA": "true",
        "QUEUE_BACKEND": "thread",
        "PERSISTENCE_MODE": "ephemeral",
        "RESTART_RECOVERY_VERIFIED": "false",
        "SNAPSHOT_STORAGE_MODE": "ephemeral_local",
        "MARKET_DATA_PROFILE": "on_demand_snapshot",
        "ON_DEMAND_REFRESH_EACH_SUBMISSION": "true",
        "CANDIDATE_PROVIDER_MODE": "openai_compatible",
        "CANDIDATE_PROVIDER_API_KEY": "candidate-test-secret",
        "RESEARCH_PROVIDER_MODE": "deepseek_responses",
        "RESEARCH_PROVIDER_API_KEY": "research-test-secret",
        "MX_SAAS_API_KEY": "mx-test-secret",
        "CODE_REVISION": "a" * 40,
    }

    command = deployment_entrypoint.command_for_environment(environment)
    assert command[:2] == ("uvicorn", "ashare_lab.main:create_app")

    for variable, value in (
        ("APP_ENV", "production"),
        ("DATABASE_URL", "postgresql+psycopg://runtime:secret@db/app"),
        ("INITIALIZE_SCHEMA", "false"),
        ("PERSISTENCE_MODE", "durable"),
        ("RESTART_RECOVERY_VERIFIED", "true"),
        ("SNAPSHOT_STORAGE_MODE", "durable_mount"),
        ("ON_DEMAND_REFRESH_EACH_SUBMISSION", "false"),
    ):
        invalid = {**environment, variable: value}
        with pytest.raises(deployment_entrypoint.DeploymentProfileError):
            deployment_entrypoint.command_for_environment(invalid)

    for variable in (
        "CANDIDATE_PROVIDER_API_KEY",
        "RESEARCH_PROVIDER_API_KEY",
        "MX_SAAS_API_KEY",
    ):
        invalid = {**environment, variable: ""}
        with pytest.raises(
            deployment_entrypoint.DeploymentProfileError,
            match="backend-only provider secrets",
        ):
            deployment_entrypoint.command_for_environment(invalid)


def test_cloud_api_uses_current_tcb_login_without_credentials() -> None:
    calls: list[tuple[str, ...]] = []

    def runner(
        command: tuple[str, ...],
        *,
        capture_output: bool,
        text: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        assert capture_output is True
        assert text is True
        assert check is False
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"Response": {"Executable": True}}),
            stderr="",
        )

    response = tc3_database_release.call_cloud_api(
        "tcb",
        "PreviewPGUserMigrations",
        api_version="2018-06-08",
        body={"EnvId": "env-one"},
        runner=runner,
    )

    assert response == {"Executable": True}
    assert calls == [
        (
            "tcb",
            "api",
            "tcb",
            "PreviewPGUserMigrations",
            "--body",
            '{"EnvId":"env-one"}',
            "--api-version",
            "2018-06-08",
            "--json",
        )
    ]
    assert not any("secret" in item.lower() for item in calls[0])


def test_migration_task_must_reach_terminal_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(
        (
            {"Status": "Accepted", "RequestId": "one"},
            {"Status": "Running", "RequestId": "two"},
            {"Status": "Succeed", "RequestId": "three"},
        )
    )

    def fake_call(
        service: str,
        action: str,
        *,
        api_version: str,
        body: Mapping[str, object],
        runner: tc3_database_release.Runner = subprocess.run,
    ) -> dict[str, object]:
        del runner
        assert (service, action, api_version) == ("tcb", "DescribeTaskResult", "2018-06-08")
        assert body == {"EnvId": "env-one", "TaskId": "task-one"}
        return next(responses)

    monkeypatch.setattr(tc3_database_release, "call_cloud_api", fake_call)

    result = tc3_database_release.wait_for_task(
        "env-one",
        "task-one",
        poll_interval_seconds=0,
        timeout_seconds=1,
    )

    assert result["Status"] == "Succeed"


def test_migration_task_failure_is_not_submission_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tc3_database_release,
        "call_cloud_api",
        lambda *args, **kwargs: {"Status": "Failed", "Reason": "DDL failed"},
    )

    with pytest.raises(tc3_database_release.CloudReleaseGateError, match="task failed"):
        tc3_database_release.wait_for_task(
            "env-one",
            "task-one",
            poll_interval_seconds=0,
            timeout_seconds=1,
        )


def test_push_migration_polls_task_and_rechecks_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = {"Version": "20260831000000", "Name": "p0d", "Query": "SELECT 1;"}
    previews = iter(
        (
            {"remoteStatus": "pending", "requestId": "preview-before"},
            {"remoteStatus": "applied", "requestId": "preview-after"},
        )
    )
    monkeypatch.setattr(tc3_database_release, "preview_migration", lambda *args: next(previews))

    def fake_call(
        service: str,
        action: str,
        *,
        api_version: str,
        body: Mapping[str, object],
        runner: tc3_database_release.Runner = subprocess.run,
    ) -> dict[str, object]:
        del service, api_version, body, runner
        assert action == "PushPGUserMigrations"
        return {"TaskId": "task-one", "RequestId": "push-request"}

    monkeypatch.setattr(tc3_database_release, "call_cloud_api", fake_call)
    monkeypatch.setattr(
        tc3_database_release,
        "wait_for_task",
        lambda *args, **kwargs: {"Status": "Succeed", "RequestId": "task-request"},
    )

    result = tc3_database_release.push_migration(
        "env-one",
        migration,
        poll_interval_seconds=0,
        timeout_seconds=1,
    )

    assert result == {
        "remoteStatus": "applied",
        "taskId": "task-one",
        "taskRequestId": "task-request",
        "requestId": "push-request",
    }


def test_preflight_and_rehearsal_evidence_are_content_bound(tmp_path: Path) -> None:
    state: dict[str, object] = {
        "tables": [],
        "applicationRowCounts": {},
        "runtimeRole": [],
        "triggers": [],
    }
    evidence: dict[str, object] = {
        "schemaVersion": "ashare-lab.cloudbase-pg-preflight.v1",
        "environmentId": "env-one",
        "runtimeRole": "ashare_runtime",
        "codeRevision": "a" * 40,
        "recordedAt": "2026-08-31T00:00:00+00:00",
        "evidenceKind": "logical_preflight_not_provider_physical_backup",
        "databaseState": state,
        "databaseStateSha256": tc3_database_release._canonical_sha256(state),
    }
    evidence["evidenceSha256"] = tc3_database_release._canonical_sha256(evidence)
    path = tmp_path / "preflight.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")

    loaded = tc3_database_release.load_preflight_evidence(
        path,
        env_id="env-one",
        code_revision="a" * 40,
    )
    assert loaded["evidenceKind"] == "logical_preflight_not_provider_physical_backup"

    evidence["runtimeRole"] = "cloudbase_admin"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    with pytest.raises(tc3_database_release.CloudReleaseGateError, match="tampered"):
        tc3_database_release.load_preflight_evidence(
            path,
            env_id="env-one",
            code_revision="a" * 40,
        )


def test_nonempty_database_requires_real_provider_backup() -> None:
    evidence: dict[str, object] = {
        "databaseState": {"applicationRowCounts": {"backtest_runs": "1"}}
    }

    with pytest.raises(
        tc3_database_release.CloudReleaseGateError,
        match="provider physical backup",
    ):
        tc3_database_release._require_logically_recoverable_preflight(evidence)


def test_migration_rehearsal_requires_remote_state_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {
        "tables": [],
        "applicationRowCounts": {},
        "runtimeRole": [],
        "triggers": [],
    }
    executions: list[str] = []
    monkeypatch.setattr(
        tc3_database_release,
        "capture_database_state",
        lambda **kwargs: state,
    )
    monkeypatch.setattr(
        tc3_database_release,
        "_execute_sql",
        lambda env_id, statement, role=None: executions.append(statement) or {},
    )

    with pytest.raises(
        tc3_database_release.CloudReleaseGateError,
        match="deferred",
    ):
        tc3_database_release.rehearse_migration(
            env_id="env-one",
            migration={
                "Version": "20260831000000",
                "Name": "p0d",
                "Query": "CREATE TABLE x(id int);",
            },
            expected_state_sha256=tc3_database_release._canonical_sha256(state),
            runtime_role="ashare_runtime",
        )

    assert executions == []


def test_database_role_password_is_never_optional_or_short(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNTIME_DATABASE_PASSWORD", "short")

    with pytest.raises(production_db.ProductionDatabaseError, match="at least 24"):
        production_db._required_runtime_password()


def test_release_cli_has_no_unverified_cloudrun_or_backup_mutation() -> None:
    parser = tc3_database_release._build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["release-canary"])
    with pytest.raises(SystemExit):
        parser.parse_args(["record-backup"])
