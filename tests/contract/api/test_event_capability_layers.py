# Starlette's TestClient currently exposes partially unknown httpx method types to Pyright.
# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from fastapi.testclient import TestClient

from ashare_lab.adapters.event_sources.eastmoney import eastmoney_preparable_event_codes
from ashare_lab.api import create_app
from ashare_lab.domain.events import EXECUTABLE_EVENT_DEFINITIONS

from .backtest_fakes import FakeRunStore, FakeSubmitter

ROOT = Path(__file__).parents[3]


def test_event_capabilities_keep_catalog_preparable_and_pinned_layers_separate() -> None:
    annual = "event.financial_results.annual_report"
    major_award = "event.contracts_orders.major_contract_won"
    license_approval = "event.macro_policy_industry.license_approval"
    preparable = eastmoney_preparable_event_codes()
    store = FakeRunStore()
    app = create_app(
        backtest_submission=FakeSubmitter(store),
        run_store=store,
        event_backtest_codes_probe=lambda: frozenset({annual}),
        event_preparable_codes_probe=eastmoney_preparable_event_codes,
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/capabilities")

    assert response.status_code == 200
    by_code = {item["event_code"]: item for item in response.json()["events"]}
    assert set(by_code) == set(EXECUTABLE_EVENT_DEFINITIONS)
    assert len(by_code) == 69
    assert {code for code, item in by_code.items() if item["preparation_available"]} == set(
        preparable
    )
    assert len(preparable) == 68

    # A pinned code is runnable now.  Its simultaneous preparability does not
    # replace or blur the stronger pinned-snapshot evidence layer.
    assert by_code[annual]["catalog_status"] == "stable"
    assert by_code[annual]["backtest_available"] is True
    assert by_code[annual]["preparation_available"] is True
    assert by_code[annual]["availability_scope"] == "pinned_snapshot"
    assert by_code[annual]["unavailable_reason"] is None

    # The issuer-announcement award classifier passed its explicit lifecycle
    # acceptance matrix, but there is no claim that this concrete request is
    # already present in the pinned snapshot.
    assert by_code[major_award]["backtest_available"] is False
    assert by_code[major_award]["preparation_available"] is True
    assert by_code[major_award]["availability_scope"] == "request_preparation"
    assert by_code[major_award]["unavailable_reason"] == "preparation_required"

    # The Catalog/DSL can name license approval, while the current public
    # announcement collector has no deterministic issuer-announcement proof.
    assert by_code[license_approval]["catalog_status"] == "stable"
    assert by_code[license_approval]["backtest_available"] is False
    assert by_code[license_approval]["preparation_available"] is False
    assert by_code[license_approval]["availability_scope"] == "unavailable"
    assert by_code[license_approval]["unavailable_reason"] == ("snapshot_coverage_unavailable")


def test_submission_accepts_preparable_award_but_rejects_compile_only_license() -> None:
    template = json.loads(
        (ROOT / "contracts/examples/strategy.event-forecast-macd.daily.v1.json").read_text(
            encoding="utf-8"
        )
    )
    major_award = deepcopy(template)
    major_award["entry"]["event_code"] = "event.contracts_orders.major_contract_won"
    license_approval = deepcopy(template)
    license_approval["entry"]["event_code"] = "event.macro_policy_industry.license_approval"
    store = FakeRunStore()
    submitter = FakeSubmitter(store)
    app = create_app(
        backtest_submission=submitter,
        run_store=store,
        event_backtest_codes_probe=frozenset,
        event_preparable_codes_probe=eastmoney_preparable_event_codes,
    )

    with TestClient(app) as client:
        accepted = client.post("/api/v1/backtest-runs", json={"strategy": major_award})
        rejected = client.post(
            "/api/v1/backtest-runs",
            json={"strategy": license_approval},
        )

    assert accepted.status_code == 202
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "event_data_unavailable"
    assert len(submitter.configs) == 1
