from dataclasses import replace
from datetime import date
from functools import partial
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine

from ashare_lab.api import create_app
from ashare_lab.api.persistent_store import SQLAlchemyDraftStore
from ashare_lab.api.schemas import StrategyDraftResponse
from ashare_lab.application.compile_strategy import (
    ClarificationTurnOutcome,
    CompileOutcome,
    CompileStatus,
    StrategyCompiler,
)
from ashare_lab.application.turn_intent import TurnIntent
from ashare_lab.domain.catalog import load_catalog_directory
from ashare_lab.ports.candidate_generation import (
    CandidateAst,
    CandidateGroundingEvidence,
    CompileInput,
    IndicatorIntent,
)

ROOT = Path(__file__).parents[3]


@pytest.fixture
def pending_app(tmp_path: Path):
    candidate = CandidateAst(
        instrument_symbol="300059.SZ", confidence=0.95,
        entry=(IndicatorIntent("technical.rsi", "1.0.0", "below", (("period", 14),), 30),),
        exit=(IndicatorIntent("technical.rsi", "1.0.0", "above", (("period", 14),), 70),),
        unsupported_code="semantic_confirmation_required",
        semantic_review_issues=("卖出条件的触发含义仍需核对",),
    )
    modified = replace(candidate, exit=(replace(candidate.exit[0], value=65),))
    compiler = StrategyCompiler(
        generator=Mock(generate=AsyncMock(side_effect=[(candidate,), (modified,)])),
        catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01",
        trusted_date_provider=lambda: date(2026, 9, 6),
    )
    compiler.classify_initial_intent = AsyncMock(return_value=TurnIntent.NEW_STRATEGY)
    compiler.classify_dialogue_intent = AsyncMock(return_value=TurnIntent.SUPPLEMENT)
    engine = create_engine(f"sqlite:///{tmp_path / 'drafts.db'}")
    store = SQLAlchemyDraftStore(engine, initialize_schema=True)
    try:
        yield create_app(compiler=compiler, draft_store=store)
    finally:
        engine.dispose()


def _create_pending(client: TestClient):
    response = client.post("/api/v1/strategy-drafts", json={
        "utterance": "300059.SZ，14日RSI低于30买入，高于70卖出",
        "as_of_date": "2026-09-06",
    })
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize("endpoint", ["create", "clarification"])
@pytest.mark.parametrize("initial_failure", ["instrument_unconfirmed", "instrument_required"])
def test_identity_recovery_persists_effective_input_and_replaces_stale_failure(
    pending_app, endpoint: str, initial_failure: str,
) -> None:
    compiler = pending_app.state.container.compiler
    utterance = "东方财富，14日RSI低于30买入，高于70卖出"
    evidence = CandidateGroundingEvidence("/instrument/symbol", 0, 4, "东方财富")
    pending = CandidateAst(
        instrument_symbol="300059.SZ", confidence=0.95,
        entry=(IndicatorIntent("technical.rsi", "1.0.0", "below", (("period", 14),), 30),),
        exit=(IndicatorIntent("technical.rsi", "1.0.0", "above", (("period", 14),), 70),),
        unsupported_code="semantic_confirmation_required",
        semantic_review_issues=("卖出条件的触发含义仍需核对",),
    )
    with TestClient(pending_app) as client:
        first = _create_pending(client) if endpoint == "clarification" else None
        compiler.resolve_unsupported_instrument = AsyncMock(return_value=("300059.SZ", evidence))
        if first is None:
            compiler._generator.generate.side_effect = [(
                replace(pending, instrument_symbol=None,
                        instrument_name="东方财富" if initial_failure == "instrument_unconfirmed"
                        else None, unsupported_code="semantic_confirmation_required"
                        if initial_failure == "instrument_unconfirmed" else None),
            ), (pending,)]
            response = client.post("/api/v1/strategy-drafts", json={
                "utterance": utterance, "as_of_date": "2026-09-06",
            })
            assert response.status_code == 201, response.text
            payload = response.json()
        else:
            compiler._generator.generate.side_effect = [(pending,)]
            compiler.answer_clarification = AsyncMock(return_value=ClarificationTurnOutcome(
                reply_kind="clarification", assistant_message="旧的股票未确认失败",
                outcome=CompileOutcome(
                    status=CompileStatus.UNSUPPORTED if initial_failure == "instrument_unconfirmed"
                    else CompileStatus.NEEDS_CLARIFICATION, diagnostic_code=initial_failure,
                ),
                compile_input=CompileInput(utterance, date(2026, 9, 6),
                                           semantic_intent="new_strategy"),
                revision_changed=False,
            ))
            response = client.post(
                f"/api/v1/strategy-drafts/{first['draft_id']}/revisions/{first['revision']}"
                "/clarification-answers", json={"answer": "仍然是东方财富，卖出规则保持不变"},
            )
            assert response.status_code == 200, response.text
            payload = response.json()["draft"]
            assert "旧的股票未确认失败" not in response.json()["assistant_message"]
            assert "卖出条件的触发含义" in response.json()["assistant_message"]
            assert payload["revision"] == first["revision"] + 1
        assert payload["status"] == "needs_clarification"
        assert payload["diagnostic_code"] == "semantic_confirmation_required"
        assert payload["strategy"] is None and not payload.get("run_requested")
        assert payload["verified_instrument"]["symbol"] == "300059.SZ"
        assert payload["verified_instrument"]["name"] == "东方财富"
        stored = client.portal.call(partial(
            pending_app.state.container.drafts.load_dialogue_state,
            draft_id=UUID(payload["draft_id"]), revision=payload["revision"],
        ))
        assert stored.compile_input.utterance == utterance
        assert stored.compile_input.instrument_context == "300059.SZ"
        assert stored.compile_input.resolved_instrument.matches(stored.compile_input)
        assert stored.compile_input.resolved_instrument.evidence == evidence
        assert evidence in stored.outcome.candidate_grounding
        compiler.resolve_unsupported_instrument.assert_awaited_once()
        assert compiler._generator.generate.await_count == 2


def test_single_pending_preview_and_followup_revision_roundtrip(pending_app) -> None:
    with TestClient(pending_app) as client:
        first = _create_pending(client)
        assert first["status"] == "needs_clarification"
        assert first["diagnostic_code"] == "semantic_confirmation_required"
        assert first["strategy"] is None and first["strategy_hash"] is None
        assert first["idea_route"] is None and first["suggested_strategy_choice_id"] is None
        assert first["suggested_strategy"]["instrument"]["symbol"] == "300059.SZ"
        assert "触发含义" in first["clarification"]
        assert "没有完整实现" in first["clarification"]
        assert first["suggested_strategy_note"] == "以下仅展示已识别部分，不是完整策略，尚未回测。"
        assert not first.get("run_requested") and not first.get("refresh_data")
        response = client.post(
            f"/api/v1/strategy-drafts/{first['draft_id']}/revisions/{first['revision']}"
            "/clarification-answers", json={"answer": "卖出阈值改为65，其他不变"},
        )
        assert response.status_code == 200, response.text
        second = response.json()["draft"]
        assert second["draft_id"] == first["draft_id"]
        assert second["revision"] == first["revision"] + 1
        assert second["diagnostic_code"] == first["diagnostic_code"]
        assert second["suggested_strategy_hash"] != first["suggested_strategy_hash"]
        assert second["suggested_strategy"]["instrument"]["symbol"] == "300059.SZ"
        assert second["strategy"] is None and not second.get("run_requested")
        stored = client.portal.call(
            partial(pending_app.state.container.drafts.load_dialogue_state,
                    draft_id=UUID(first["draft_id"]), revision=second["revision"]),
        )
        assert stored.outcome.semantic_review_issues == ("卖出条件的触发含义仍需核对",)
        assert stored.outcome.revision_base_strategy is None
        assert stored.compile_input.instrument_context == "300059.SZ"


def test_execution_prerequisite_preview_is_a_non_executable_contract(pending_app) -> None:
    from ashare_lab.domain.strategy.price_plans import ConditionalPlan, ConditionParameters

    compiler = pending_app.state.container.compiler
    issue = (
        "已识别卖出条件，但当前新策略没有买入规则或期初可卖持仓；"
        "卖出规则仍保留，补充买入规则或期初持仓后，将继续检查数据与执行条件。"
    )
    candidate = CandidateAst(
        instrument_symbol="300059.SZ", entry=(), exit=(), confidence=.95,
        trading_plan=ConditionalPlan(parameters=ConditionParameters.model_validate({
            "rules": [{"kind": "pullback", "side": "sell", "gap": 2}],
        })),
        unsupported_code="execution_prerequisite_required",
        semantic_review_issues=(issue,),
    )
    compiler._generator.generate.side_effect = [(candidate,)]
    with TestClient(pending_app) as client:
        response = client.post("/api/v1/strategy-drafts", json={
            "utterance": "300059.SZ涨起来先拿着，回落2%再卖",
            "as_of_date": "2026-09-06",
        })
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["status"] == "needs_clarification"
    assert payload["diagnostic_code"] == "execution_prerequisite_required"
    assert payload["strategy"] is None and payload["suggested_strategy"] is not None
    assessment = payload["execution_assessment"]
    assert assessment["status"] == "understood_not_executable"
    assert assessment["missing"] == ["买入规则或期初可卖持仓"]
    assert assessment["interpreted_strategy"] == payload["suggested_strategy"]
    assert assessment["strategy_hash"] == payload["suggested_strategy_hash"]
    assert "策略规则已识别并保留" in payload["clarification"]
    assert "具体差异" not in payload["clarification"]
    assert payload["suggested_strategy_note"] == "以下展示已识别规则；尚缺执行前提，未回测。"
    assert not payload.get("run_requested") and not payload.get("refresh_data")


@pytest.mark.parametrize("mutation", [
    "ready", "execute", "refresh", "choice", "missing_note", "wrong_hash", "other_diagnostic",
])
def test_pending_preview_api_exception_cannot_be_used_as_execution_authority(
    pending_app, mutation: str,
) -> None:
    with TestClient(pending_app) as client:
        payload = _create_pending(client)
    if mutation == "ready":
        payload.update(status="ready", strategy=payload["suggested_strategy"],
                       strategy_hash=payload["suggested_strategy_hash"])
    elif mutation == "execute":
        payload["run_requested"] = True
    elif mutation == "refresh":
        payload["refresh_data"] = True
    elif mutation == "choice":
        payload["suggested_strategy_choice_id"] = "approve"
    elif mutation == "missing_note":
        payload["suggested_strategy_note"] = None
    elif mutation == "wrong_hash":
        payload["suggested_strategy_hash"] = "sha256:" + "0" * 64
    else:
        payload["diagnostic_code"] = "idea_guidance_required"
    with pytest.raises(ValidationError):
        StrategyDraftResponse.model_validate(payload)
