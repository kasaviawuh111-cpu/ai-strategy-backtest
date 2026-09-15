# Starlette's TestClient currently exposes partially unknown httpx method types to Pyright.
# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ashare_lab.adapters.market_data import MarketDataCapabilityError, SnapshotScopeError
from ashare_lab.adapters.market_data.on_demand_snapshot import (
    SnapshotPreparationDocumentTextIncompleteError,
    SnapshotPreparationError,
    SnapshotPreparationFailedError,
    SnapshotPreparationIncompleteError,
    SnapshotPreparationUnsupportedError,
)
from ashare_lab.api import create_app
from ashare_lab.application.backtest_submission import BacktestRunConfig
from ashare_lab.domain.strategy import StrategySpec
from ashare_lab.ports.backtest_runs import CreateRunResult

from .backtest_fakes import FakeRunStore

ROOT = Path(__file__).parents[3]


class RaisingSubmitter:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def submit(
        self,
        strategy: StrategySpec,
        config: BacktestRunConfig,
    ) -> CreateRunResult:
        del strategy, config
        self.calls += 1
        raise self.error


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
def document_text_strategy_payload(event_strategy_payload: dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(json.dumps(event_strategy_payload))
    payload["entry"] = {
        "type": "event_condition",
        "event_code": "event.financial_results.annual_report",
        "definition_version": "1.0.0",
        "trigger": "published",
        "attributes": {},
        "document_text": {
            "metric_id": "document.literal_mention_count",
            "metric_version": "1.0.0",
            "term": "AI",
            "normalization": "nfkc",
            "match_mode": "ascii_token",
            "case_sensitive": False,
            "comparator": "gt",
            "value": 5,
        },
    }
    return StrategySpec.model_validate(payload).model_dump(mode="json")


@pytest.mark.parametrize(
    "source_error",
    [
        SnapshotPreparationFailedError("provider token=secret network unavailable"),
        SnapshotPreparationIncompleteError("prepared path=/private/snapshot was incomplete"),
        SnapshotPreparationError("future stable preparation failure"),
    ],
)
def test_temporary_preparation_failures_use_stable_non_leaky_503(
    strategy_payload: dict[str, Any],
    source_error: SnapshotPreparationError,
) -> None:
    # Preparation failures deliberately inherit the generic market-data
    # capability error; route ordering must still preserve the retriable 503.
    assert isinstance(source_error, MarketDataCapabilityError)
    store = FakeRunStore()
    submitter = RaisingSubmitter(source_error)
    app = create_app(backtest_submission=submitter, run_store=store)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/backtest-runs",
            json={"strategy": strategy_payload},
        )

    _assert_error(
        response,
        status_code=503,
        code="backtest_data_temporarily_unavailable",
    )
    assert response.json()["error"]["message"] == (
        "本次历史数据暂未准备完成，请稍后重试；无需重新描述买卖规则。本次未启动回测。"
    )
    assert "secret" not in response.text
    assert "/private" not in response.text
    assert submitter.calls == 1
    assert store.records == {}


def test_unsupported_preparation_scope_uses_stable_422(
    strategy_payload: dict[str, Any],
) -> None:
    store = FakeRunStore()
    submitter = RaisingSubmitter(
        SnapshotPreparationUnsupportedError("Eastmoney cannot prove timestamps before 2017-01-01")
    )
    app = create_app(backtest_submission=submitter, run_store=store)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/backtest-runs",
            json={"strategy": strategy_payload},
        )

    _assert_error(
        response,
        status_code=422,
        code="backtest_data_request_unsupported",
    )
    assert response.json()["error"]["message"] == (
        "当前数据源不支持本次股票、回测区间或所需的数据能力。"
        "请核对股票和区间，或补齐对应数据；无需重新描述买卖规则。本次未启动回测。"
    )
    assert "Eastmoney" not in response.text
    assert submitter.calls == 1
    assert store.records == {}


@pytest.mark.parametrize(
    "source_error",
    [
        SnapshotScopeError("requested period is outside Choice snapshot coverage"),
        MarketDataCapabilityError("no pinned snapshot covers the requested instrument"),
    ],
)
def test_pinned_snapshot_scope_failures_use_stable_non_leaky_422(
    strategy_payload: dict[str, Any],
    source_error: Exception,
) -> None:
    store = FakeRunStore()
    submitter = RaisingSubmitter(source_error)
    app = create_app(backtest_submission=submitter, run_store=store)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/backtest-runs",
            json={"strategy": strategy_payload},
        )

    _assert_error(
        response,
        status_code=422,
        code="backtest_data_request_unsupported",
    )
    assert "Choice" not in response.text
    assert "pinned snapshot" not in response.text
    assert submitter.calls == 1
    assert store.records == {}


def test_missing_frozen_report_text_uses_a_dedicated_non_leaky_422(
    document_text_strategy_payload: dict[str, Any],
) -> None:
    store = FakeRunStore()
    submitter = RaisingSubmitter(
        MarketDataCapabilityError(
            "event document text is not frozen; source=/private/report.pdf token=secret"
        )
    )
    annual_report = "event.financial_results.annual_report"
    app = create_app(
        backtest_submission=submitter,
        run_store=store,
        event_preparable_codes_probe=lambda: frozenset({annual_report}),
        event_document_text_preparable_codes_probe=lambda: frozenset({annual_report}),
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/backtest-runs",
            json={"strategy": document_text_strategy_payload},
        )

    _assert_error(
        response,
        status_code=422,
        code="event_document_text_data_unavailable",
    )
    assert response.json()["error"]["message"] == (
        "本次回测缺少可核验的完整报告正文快照，无法计算正文条件。"
        "请补齐正文数据后重试；不会用标题代替正文，无需重新描述买卖规则。本次未启动回测。"
    )
    assert "/private" not in response.text
    assert "secret" not in response.text
    assert submitter.calls == 1
    assert store.records == {}


def test_document_text_capability_gap_is_rejected_before_submitter(
    document_text_strategy_payload: dict[str, Any],
) -> None:
    store = FakeRunStore()
    submitter = RaisingSubmitter(AssertionError("submitter must not be called"))
    annual_report = "event.financial_results.annual_report"
    app = create_app(
        backtest_submission=submitter,
        run_store=store,
        event_backtest_codes_probe=lambda: frozenset({annual_report}),
        event_document_text_backtest_codes_probe=frozenset,
        event_document_text_preparable_codes_probe=frozenset,
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/backtest-runs",
            json={"strategy": document_text_strategy_payload},
        )

    _assert_error(
        response,
        status_code=422,
        code="event_document_text_data_unavailable",
    )
    assert submitter.calls == 0
    assert store.records == {}


def test_document_text_incomplete_preparation_uses_dedicated_non_leaky_422(
    document_text_strategy_payload: dict[str, Any],
) -> None:
    source_error = SnapshotPreparationDocumentTextIncompleteError(
        "scanned report path=/private/report.pdf token=secret"
    )
    # This inheritance is why route ordering is part of the public contract.
    assert isinstance(source_error, SnapshotPreparationError)
    assert isinstance(source_error, MarketDataCapabilityError)
    store = FakeRunStore()
    submitter = RaisingSubmitter(source_error)
    annual_report = "event.financial_results.annual_report"
    app = create_app(
        backtest_submission=submitter,
        run_store=store,
        event_preparable_codes_probe=lambda: frozenset({annual_report}),
        event_document_text_preparable_codes_probe=lambda: frozenset({annual_report}),
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/backtest-runs",
            json={"strategy": document_text_strategy_payload},
        )

    _assert_error(
        response,
        status_code=422,
        code="event_document_text_data_unavailable",
    )
    assert "/private" not in response.text
    assert "secret" not in response.text
    assert submitter.calls == 1
    assert store.records == {}


def test_non_document_event_incomplete_preparation_remains_temporary_503(
    event_strategy_payload: dict[str, Any],
) -> None:
    store = FakeRunStore()
    submitter = RaisingSubmitter(
        SnapshotPreparationIncompleteError("event coverage was incomplete")
    )
    event_code = str(event_strategy_payload["entry"]["event_code"])
    app = create_app(
        backtest_submission=submitter,
        run_store=store,
        event_backtest_codes_probe=lambda: frozenset({event_code}),
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/backtest-runs",
            json={"strategy": event_strategy_payload},
        )

    _assert_error(
        response,
        status_code=503,
        code="backtest_data_temporarily_unavailable",
    )
    assert submitter.calls == 1
    assert store.records == {}


def test_document_text_provider_failure_remains_temporary_503(
    document_text_strategy_payload: dict[str, Any],
) -> None:
    store = FakeRunStore()
    submitter = RaisingSubmitter(
        SnapshotPreparationFailedError("provider token=secret network unavailable")
    )
    annual_report = "event.financial_results.annual_report"
    app = create_app(
        backtest_submission=submitter,
        run_store=store,
        event_preparable_codes_probe=lambda: frozenset({annual_report}),
        event_document_text_preparable_codes_probe=lambda: frozenset({annual_report}),
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/backtest-runs",
            json={"strategy": document_text_strategy_payload},
        )

    _assert_error(
        response,
        status_code=503,
        code="backtest_data_temporarily_unavailable",
    )
    assert "secret" not in response.text
    assert submitter.calls == 1
    assert store.records == {}


def test_document_text_generic_incomplete_without_quality_type_remains_503(
    document_text_strategy_payload: dict[str, Any],
) -> None:
    store = FakeRunStore()
    submitter = RaisingSubmitter(
        SnapshotPreparationIncompleteError("daily price coverage was incomplete")
    )
    annual_report = "event.financial_results.annual_report"
    app = create_app(
        backtest_submission=submitter,
        run_store=store,
        event_backtest_codes_probe=lambda: frozenset({annual_report}),
        event_document_text_backtest_codes_probe=lambda: frozenset({annual_report}),
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/backtest-runs",
            json={"strategy": document_text_strategy_payload},
        )

    _assert_error(
        response,
        status_code=503,
        code="backtest_data_temporarily_unavailable",
    )
    assert submitter.calls == 1
    assert store.records == {}


def _assert_error(response: Any, *, status_code: int, code: str) -> None:
    assert response.status_code == status_code
    payload: dict[str, Any] = response.json()
    assert set(payload) == {"error", "request_id"}
    assert payload["error"]["code"] == code
    assert payload["error"]["details"] == []
    assert response.headers["X-Request-ID"] == payload["request_id"]
