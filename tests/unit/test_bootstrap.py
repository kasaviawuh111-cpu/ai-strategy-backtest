import hashlib
import json
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient
from httpx import Response

from ashare_lab import bootstrap as bootstrap_module
from ashare_lab import settings as settings_module
from ashare_lab.adapters.event_sources import (
    EventCollectionRequest,
    EventCollectionResult,
    EventSourceCollection,
    build_event_acquisition_coverage,
)
from ashare_lab.adapters.event_sources.eastmoney import eastmoney_preparable_event_codes
from ashare_lab.adapters.market_data import SnapshotPreparationFailedError
from ashare_lab.adapters.market_data.choice_snapshot import (
    STRICT_CORPORATE_ACTION_CATEGORIES,
    STRICT_CORPORATE_ACTION_COVERAGE_SCOPE,
)
from ashare_lab.bootstrap import create_configured_app
from ashare_lab.domain.shared import InstrumentId
from ashare_lab.ports.market_data import DataRequirements, DateRange
from ashare_lab.settings import AppSettings
from tests.unit.adapters.event_sources.query_evidence import eastmoney_query_evidence
from tests.unit.adapters.session_reference_fixture import choice_snapshot_fixture


def _write_snapshot_manifest(data_root: Path, *, strict_events: bool = False) -> None:
    files: dict[str, object] = {"corporate_actions.parquet": {}}
    payload: dict[str, object] = {
        "capabilities": {
            "corporateActions": "validated_for_demo",
            "corporateActionLedger": "point_in_time.v1",
        },
        "corporateActionCoverage": {
            "status": "complete",
            "querySucceeded": True,
            "provider": "test-provider",
            "rawResponseSha256": "c" * 64,
        },
        "files": files,
    }
    if strict_events:
        payload["schemaVersion"] = "ashare-lab.composite-research-snapshot.v1"
        payload["capabilities"]["events"] = "validated_for_demo"  # type: ignore[index]
        payload["eventPolicy"] = {
            "strictDemoSecondsOnly": True,
            "retrievalClockUsedForReplay": False,
        }
        files.update({"events.parquet": {}, "event_observations.parquet": {}})
    (data_root / "snapshot_manifest.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def _write_composite_v2(output_root: Path) -> Path:
    staging = output_root / "staging"
    staging.mkdir(parents=True)
    for name in (
        "daily_ohlcv.parquet",
        "signal_daily_ohlcv.parquet",
        "events.parquet",
        "event_observations.parquet",
    ):
        (staging / name).write_bytes(f"fixture:{name}".encode())
    choice_fixture = choice_snapshot_fixture(output_root / "choice-fixture")
    shutil.copy2(
        choice_fixture.path / "corporate_actions.parquet",
        staging / "corporate_actions.parquet",
    )
    _write_session_reference(staging / "instrument_sessions.parquet")
    source = staging / "source"
    source.mkdir()
    (source / "choice_snapshot_manifest.json").write_text("{}", encoding="utf-8")
    (source / "event_snapshot_manifest.json").write_text("{}", encoding="utf-8")
    paths = tuple(path for path in staging.rglob("*") if path.is_file())
    files = {
        path.relative_to(staging).as_posix(): {
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in paths
    }
    event_code = "event.financial_results.annual_report"
    instrument_id = InstrumentId("300059.SZ")
    coverage_start = date(2025, 1, 2)
    coverage_end = date(2025, 1, 2)
    event_coverage = build_event_acquisition_coverage(
        EventCollectionRequest(
            instrument_id=instrument_id,
            start=coverage_start,
            end=coverage_end,
            retrieved_at=datetime(2025, 1, 3, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        ),
        EventCollectionResult(
            observations=(),
            sources=(
                EventSourceCollection(
                    provider="eastmoney",
                    status="empty",
                    observations=(),
                    acquisition_evidence=eastmoney_query_evidence(
                        instrument_id=instrument_id,
                        start=coverage_start,
                        end=coverage_end,
                        event_codes=(event_code,),
                        total_hits=0,
                    ),
                ),
            ),
        ),
        requested_event_codes=(event_code,),
    )
    body: dict[str, object] = {
        "schemaVersion": "ashare-lab.composite-research-snapshot.v2",
        "capabilities": {
            "corporateActions": "validated_for_demo",
            "corporateActionLedger": "point_in_time.v1",
            "events": "validated_for_demo",
        },
        "corporateActionCoverage": {
            "status": "complete",
            "querySucceeded": True,
            "provider": "test-provider",
            "start": coverage_start.isoformat(),
            "end": coverage_end.isoformat(),
            "rawResponseSha256": "c" * 64,
            "coverageScope": STRICT_CORPORATE_ACTION_COVERAGE_SCOPE,
            "supportedCategories": list(STRICT_CORPORATE_ACTION_CATEGORIES),
            "unsupportedCategories": [],
        },
        "eventAcquisitionCoverage": event_coverage,
        "eventPolicy": {
            "strictDemoSecondsOnly": True,
            "retrievalClockUsedForReplay": False,
        },
        "rowCounts": {"eventObservations": 0, "corporateActions": 0},
        "files": files,
    }
    digest = hashlib.sha256(
        json.dumps(body, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    body["snapshotId"] = f"composite:{digest}"
    final = output_root / digest
    staging.rename(final)
    (final / "snapshot_manifest.json").write_text(
        json.dumps(body),
        encoding="utf-8",
    )
    return final


def _write_session_reference(path: Path) -> None:
    table = pa.table(
        {
            "stock_code": ["300059"],
            "date": ["2025-01-02"],
            "board": ["chinext"],
            "trading_status": ["trading"],
            "previous_close": [10.0],
            "upper_limit": [12.0],
            "lower_limit": [8.0],
            "minimum_buy_quantity": [100],
            "buy_quantity_increment": [100],
            "price_tick": [0.01],
            "t_plus_one": [True],
            "is_st": [False],
        }
    )
    pq.write_table(table, path)


def test_injected_code_revision_cannot_masquerade_as_local_git(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual = "a" * 40
    monkeypatch.setattr(settings_module, "_repository_has_git_metadata", lambda: True)
    monkeypatch.setattr(settings_module, "_working_tree_revision", lambda: actual)

    assert AppSettings(code_revision=actual).code_revision_matches_workspace is True
    assert AppSettings(code_revision="b" * 40).code_revision_matches_workspace is False


def test_dirty_revision_cannot_construct_strict_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dirty_revision = "a" * 40 + "+dirty"
    monkeypatch.setattr(settings_module, "_repository_has_git_metadata", lambda: True)
    monkeypatch.setattr(settings_module, "_working_tree_revision", lambda: dirty_revision)
    settings = AppSettings(
        app_env="local",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'runs.db'}",
        data_root=tmp_path,
        catalog_root=Path(__file__).parents[2] / "catalogs",
        market_data_profile="composite_snapshot",
        session_reference_mode="parquet",
        session_reference_path=tmp_path / "instrument_sessions.parquet",
        event_data_required=True,
        code_revision=dirty_revision,
    )

    with pytest.raises(RuntimeError, match="clean 40-character Git SHA"):
        create_configured_app(settings)
    assert not (tmp_path / "runs.db").exists()


def test_forged_revision_cannot_construct_strict_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual_revision = "a" * 40
    monkeypatch.setattr(settings_module, "_repository_has_git_metadata", lambda: True)
    monkeypatch.setattr(settings_module, "_working_tree_revision", lambda: actual_revision)
    settings = AppSettings(
        app_env="local",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'runs.db'}",
        data_root=tmp_path,
        catalog_root=Path(__file__).parents[2] / "catalogs",
        market_data_profile="composite_snapshot",
        session_reference_mode="parquet",
        session_reference_path=tmp_path / "instrument_sessions.parquet",
        event_data_required=True,
        code_revision="b" * 40,
    )

    with pytest.raises(RuntimeError, match="does not match the clean local Git workspace"):
        create_configured_app(settings)
    assert not (tmp_path / "runs.db").exists()


def test_production_cannot_construct_runtime_from_non_strict_profile(
    tmp_path: Path,
) -> None:
    settings = AppSettings(
        app_env="production",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'runs.db'}",
        data_root=tmp_path,
        catalog_root=Path(__file__).parents[2] / "catalogs",
        market_data_profile="generic_parquet",
        session_reference_mode="parquet",
        session_reference_path=tmp_path / "instrument_sessions.parquet",
        event_data_required=True,
        code_revision="a" * 40,
    )

    with pytest.raises(RuntimeError, match="MARKET_DATA_PROFILE=composite_snapshot"):
        create_configured_app(settings)
    assert not (tmp_path / "runs.db").exists()


def test_configured_app_exposes_backtest_capability(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / "daily_ohlcv.parquet").touch()
    settings = AppSettings(
        app_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'runs.db'}",
        data_root=data_root,
        catalog_root=Path(__file__).parents[2] / "catalogs",
        queue_backend="thread",
        market_data_profile="generic_parquet",
        session_reference_mode="research_300059",
        event_data_required=False,
    )
    app = create_configured_app(settings)
    event_strategy = json.loads(
        (
            Path(__file__).parents[2]
            / "contracts/examples/strategy.event-forecast-macd.daily.v1.json"
        ).read_text(encoding="utf-8")
    )

    with TestClient(app) as client:
        capability = cast(
            Response,
            client.get(  # pyright: ignore[reportUnknownMemberType]
                "/api/v1/capabilities"
            ),
        )
        submission = cast(
            Response,
            client.post(  # pyright: ignore[reportUnknownMemberType]
                "/api/v1/backtest-runs",
                json={"strategy": event_strategy},
            ),
        )

    assert capability.status_code == 200
    assert capability.json()["backtest_execution_available"] is True
    assert capability.json()["event_backtest_available"] is False
    assert submission.status_code == 422
    assert submission.json()["error"]["code"] == "event_data_unavailable"


def test_technical_demo_can_be_ready_without_event_dataset(tmp_path: Path) -> None:
    data_root = choice_snapshot_fixture(tmp_path / "choice").path
    settings = AppSettings(
        app_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'runs.db'}",
        data_root=data_root,
        catalog_root=Path(__file__).parents[2] / "catalogs",
        queue_backend="thread",
        market_data_profile="choice_snapshot",
        session_reference_mode="research_300059",
        event_data_required=False,
    )
    app = create_configured_app(settings)

    with TestClient(app) as client:
        response = cast(
            Response,
            client.get(  # pyright: ignore[reportUnknownMemberType]
                "/api/v1/ready"
            ),
        )
        capability = cast(
            Response,
            client.get(  # pyright: ignore[reportUnknownMemberType]
                "/api/v1/capabilities"
            ),
        )

    assert response.status_code == 200
    assert response.json()["checks"] == {
        "daily_data": "ok",
        "corporate_actions": "ok",
        "corporate_action_manifest": "ok",
        "job_queue": "ok",
        "run_store": "ok",
        "session_reference": "ok",
        "signal_data": "ok",
        "snapshot_manifest": "ok",
    }
    assert capability.json()["event_backtest_available"] is False


def test_event_data_switch_disables_capability_even_when_file_exists(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / "events.parquet").touch()
    settings = AppSettings(
        app_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'runs.db'}",
        data_root=data_root,
        catalog_root=Path(__file__).parents[2] / "catalogs",
        queue_backend="thread",
        market_data_profile="generic_parquet",
        session_reference_mode="research_300059",
        event_data_required=False,
    )
    app = create_configured_app(settings)

    with TestClient(app) as client:
        capability = cast(
            Response,
            client.get(  # pyright: ignore[reportUnknownMemberType]
                "/api/v1/capabilities"
            ),
        )

    assert capability.status_code == 200
    assert capability.json()["event_backtest_available"] is False


def test_composite_startup_requires_event_observation_audit_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = "a" * 40
    monkeypatch.setattr(settings_module, "_repository_has_git_metadata", lambda: True)
    monkeypatch.setattr(settings_module, "_working_tree_revision", lambda: revision)
    data_root = tmp_path / "data"
    data_root.mkdir()
    for name in (
        "daily_ohlcv.parquet",
        "signal_daily_ohlcv.parquet",
        "events.parquet",
        "corporate_actions.parquet",
    ):
        (data_root / name).touch()
    session_path = data_root / "instrument_sessions.parquet"
    _write_session_reference(session_path)
    _write_snapshot_manifest(data_root, strict_events=True)
    settings = AppSettings(
        app_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'runs.db'}",
        data_root=data_root,
        catalog_root=Path(__file__).parents[2] / "catalogs",
        queue_backend="thread",
        market_data_profile="composite_snapshot",
        session_reference_mode="parquet",
        session_reference_path=session_path,
        event_data_required=True,
        code_revision=revision,
    )

    with pytest.raises(RuntimeError, match=r"event_observations\.parquet"):
        create_configured_app(settings)


def test_composite_readiness_accepts_only_persisted_strict_event_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = "a" * 40
    monkeypatch.setattr(settings_module, "_repository_has_git_metadata", lambda: True)
    monkeypatch.setattr(settings_module, "_working_tree_revision", lambda: revision)
    data_root = _write_composite_v2(tmp_path / "composite")
    session_path = data_root / "instrument_sessions.parquet"
    settings = AppSettings(
        app_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'runs.db'}",
        data_root=data_root,
        catalog_root=Path(__file__).parents[2] / "catalogs",
        queue_backend="thread",
        market_data_profile="composite_snapshot",
        session_reference_mode="parquet",
        session_reference_path=session_path,
        event_data_required=True,
        code_revision=revision,
    )
    app = create_configured_app(settings)
    startup_pin = app.state.runtime.execution.strict_snapshot
    backtest_anchor_date = app.state.runtime.execution.backtest_anchor_date
    readiness_pin = app.state.runtime.execution.market_data.pin_strict_composite_snapshot()

    with TestClient(app) as client:
        readiness = cast(
            Response,
            client.get(  # pyright: ignore[reportUnknownMemberType]
                "/api/v1/ready"
            ),
        )
        capability = cast(
            Response,
            client.get(  # pyright: ignore[reportUnknownMemberType]
                "/api/v1/capabilities"
            ),
        )
        draft = cast(
            Response,
            client.post(  # pyright: ignore[reportUnknownMemberType]
                "/api/v1/strategy-drafts",
                json={
                    "utterance": "MACD金叉买入，死叉卖出，近五年",
                    "instrument_context": "300059.SZ",
                    "as_of_date": "2026-08-06",
                },
            ),
        )
        (data_root / "events.parquet").write_bytes(b"changed-after-startup")
        degraded = cast(
            Response,
            client.get(  # pyright: ignore[reportUnknownMemberType]
                "/api/v1/ready"
            ),
        )
        degraded_capability = cast(
            Response,
            client.get(  # pyright: ignore[reportUnknownMemberType]
                "/api/v1/capabilities"
            ),
        )

    assert readiness.status_code == 200
    assert readiness.json()["checks"]["strict_snapshot_pin"] == "ok"
    assert capability.json()["event_backtest_available"] is True
    assert backtest_anchor_date == date(2025, 1, 2)
    assert draft.status_code == 201
    assert draft.json()["strategy"]["backtest"] == {
        "start": "2020-01-02",
        "end": "2025-01-02",
        "initial_cash_cny": 1_000_000,
    }
    assert {
        item["event_code"] for item in capability.json()["events"] if item["backtest_available"]
    } == {"event.financial_results.annual_report"}
    assert all(item["catalog_status"] == "stable" for item in capability.json()["events"])
    assert startup_pin is not None
    assert readiness_pin == startup_pin
    assert degraded.status_code == 503
    assert "strict_snapshot_pin" in degraded.json()["error"]["message"]
    assert degraded_capability.json()["event_backtest_available"] is False
    assert not any(item["backtest_available"] for item in degraded_capability.json()["events"])


def test_v1_composite_manifest_cannot_construct_a_ready_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = "a" * 40
    monkeypatch.setattr(settings_module, "_repository_has_git_metadata", lambda: True)
    monkeypatch.setattr(settings_module, "_working_tree_revision", lambda: revision)
    data_root = tmp_path / "v1"
    data_root.mkdir()
    for name in (
        "daily_ohlcv.parquet",
        "signal_daily_ohlcv.parquet",
        "events.parquet",
        "event_observations.parquet",
        "corporate_actions.parquet",
    ):
        (data_root / name).touch()
    session_path = data_root / "instrument_sessions.parquet"
    _write_session_reference(session_path)
    _write_snapshot_manifest(data_root, strict_events=True)
    settings = AppSettings(
        app_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'runs.db'}",
        data_root=data_root,
        catalog_root=Path(__file__).parents[2] / "catalogs",
        queue_backend="thread",
        market_data_profile="composite_snapshot",
        session_reference_mode="parquet",
        session_reference_path=session_path,
        event_data_required=True,
        code_revision=revision,
    )

    with pytest.raises(RuntimeError, match=r"unsupported schemaVersion"):
        create_configured_app(settings)
    assert not (tmp_path / "runs.db").exists()


@pytest.mark.parametrize(
    ("missing_filename", "failed_check"),
    [
        ("signal_daily_ohlcv.parquet", "signal_data"),
        ("corporate_actions.parquet", "corporate_actions"),
        ("snapshot_manifest.json", "snapshot_manifest"),
    ],
)
def test_choice_snapshot_readiness_requires_signal_and_manifest(
    tmp_path: Path,
    missing_filename: str,
    failed_check: str,
) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    for name in (
        "daily_ohlcv.parquet",
        "signal_daily_ohlcv.parquet",
        "corporate_actions.parquet",
    ):
        if name != missing_filename:
            (data_root / name).touch()
    if missing_filename != "snapshot_manifest.json":
        _write_snapshot_manifest(data_root)
    settings = AppSettings(
        app_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'runs.db'}",
        data_root=data_root,
        catalog_root=Path(__file__).parents[2] / "catalogs",
        queue_backend="thread",
        market_data_profile="choice_snapshot",
        session_reference_mode="research_300059",
        event_data_required=False,
    )
    app = create_configured_app(settings)

    with TestClient(app) as client:
        response = cast(
            Response,
            client.get(  # pyright: ignore[reportUnknownMemberType]
                "/api/v1/ready"
            ),
        )

    assert response.status_code == 503
    assert failed_check in response.json()["error"]["message"]


def test_production_rejects_research_session_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = "a" * 40
    monkeypatch.setattr(settings_module, "_repository_has_git_metadata", lambda: True)
    monkeypatch.setattr(settings_module, "_working_tree_revision", lambda: revision)
    settings = AppSettings(
        app_env="production",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'runs.db'}",
        data_root=tmp_path,
        catalog_root=Path(__file__).parents[2] / "catalogs",
        market_data_profile="composite_snapshot",
        session_reference_mode="research_300059",
        event_data_required=True,
        code_revision=revision,
    )

    with pytest.raises(RuntimeError, match="SESSION_REFERENCE_MODE=parquet"):
        create_configured_app(settings)


def test_parquet_session_hash_is_injected_into_submission_versions(tmp_path: Path) -> None:
    session_path = tmp_path / "instrument_sessions.parquet"
    _write_session_reference(session_path)
    settings = AppSettings(
        app_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'runs.db'}",
        data_root=tmp_path,
        catalog_root=Path(__file__).parents[2] / "catalogs",
        market_data_profile="generic_parquet",
        session_reference_mode="parquet",
        session_reference_path=session_path,
        event_data_required=False,
    )

    app = create_configured_app(settings)

    version = app.state.runtime.execution.executor.session_reference.version
    assert version.startswith("parquet-instrument-sessions.v2+sha256:")


def test_on_demand_profile_is_ready_without_pretending_a_snapshot_is_preloaded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ashare_lab.bootstrap._on_demand_provider_runtime_available",
        lambda: True,
    )
    monkeypatch.setattr(
        "ashare_lab.bootstrap._on_demand_document_text_runtime_available",
        lambda: True,
    )
    settings = AppSettings(
        app_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'runs.db'}",
        catalog_root=Path(__file__).parents[2] / "catalogs",
        queue_backend="thread",
        market_data_profile="on_demand_snapshot",
        event_data_required=True,
        choice_snapshot_root=tmp_path / "choice",
        technical_snapshot_root=tmp_path / "technical",
        event_snapshot_root=tmp_path / "events",
        composite_snapshot_root=tmp_path / "composite",
        snapshot_preparation_root=tmp_path / "preparations",
    )
    app = create_configured_app(settings)

    with TestClient(app) as client:
        readiness = cast(Response, client.get("/api/v1/ready"))
        capability = cast(Response, client.get("/api/v1/capabilities"))

    assert readiness.status_code == 200
    assert readiness.json()["checks"]["on_demand_snapshot_registry"] == "ok"
    assert "daily_data" not in readiness.json()["checks"]
    assert capability.json()["event_backtest_available"] is False
    assert capability.json()["event_preparation_available"] is True
    assert capability.json()["event_availability_scope"] == "request_preparation"
    assert {
        item["event_code"] for item in capability.json()["events"] if item["preparation_available"]
    } == set(eastmoney_preparable_event_codes())
    assert len(eastmoney_preparable_event_codes()) == 68
    assert "event.macro_policy_industry.license_approval" not in eastmoney_preparable_event_codes()
    assert all(not item["backtest_available"] for item in capability.json()["events"])
    by_code = {item["event_code"]: item for item in capability.json()["events"]}
    assert by_code["event.financial_results.annual_report"]["document_text"] == {
        "catalog_available": True,
        "backtest_available": False,
        "preparation_available": True,
        "availability_scope": "request_preparation",
        "unavailable_reason": "preparation_required",
    }
    assert by_code["event.contracts_orders.major_contract_won"]["document_text"] == {
        "catalog_available": False,
        "backtest_available": False,
        "preparation_available": False,
        "availability_scope": "unavailable",
        "unavailable_reason": "not_catalog_available",
    }
    assert app.state.runtime.execution.executor.sessions_from_snapshot is True
    assert all(
        path.is_dir()
        for path in (
            settings.choice_snapshot_root,
            settings.technical_snapshot_root,
            settings.event_snapshot_root,
            settings.composite_snapshot_root,
            settings.snapshot_preparation_root,
        )
    )


def test_on_demand_bootstrap_never_selects_standalone_source_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    choice_root = tmp_path / "choice"
    choice_snapshot_fixture(choice_root)
    settings = AppSettings(
        app_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'runs.db'}",
        catalog_root=Path(__file__).parents[2] / "catalogs",
        market_data_profile="on_demand_snapshot",
        choice_snapshot_root=choice_root,
        technical_snapshot_root=tmp_path / "technical",
        event_snapshot_root=tmp_path / "events",
        composite_snapshot_root=tmp_path / "composite",
        snapshot_preparation_root=tmp_path / "preparations",
        on_demand_refresh_each_submission=False,
    )

    def provider_unavailable(
        _self: object,
        _requirements: DataRequirements,
        _period: DateRange,
    ) -> object:
        raise SnapshotPreparationFailedError("fixture provider unavailable")

    monkeypatch.setattr(
        bootstrap_module.InternalDemoSnapshotPreparer,
        "prepare",
        provider_unavailable,
    )
    market_data, sessions_from_snapshot = bootstrap_module._build_market_data_repository(settings)
    period = DateRange(date(2025, 1, 2), date(2025, 1, 3))
    requirements = DataRequirements(
        instruments=(InstrumentId("300059.SZ"),),
        datasets=("daily_ohlcv", "corporate_actions"),
    )

    with pytest.raises(SnapshotPreparationFailedError, match="provider unavailable"):
        market_data.pin_snapshot(requirements, period)

    assert sessions_from_snapshot is True
    assert market_data._registry._choice_root is None  # type: ignore[attr-defined]
    assert market_data._registry._technical_root is None  # type: ignore[attr-defined]
    assert market_data._preparer._choice_output_root == choice_root  # type: ignore[attr-defined]
    assert market_data._preparer._technical_output_root == (  # type: ignore[attr-defined]
        settings.technical_snapshot_root
    )


@pytest.mark.parametrize(
    ("available_modules", "expected"),
    [
        (frozenset({"baostock", "httpx"}), True),
        (frozenset({"baostock", "EmQuantAPI"}), True),
        (frozenset({"httpx", "EmQuantAPI"}), False),
        (frozenset({"baostock"}), False),
    ],
)
def test_on_demand_runtime_requires_baostock_and_one_daily_provider(
    monkeypatch: pytest.MonkeyPatch,
    available_modules: frozenset[str],
    expected: bool,
) -> None:
    monkeypatch.setattr(
        bootstrap_module.importlib.util,
        "find_spec",
        lambda name: object() if name in available_modules else None,
    )

    assert bootstrap_module._on_demand_provider_runtime_available() is expected


def test_on_demand_readiness_requires_push2_fallback_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ashare_lab.bootstrap._on_demand_provider_runtime_available",
        lambda: True,
    )
    settings = AppSettings(
        app_env="test",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'runs.db'}",
        catalog_root=Path(__file__).parents[2] / "catalogs",
        market_data_profile="on_demand_snapshot",
        choice_snapshot_root=tmp_path / "choice",
        technical_snapshot_root=tmp_path / "technical",
        event_snapshot_root=tmp_path / "events",
        composite_snapshot_root=tmp_path / "composite",
        snapshot_preparation_root=tmp_path / "preparations",
    )
    app = create_configured_app(settings)
    original_is_file = Path.is_file

    def is_file_without_push2(path: Path) -> bool:
        if path.name == "prepare_eastmoney_snapshot.py":
            return False
        return original_is_file(path)

    monkeypatch.setattr(Path, "is_file", is_file_without_push2)

    assert not bootstrap_module._on_demand_dependencies_available(
        settings,
        app.state.runtime.execution.market_data,
    )
