# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false
"""Model-authored clarification text stays separate from executable strategy state."""

import asyncio
from datetime import date
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from ashare_lab.adapters.language import RuleBasedCandidateGenerator
from ashare_lab.api import create_app
from ashare_lab.api.store import InMemoryDraftStore
from ashare_lab.application.compile_strategy import StrategyCompiler
from ashare_lab.domain.catalog import load_catalog_directory
from ashare_lab.ports.candidate_generation import (
    CandidateAst,
    CandidateGenerator,
    CandidateProvenance,
    CompileInput,
)
from ashare_lab.ports.clarification_dialogue import (
    ClarificationDialogueAssessment,
    ClarificationDialogueRequest,
)
from ashare_lab.ports.strategy_editing import StrategyEditRequest, StrategyEditResult

ROOT = Path(__file__).resolve().parents[3]
AS_OF = date(2026, 9, 6)
ORIGINAL = "估值过低的股票反转买"
REPLY = "你想在低估值股票出现反转时买入，准备怎样判断估值已经足够低？"
PROVENANCE = CandidateProvenance(
    source="bounded_provider", provider="fixture", model="fixture-model",
    prompt_version="fixture.v1", schema_version="fixture.v1",
    capability_projection_version="fixture.v1",
    capability_projection_hash="sha256:" + "a" * 64,
    upstream_pattern_commit="a" * 40, candidate_rank=1,
)


class _FailedCandidates:
    def __init__(self, codes: tuple[str, ...], *, accept_exit: bool = False) -> None:
        self.codes = codes
        self.accept_exit = accept_exit
        self.requests: list[CompileInput] = []

    async def generate(self, request: CompileInput) -> tuple[CandidateAst, ...]:
        self.requests.append(request)
        codes = (("entry_rule_not_recognized",)
                 if self.accept_exit and "MACD死叉卖出" in request.utterance else self.codes)
        return tuple(CandidateAst(
            instrument_symbol=None, entry=(), exit=(), confidence=0,
            unsupported_code=code, provenance=PROVENANCE,
        ) for code in codes)


class _Dialogue:
    def __init__(self, reply: str | None = REPLY) -> None:
        self.reply = reply
        self.requests: list[ClarificationDialogueRequest] = []

    async def assess(
        self, request: ClarificationDialogueRequest,
    ) -> ClarificationDialogueAssessment | None:
        self.requests.append(request)
        if self.reply is None:
            return None
        return ClarificationDialogueAssessment(
            reply_kind="unclear", acknowledgement_id="ask_rephrase", natural_reply=self.reply,
        )


def _compiler(generator: CandidateGenerator, dialogue: _Dialogue) -> StrategyCompiler:
    return StrategyCompiler(
        generator=generator, catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01",
        trusted_date_provider=lambda: AS_OF, clarification_dialogue_router=dialogue,
    )


@pytest.mark.parametrize("codes", [
    ("candidate_provider_low_confidence",),
    ("candidate_provider_low_confidence", "candidate_provider_low_confidence"),
    ("candidate_provider_invalid_output", "candidate_provider_low_confidence"),
])
def test_unresolved_candidate_reply_is_model_authored_and_preserves_input(
    codes: tuple[str, ...],
) -> None:
    generator, dialogue, store = _FailedCandidates(codes), _Dialogue(), InMemoryDraftStore()
    app = create_app(compiler=_compiler(generator, dialogue), draft_store=store)
    with TestClient(app) as client:
        response = client.post("/api/v1/strategy-drafts", json={
            "utterance": ORIGINAL, "as_of_date": AS_OF.isoformat(),
        })
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["status"] == "needs_clarification"
    assert payload["clarification"] == REPLY
    assert not payload.get("strategy") and not payload.get("strategy_hash")
    assert not payload.get("run_requested") and not payload.get("refresh_data")
    assert len(generator.requests) == len(dialogue.requests) == 1
    assert dialogue.requests[0].response_only
    assert dialogue.requests[0].answer == ORIGINAL
    assert ORIGINAL in dialogue.requests[0].context_summary
    stored = asyncio.run(store.load_latest_dialogue_state(draft_id=UUID(payload["draft_id"])))
    assert stored.compile_input.utterance == ORIGINAL
    assert stored.outcome.strategy is None and stored.outcome.idea_route is None
    assert stored.recent_turns[-1].assistant_text == REPLY


def test_ambiguous_indicator_asks_with_model_without_inventing_period_or_direction() -> None:
    original = "东方财富均线交叉买，反之卖"
    reply = "你想用均线交叉决定买卖，准备比较哪两个周期的均线、哪个方向交叉时买入？"
    generator, dialogue = _FailedCandidates(()), _Dialogue(reply)
    with TestClient(create_app(compiler=_compiler(generator, dialogue))) as client:
        response = client.post("/api/v1/strategy-drafts", json={
            "utterance": original, "as_of_date": AS_OF.isoformat(),
        })
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["diagnostic_code"] == "indicator_trigger_requires_clarification"
    assert payload["clarification"] == reply
    assert not payload.get("strategy") and not payload.get("run_requested")
    assert generator.requests == []
    assert len(dialogue.requests) == 1 and dialogue.requests[0].answer == original
    assert original in dialogue.requests[0].context_summary


def test_exit_supplement_keeps_original_unresolved_entry_for_next_clarification() -> None:
    generator = _FailedCandidates(("candidate_provider_low_confidence",), accept_exit=True)
    dialogue, store = _Dialogue(), InMemoryDraftStore()
    app = create_app(compiler=_compiler(generator, dialogue), draft_store=store)
    with TestClient(app) as client:
        created = client.post("/api/v1/strategy-drafts", json={
            "utterance": ORIGINAL, "as_of_date": AS_OF.isoformat(),
        }).json()
        dialogue.reply = "卖出按你补充的MACD死叉，买入时准备怎样判断估值足够低？"
        response = client.post(
            f"/api/v1/strategy-drafts/{created['draft_id']}/revisions/"
            f"{created['revision']}/clarification-answers",
            json={"answer": "MACD死叉卖出"},
        )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["assistant_message"] == dialogue.reply
    assert payload["draft"]["status"] == "needs_clarification"
    assert not payload["draft"].get("strategy")
    assert len(generator.requests) == 2
    assert ORIGINAL in generator.requests[-1].utterance
    assert "MACD死叉卖出" in generator.requests[-1].utterance
    assert ORIGINAL in dialogue.requests[-1].context_summary
    stored = asyncio.run(store.load_latest_dialogue_state(draft_id=UUID(created["draft_id"])))
    assert ORIGINAL in stored.compile_input.utterance
    assert "MACD死叉卖出" in stored.compile_input.utterance
    assert stored.outcome.strategy is None and not stored.outcome.run_requested


def test_unavailable_dialogue_does_not_replay_generic_rewrite_request_or_authorize_run() -> None:
    dialogue = _Dialogue(None)
    compiler = _compiler(_FailedCandidates(("candidate_provider_low_confidence",)), dialogue)
    with TestClient(create_app(compiler=compiler)) as client:
        response = client.post("/api/v1/strategy-drafts", json={
            "utterance": ORIGINAL, "as_of_date": AS_OF.isoformat(),
        })
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["clarification"] == "对话模型这次未能返回有效回复，请稍后重试。"
    assert not payload.get("strategy") and not payload.get("run_requested")
    assert len(dialogue.requests) == 1


def test_explicit_edit_can_switch_to_new_strategy_when_editor_says_not_edit() -> None:
    class NotEdit:
        async def edit(self, request: StrategyEditRequest) -> StrategyEditResult:
            return StrategyEditResult(
                disposition="not_edit", message="这是新的交易规则。",
                strategy=None, provenance=PROVENANCE,
            )

    compiler = _compiler(RuleBasedCandidateGenerator(), _Dialogue("新规则已准备好，可以核对。"))
    compiler._strategy_editor = NotEdit()
    with TestClient(create_app(compiler=compiler)) as client:
        created = client.post("/api/v1/strategy-drafts", json={
            "utterance": "MACD金叉买入，死叉卖出", "instrument_context": "300059.SZ",
            "as_of_date": AS_OF.isoformat(),
        }).json()
        response = client.post("/api/v1/strategy-drafts", headers={
            "X-Conversation-Parent-Draft-ID": created["draft_id"],
        }, json={
            "utterance": "RSI低于30买入，高于70卖出", "instrument_context": "300059.SZ",
            "as_of_date": AS_OF.isoformat(), "edit_current_strategy": True,
        })
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "ready"
    assert response.json()["strategy"]["entry"]["indicator_id"] == "technical.rsi"
    assert not response.json().get("run_requested")
