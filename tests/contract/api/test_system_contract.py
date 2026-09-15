# Starlette's TestClient currently exposes partially unknown httpx method types to Pyright.
# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false

from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from ashare_lab.adapters.market_data.mx_daily_history import MxDailyHistoryClient
from ashare_lab.adapters.market_data.mx_indicator_contract import (
    build_indicator_contract,
)
from ashare_lab.adapters.persistence.backtest_runs import InMemoryBacktestRunStore
from ashare_lab.api import create_app
from ashare_lab.application.skill_backtest_service import SkillBacktestService
from ashare_lab.domain.catalog import (
    load_catalog_directory,
    load_coverage_catalog_directory,
)
from ashare_lab.domain.events import EXECUTABLE_EVENT_DEFINITIONS
from ashare_lab.domain.signals.runtime import SignalRuntimeError
from ashare_lab.ports.provider_indicator_data import HistoricalIndicatorData

from .backtest_fakes import FakeRunStore, FakeSubmitter

ROOT = Path(__file__).parents[3]


def test_skill_capabilities_publish_routes_without_relaxing_provider_contract() -> None:
    store = InMemoryBacktestRunStore()
    service = SkillBacktestService(
        history=cast(MxDailyHistoryClient, object()),
        indicators=cast(HistoricalIndicatorData, object()), store=store,
    )
    try:
        with TestClient(create_app(backtest_submission=service, run_store=store)) as client:
            response = client.get("/api/v1/capabilities")
        assert response.status_code == 200
        payload = response.json()
        indicators = {item["indicator_id"]: item for item in payload["indicators"]}
        assert payload["backtest_execution_available"]
        assert len(indicators) == 37
        assert all(item["status"] == "stable" for item in indicators.values())
        assert sum(item["data_source"] == "skill_ohlcv_python"
                   for item in indicators.values()) == 30
        assert sum(item["data_source"] == "provider_indicator"
                   for item in indicators.values()) == 7
        for indicator_id, route in service.indicator_routes.items():
            assert indicators[indicator_id]["data_source"] == route.source
            assert indicators[indicator_id]["formula_summary"] == route.formula_summary
        assert indicators["technical.macd"]["status"] == "stable"
        assert "尚未通过验证" not in indicators["technical.macd"]["description"]
        assert indicators["technical.bollinger"]["data_source"] == "skill_ohlcv_python"
        assert indicators["technical.rsi"]["data_source"] == "provider_indicator"
        # This direct-caller fixture exposes the legacy formula profile. Fresh
        # provider discovery is allowed, but still needs actual parameter proof.
        contract = build_indicator_contract(
            "technical.macd", "MACD(12,26,9)", ("DIF值", "DEA值"),
            condition_params={"fast": 12, "slow": 26, "signal": 9},
        )
        field = {"returnName": "MACD(DIF值)", "returnSourceCode": "MACD_DIF",
                 "fixedParamValue": "N1=12,N2=26,M=9,AdjustFlag=2,Period=1"}
        assert contract.bind_field("DIF值", field)
        assert not contract.bind_field("DIF值", {
            **field, "fixedParamValue": "N1=12,N2=26,M=9,AdjustFlag=3,Period=1",
        })
    finally:
        service.shutdown()


def test_health_echoes_safe_request_id(client: TestClient) -> None:
    response = client.get("/api/v1/health", headers={"X-Request-ID": "mobile-req:42"})

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["X-Request-ID"] == "mobile-req:42"


def test_readiness_is_stricter_than_liveness(client: TestClient) -> None:
    unavailable = client.get("/api/v1/ready")
    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "service_not_ready"

    app = create_app(
        readiness_probe=lambda: {
            "daily_data": True,
            "event_data": True,
            "job_queue": True,
        }
    )
    with TestClient(app) as ready_client:
        ready = ready_client.get("/api/v1/ready")

    assert ready.status_code == 200
    assert ready.json() == {
        "status": "ready",
        "checks": {"daily_data": "ok", "event_data": "ok", "job_queue": "ok"},
    }


def test_version_pins_catalog_release(client: TestClient) -> None:
    payload: dict[str, Any] = client.get("/api/v1/version").json()

    assert payload["service"] == "ashare-strategy-api"
    assert payload["service_version"] == "test-version"
    assert payload["api_version"] == "v1"
    assert payload["catalog_snapshot_hash"].startswith("sha256:")
    assert payload["catalog_releases"] == [
        {
            "catalog_id": "cn_a.signals",
            "release_version": "2026.09.01",
            "content_hash": payload["catalog_releases"][0]["content_hash"],
        }
    ]


def test_capabilities_are_machine_readable_and_do_not_claim_backtest_execution(
    client: TestClient,
) -> None:
    response = client.get("/api/v1/capabilities")
    payload: dict[str, Any] = response.json()

    assert response.status_code == 200
    assert payload["markets"] == ["CN_A"]
    assert payload["strategy_scopes"] == ["single_instrument", "long_only"]
    assert payload["backtest_execution_available"] is False
    assert payload["event_backtest_available"] is False
    assert payload["event_preparation_available"] is False
    assert payload["event_availability_scope"] == "unavailable"
    assert payload["event_catalog_status"] == "published"
    assert set(payload) == {
        "available_data_end",
        "markets",
        "input_modes",
        "strategy_scopes",
        "indicators",
        "events",
        "execution_policies",
        "event_catalog_status",
        "event_backtest_available",
        "event_preparation_available",
        "event_availability_scope",
        "backtest_execution_available",
        "skill_data_discovery",
        "limits",
    }
    event_codes = {item["event_code"] for item in payload["events"]}
    assert event_codes == set(EXECUTABLE_EVENT_DEFINITIONS)
    assert len(event_codes) == 69
    assert {
        "event.dividends_corporate_actions.cash_dividend_proposal",
        "event.repurchase_capital.repurchase_first_execution",
        "event.restricted_shares_pledges.restricted_shares_unlock",
        "event.regulation_risk.administrative_penalty",
        "event.litigation_credit.debt_default",
    } <= event_codes
    assert all(item["catalog_status"] == "stable" for item in payload["events"])
    assert all(item["status"] == "unavailable" for item in payload["events"])
    assert all(item["backtest_available"] is False for item in payload["events"])
    assert all(item["preparation_available"] is False for item in payload["events"])
    assert all(item["availability_scope"] == "unavailable" for item in payload["events"])
    assert all(
        item["unavailable_reason"] == "snapshot_coverage_unavailable" for item in payload["events"]
    )
    assert {item["indicator_id"] for item in payload["indicators"]} == {
        "amount.average",
        "market.amount",
        "market.turnover_rate",
        "market.volume",
        "price.amplitude",
        "price.close",
        "price.consecutive_up",
        "price.return_pct",
        "price.rolling_high",
        "price.true_range",
        "technical.adx",
        "technical.atr",
        "technical.bbi",
        "technical.bias",
        "technical.bollinger",
        "technical.cci",
        "technical.ema",
        "technical.ema_bias",
        "technical.dmi",
        "technical.donchian",
        "technical.historical_volatility",
        "technical.kdj",
        "technical.ma",
        "technical.ma_cross",
        "technical.macd",
        "technical.momentum",
        "technical.natr",
        "technical.obv",
        "technical.rsi",
        "technical.roc",
        "technical.return_stddev",
        "technical.stochastic",
        "technical.trend_regime",
        "technical.williams_r",
        "volume.price_confirmation",
        "volume.price_divergence",
        "volume.relative",
    }


def test_all_stable_indicator_capabilities_match_both_active_catalogs(
    client: TestClient,
) -> None:
    catalog = load_catalog_directory(ROOT / "catalogs")
    coverage_catalog = load_coverage_catalog_directory(ROOT / "catalogs" / "coverage")
    executable = {item.id: item for item in catalog.indicators if item.status == "stable"}
    coverage = {item.id: item for item in coverage_catalog.metrics if item.status == "stable"}

    response = client.get("/api/v1/capabilities")
    indicators: list[dict[str, Any]] = response.json()["indicators"]

    assert response.status_code == 200
    assert len(indicators) == len(executable) == len(coverage) == 37
    assert set(executable) == set(coverage)
    assert [item["indicator_id"] for item in indicators] == sorted(executable)
    for item in indicators:
        definition = executable[item["indicator_id"]]
        metadata = coverage[item["indicator_id"]]
        assert item == {
            "indicator_id": definition.id,
            "definition_version": definition.version,
            "status": definition.status,
            "display_name": metadata.name_zh,
            "description": metadata.description,
            "data_source": None,
            "formula_summary": None,
            "warmup_bars": definition.warmup_bars,
            "timeframes": list(definition.timeframes),
            "evaluation_modes": list(definition.evaluation_modes),
            "triggers": [trigger.id for trigger in definition.triggers],
            "trigger_definitions": [
                {
                    "id": trigger.id,
                    "display_name": None,
                    "description": None,
                    "value_requirement": trigger.value_requirement,
                    "minimum": trigger.minimum,
                    "maximum": trigger.maximum,
                    "unit": None,
                    "exclusive_minimum": trigger.exclusive_minimum,
                    "exclusive_maximum": trigger.exclusive_maximum,
                }
                for trigger in definition.triggers
            ],
            "parameters": [
                {
                    "name": parameter.name,
                    "value_type": parameter.value_type,
                    "required": parameter.required,
                    "default": parameter.default,
                    "minimum": parameter.minimum,
                    "maximum": parameter.maximum,
                    "choices": list(parameter.choices),
                    "display_name": None,
                    "unit": None,
                }
                for parameter in definition.parameters
            ],
        }


def test_capabilities_fail_closed_when_stable_indicator_catalogs_drift() -> None:
    coverage_catalog = load_coverage_catalog_directory(ROOT / "catalogs" / "coverage")
    removed = next(item for item in coverage_catalog.metrics if item.status == "stable")
    drifted = coverage_catalog.model_copy(
        update={
            "metrics": tuple(item for item in coverage_catalog.metrics if item.id != removed.id)
        }
    )
    app = create_app(coverage_catalog=drifted)

    with TestClient(app) as drifted_client:
        response = drifted_client.get("/api/v1/capabilities")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "indicator_catalog_mismatch"


def test_app_startup_fails_when_stable_catalog_has_no_runtime_evaluator() -> None:
    catalog = load_catalog_directory(ROOT / "catalogs")
    removed = next(item for item in catalog.indicators if item.status == "stable")
    drifted = catalog.model_copy(
        update={"indicators": tuple(item for item in catalog.indicators if item.id != removed.id)}
    )

    with pytest.raises(SignalRuntimeError, match="Catalog/evaluator mismatch"):
        create_app(catalog=drifted)


def test_capabilities_distinguish_pinned_snapshot_from_request_preparation() -> None:
    annual = "event.financial_results.annual_report"
    quarterly = "event.financial_results.quarterly_report"

    pinned_store = FakeRunStore()
    pinned_app = create_app(
        backtest_submission=FakeSubmitter(pinned_store),
        run_store=pinned_store,
        event_backtest_codes_probe=lambda: frozenset({annual}),
    )
    with TestClient(pinned_app) as pinned_client:
        pinned = pinned_client.get("/api/v1/capabilities").json()

    pinned_by_code = {item["event_code"]: item for item in pinned["events"]}
    assert pinned["event_backtest_available"] is True
    assert pinned["event_preparation_available"] is False
    assert pinned["event_availability_scope"] == "pinned_snapshot"
    assert pinned_by_code[annual]["backtest_available"] is True
    assert pinned_by_code[annual]["availability_scope"] == "pinned_snapshot"

    on_demand_store = FakeRunStore()
    on_demand_app = create_app(
        backtest_submission=FakeSubmitter(on_demand_store),
        run_store=on_demand_store,
        event_backtest_codes_probe=frozenset,
        event_preparable_codes_probe=lambda: frozenset({annual, quarterly}),
    )
    with TestClient(on_demand_app) as on_demand_client:
        on_demand = on_demand_client.get("/api/v1/capabilities").json()

    on_demand_by_code = {item["event_code"]: item for item in on_demand["events"]}
    assert on_demand["event_backtest_available"] is False
    assert on_demand["event_preparation_available"] is True
    assert on_demand["event_availability_scope"] == "request_preparation"
    assert on_demand_by_code[annual] == {
        "event_code": annual,
        "definition_version": on_demand_by_code[annual]["definition_version"],
        "catalog_status": "stable",
        "status": "unavailable",
        "backtest_available": False,
        "preparation_available": True,
        "availability_scope": "request_preparation",
        "unavailable_reason": "preparation_required",
        "document_text": {
            "catalog_available": True,
            "backtest_available": False,
            "preparation_available": False,
            "availability_scope": "unavailable",
            "unavailable_reason": "snapshot_coverage_unavailable",
        },
        "triggers": ["published"],
    }
    assert on_demand_by_code[quarterly]["backtest_available"] is False


def test_openapi_exposes_compilation_and_async_backtest_vertical_slices(
    client: TestClient,
) -> None:
    document: dict[str, Any] = client.get("/api/v1/openapi.json").json()
    paths: dict[str, Any] = document["paths"]

    assert set(paths) == {
        "/api/v1/health",
        "/api/v1/ready",
        "/api/v1/version",
        "/api/v1/capabilities",
        "/api/v1/strategy-drafts",
        "/api/v1/strategy-drafts/{draft_id}/revisions",
        "/api/v1/strategy-drafts/{draft_id}/revisions/{revision}/clarification-answers",
        "/api/v1/backtest-runs",
        "/api/v1/backtest-runs/prepare",
        "/api/v1/backtest-runs/{run_id}",
        "/api/v1/backtest-runs/{run_id}/cancel",
        "/api/v1/backtest-runs/{run_id}/summary",
        "/api/v1/backtest-runs/{run_id}/series",
        "/api/v1/backtest-runs/{run_id}/trades",
        "/api/v1/backtest-runs/{run_id}/review",
        "/api/v1/market/instruments",
        "/api/v1/market/query",
        "/api/v1/market/screen",
        "/api/v1/market/screen-query",
        "/api/v1/market/series-discovery",
        "/api/v1/portfolio-reviews/analyze",
        "/api/v1/portfolio-reviews/import-contract",
        "/api/v1/portfolio-reviews/imports/parse",
        "/api/v1/portfolio-reviews/narrate-highlight",
        "/api/v2/strategy-drafts",
        "/api/v2/strategy-drafts/{draft_id}/revisions/{revision}",
        "/api/v2/strategy-validations",
        "/api/v2/backtest-runs",
        "/api/v2/backtest-runs/{run_id}",
    }
    assert paths["/api/v1/strategy-drafts"]["post"]["operationId"] == "createStrategyDraft"
    assert (
        paths["/api/v1/strategy-drafts/{draft_id}/revisions"]["post"]["operationId"]
        == "reviseStrategyDraft"
    )
    error_schema = document["components"]["schemas"]["ErrorEnvelope"]
    assert set(error_schema["required"]) == {"error", "request_id"}
    assert "413" in paths["/api/v1/strategy-drafts"]["post"]["responses"]
    assert paths["/api/v1/backtest-runs"]["post"]["operationId"] == "createBacktestRun"
    assert "503" in paths["/api/v1/backtest-runs"]["post"]["responses"]
    assert paths["/api/v2/strategy-drafts"]["post"]["operationId"] == ("createStrategyV2Draft")
    assert paths["/api/v2/strategy-validations"]["post"]["operationId"] == (
        "validateStrategyV2Draft"
    )
    assert paths["/api/v2/backtest-runs"]["post"]["operationId"] == ("createStrategyV2BacktestRun")
