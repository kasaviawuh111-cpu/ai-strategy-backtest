# Starlette's TestClient currently exposes partially unknown httpx method types to Pyright.
# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false

from __future__ import annotations

from fastapi.testclient import TestClient

from ashare_lab.api import create_app

from .backtest_fakes import FakeRunStore, FakeSubmitter

ANNUAL_REPORT = "event.financial_results.annual_report"
MAJOR_CONTRACT = "event.contracts_orders.major_contract_won"


def test_document_text_capability_does_not_inherit_generic_event_availability() -> None:
    store = FakeRunStore()
    app = create_app(
        backtest_submission=FakeSubmitter(store),
        run_store=store,
        event_backtest_codes_probe=lambda: frozenset({ANNUAL_REPORT}),
        event_preparable_codes_probe=lambda: frozenset({ANNUAL_REPORT, MAJOR_CONTRACT}),
        event_document_text_backtest_codes_probe=frozenset,
        event_document_text_preparable_codes_probe=lambda: frozenset({ANNUAL_REPORT}),
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/capabilities")

    assert response.status_code == 200
    by_code = {item["event_code"]: item for item in response.json()["events"]}
    assert by_code[ANNUAL_REPORT]["backtest_available"] is True
    assert by_code[ANNUAL_REPORT]["availability_scope"] == "pinned_snapshot"
    assert by_code[ANNUAL_REPORT]["document_text"] == {
        "catalog_available": True,
        "backtest_available": False,
        "preparation_available": True,
        "availability_scope": "request_preparation",
        "unavailable_reason": "preparation_required",
    }
    assert by_code[MAJOR_CONTRACT]["preparation_available"] is True
    assert by_code[MAJOR_CONTRACT]["document_text"] == {
        "catalog_available": False,
        "backtest_available": False,
        "preparation_available": False,
        "availability_scope": "unavailable",
        "unavailable_reason": "not_catalog_available",
    }


def test_document_text_capability_requires_an_independently_proven_pinned_lane() -> None:
    store = FakeRunStore()
    app = create_app(
        backtest_submission=FakeSubmitter(store),
        run_store=store,
        event_backtest_codes_probe=lambda: frozenset({ANNUAL_REPORT}),
        event_document_text_backtest_codes_probe=lambda: frozenset({ANNUAL_REPORT}),
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/capabilities")

    assert response.status_code == 200
    annual = next(item for item in response.json()["events"] if item["event_code"] == ANNUAL_REPORT)
    assert annual["document_text"] == {
        "catalog_available": True,
        "backtest_available": True,
        "preparation_available": False,
        "availability_scope": "pinned_snapshot",
        "unavailable_reason": None,
    }


def test_document_text_catalog_publication_alone_is_not_data_readiness() -> None:
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/api/v1/capabilities")

    assert response.status_code == 200
    annual = next(item for item in response.json()["events"] if item["event_code"] == ANNUAL_REPORT)
    assert annual["document_text"] == {
        "catalog_available": True,
        "backtest_available": False,
        "preparation_available": False,
        "availability_scope": "unavailable",
        "unavailable_reason": "snapshot_coverage_unavailable",
    }
