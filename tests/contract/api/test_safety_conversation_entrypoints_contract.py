# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportPrivateUsage=false
"""Safety/conversation DTO regressions with deterministic fixtures, not model acceptance."""

import asyncio
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from ashare_lab.adapters.market_data.mx_saas import MxSaasProviderUnavailableError
from ashare_lab.api import create_app
from ashare_lab.api.store import InMemoryDraftStore
from ashare_lab.ports.live_market_data import LiveFinanceDataResult
from ashare_lab.ports.strategy_editing import StrategyEditRequest, StrategyEditResult

from .backtest_fakes import FakeRunStore, FakeSubmitter

# Reuse the same sibling contract fixtures rather than duplicate a second harness.
from .test_conversation_edit_recovery_contract import (
    FOOD,
    ORIGINAL,
    PROVENANCE,
    REPLIES,
    SAFETY,
    WEATHER,
    _compiler,
    _DialogueFixture,
    _EditorFixture,
    _initial,
    _parent_turn,
    _RecordingGenerator,
    _state,
)

PENDING_EDIT = "把周期改成10"
PENDING_QUESTION = "这次要改MACD的快线、慢线还是信号周期？"


class _PendingEditor(_EditorFixture):
    async def edit(self, request: StrategyEditRequest) -> StrategyEditResult | None:
        if request.answer == PENDING_EDIT:
            self.requests.append(request)
            return StrategyEditResult("clarify", PENDING_QUESTION, None, PROVENANCE)
        return await super().edit(request)


@pytest.mark.parametrize("endpoint", ["create", "clarification"])
@pytest.mark.parametrize("pending", [False, True])
def test_latest_safety_and_conversation_reply_never_replays_pending_prose_or_run_intent(
    endpoint: str, pending: bool,
) -> None:
    generator, editor, dialogue = _RecordingGenerator(), _PendingEditor(), _DialogueFixture()
    store, runs = InMemoryDraftStore(), FakeRunStore()
    submitter = FakeSubmitter(runs)
    app = create_app(
        compiler=_compiler(generator, editor, dialogue), draft_store=store,
        backtest_submission=submitter, run_store=runs,
    )
    with TestClient(app) as client:
        current = _initial(client)
        if pending:
            response = _parent_turn(client, current["draft_id"], PENDING_EDIT)
            assert response.status_code == 201, response.text
            current = response.json()
            assert current["clarification"] == PENDING_QUESTION
        before = _state(store, current["draft_id"])
        # Simulate an earlier explicitly authorized edit/run. A later safety or
        # conversation turn must not replay these flags merely by retaining DSL.
        seeded = asyncio.run(store.revise(
            draft_id=before.draft_id,
            outcome=replace(before.outcome, run_requested=True, refresh_data=True,
                            pending_edit_run_requested=True, pending_edit_refresh_data=True),
            request_hash="fixture-prior-run-intent", idempotency_key=None,
        )).value
        revision = seeded.revision
        previous_inputs = [turn.user_text for turn in before.recent_turns]
        for text in (SAFETY, FOOD, WEATHER):
            editor_count, generator_count = len(editor.requests), len(generator.requests)
            if endpoint == "create":
                response = _parent_turn(client, current["draft_id"], text)
                assert response.status_code == 201, response.text
                payload = response.json()
                draft = payload
            else:
                response = client.post(
                    f"/api/v1/strategy-drafts/{current['draft_id']}/revisions/"
                    f"{revision}/clarification-answers", json={"answer": text},
                )
                assert response.status_code == 200, response.text
                payload = response.json()
                draft = payload["draft"]
            # Exactly the latest model-fixture reply, never a prefix plus old question.
            assert payload["assistant_message"] == REPLIES[text]
            assert PENDING_QUESTION not in payload["assistant_message"]
            assert REPLIES[ORIGINAL] not in payload["assistant_message"]
            assert draft["draft_id"] == current["draft_id"]
            assert draft["revision"] == revision
            assert draft["strategy"] == current["strategy"]
            assert draft["strategy_hash"] == current["strategy_hash"]
            assert draft["execution_settings"] == current["execution_settings"]
            assert not draft.get("run_requested") and not draft.get("refresh_data")
            assert len(generator.requests) == generator_count
            if text == SAFETY:
                assert len(editor.requests) == editor_count
                request = dialogue.requests[-1]
                assert request.answer == SAFETY and request.diagnostic_code == "safety_support"
            else:
                assert len(editor.requests) == editor_count + 1
                request = editor.requests[-1]
                assert request.answer == text
            assert [turn.user_text for turn in request.recent_turns] == previous_inputs
            previous_inputs.append(text)
            stored = _state(store, current["draft_id"])
            assert stored.compile_input == before.compile_input
            assert stored.outcome.strategy == before.outcome.strategy
            assert stored.outcome.revision_base_strategy == before.outcome.revision_base_strategy
            assert stored.outcome.execution_settings == before.outcome.execution_settings
            assert [turn.user_text for turn in stored.recent_turns] == previous_inputs
            assert stored.recent_turns[-1].assistant_text == REPLIES[text]
    assert runs.records == {} and submitter.configs == []


@pytest.mark.parametrize("endpoint", ["create", "clarification"])
def test_unknown_name_does_not_delay_safety_or_conversation_with_identity_requery(
    endpoint: str,
) -> None:
    class UnavailableIdentity:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def query_finance(
            self, *, query: str, indicators: str | None,
        ) -> LiveFinanceDataResult:
            assert indicators == "证券代码和股票简称"
            self.calls.append(query)
            raise MxSaasProviderUnavailableError("fixture identity unavailable")

    provider = UnavailableIdentity()
    generator, editor, dialogue = _RecordingGenerator(), _EditorFixture(), _DialogueFixture()
    store, runs = InMemoryDraftStore(), FakeRunStore()
    submitter = FakeSubmitter(runs)
    app = create_app(
        compiler=_compiler(generator, editor, dialogue), draft_store=store,
        backtest_submission=submitter, run_store=runs, live_finance_data=provider,
    )
    with TestClient(app) as client:
        current = _initial(client)
        before = _state(store, current["draft_id"])
        assert len(provider.calls) == 1  # Initial display-name enrichment failed.
        assert before.last_verified_instrument is not None
        assert before.last_verified_instrument.name is None
        seeded = asyncio.run(store.revise(
            draft_id=before.draft_id,
            outcome=replace(before.outcome, run_requested=True, refresh_data=True),
            request_hash="fixture-unknown-name-prior-run", idempotency_key=None,
        )).value
        for text in (SAFETY, FOOD, WEATHER):
            if endpoint == "create":
                response = _parent_turn(client, current["draft_id"], text)
                assert response.status_code == 201, response.text
                payload = response.json()
                draft = payload
            else:
                response = client.post(
                    f"/api/v1/strategy-drafts/{current['draft_id']}/revisions/"
                    f"{seeded.revision}/clarification-answers", json={"answer": text},
                )
                assert response.status_code == 200, response.text
                payload = response.json()
                draft = payload["draft"]
            assert payload["assistant_message"] == REPLIES[text]
            assert len(provider.calls) == 1  # No new display-only query on a detour.
            assert draft["revision"] == seeded.revision
            assert draft["strategy_hash"] == current["strategy_hash"]
            assert draft["execution_settings"] == current["execution_settings"]
            assert not draft.get("run_requested") and not draft.get("refresh_data")
        assert [request.answer for request in editor.requests] == [FOOD, WEATHER]
        assert len(generator.requests) == 1
    assert runs.records == {} and submitter.configs == []
