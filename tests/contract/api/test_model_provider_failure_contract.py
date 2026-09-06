# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false
"""Offline HTTP and persistence checks; no model or backtest is executed."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from ashare_lab.adapters.language import RuleBasedCandidateGenerator
from ashare_lab.adapters.language.vibe_candidates import (
    CandidateFailureKind,
    CandidateTransportError,
)
from ashare_lab.adapters.persistence.backtest_runs import create_backtest_run_engine
from ashare_lab.api import create_app
from ashare_lab.api.errors import install_exception_handlers
from ashare_lab.api.persistent_store import DIALOGUE_METADATA, SQLAlchemyDraftStore
from ashare_lab.api.schemas import StrategyDraftResponse
from ashare_lab.application.compile_strategy import StrategyCompiler
from ashare_lab.domain.catalog import load_catalog_directory
from ashare_lab.ports.candidate_generation import CandidateAst, CandidateProvenance, CompileInput
from ashare_lab.ports.clarification_dialogue import (
    ClarificationDialogueAssessment,
    ClarificationDialogueRequest,
)
from ashare_lab.ports.strategy_editing import StrategyEditRequest, StrategyEditResult

ROOT = Path(__file__).resolve().parents[3]
_PRIVATE_MARKERS = ("fixture-private-provider-body", "fixture-private-reasoning")
_INITIAL = {
    "utterance": "创20日新高买入，下穿20日均线卖出，回测近1年，本金10万元",
    "instrument_context": "300059.SZ",
    "as_of_date": "2026-09-05",
}


def _failure(
    kind: CandidateFailureKind = "insufficient_balance", http_status: int | None = 402,
) -> CandidateTransportError:
    return CandidateTransportError(
        " ".join(_PRIVATE_MARKERS), failure_kind=kind, http_status=http_status,
    )


class _FailingGenerator:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, request: CompileInput) -> tuple[CandidateAst, ...]:
        self.calls += 1
        raise _failure()


class _Editor:
    def __init__(self, *, fail: bool) -> None:
        self.fail = fail
        self.requests: list[StrategyEditRequest] = []

    async def edit(self, request: StrategyEditRequest) -> StrategyEditResult:
        self.requests.append(request)
        if self.fail:
            raise _failure()
        return StrategyEditResult(
            disposition="clarify", message="请确认修改哪个周期。", strategy=None,
            provenance=CandidateProvenance(
                source="bounded_provider", provider="offline-test", model="fixture",
                prompt_version="test.v1", schema_version="test.v1",
                capability_projection_version="test.v1",
                capability_projection_hash="sha256:" + "a" * 64,
                upstream_pattern_commit="a" * 40, candidate_rank=1,
            ),
        )


class _FailingClarificationRouter:
    def __init__(self) -> None:
        self.requests: list[ClarificationDialogueRequest] = []

    async def assess(
        self, request: ClarificationDialogueRequest,
    ) -> ClarificationDialogueAssessment | None:
        self.requests.append(request)
        raise _failure()


@pytest.fixture
def api(tmp_path: Path) -> Iterator[tuple[TestClient, StrategyCompiler, SQLAlchemyDraftStore]]:
    compiler = StrategyCompiler(
        generator=RuleBasedCandidateGenerator(), catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01",
        backtest_anchor_date=date(2026, 9, 5),
    )
    engine = create_backtest_run_engine(f"sqlite+pysqlite:///{tmp_path / 'model-errors.db'}")
    store = SQLAlchemyDraftStore(engine, initialize_schema=True)
    try:
        with TestClient(create_app(
            compiler=compiler, draft_store=store, catalog_root=ROOT / "catalogs",
            include_portfolio_review=False,
        )) as client:
            yield client, compiler, store
    finally:
        engine.dispose()


def _ready(client: TestClient) -> StrategyDraftResponse:
    response = client.post("/api/v1/strategy-drafts", json=_INITIAL)
    assert response.status_code == 201
    draft = StrategyDraftResponse.model_validate_json(response.content)
    assert draft.status == "ready" and draft.strategy is not None
    revised = client.post(f"/api/v1/strategy-drafts/{draft.draft_id}/revisions", json={
        "strategy": draft.strategy.model_dump(mode="json"),
        "execution_settings": {"slippage_bps": "11", "minimum_commission_cny": "9"},
    })
    assert revised.status_code == 201
    draft = StrategyDraftResponse.model_validate_json(revised.content)
    assert draft.revision == 2 and draft.status == "ready"
    assert draft.execution_settings.slippage_bps == 11
    assert draft.execution_settings.minimum_commission_cny == 9
    return draft


def _database_snapshot(store: SQLAlchemyDraftStore) -> dict[str, tuple[tuple[object, ...], ...]]:
    with store.engine.connect() as connection:
        return {
            table.name: tuple(tuple(row) for row in connection.execute(
                table.select().order_by(*table.primary_key.columns),
            ))
            for table in DIALOGUE_METADATA.sorted_tables
        }


def _assert_safe_response(
    response: Response, caplog: pytest.LogCaptureFixture, *,
    kind: CandidateFailureKind = "insufficient_balance", upstream_status: int | None = 402,
    api_status: int = 503,
) -> None:
    assert response.status_code == api_status
    body = response.json()
    error = body["error"]
    assert error["code"] == f"candidate_provider_{kind}"
    assert error["message"] == _failure(kind, upstream_status).public_message
    assert error["details"] == ([] if upstream_status is None else [{
        "location": "model_provider.http_status", "message": str(upstream_status),
        "type": "upstream_http_status",
    }])
    assert response.headers["X-Request-ID"] == body["request_id"]
    assert "strategy" not in body and "run_requested" not in body
    for marker in _PRIVATE_MARKERS:
        assert marker not in response.text
        assert marker not in caplog.text


@pytest.mark.parametrize(
    ("kind", "upstream_status", "api_status"),
    [
        ("authentication_failed", 401, 503),
        ("permission_denied", 403, 503),
        ("insufficient_balance", 402, 503),
        ("rate_limited", 429, 429),
        ("timeout", None, 504),
    ],
)
def test_global_handler_exposes_only_classified_failure_fields(
    kind: CandidateFailureKind, upstream_status: int | None, api_status: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = FastAPI()
    install_exception_handlers(app)

    @app.get("/offline-model-failure")
    async def fail() -> None:
        raise _failure(kind, upstream_status)

    with TestClient(app) as client:
        response = client.get("/offline-model-failure")
    _assert_safe_response(
        response, caplog, kind=kind, upstream_status=upstream_status, api_status=api_status,
    )


@pytest.mark.parametrize("operation", ["compile", "edit"])
def test_failed_compile_or_edit_preserves_the_exact_ready_draft(
    api: tuple[TestClient, StrategyCompiler, SQLAlchemyDraftStore], operation: str,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    client, compiler, store = api
    original = _ready(client)
    saved = asyncio.run(store.latest_for_answer(
        draft_id=original.draft_id, revision=original.revision,
    ))
    before = _database_snapshot(store)
    generator = _FailingGenerator()
    editor = _Editor(fail=True)
    body: dict[str, object] = {
        "utterance": "RSI低于30买入，高于70卖出，回测近1年",
        "as_of_date": "2026-09-05", "execution_settings": {"slippage_bps": "99"},
    }
    if operation == "compile":
        monkeypatch.setattr(compiler, "_generator", generator)
        body["instrument_context"] = "300059.SZ"
    else:
        monkeypatch.setattr(compiler, "_strategy_editor", editor)
        body.update({"utterance": "把买入周期改为10", "edit_current_strategy": True})
    response = client.post("/api/v1/strategy-drafts", json=body, headers={
        "X-Conversation-Parent-Draft-ID": str(original.draft_id),
    })
    _assert_safe_response(response, caplog)
    assert generator.calls == (1 if operation == "compile" else 0)
    assert len(editor.requests) == (1 if operation == "edit" else 0)
    assert _database_snapshot(store) == before
    assert asyncio.run(store.latest_for_answer(
        draft_id=original.draft_id, revision=original.revision,
    )) == saved


def test_failed_clarification_preserves_pending_and_original_drafts(
    api: tuple[TestClient, StrategyCompiler, SQLAlchemyDraftStore],
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    client, compiler, store = api
    original = _ready(client)
    monkeypatch.setattr(compiler, "_strategy_editor", _Editor(fail=False))
    pending_response = client.post("/api/v1/strategy-drafts", headers={
        "X-Conversation-Parent-Draft-ID": str(original.draft_id),
    }, json={
        "utterance": "把20改为10", "as_of_date": "2026-09-05", "edit_current_strategy": True,
    })
    assert pending_response.status_code == 201
    pending = StrategyDraftResponse.model_validate_json(pending_response.content)
    assert pending.status == "needs_clarification"
    assert pending.diagnostic_code == "strategy_edit_clarification"
    saved = asyncio.run(store.latest_for_answer(
        draft_id=pending.draft_id, revision=pending.revision,
    ))
    assert saved.outcome.revision_base_strategy == original.strategy
    assert saved.outcome.execution_settings == original.execution_settings
    before = _database_snapshot(store)
    router = _FailingClarificationRouter()
    monkeypatch.setattr(compiler, "_strategy_editor", None)
    monkeypatch.setattr(compiler, "_clarification_dialogue_router", router)
    response = client.post(
        f"/api/v1/strategy-drafts/{pending.draft_id}/revisions/{pending.revision}"
        "/clarification-answers",
        json={"answer": "还没想好", "execution_settings": {"slippage_bps": "99"}},
    )
    _assert_safe_response(response, caplog)
    assert len(router.requests) == 1
    assert router.requests[0].diagnostic_code == "strategy_edit_clarification"
    assert _database_snapshot(store) == before
    assert asyncio.run(store.latest_for_answer(
        draft_id=pending.draft_id, revision=pending.revision,
    )) == saved
