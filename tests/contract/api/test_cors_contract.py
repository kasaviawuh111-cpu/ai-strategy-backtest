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
                "Access-Control-Request-Headers": "content-type,idempotency-key",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ("https://example.workbuddy.link")
    assert "POST" in response.headers["access-control-allow-methods"]
    assert "Idempotency-Key" in response.headers["access-control-allow-headers"]


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
