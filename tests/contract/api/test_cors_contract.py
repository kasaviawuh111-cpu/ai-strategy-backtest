from __future__ import annotations

from fastapi.testclient import TestClient

from ashare_lab.api.app import create_app


def test_configured_origin_can_preflight_strategy_request() -> None:
    app = create_app(cors_allowed_origins=("https://example.workbuddy.link",))

    with TestClient(app) as client:
        response = client.options(
            "/api/v1/strategy-drafts",
            headers={
                "Origin": "https://example.workbuddy.link",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": (
                    "accept,content-type,idempotency-key,x-request-id"
                ),
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ("https://example.workbuddy.link")
    allowed_methods = {
        item.strip().upper() for item in response.headers["access-control-allow-methods"].split(",")
    }
    allowed_headers = {
        item.strip().lower() for item in response.headers["access-control-allow-headers"].split(",")
    }
    assert {"GET", "POST", "OPTIONS"} <= allowed_methods
    assert {"accept", "content-type", "idempotency-key", "x-request-id"} <= allowed_headers


def test_unconfigured_origin_is_not_authorized() -> None:
    app = create_app(cors_allowed_origins=("https://example.workbuddy.link",))

    with TestClient(app) as client:
        response = client.options(
            "/api/v1/strategy-drafts",
            headers={
                "Origin": "https://attacker.example",
                "Access-Control-Request-Method": "POST",
            },
        )

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers
