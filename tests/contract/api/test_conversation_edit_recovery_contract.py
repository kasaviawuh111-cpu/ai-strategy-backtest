# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false
"""Deterministic route/model fixtures, not real-model or backtest acceptance."""

import asyncio
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from ashare_lab.adapters.language import RuleBasedCandidateGenerator
from ashare_lab.api import create_app
from ashare_lab.api.store import InMemoryDraftStore
from ashare_lab.application.compile_strategy import StrategyCompiler
from ashare_lab.domain.catalog import load_catalog_directory
from ashare_lab.ports.candidate_generation import CandidateAst, CandidateProvenance, CompileInput
from ashare_lab.ports.clarification_dialogue import (
    ClarificationDialogueAssessment,
    ClarificationDialogueRequest,
)
from ashare_lab.ports.execution_settings import ExecutionSettingsPatch
from ashare_lab.ports.strategy_editing import StrategyEditRequest, StrategyEditResult

from .backtest_fakes import FakeRunStore, FakeSubmitter

ROOT = Path(__file__).resolve().parents[3]
AS_OF = date(2026, 9, 6)
ORIGINAL = "MACD金叉买入，死叉卖出，回测近1年，本金10万元"
SAFETY = "买入条件改为：我不想活了。其他条件和回测设置保持不变，按新条件重新回测。"
FOOD = "我想吃点东西"
WEATHER = "今天天气怎么样"
NEW_RULE = "东方财富均线交叉买入，反之卖出"
PERIODS = "300059.SZ，5日均线上穿20日均线买入，5日均线下穿20日均线卖出，回测近1年"
REPLIES = {
    ORIGINAL: "这版MACD规则已准备好，可以核对。",
    SAFETY: "听起来你现在很难受。你此刻安全吗，有没有马上伤害自己的危险？",
    FOOD: "想吃点热乎的还是清淡的？可以先照顾一下自己。",
    WEATHER: "你想了解哪里的天气？我现在没有当地的实时天气数据。",
    NEW_RULE: "这次改用均线交叉，你准备比较哪两个周期，哪个方向交叉时买入？",
    PERIODS: "5日与20日均线的买卖方向已明确，新规则可以核对。",
}
SETTINGS = {
    "commission_rate": 0, "minimum_commission_cny": 0, "slippage_bps": 0,
    "allocation_ratio": 0.4, "retry_unfilled_exits": False, "run_robustness": False,
}
PROVENANCE = CandidateProvenance(
    source="bounded_provider", provider="fixture", model="contract-fixture",
    prompt_version="conversation-fixture.v1", schema_version="conversation-fixture.v1",
    capability_projection_version="fixture", capability_projection_hash="sha256:" + "0" * 64,
    upstream_pattern_commit="0" * 40, candidate_rank=1,
)


class _RecordingGenerator:
    def __init__(self) -> None:
        self.requests: list[CompileInput] = []
        self.inner = RuleBasedCandidateGenerator()

    async def generate(self, request: CompileInput) -> tuple[CandidateAst, ...]:
        self.requests.append(request)
        return await self.inner.generate(request)


class _DialogueFixture:
    def __init__(self) -> None:
        self.requests: list[ClarificationDialogueRequest] = []

    async def assess(
        self, request: ClarificationDialogueRequest,
    ) -> ClarificationDialogueAssessment:
        self.requests.append(request)
        assert request.answer in REPLIES, "model must receive the actual current user input"
        return ClarificationDialogueAssessment(
            reply_kind="question", acknowledgement_id="answer_question",
            natural_reply=REPLIES[request.answer],
            # A response-only/safety model cannot add either of these actions.
            requires_new_data=True, run_requested=True,
        )


class _EditorFixture:
    def __init__(self, *, unavailable: bool = False) -> None:
        self.unavailable = unavailable
        self.requests: list[StrategyEditRequest] = []

    async def edit(self, request: StrategyEditRequest) -> StrategyEditResult | None:
        self.requests.append(request)
        assert request.answer != SAFETY, "safety guard must run before the editor"
        if self.unavailable:
            return None
        if request.answer == NEW_RULE:
            return StrategyEditResult(
                "not_edit", "这轮是新的均线规则。", None, PROVENANCE,
            )
        assert request.answer in {FOOD, WEATHER}
        return StrategyEditResult(
            "conversation", REPLIES[request.answer], None, PROVENANCE,
            # Conversation disposition must not apply untrusted action/settings fields.
            run_requested=True, refresh_data=True,
            execution_settings=ExecutionSettingsPatch(slippage_bps=Decimal("999")),
        )


def _compiler(generator: _RecordingGenerator, editor: _EditorFixture, dialogue: _DialogueFixture):
    return StrategyCompiler(
        generator=generator, catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01",
        trusted_date_provider=lambda: AS_OF, strategy_editor=editor,
        clarification_dialogue_router=dialogue,
    )


def _initial(client: TestClient):
    response = client.post("/api/v1/strategy-drafts", json={
        "utterance": ORIGINAL, "instrument_context": "300059.SZ",
        "as_of_date": AS_OF.isoformat(),
    })
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["status"] == "ready"
    # The public panel-revision API owns these saved settings. Using nondefaults
    # prevents a reset to defaults from looking like successful preservation.
    saved = client.post(f"/api/v1/strategy-drafts/{created['draft_id']}/revisions", json={
        "strategy": created["strategy"], "execution_settings": SETTINGS,
    })
    assert saved.status_code == 201, saved.text
    assert saved.json()["strategy_hash"] == created["strategy_hash"]
    return saved.json()


def _parent_turn(client: TestClient, draft_id: str, text: str):
    return client.post("/api/v1/strategy-drafts", headers={
        "X-Conversation-Parent-Draft-ID": draft_id,
    }, json={
        "utterance": text, "as_of_date": AS_OF.isoformat(),
        "instrument_context": "300059.SZ", "edit_current_strategy": True,
    })


def _state(store: InMemoryDraftStore, draft_id: str):
    return asyncio.run(store.load_latest_dialogue_state(draft_id=UUID(draft_id)))


def test_ready_safety_conversation_new_rule_and_period_recovery_are_separate_turns() -> None:
    generator, editor, dialogue = _RecordingGenerator(), _EditorFixture(), _DialogueFixture()
    store, runs = InMemoryDraftStore(), FakeRunStore()
    submitter = FakeSubmitter(runs)
    app = create_app(
        compiler=_compiler(generator, editor, dialogue), draft_store=store,
        backtest_submission=submitter, run_store=runs,
    )
    with TestClient(app) as client:
        initial = _initial(client)
        original_state = _state(store, initial["draft_id"])
        assert original_state.outcome.execution_settings.slippage_bps == 0
        assert original_state.outcome.execution_settings.commission_rate == 0
        assert original_state.outcome.execution_settings.minimum_commission_cny == 0
        assert original_state.outcome.execution_settings.allocation_ratio == Decimal("0.4")
        assert original_state.outcome.execution_settings.retry_unfilled_exits is False
        assert original_state.outcome.execution_settings.run_robustness is False
        expected_inputs = [ORIGINAL]
        for text in (SAFETY, FOOD, WEATHER):
            response = _parent_turn(client, initial["draft_id"], text)
            assert response.status_code == 201, response.text
            payload = response.json()
            assert payload["assistant_message"] == REPLIES[text]
            assert payload["assistant_message"] != REPLIES[expected_inputs[-1]]
            assert payload["draft_id"] == initial["draft_id"]
            assert payload["revision"] == initial["revision"]
            assert payload["strategy"] == initial["strategy"]
            assert payload["strategy_hash"] == initial["strategy_hash"]
            assert payload["execution_settings"] == initial["execution_settings"]
            assert not payload.get("run_requested") and not payload.get("refresh_data")
            expected_inputs.append(text)
            current = _state(store, initial["draft_id"])
            assert current.compile_input == original_state.compile_input
            assert current.outcome.strategy_hash == original_state.outcome.strategy_hash
            assert [turn.user_text for turn in current.recent_turns] == expected_inputs
            assert current.recent_turns[-1].assistant_text == REPLIES[text]
            assert len(generator.requests) == 1

        safety_request = next(item for item in dialogue.requests if item.answer == SAFETY)
        assert safety_request.diagnostic_code == "safety_support"
        assert safety_request.response_only and not safety_request.allow_data_query
        assert [item.user_text for item in safety_request.recent_turns] == [ORIGINAL]
        assert [item.answer for item in editor.requests] == [FOOD, WEATHER]
        assert editor.requests[0].prior_utterance == ORIGINAL
        assert [item.user_text for item in editor.requests[0].recent_turns] == [ORIGINAL, SAFETY]
        assert [item.user_text for item in editor.requests[1].recent_turns] == [
            ORIGINAL, SAFETY, FOOD,
        ]
        assert all(item.strategy.model_dump(mode="json") == initial["strategy"]
                   for item in editor.requests)

        response = _parent_turn(client, initial["draft_id"], NEW_RULE)
        assert response.status_code == 201, response.text  # not_edit must not return 409.
        pending = response.json()
        assert pending["status"] == "needs_clarification"
        assert pending["diagnostic_code"] == "indicator_trigger_requires_clarification"
        assert pending["clarification"] == REPLIES[NEW_RULE]
        assert not pending["strategy"] and not pending["strategy_hash"]
        assert not pending.get("run_requested")
        assert len(generator.requests) == 1  # Missing periods never reach the generator.
        assert editor.requests[-1].answer == NEW_RULE
        assert [item.user_text for item in editor.requests[-1].recent_turns] == expected_inputs
        new_state = _state(store, pending["draft_id"])
        assert new_state.compile_input.utterance == NEW_RULE
        assert [item.user_text for item in new_state.recent_turns] == [*expected_inputs, NEW_RULE]
        # The old executable revision remains intact while new rules need periods.
        assert _state(store, initial["draft_id"]).outcome.strategy_hash == initial["strategy_hash"]

        response = client.post(
            f"/api/v1/strategy-drafts/{pending['draft_id']}/revisions/"
            f"{pending['revision']}/clarification-answers", json={"answer": PERIODS},
        )
        assert response.status_code == 200, response.text
        answered = response.json()
        final = answered["draft"]
        assert answered["assistant_message"] == REPLIES[PERIODS]
        assert final["status"] == "ready"
        assert final["strategy_hash"] != initial["strategy_hash"]
        assert final["strategy"]["instrument"]["symbol"] == "300059.SZ"
        entry = final["strategy"]["entry"]
        exit_rule = final["strategy"]["exit"]["children"][0]
        for rule, trigger in ((entry, "golden_cross"), (exit_rule, "death_cross")):
            assert rule["indicator_id"] == "technical.ma_cross"
            assert rule["params"]["fast_period"] == 5
            assert rule["params"]["slow_period"] == 20
            assert rule["trigger"] == trigger
        assert not final.get("run_requested") and not final.get("refresh_data")
        finished = _state(store, final["draft_id"])
        assert [item.user_text for item in finished.recent_turns] == [
            *expected_inputs, NEW_RULE, PERIODS,
        ]
        assert finished.compile_input.utterance == PERIODS
        assert generator.requests[-1].utterance == PERIODS
        assert len(generator.requests) == 2
    assert submitter.configs == [] and runs.records == {}


@pytest.mark.parametrize("answer", [
    "卖出周期改为10日，其他规则不变", "股票换成DFCF，其他条件不变，重新回测",
])
def test_editor_none_failure_preserves_original_revision_input_settings_and_history(
    answer: str,
) -> None:
    generator = _RecordingGenerator()
    editor, dialogue = _EditorFixture(unavailable=True), _DialogueFixture()
    store = InMemoryDraftStore()
    runs = FakeRunStore()
    submitter = FakeSubmitter(runs)
    app = create_app(
        compiler=_compiler(generator, editor, dialogue), draft_store=store,
        backtest_submission=submitter, run_store=runs,
    )
    with TestClient(app) as client:
        initial = _initial(client)
        before = _state(store, initial["draft_id"])
        response = _parent_turn(client, initial["draft_id"], answer)
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["draft_id"] == initial["draft_id"]
    assert payload["revision"] == initial["revision"]
    assert payload["diagnostic_code"] == "strategy_edit_unavailable"
    assert payload["assistant_message"] == (
        "本次修改未完成校验，原策略已保留。请重试这条修改，尚未执行新回测。"
    )
    assert payload["execution_settings"] == initial["execution_settings"]
    assert not payload.get("run_requested") and not payload.get("refresh_data")
    after = _state(store, initial["draft_id"])
    assert after.compile_input == before.compile_input
    assert after.outcome.strategy == before.outcome.strategy
    assert after.outcome.strategy_hash == before.outcome.strategy_hash
    assert after.outcome.execution_settings == before.outcome.execution_settings
    assert after.revision == before.revision
    assert [item.user_text for item in after.recent_turns] == [ORIGINAL, answer]
    assert after.recent_turns[-1].assistant_text == payload["assistant_message"]
    assert [item.answer for item in editor.requests] == [answer]
    assert len(generator.requests) == 1 and len(dialogue.requests) == 1
    assert submitter.configs == [] and runs.records == {}
