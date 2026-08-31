# Starlette's TestClient currently exposes partially unknown httpx method types to Pyright.
# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false

from typing import Any

from fastapi.testclient import TestClient


def test_strategy_draft_api_preserves_explicit_backtest_period(client: TestClient) -> None:
    response = client.post(
        "/api/v1/strategy-drafts",
        json={
            "utterance": ("东方财富 MACD 金叉买入，死叉卖出，回测 2021-08-06 至 2026-08-06"),
            "instrument_context": "300059.SZ",
            "as_of_date": "2026-08-29",
        },
    )

    assert response.status_code == 201
    payload: dict[str, Any] = response.json()
    assert payload["status"] == "ready"
    assert payload["strategy"]["backtest"] == {
        "start": "2021-08-06",
        "end": "2026-08-06",
        "initial_cash_cny": 1_000_000,
    }
    provenance = {item["path"]: item["source"] for item in payload["provenance"]}
    assert provenance["/backtest/start"] == "utterance/explicit_date_range"
    assert provenance["/backtest/end"] == "utterance/explicit_date_range"


def test_strategy_draft_api_rejects_incomplete_backtest_period(client: TestClient) -> None:
    response = client.post(
        "/api/v1/strategy-drafts",
        json={
            "utterance": "MACD金叉买入，死叉卖出，回测2021-08-06至",
            "instrument_context": "300059.SZ",
            "as_of_date": "2026-08-29",
        },
    )

    assert response.status_code == 201
    payload: dict[str, Any] = response.json()
    assert payload["status"] == "unsupported"
    assert payload["strategy"] is None
    assert payload["diagnostic_code"] == "backtest_date_range_incomplete"
