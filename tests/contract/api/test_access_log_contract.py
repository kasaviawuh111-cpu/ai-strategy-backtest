from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from ashare_lab.api.app import create_app


def test_success_access_log_contains_response_request_id_without_query_or_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app()

    with (
        caplog.at_level(logging.INFO, logger="uvicorn.error"),
        TestClient(app) as client,
    ):
        response = client.get(
            "/api/v1/health?secret=must-not-be-logged",
            headers={"X-Request-ID": "browser-run:technical"},
        )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "browser-run:technical"
    record = next(item for item in caplog.records if item.name == "uvicorn.error")
    rendered = record.getMessage()
    assert "request_id=browser-run:technical" in rendered
    assert "method=GET" in rendered
    assert "path=/api/v1/health" in rendered
    assert "status=200" in rendered
    assert "duration_ms=" in rendered
    assert "must-not-be-logged" not in rendered
    assert "secret=" not in rendered


def test_rejected_request_is_also_correlated_in_access_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app(max_body_bytes=8)

    with (
        caplog.at_level(logging.INFO, logger="uvicorn.error"),
        TestClient(app) as client,
    ):
        response = client.post(
            "/api/v1/strategy-drafts",
            content=b"sensitive-body",
            headers={
                "Content-Type": "application/json",
                "X-Request-ID": "browser-run:rejected",
            },
        )

    assert response.status_code == 413
    rendered = "\n".join(
        item.getMessage() for item in caplog.records if item.name == "uvicorn.error"
    )
    assert "request_id=browser-run:rejected" in rendered
    assert "status=413" in rendered
    assert "sensitive-body" not in rendered
