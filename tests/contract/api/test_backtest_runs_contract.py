# Starlette's TestClient currently exposes partially unknown httpx method types to Pyright.
# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false

import json
from collections.abc import Iterator
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ashare_lab.api import create_app
from ashare_lab.domain.execution import CapacityMode
from ashare_lab.domain.shared import RunId
from ashare_lab.domain.strategy import StrategySpec
from ashare_lab.ports.backtest_runs import (
    BacktestJobState,
    BacktestResultIntegrityPolicy,
)

from .backtest_fakes import (
    FakeRunStore,
    FakeSubmitter,
    make_record,
    result_bundle_json,
)

ROOT = Path(__file__).parents[3]
RUNTIME_EVENT_CODES = frozenset(
    {
        "event.financial_results.annual_report",
        "event.financial_results.semiannual_report",
        "event.financial_results.quarterly_report",
        "event.financial_results.earnings_forecast_published",
        "event.financial_results.earnings_flash_report",
    }
)


@pytest.fixture
def strategy_payload() -> dict[str, Any]:
    spec = StrategySpec.model_validate_json(
        (ROOT / "contracts/examples/strategy.macd-volume.daily.v1.json").read_text(encoding="utf-8")
    )
    return spec.model_dump(mode="json")


@pytest.fixture
def event_strategy_payload() -> dict[str, Any]:
    spec = StrategySpec.model_validate_json(
        (ROOT / "contracts/examples/strategy.event-forecast-macd.daily.v1.json").read_text(
            encoding="utf-8"
        )
    )
    return spec.model_dump(mode="json")


@pytest.fixture
def configured_api() -> Iterator[tuple[TestClient, FakeRunStore, FakeSubmitter]]:
    store = FakeRunStore()
    submitter = FakeSubmitter(store)
    app = create_app(
        backtest_submission=submitter,
        run_store=store,
        event_backtest_codes_probe=lambda: RUNTIME_EVENT_CODES,
    )
    with TestClient(app) as client:
        yield client, store, submitter


def test_refresh_is_not_silently_ignored_by_snapshot_backtest(
    configured_api: tuple[TestClient, FakeRunStore, FakeSubmitter],
    strategy_payload: dict[str, Any],
) -> None:
    client, _store, submitter = configured_api
    response = client.post("/api/v1/backtest-runs", json={
        "strategy": strategy_payload, "config": {"refreshData": True},
    })
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "data_refresh_unavailable"
    assert submitter.configs == []


def test_unconfigured_backtest_routes_exist_but_return_503(
    client: TestClient,
    strategy_payload: dict[str, Any],
) -> None:
    create_response = client.post(
        "/api/v1/backtest-runs",
        json={"strategy": strategy_payload},
    )
    status_response = client.get("/api/v1/backtest-runs/run:not-configured")

    _assert_error(create_response, status_code=503, code="backtest_service_unavailable")
    _assert_error(status_response, status_code=503, code="backtest_service_unavailable")
    assert client.get("/api/v1/capabilities").json()["backtest_execution_available"] is False


def test_app_rejects_half_configured_backtest_runtime() -> None:
    store = FakeRunStore()
    with pytest.raises(ValueError, match="configured together"):
        create_app(backtest_submission=FakeSubmitter(store))
    with pytest.raises(ValueError, match="configured together"):
        create_app(run_store=store)


def test_event_submission_uses_stable_422_when_runtime_data_is_unavailable(
    event_strategy_payload: dict[str, Any],
) -> None:
    store = FakeRunStore()
    submitter = FakeSubmitter(store)
    app = create_app(
        backtest_submission=submitter,
        run_store=store,
        event_backtest_probe=lambda: False,
    )

    with TestClient(app) as disabled_client:
        capability = disabled_client.get("/api/v1/capabilities")
        response = disabled_client.post(
            "/api/v1/backtest-runs",
            json={"strategy": event_strategy_payload},
        )

    assert capability.status_code == 200
    assert capability.json()["event_backtest_available"] is False
    _assert_error(response, status_code=422, code="event_data_unavailable")
    assert "events.parquet" in response.json()["error"]["message"]
    assert submitter.configs == []


def test_event_submission_accepts_explicit_request_preparation_capability(
    event_strategy_payload: dict[str, Any],
) -> None:
    store = FakeRunStore()
    submitter = FakeSubmitter(store)
    forecast = "event.financial_results.earnings_forecast_published"
    app = create_app(
        backtest_submission=submitter,
        run_store=store,
        event_backtest_codes_probe=frozenset,
        event_preparable_codes_probe=lambda: frozenset({forecast}),
    )

    with TestClient(app) as on_demand_client:
        capability = on_demand_client.get("/api/v1/capabilities").json()
        response = on_demand_client.post(
            "/api/v1/backtest-runs",
            json={"strategy": event_strategy_payload},
        )

    assert capability["event_backtest_available"] is False
    assert capability["event_preparation_available"] is True
    assert response.status_code == 202
    assert len(submitter.configs) == 1


def test_submit_is_async_shaped_and_fingerprint_idempotent(
    configured_api: tuple[TestClient, FakeRunStore, FakeSubmitter],
    strategy_payload: dict[str, Any],
) -> None:
    client, _store, submitter = configured_api
    request = {
        "strategy": strategy_payload,
        "config": {
            "capacityMode": "unlimited",
            "slippageBps": "8",
            "allocationRatio": "0.9",
            "edgeEntryValiditySessions": 4,
            "eventEntryValiditySessions": 1,
            "limitHandling": "wait_for_unlock",
            "stateEntryValiditySessions": 1,
        },
    }
    headers = {"Idempotency-Key": "h5-backtest-001"}

    first = client.post("/api/v1/backtest-runs", json=request, headers=headers)
    replay = client.post("/api/v1/backtest-runs", json=request, headers=headers)

    assert first.status_code == replay.status_code == 202
    assert first.json()["state"] == "queued"
    assert first.json()["progress"] == 0
    assert first.json()["progressLabel"] == "queued"
    assert first.json()["resultAvailable"] is False
    assert first.json()["replayed"] is False
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["replayed"] is True
    assert first.headers["Idempotency-Replayed"] == "false"
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert submitter.configs[-1].slippage_bps == Decimal("8")
    assert submitter.configs[-1].allocation_ratio == Decimal("0.9")
    assert submitter.configs[-1].capacity_mode is CapacityMode.UNLIMITED
    assert submitter.configs[-1].edge_entry_validity_sessions == 4
    assert submitter.configs[-1].event_entry_validity_sessions == 1
    assert submitter.configs[-1].state_entry_validity_sessions == 1
    assert client.get("/api/v1/capabilities").json()["backtest_execution_available"] is True
    assert client.get("/api/v1/capabilities").json()["event_backtest_available"] is True
    event_capabilities = {
        item["event_code"]: item for item in client.get("/api/v1/capabilities").json()["events"]
    }
    assert {
        code for code, item in event_capabilities.items() if item["backtest_available"]
    } == RUNTIME_EVENT_CODES
    assert (
        event_capabilities["event.financial_results.earnings_forecast_published"]["status"]
        == "available"
    )
    assert (
        event_capabilities["event.contracts_orders.major_contract_won"]["status"] == "unavailable"
    )


def test_invalid_edited_year_is_rejected_before_queueing(
    configured_api: tuple[TestClient, FakeRunStore, FakeSubmitter],
    strategy_payload: dict[str, Any],
) -> None:
    client, _store, submitter = configured_api
    strategy_payload["backtest"].update(start="0686-09-05", end="2026-09-05")
    response = client.post("/api/v1/backtest-runs", json={"strategy": strategy_payload})
    _assert_error(response, status_code=422, code="backtest_date_range_invalid")
    assert "1990" in response.json()["error"]["message"]
    assert submitter.configs == []


def test_valid_long_range_and_zero_slippage_are_not_rewritten(
    configured_api: tuple[TestClient, FakeRunStore, FakeSubmitter],
    strategy_payload: dict[str, Any],
) -> None:
    client, store, submitter = configured_api
    strategy_payload["backtest"].update(start="2010-09-05", end="2026-09-05")
    response = client.post("/api/v1/backtest-runs", json={
        "strategy": strategy_payload, "config": {"slippageBps": "0"},
    })
    assert response.status_code == 202
    assert submitter.configs[-1].slippage_bps == Decimal("0")
    record = store.get(RunId(response.json()["id"]))
    assert record is not None
    assert json.loads(record.strategy_json)["backtest"] == strategy_payload["backtest"]


def test_financial_draft_strategy_must_be_submitted_without_rebuilding_execution(
    configured_api: tuple[TestClient, FakeRunStore, FakeSubmitter],
) -> None:
    client, _store, submitter = configured_api
    compiled = client.post(
        "/api/v1/strategy-drafts",
        json={
            "utterance": "东方财富市盈率低于20倍买入，MACD死叉卖出，回测近1年",
            "instrument_context": "300059.SZ",
            "as_of_date": "2026-08-06",
        },
    )

    assert compiled.status_code == 201
    draft = compiled.json()
    assert draft["status"] == "ready"
    strategy = draft["strategy"]
    assert strategy["entry"]["metric_id"] == "valuation.pe"
    assert strategy["execution"] == {
        "timezone": "Asia/Shanghai",
        "entry_policy": "next_tradable_session_open",
        "exit_policy": "next_tradable_session_open",
        "data_capability": "daily_ohlcv_financials",
        "execution_resolution": "1d",
        "evaluation_frequency": "financial_available_plus_1d_close",
        "position_policy": "single_position_no_pyramiding",
        "t_plus_one": True,
    }

    accepted = client.post(
        "/api/v1/backtest-runs",
        json={"strategy": strategy, "config": {"runRobustness": False}},
    )
    assert accepted.status_code == 202
    assert len(submitter.configs) == 1

    rebuilt = deepcopy(strategy)
    rebuilt["execution"]["evaluation_frequency"] = "1d_close"
    rejected = client.post(
        "/api/v1/backtest-runs",
        json={"strategy": rebuilt, "config": {"runRobustness": False}},
    )
    _assert_error(rejected, status_code=422, code="request_validation_failed")
    assert len(submitter.configs) == 1


def test_completed_submission_replay_revalidates_result_integrity(
    configured_api: tuple[TestClient, FakeRunStore, FakeSubmitter],
    strategy_payload: dict[str, Any],
) -> None:
    client, store, _submitter = configured_api
    request = {"strategy": strategy_payload}
    first = client.post("/api/v1/backtest-runs", json=request)
    run_id = first.json()["id"]
    payload: dict[str, Any] = json.loads(result_bundle_json(run_id))
    payload["summary"]["interpretation"] = "tampered replay"
    store.transition(
        RunId(run_id),
        expected=(BacktestJobState.QUEUED,),
        target=BacktestJobState.SUCCEEDED,
        progress_percent=100,
        progress_label="succeeded",
        result_json=json.dumps(payload, ensure_ascii=False),
    )

    replay = client.post("/api/v1/backtest-runs", json=request)

    _assert_error(
        replay,
        status_code=500,
        code="backtest_result_integrity_mismatch",
    )


def test_submit_rejects_multi_session_state_signal_validity(
    configured_api: tuple[TestClient, FakeRunStore, FakeSubmitter],
    strategy_payload: dict[str, Any],
) -> None:
    client, _store, submitter = configured_api

    response = client.post(
        "/api/v1/backtest-runs",
        json={
            "strategy": strategy_payload,
            "config": {"stateEntryValiditySessions": 2},
        },
    )

    assert response.status_code == 422
    assert submitter.configs == []


def test_event_submission_rejects_catalog_code_outside_pinned_snapshot_coverage(
    configured_api: tuple[TestClient, FakeRunStore, FakeSubmitter],
    event_strategy_payload: dict[str, Any],
) -> None:
    client, _store, submitter = configured_api
    outside_coverage = deepcopy(event_strategy_payload)
    outside_coverage["entry"]["event_code"] = "event.contracts_orders.major_contract_won"

    response = client.post("/api/v1/backtest-runs", json={"strategy": outside_coverage})

    _assert_error(response, status_code=422, code="event_data_unavailable")
    assert submitter.configs == []


def test_execution_capacity_mode_defaults_to_point_in_time_volume(
    configured_api: tuple[TestClient, FakeRunStore, FakeSubmitter],
    strategy_payload: dict[str, Any],
) -> None:
    client, _store, submitter = configured_api

    response = client.post("/api/v1/backtest-runs", json={"strategy": strategy_payload})

    assert response.status_code == 202
    assert submitter.configs[-1].capacity_mode is CapacityMode.POINT_IN_TIME_VOLUME
    assert submitter.configs[-1].edge_entry_validity_sessions == 3
    assert submitter.configs[-1].event_entry_validity_sessions == 1
    assert submitter.configs[-1].state_entry_validity_sessions == 1


@pytest.mark.parametrize(
    "state",
    [
        BacktestJobState.QUEUED,
        BacktestJobState.RUNNING_DATA,
        BacktestJobState.RUNNING_SIGNAL,
        BacktestJobState.RUNNING_EXECUTION,
        BacktestJobState.RUNNING_REPORT,
        BacktestJobState.CANCEL_REQUESTED,
        BacktestJobState.SUCCEEDED,
        BacktestJobState.FAILED,
        BacktestJobState.CANCELLED,
    ],
)
def test_status_contract_covers_every_worker_state(
    configured_api: tuple[TestClient, FakeRunStore, FakeSubmitter],
    state: BacktestJobState,
) -> None:
    client, store, _submitter = configured_api
    run_id = f"run:state:{state.value.replace(':', '-')}"
    result_json = result_bundle_json(run_id) if state is BacktestJobState.SUCCEEDED else None
    expected_result_hash = (
        json.loads(result_json)["audit"]["resultHash"] if result_json is not None else None
    )
    store.seed(make_record(run_id, state=state, result_json=result_json))

    response = client.get(f"/api/v1/backtest-runs/{run_id}")
    payload: dict[str, Any] = response.json()

    assert response.status_code == 200
    assert payload["id"] == run_id
    assert payload["state"] == state.value
    assert payload["progressLabel"] == state.value
    assert payload["resultAvailable"] is (state is BacktestJobState.SUCCEEDED)
    assert payload["resultHash"] == expected_result_hash
    assert payload["error"] == ("worker_failed" if state is BacktestJobState.FAILED else None)
    assert payload["createdAt"].endswith("Z")
    assert payload["updatedAt"].endswith("Z")


def test_cancel_is_idempotent_and_terminal_success_conflicts(
    configured_api: tuple[TestClient, FakeRunStore, FakeSubmitter],
) -> None:
    client, store, _submitter = configured_api
    queued_id = "run:cancel:queued"
    success_id = "run:cancel:succeeded"
    store.seed(make_record(queued_id, state=BacktestJobState.QUEUED))
    store.seed(
        make_record(
            success_id,
            state=BacktestJobState.SUCCEEDED,
            fingerprint="sha256:" + "b" * 64,
            result_json=result_bundle_json(success_id),
        )
    )

    first = client.post(f"/api/v1/backtest-runs/{queued_id}/cancel")
    replay = client.post(f"/api/v1/backtest-runs/{queued_id}/cancel")
    terminal = client.post(f"/api/v1/backtest-runs/{success_id}/cancel")

    assert first.status_code == replay.status_code == 202
    assert first.json()["state"] == "cancel_requested"
    assert first.json()["cancellationRequested"] is True
    assert replay.json() == first.json()
    _assert_error(terminal, status_code=409, code="backtest_run_not_cancellable")


def test_completed_result_bundle_is_split_without_fabricating_nullable_metrics(
    configured_api: tuple[TestClient, FakeRunStore, FakeSubmitter],
) -> None:
    client, store, _submitter = configured_api
    run_id = "run:result:ready"
    store.seed(
        make_record(
            run_id,
            state=BacktestJobState.SUCCEEDED,
            result_json=result_bundle_json(run_id),
        )
    )

    summary = client.get(f"/api/v1/backtest-runs/{run_id}/summary")
    series = client.get(f"/api/v1/backtest-runs/{run_id}/series")
    activities = client.get(f"/api/v1/backtest-runs/{run_id}/trades")

    assert summary.status_code == series.status_code == activities.status_code == 200
    assert summary.json()["runId"] == run_id
    assert summary.json()["benchmarkReturn"] is None
    assert summary.json()["benchmarkComparisonStatus"] == "benchmark_unavailable"
    assert summary.json()["annualizedReturn"] is None
    assert summary.json()["sharpeRatio"] is None
    assert summary.json()["winRate"] is None
    assert summary.json()["initialCashCny"] is None
    assert [point["date"] for point in series.json()] == ["2025-01-02", "2025-01-03"]
    assert [item["kind"] for item in activities.json()] == ["signal", "fill"]
    assert activities.json()[1]["price"] == 10.2


@pytest.mark.parametrize("suffix", ["", "/summary", "/series", "/trades"])
@pytest.mark.parametrize(
    ("section", "item_index", "field", "changed_value"),
    [
        ("summary", None, "interpretation", "篡改后的结果说明"),
        ("series", 1, "equity", 999.0),
        ("activities", 0, "reason", "篡改后的信号原因"),
    ],
)
def test_result_reads_fail_closed_when_persisted_bundle_is_tampered(
    configured_api: tuple[TestClient, FakeRunStore, FakeSubmitter],
    suffix: str,
    section: str,
    item_index: int | None,
    field: str,
    changed_value: object,
) -> None:
    client, store, _submitter = configured_api
    run_id = f"run:result:tampered:{section}"
    payload: dict[str, Any] = json.loads(result_bundle_json(run_id))
    target: Any = payload[section]
    if item_index is not None:
        target = target[item_index]
    target[field] = changed_value
    store.seed(
        make_record(
            run_id,
            state=BacktestJobState.SUCCEEDED,
            result_json=json.dumps(payload, ensure_ascii=False),
        )
    )

    response = client.get(f"/api/v1/backtest-runs/{run_id}{suffix}")

    _assert_error(
        response,
        status_code=500,
        code="backtest_result_integrity_mismatch",
    )


@pytest.mark.parametrize("suffix", ["", "/summary", "/series", "/trades"])
def test_legacy_bundle_with_result_hash_fails_closed(
    configured_api: tuple[TestClient, FakeRunStore, FakeSubmitter],
    suffix: str,
) -> None:
    client, store, _submitter = configured_api
    run_id = "run:result:legacy-hash"
    payload: dict[str, Any] = json.loads(result_bundle_json(run_id))
    payload["audit"].pop("hashSchemaVersion")
    payload["audit"].pop("engineResultHash")
    payload["audit"]["resultHash"] = "sha256:" + "d" * 64
    store.seed(
        make_record(
            run_id,
            state=BacktestJobState.SUCCEEDED,
            result_json=json.dumps(payload, ensure_ascii=False),
            result_integrity_policy=BacktestResultIntegrityPolicy.LEGACY_UNVERIFIED,
        )
    )

    response = client.get(f"/api/v1/backtest-runs/{run_id}{suffix}")

    _assert_error(
        response,
        status_code=500,
        code="backtest_result_integrity_mismatch",
    )


def test_legacy_bundle_without_result_hash_is_readable_as_unverified(
    configured_api: tuple[TestClient, FakeRunStore, FakeSubmitter],
) -> None:
    client, store, _submitter = configured_api
    run_id = "run:result:legacy-unverified"
    payload: dict[str, Any] = json.loads(result_bundle_json(run_id))
    payload["audit"].pop("hashSchemaVersion")
    payload["audit"].pop("engineResultHash")
    payload["audit"].pop("resultHash")
    store.seed(
        make_record(
            run_id,
            state=BacktestJobState.SUCCEEDED,
            result_json=json.dumps(payload, ensure_ascii=False),
            result_integrity_policy=BacktestResultIntegrityPolicy.LEGACY_UNVERIFIED,
        )
    )

    status_response = client.get(f"/api/v1/backtest-runs/{run_id}")
    summary_response = client.get(f"/api/v1/backtest-runs/{run_id}/summary")
    series_response = client.get(f"/api/v1/backtest-runs/{run_id}/series")
    trades_response = client.get(f"/api/v1/backtest-runs/{run_id}/trades")

    assert (
        status_response.status_code
        == summary_response.status_code
        == series_response.status_code
        == trades_response.status_code
        == 200
    )
    assert status_response.json()["resultHash"] is None
    assert status_response.json()["resultAvailable"] is True
    assert summary_response.json()["runId"] == run_id
    assert summary_response.json()["runEvidence"] is None
    assert len(series_response.json()) == 2
    assert len(trades_response.json()) == 2


@pytest.mark.parametrize("suffix", ["", "/summary", "/series", "/trades"])
def test_modern_run_cannot_be_downgraded_by_deleting_hash_identity(
    configured_api: tuple[TestClient, FakeRunStore, FakeSubmitter],
    suffix: str,
) -> None:
    client, store, _submitter = configured_api
    run_id = "run:result:modern-downgrade-attempt"
    payload: dict[str, Any] = json.loads(result_bundle_json(run_id))
    payload["audit"].pop("hashSchemaVersion")
    payload["audit"].pop("resultHash")
    store.seed(
        make_record(
            run_id,
            state=BacktestJobState.SUCCEEDED,
            result_json=json.dumps(payload, ensure_ascii=False),
        )
    )

    response = client.get(f"/api/v1/backtest-runs/{run_id}{suffix}")

    _assert_error(
        response,
        status_code=500,
        code="backtest_result_integrity_mismatch",
    )


@pytest.mark.parametrize("suffix", ["", "/summary", "/series", "/trades"])
@pytest.mark.parametrize(
    ("audit_change", "value"),
    [
        ("missing_result_hash", None),
        ("unsupported_hash_schema", "ashare-lab.backtest-result-hash.v999"),
    ],
)
def test_modern_bundle_requires_supported_recomputable_hash_evidence(
    configured_api: tuple[TestClient, FakeRunStore, FakeSubmitter],
    suffix: str,
    audit_change: str,
    value: object,
) -> None:
    client, store, _submitter = configured_api
    run_id = f"run:result:modern-hash:{audit_change}"
    payload: dict[str, Any] = json.loads(result_bundle_json(run_id))
    if audit_change == "missing_result_hash":
        payload["audit"].pop("resultHash")
    else:
        payload["audit"]["hashSchemaVersion"] = value
    store.seed(
        make_record(
            run_id,
            state=BacktestJobState.SUCCEEDED,
            result_json=json.dumps(payload, ensure_ascii=False),
        )
    )

    response = client.get(f"/api/v1/backtest-runs/{run_id}{suffix}")

    _assert_error(
        response,
        status_code=500,
        code="backtest_result_integrity_mismatch",
    )


@pytest.mark.parametrize("suffix", ["summary", "series", "trades"])
def test_result_read_before_success_returns_409(
    configured_api: tuple[TestClient, FakeRunStore, FakeSubmitter],
    suffix: str,
) -> None:
    client, store, _submitter = configured_api
    run_id = "run:result:running"
    store.seed(make_record(run_id, state=BacktestJobState.RUNNING_EXECUTION))

    response = client.get(f"/api/v1/backtest-runs/{run_id}/{suffix}")

    _assert_error(response, status_code=409, code="backtest_result_not_ready")


def test_missing_and_corrupt_results_use_stable_errors(
    configured_api: tuple[TestClient, FakeRunStore, FakeSubmitter],
) -> None:
    client, store, _submitter = configured_api
    corrupt_id = "run:result:corrupt"
    store.seed(
        make_record(
            corrupt_id,
            state=BacktestJobState.SUCCEEDED,
            result_json='{"unexpected":true}',
        )
    )

    missing = client.get("/api/v1/backtest-runs/run:missing")
    corrupt = client.get(f"/api/v1/backtest-runs/{corrupt_id}/summary")

    _assert_error(missing, status_code=404, code="backtest_run_not_found")
    _assert_error(corrupt, status_code=500, code="backtest_result_invalid")


def test_execution_config_is_bounded_at_the_http_edge(
    configured_api: tuple[TestClient, FakeRunStore, FakeSubmitter],
    strategy_payload: dict[str, Any],
) -> None:
    client, _store, _submitter = configured_api
    response = client.post(
        "/api/v1/backtest-runs",
        json={
            "strategy": strategy_payload,
            "config": {"participationRate": 0, "maxExitAttempts": 0},
        },
    )

    _assert_error(response, status_code=422, code="request_validation_failed")


def test_submit_rejects_a_strategy_that_bypasses_the_published_catalog(
    configured_api: tuple[TestClient, FakeRunStore, FakeSubmitter],
    strategy_payload: dict[str, Any],
) -> None:
    client, _store, submitter = configured_api
    invalid_strategy = deepcopy(strategy_payload)
    invalid_strategy["entry"]["children"][0]["indicator_id"] = "technical.unknown"

    response = client.post(
        "/api/v1/backtest-runs",
        json={"strategy": invalid_strategy},
    )

    _assert_error(response, status_code=422, code="backtest_submission_invalid")
    assert submitter.configs == []


def test_submit_rejects_a_share_code_suffix_mismatch(
    configured_api: tuple[TestClient, FakeRunStore, FakeSubmitter],
    strategy_payload: dict[str, Any],
) -> None:
    client, _store, submitter = configured_api
    invalid_strategy = deepcopy(strategy_payload)
    invalid_strategy["instrument"]["symbol"] = "300059.SH"

    response = client.post(
        "/api/v1/backtest-runs",
        json={"strategy": invalid_strategy},
    )

    _assert_error(response, status_code=422, code="backtest_submission_invalid")
    assert submitter.configs == []


def _assert_error(response: Any, *, status_code: int, code: str) -> None:
    assert response.status_code == status_code
    payload: dict[str, Any] = response.json()
    assert set(payload) == {"error", "request_id"}
    assert payload["error"]["code"] == code
    assert response.headers["X-Request-ID"] == payload["request_id"]
