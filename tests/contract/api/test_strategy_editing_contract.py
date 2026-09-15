# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false
"""Focused contract fixtures; real-model acceptance is run separately in the UI."""

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from ashare_lab.adapters.language import RuleBasedCandidateGenerator
from ashare_lab.adapters.language.vibe_candidates import (
    CandidateTransportRequest,
    CandidateTransportResponse,
    build_candidate_capability_matrix,
)
from ashare_lab.adapters.language.vibe_clarification import VibeClarificationDialogueRouter
from ashare_lab.adapters.market_data.mx_saas import MxSaasProviderNoDataError
from ashare_lab.api import create_app
from ashare_lab.api.store import InMemoryDraftStore
from ashare_lab.application.compile_strategy import (
    CompileOutcome,
    CompileStatus,
    StrategyCompiler,
    _exact_edit_choice,
)
from ashare_lab.domain.catalog import load_catalog_directory, load_coverage_catalog_directory
from ashare_lab.domain.strategy import FirstOfExit, IndicatorCondition, StrategySpec, canonical_json
from ashare_lab.ports.backtest_runs import BacktestJobState
from ashare_lab.ports.candidate_generation import (
    CandidateAst,
    CandidateProvenance,
    CompileInput,
    IndicatorIntent,
)
from ashare_lab.ports.clarification_dialogue import (
    ClarificationDialogueAssessment,
    ClarificationOption,
)
from ashare_lab.ports.live_market_data import (
    LiveFinanceDataResult,
    LiveMarketDataProvenance,
    LiveMarketDataResult,
)
from ashare_lab.ports.strategy_editing import (
    StrategyEditRequest,
    StrategyEditResult,
    StrategyEditSemanticError,
)

from .backtest_fakes import FakeRunStore, FakeSubmitter, make_record, result_bundle_json

ROOT = Path(__file__).parents[3]


@pytest.mark.parametrize("answer,selected", [
    ("收盘确认", True), ("收盘确认（", True), ("edit-choice-1", True),
    ("不要收盘确认", False), ("收盘确认是什么意思", False), ("收盘确认并换股票", False),
])
def test_exact_edit_choice_only_resolves_saved_labels(answer, selected):
    option = ClarificationOption("edit-choice-1", "收盘确认", "按收盘判断")
    assert _exact_edit_choice(answer, (option,)) == (option if selected else None)
    assert _exact_edit_choice(answer, (option, replace(option, id="edit-choice-2"))) == (
        option if answer == option.id else None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["edit-choice-1", "stale-choice", None])
async def test_saved_edit_choice_roundtrips_and_only_current_selection_reaches_editor(choice):
    option = ClarificationOption("edit-choice-1", "收盘确认", "仅按收盘状态确认条件")
    requests = []

    class Router:
        async def assess(self, request):
            assert request.options == (option,)
            return ClarificationDialogueAssessment(
                reply_kind="preference", acknowledgement_id="respect_preference",
                natural_reply="已收到。", selected_option_id=choice,
            )

    class Editor:
        async def edit(self, request):
            requests.append(request)
            result = await _Editor().edit(request)
            return replace(result, disposition="clarify", strategy=None,
                           run_requested=False, clarification_options=(option,))

    compiler = StrategyCompiler(
        generator=RuleBasedCandidateGenerator(), catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01", strategy_editor=Editor(),
        backtest_anchor_date=date(2026, 9, 5),
    )
    original = CompileInput(utterance="上穿20日均线买入，下穿20日均线卖出",
                            instrument_context="300059.SZ", as_of_date=date(2026, 9, 5))
    prior = await compiler.compile(original)
    first = await compiler.edit_current_strategy(
        original_input=original, prior_outcome=prior, answer="改买入确认方式",
    )
    assert first.outcome.edit_clarification_options == (option,)
    codec = TypeAdapter(CompileOutcome)
    saved = codec.validate_json(codec.dump_json(first.outcome))
    compiler._clarification_dialogue_router = Router()
    await compiler.edit_current_strategy(
        original_input=original, prior_outcome=saved, answer="就按收盘的方式确认吧",
    )
    assert requests[-1].answer == "就按收盘的方式确认吧"
    assert requests[-1].selected_clarification == (option if choice == option.id else None)
    assert requests[-1].strategy == prior.strategy
    assert requests[-1].pending_edit_inputs == ("改买入确认方式",)


@pytest.mark.asyncio
async def test_semantic_edit_failure_is_not_connection_failure_or_new_revision():
    class Editor:
        async def edit(self, request):
            raise StrategyEditSemanticError("bounded correction exhausted")

    compiler = StrategyCompiler(
        generator=RuleBasedCandidateGenerator(), catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01", strategy_editor=Editor(),
        backtest_anchor_date=date(2026, 9, 5),
    )
    original = CompileInput(utterance="上穿20日均线买入，下穿20日均线卖出",
                            instrument_context="300059.SZ", as_of_date=date(2026, 9, 5))
    prior = await compiler.compile(original)
    result = await compiler.edit_current_strategy(
        original_input=original, prior_outcome=prior, answer="收盘价是否达到当日涨停价",
    )
    assert result is not None and not result.revision_changed
    assert result.outcome.diagnostic_code == "strategy_edit_semantic_mismatch"
    assert result.outcome.revision_base_strategy == prior.strategy
    assert result.outcome.strategy is None and not result.outcome.run_requested
    assert "不需要重复回答" in result.assistant_message


@pytest.mark.asyncio
@pytest.mark.parametrize("identity_kind", ["selected", "absent", "ambiguous", "unavailable"])
async def test_rule_editor_cannot_override_independently_selected_target(identity_kind):
    requests = []

    class IdentityRouter:
        async def assess(self, request):
            requests.append(request)
            assert request.identity_only and not request.prior_utterance and not request.options
            if identity_kind == "unavailable":
                return None
            return ClarificationDialogueAssessment(
                reply_kind="unclear" if identity_kind == "ambiguous" else "preference",
                acknowledgement_id="ask_rephrase" if identity_kind == "ambiguous"
                    else "respect_preference",
                natural_reply="请确认目标股票。",
                instrument_name="东方财富" if identity_kind == "selected" else None,
                instrument_selected=identity_kind == "selected",
            )

    compiler = StrategyCompiler(
        generator=RuleBasedCandidateGenerator(), catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01", strategy_editor=_Editor(),
        backtest_anchor_date=date(2026, 9, 5),
        instrument_name_resolver=lambda name: {"东方财富": "300059.SZ"}[name],
    )
    original = CompileInput(utterance="上穿20日均线买入，下穿20日均线卖出",
                            instrument_context="300308.SZ", as_of_date=date(2026, 9, 5))
    prior = await compiler.compile(original)
    compiler._clarification_dialogue_router = IdentityRouter()
    result = await compiler.edit_current_strategy(
        original_input=original, prior_outcome=prior,
        answer="东方财富，卖出改成10日均线，先不运行。" if identity_kind != "absent"
               else "卖出改成10日均线，先不运行。",
    )
    assert result is not None and len(requests) == 1
    assert not result.outcome.run_requested
    if identity_kind in {"ambiguous", "unavailable"}:
        assert result.outcome.strategy is None
        assert result.outcome.strategy_hash is None
        assert result.outcome.revision_base_strategy == prior.strategy
    else:
        assert result.outcome.strategy.instrument.symbol == (
            "300059.SZ" if identity_kind == "selected" else "300308.SZ"
        )
        assert result.outcome.strategy.entry == prior.strategy.entry
        assert result.outcome.strategy.exit.children[0].params["period"] == 10


def test_unsupported_minutes_retains_identity_for_daily_followup() -> None:
    class IdentityTransport:
        def __init__(self) -> None:
            self.requests: list[CandidateTransportRequest] = []

        async def generate_json(
            self, request: CandidateTransportRequest,
        ) -> CandidateTransportResponse:
            self.requests.append(request)
            if request.response_schema_name == "initial_dialogue_intent":
                return {"intent": "new_strategy"}
            if request.response_schema_name == "contextual_dialogue_intent":
                return {"intent": "supplement"}
            if request.response_schema_name == "dialogue_reply_semantic_review":
                return {"facts": "supported", "state_and_authority": "supported",
                        "user_intent_and_tone": "supported"}
            if request.user_payload and request.user_payload.get("responseOnly"):
                return {
                    "reply_kind": "unclear", "acknowledgement_id": "ask_rephrase",
                    "natural_reply": "日线规则已准备好，尚未启动回测。",
                    "recommended_option_ids": [],
                }
            return {
                "reply_kind": "preference", "acknowledgement_id": "respect_preference",
                "natural_reply": "已确认你指定的股票，分钟线回测仍不受支持。",
                "instrument_name": "东方财富", "instrument_selected": True,
                "recommended_option_ids": [], "selected_option_id": None,
                "strategy_inspiration": None, "requires_new_data": False,
            }

    class InterpretedCandidates:
        """Explicit model-result fixtures keep timeframe interpretation out of this test."""

        def __init__(self) -> None:
            self.requests: list[CompileInput] = []

        async def generate(self, request: CompileInput) -> tuple[CandidateAst, ...]:
            self.requests.append(request)
            assert request.semantic_intent == "new_strategy"
            if len(self.requests) == 1:
                assert request.instrument_context is None
                return (CandidateAst(
                    instrument_symbol=None, entry=(), exit=(), confidence=0.95,
                    unsupported_code="non_daily_timeframe_not_supported",
                ),)
            assert request.instrument_context == "300059.SZ"
            assert "东方财富用5分钟K线" in request.utterance
            assert "那就改成日线" in request.utterance
            return (CandidateAst(
                instrument_symbol=request.instrument_context, confidence=0.95,
                entry=(IndicatorIntent("technical.ma_cross", "1.0.0", "golden_cross",
                                       (("fast_period", 5), ("slow_period", 20),
                                        ("price_field", "close"))),),
                exit=(IndicatorIntent("technical.ma_cross", "1.0.0", "death_cross",
                                      (("fast_period", 5), ("slow_period", 20),
                                       ("price_field", "close"))),),
                backtest_lookback_years=1,
            ),)

    transport = IdentityTransport()
    interpreted = InterpretedCandidates()
    catalog = load_catalog_directory(ROOT / "catalogs")
    live = _UnexpectedLiveData()
    compiler = StrategyCompiler(
        generator=interpreted, catalog=catalog,
        catalog_id="cn_a.signals", release_version="2026.09.01",
        instrument_name_resolver=lambda name: {"东方财富": "300059.SZ"}[name],
        clarification_dialogue_router=VibeClarificationDialogueRouter(
            transport, capability_matrix=build_candidate_capability_matrix(
                catalog, load_coverage_catalog_directory(ROOT / "catalogs" / "coverage"),
            ),
            model_semantic_review=True,
        ),
        backtest_anchor_date=date(2026, 9, 5),
    )
    with TestClient(create_app(compiler=compiler, live_market_data=live)) as client:
        first = client.post("/api/v1/strategy-drafts", json={
            "utterance": "东方财富用5分钟K线，5均线上穿20均线买，下穿卖，测最近一年。",
            "as_of_date": "2026-09-05",
        })
        assert first.status_code == 201, first.text
        original = first.json()
        assert original["status"] == "unsupported"
        assert original["strategy"] is None
        assert not original.get("run_requested")
        assert original["diagnostic_code"] == "non_daily_timeframe_not_supported"
        assert original.get("verified_instrument"), original
        assert original["verified_instrument"]["symbol"] == "300059.SZ"
        assert original["verified_instrument"]["name"] == "东方财富"
        assert original["diagnostic_code"] == "non_daily_timeframe_not_supported"
        assert original["idea_route"] is None
        second = client.post("/api/v1/strategy-drafts", headers={
            "X-Conversation-Parent-Draft-ID": original["draft_id"],
        }, json={
            "utterance": "那就改成日线，5日均线上穿20日均线买，5日均线下穿20日均线卖，近一年。",
            "as_of_date": "2026-09-05",
        })
        assert second.status_code == 201, second.text
        daily = second.json()
        assert daily["status"] == "ready"
        assert daily["strategy"]["instrument"]["symbol"] == "300059.SZ"
        assert daily["strategy"]["entry"]["params"] == {
            "fast_period": 5, "slow_period": 20, "price_field": "close",
        }
        assert daily["strategy"]["entry"]["trigger"] == "golden_cross"
        assert daily["strategy"]["exit"]["children"][0]["trigger"] == "death_cross"
        assert not daily.get("run_requested")
        assert daily["idea_route"] is None
    assert not live.calls
    assert len(interpreted.requests) == 2
    identity_requests = [r for r in transport.requests
                         if r.user_payload and r.user_payload.get("identityOnly")]
    assert len(identity_requests) == 1
    payload = identity_requests[0].user_payload
    assert payload is not None
    assert payload["diagnosticCode"] == "non_daily_timeframe_not_supported"
    assert payload["allowDataQuery"] is False


class _Editor:
    def __init__(self) -> None:
        self.requests: list[StrategyEditRequest] = []

    async def edit(self, request: StrategyEditRequest) -> StrategyEditResult:
        self.requests.append(request)
        provenance = CandidateProvenance(
            source="bounded_provider", provider="test", model="contract-fixture",
            prompt_version="strategy-edit.prompt.v1", schema_version="strategy-edit.v1",
            capability_projection_version="test", capability_projection_hash="sha256:" + "0" * 64,
            upstream_pattern_commit="0" * 40, candidate_rank=1,
        )
        if request.answer == "把20改为10":
            return StrategyEditResult("clarify", "修改买入还是卖出的周期？", None, provenance)
        exit_rule = request.strategy.exit.children[0]
        assert isinstance(exit_rule, IndicatorCondition)
        edited = request.strategy.model_copy(update={"exit": FirstOfExit(children=(
            exit_rule.model_copy(update={"params": {"period": 10, "price_field": "close"}}),
        ))})
        return StrategyEditResult("apply", "只修改卖出周期为10日。", edited, provenance)


class _UnexpectedLiveData:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def screen(self, *, query: str, asset_type: str) -> LiveMarketDataResult:
        self.calls.append("screen")
        raise AssertionError("A strategy edit must not screen stocks")

    async def query_finance(
        self, *, query: str, indicators: str | None,
    ) -> LiveFinanceDataResult:
        if indicators == "证券代码和股票简称":
            # Identity display enrichment is distinct from interpreting an edit
            # as a request for current quotes or an all-universe screen.
            return LiveFinanceDataResult(
                provider="identity-fixture", query=query, indicators=indicators,
                tables=(), provenance=LiveMarketDataProvenance(
                    response_sha256="sha256:" + "a" * 64,
                    retrieved_at=datetime.now(UTC), schema_version="identity-fixture.v1",
                ),
            )
        self.calls.append("finance")
        raise AssertionError("A strategy edit must not become a financial-data query")


@pytest.mark.parametrize(
    ("utterance", "explicit_edit"),
    [
        ("买入条件改为：收盘价创前80日新高。其他条件和回测设置保持不变。", False),
        ("买入条件改为：收盘价创近80日新高。其他条件和回测设置保持不变。", False),
        ("收盘价创前80日新高", True),
    ],
)
def test_history_window_edit_uses_saved_strategy_not_stock_screening(
    utterance: str, explicit_edit: bool,
) -> None:
    class EntryEditor(_Editor):
        async def edit(self, request: StrategyEditRequest) -> StrategyEditResult:
            result = await super().edit(request)
            entry = request.strategy.entry
            assert isinstance(entry, IndicatorCondition)
            edited = request.strategy.model_copy(update={
                "entry": entry.model_copy(update={"params": {**entry.params, "period": 80}}),
            })
            return replace(result, message="只修改买入新高周期为80日。", strategy=edited)

    editor = EntryEditor()
    provider = _UnexpectedLiveData()
    compiler = StrategyCompiler(
        generator=RuleBasedCandidateGenerator(), catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01", strategy_editor=editor,
        backtest_anchor_date=date(2026, 9, 5),
    )
    with TestClient(create_app(
        compiler=compiler, live_market_data=provider, live_finance_data=provider,
    )) as client:
        initial = client.post("/api/v1/strategy-drafts", json={
            "utterance": "创20日新高买入，下穿20日均线卖出，回测近1年，本金10万元",
            "instrument_context": "300059.SZ",
            "as_of_date": "2026-09-05",
        }).json()
        assert initial["status"] == "ready"
        body: dict[str, object] = {"utterance": utterance, "as_of_date": "2026-09-05"}
        if explicit_edit:
            body["edit_current_strategy"] = True
        response = client.post("/api/v1/strategy-drafts", headers={
            "X-Conversation-Parent-Draft-ID": initial["draft_id"],
        }, json=body)
        assert response.status_code == 201, response.text
        edited = response.json()
        assert edited["status"] == "ready"
        assert len(editor.requests) == 1
        assert editor.requests[0].answer == utterance
        expected = initial["strategy"]
        assert expected["entry"]["indicator_id"] == "price.rolling_high"
        expected["entry"]["params"]["period"] = 80
        assert edited["strategy"] == expected
        assert provider.calls == []


def test_explicit_slot_edit_without_parent_fails_without_screening() -> None:
    provider = _UnexpectedLiveData()
    with TestClient(create_app(live_market_data=provider, live_finance_data=provider)) as client:
        response = client.post("/api/v1/strategy-drafts", json={
            "utterance": "收盘价创前80日新高", "edit_current_strategy": True,
            "as_of_date": "2026-09-05",
        })
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "strategy_edit_context_required"
        assert provider.calls == []


def test_indicator_definition_question_uses_context_model_before_data_routing() -> None:
    class DiscussEditor(_Editor):
        async def edit(self, request: StrategyEditRequest) -> StrategyEditResult:
            result = await super().edit(request)
            return replace(result, disposition="discuss", strategy=None,
                           message="新高比较前面已完成的收盘价，不包含今天。")

    editor = DiscussEditor()
    provider = _UnexpectedLiveData()
    compiler = StrategyCompiler(
        generator=RuleBasedCandidateGenerator(), catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01", strategy_editor=editor,
        backtest_anchor_date=date(2026, 9, 5),
    )
    with TestClient(create_app(
        compiler=compiler, live_market_data=provider, live_finance_data=provider,
    )) as client:
        initial = client.post("/api/v1/strategy-drafts", json={
            "utterance": "创30日新高买入，下穿20日均线卖出，回测近1年",
            "instrument_context": "300059.SZ", "as_of_date": "2026-09-05",
        }).json()
        question = "这里的新高是跟前面的收盘价比，还是包括今天一起算？"
        response = client.post("/api/v1/strategy-drafts", headers={
            "X-Conversation-Parent-Draft-ID": initial["draft_id"],
        }, json={"utterance": question, "as_of_date": "2026-09-05"})
        assert response.status_code == 201, response.text
        discussion = response.json()
        assert discussion["diagnostic_code"] == "strategy_discussion"
        assert (discussion["draft_id"], discussion["revision"]) == (
            initial["draft_id"], initial["revision"],
        )
        second = client.post(
            f"/api/v1/strategy-drafts/{discussion['draft_id']}/revisions/"
            f"{discussion['revision']}/clarification-answers", json={"answer": question},
        )
        assert second.status_code == 200, second.text
        assert second.json()["draft"]["diagnostic_code"] == "strategy_discussion"
        assert second.json()["draft"]["revision"] == initial["revision"]
        assert not second.json()["draft"].get("run_requested")
        assert not second.json()["draft"].get("refresh_data")
        assert len(editor.requests) == 2
        assert all(item.strategy.model_dump(mode="json") == initial["strategy"]
                   for item in editor.requests)
        assert provider.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["create", "answer"])
@pytest.mark.parametrize("prior_optimization", [False, True])
async def test_discussion_preserves_revision_without_replaying_execution_or_review(
    endpoint: str, prior_optimization: bool,
) -> None:
    class DiscussEditor(_Editor):
        async def edit(self, request: StrategyEditRequest) -> StrategyEditResult:
            result = await super().edit(request)
            if request.answer == "好，我明白了。":
                return replace(result, disposition="discuss", strategy=None, message="好的。")
            return result

    class NoReview:
        async def review(self, request: object) -> None:
            raise AssertionError("Acknowledgement must not request another review")

    store, editor = InMemoryDraftStore(), DiscussEditor()
    compiler = StrategyCompiler(
        generator=RuleBasedCandidateGenerator(), catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01", strategy_editor=editor,
        backtest_anchor_date=date(2026, 9, 5),
    )
    source = CompileInput(
        utterance="上穿20日均线买入，下穿20日均线卖出，回测近1年",
        instrument_context="300059.SZ", as_of_date=date(2026, 9, 5),
    )
    base = await compiler.compile(source)
    previous = (replace(base, status=CompileStatus.NEEDS_CLARIFICATION, strategy=None,
                        revision_base_strategy=base.strategy,
                        diagnostic_code="strategy_optimization_requested")
                if prior_optimization else base)
    created = await store.create(
        outcome=replace(previous, run_requested=True, refresh_data=True), compile_input=source,
        request_hash="discussion-fixture", idempotency_key=None,
    )
    draft_id = str(created.value.draft_id)
    app = create_app(compiler=compiler, draft_store=store,
                     backtest_review_advisor=NoReview())  # type: ignore[arg-type]
    with TestClient(app) as client:
        if endpoint == "create":
            headers = {
                "X-Conversation-Parent-Draft-ID": draft_id,
                "Idempotency-Key": "unchanged-discussion",
            }
            body = {"utterance": "好，我明白了。", "as_of_date": "2026-09-05"}
            response = client.post("/api/v1/strategy-drafts", headers=headers, json=body)
            assert response.status_code == 201, response.text
            reply = response.json()
            replay = client.post("/api/v1/strategy-drafts", headers=headers, json=body)
            assert replay.status_code == 201 and replay.json() == reply
            conflict = client.post("/api/v1/strategy-drafts", headers=headers,
                                   json={**body, "utterance": "卖出改为下穿10日均线"})
            assert conflict.status_code == 409
            assert conflict.json()["error"]["code"] == "idempotency_key_conflict"
        else:
            response = client.post(
                f"/api/v1/strategy-drafts/{draft_id}/revisions/1/clarification-answers",
                json={"answer": "好，我明白了。"},
            )
            assert response.status_code == 200, response.text
            reply = response.json()["draft"]
        assert (reply["draft_id"], reply["revision"]) == (draft_id, 1)
        assert reply["diagnostic_code"] == "strategy_discussion"
        assert not reply.get("run_requested") and not reply.get("refresh_data")
        assert not reply.get("backtest_review")
        edited = client.post(
            f"/api/v1/strategy-drafts/{draft_id}/revisions/1/clarification-answers",
            json={"answer": "卖出改为下穿10日均线"},
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["draft"]["revision"] == 2
        assert edited.json()["draft"]["strategy"]["entry"] == base.strategy.model_dump()["entry"]
    history = await store.dialogue_history(draft_id=created.value.draft_id)
    assert len(history) == 2 and history[0].revision == 1
    assert history[0].assistant_text == "好的。"


def test_real_lookup_detour_keeps_strategy_even_when_data_is_unavailable() -> None:
    class Data(_UnexpectedLiveData):
        async def query_finance(
            self, *, query: str, indicators: str | None,
        ) -> LiveFinanceDataResult:
            if indicators == "证券代码和股票简称":
                return await super().query_finance(query=query, indicators=indicators)
            self.calls.append(query)
            raise MxSaasProviderNoDataError("fixture empty response")

    class Editor(_Editor):
        async def edit(self, request: StrategyEditRequest) -> StrategyEditResult:
            result = await super().edit(request)
            if "最新收盘价" in request.answer:
                return replace(result, disposition="not_edit", strategy=None)
            return result

    editor, provider = Editor(), Data()
    compiler = StrategyCompiler(
        generator=RuleBasedCandidateGenerator(), catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01", strategy_editor=editor,
        backtest_anchor_date=date(2026, 9, 5),
    )
    with TestClient(create_app(compiler=compiler, live_finance_data=provider)) as client:
        initial = client.post("/api/v1/strategy-drafts", json={
            "utterance": "创30日新高买入，下穿20日均线卖出，回测近1年",
            "instrument_context": "300059.SZ", "as_of_date": "2026-09-05",
        }).json()
        lookup = client.post("/api/v1/strategy-drafts", headers={
            "X-Conversation-Parent-Draft-ID": initial["draft_id"],
        }, json={"utterance": "查一下东方财富最新收盘价", "as_of_date": "2026-09-05"}).json()
        assert lookup["query_diagnostic_code"] == "live_market_data_no_results"
        for field in (
            "draft_id", "revision", "status", "strategy", "strategy_hash", "diagnostic_code",
        ):
            assert lookup[field] == initial[field]
        edited = client.post("/api/v1/strategy-drafts", headers={
            "X-Conversation-Parent-Draft-ID": lookup["draft_id"],
        }, json={"utterance": "卖出改下穿10日均线", "as_of_date": "2026-09-05"}).json()
        assert len(provider.calls) == 1 and len(editor.requests) == 2
        assert edited["status"] == "ready"
        assert edited["strategy"]["entry"] == initial["strategy"]["entry"]
        assert edited["strategy"]["instrument"] == initial["strategy"]["instrument"]


def test_result_discussion_keeps_earliest_report_not_just_latest_two() -> None:
    class Editor(_Editor):
        async def edit(self, request: StrategyEditRequest) -> StrategyEditResult:
            result = await super().edit(request)
            return replace(result, disposition="discuss", strategy=None, message="保留三版供核对。")

    editor, store = Editor(), FakeRunStore()
    compiler = StrategyCompiler(
        generator=RuleBasedCandidateGenerator(), catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01", strategy_editor=editor,
        backtest_anchor_date=date(2026, 9, 5),
    )
    with TestClient(create_app(compiler=compiler, run_store=store,
                              backtest_submission=FakeSubmitter(store))) as client:
        initial = client.post("/api/v1/strategy-drafts", json={
            "utterance": "创30日新高买入，下穿10日均线卖出，回测近1年",
            "instrument_context": "300059.SZ", "as_of_date": "2026-09-05",
        }).json()
        strategy = StrategySpec.model_validate(initial["strategy"])
        ids = ["run:earliest", "run:previous", "run:latest"]
        for run_id in ids:
            store.seed(make_record(run_id, state=BacktestJobState.SUCCEEDED,
                                   strategy_json=canonical_json(strategy),
                                   result_json=result_bundle_json(run_id)))
        response = client.post("/api/v1/strategy-drafts", headers={
            "X-Conversation-Parent-Draft-ID": initial["draft_id"],
        }, json={"utterance": "与最早那版比较", "as_of_date": "2026-09-05",
                 "related_run_ids": ids})
        assert response.status_code == 201, response.text
        assert [item["runId"] for item in editor.requests[-1].backtest_results] == ids
        discussion = response.json()
        reply = client.post(
            f"/api/v1/strategy-drafts/{discussion['draft_id']}/revisions/"
            f"{discussion['revision']}/clarification-answers",
            json={"answer": "好，我明白了。", "related_run_ids": ids},
        )
        assert reply.status_code == 200, reply.text
        assert len(editor.requests) == 2
        assert not reply.json()["draft"].get("run_requested")
        assert [item["runId"] for item in editor.requests[-1].backtest_results] == ids


def test_edited_draft_uses_latest_saved_dsl_and_preserves_it_across_clarification() -> None:
    editor = _Editor()
    compiler = StrategyCompiler(
        generator=RuleBasedCandidateGenerator(),
        catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01",
        strategy_editor=editor, backtest_anchor_date=date(2026, 9, 5),
    )
    with TestClient(create_app(compiler=compiler)) as client:
        initial = client.post("/api/v1/strategy-drafts", json={
            "utterance": "上穿20日均线买入，下穿20日均线卖出，回测近1年，本金10万元",
            "instrument_context": "600519.SH",
            "as_of_date": "2026-09-05",
        }).json()
        assert initial["status"] == "ready"
        current_strategy = initial["strategy"]
        current_strategy["entry"]["params"]["period"] = 30
        saved = client.post(f"/api/v1/strategy-drafts/{initial['draft_id']}/revisions", json={
            "strategy": current_strategy,
        })
        assert saved.status_code == 201
        # Text still mentions 20; latest saved executable rules must win.
        ambiguous = client.post("/api/v1/strategy-drafts", headers={
            "X-Conversation-Parent-Draft-ID": initial["draft_id"],
        }, json={"utterance": "把20改为10", "as_of_date": "2026-09-05"})
        assert ambiguous.status_code == 201, ambiguous.text
        pending = ambiguous.json()
        assert pending["status"] == "needs_clarification"
        assert pending["strategy"] is None
        assert editor.requests[0].strategy.entry.params["period"] == 30  # type: ignore[union-attr]
        answer = client.post(
            f"/api/v1/strategy-drafts/{pending['draft_id']}"
            f"/revisions/{pending['revision']}/clarification-answers",
            json={"answer": "只改卖出的"},
        )
        assert answer.status_code == 200, answer.text
        draft = answer.json()["draft"]
        assert draft["status"] == "ready"
        assert draft["strategy"]["entry"] == current_strategy["entry"]
        assert draft["strategy"]["exit"]["children"][0]["params"]["period"] == 10
        assert draft["strategy"]["backtest"] == current_strategy["backtest"]
        assert editor.requests[-1].recent_turns[-1].user_text == "把20改为10"
        assert draft["candidate_provenance"]["prompt_version"] == "strategy-edit.prompt.v1"


@pytest.mark.parametrize("unchanged", [False, True])
def test_invalid_edit_keeps_old_strategy_non_executable_until_recovered(unchanged: bool) -> None:
    class InvalidEditor(_Editor):
        async def edit(self, request: StrategyEditRequest) -> StrategyEditResult:
            result = await super().edit(request)
            assert result.strategy is not None
            if unchanged:
                return replace(result, strategy=request.strategy)
            return replace(result, strategy=result.strategy.model_copy(update={
                "instrument": result.strategy.instrument.model_copy(update={"symbol": "300059.SZ"}),
            }))

    compiler = StrategyCompiler(
        generator=RuleBasedCandidateGenerator(), catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01", strategy_editor=InvalidEditor(),
        backtest_anchor_date=date(2026, 9, 5),
    )
    with TestClient(create_app(compiler=compiler)) as client:
        initial = client.post("/api/v1/strategy-drafts", json={
            "utterance": "上穿20日均线买入，下穿20日均线卖出，回测近1年",
            "instrument_context": "600519.SH",
            "as_of_date": "2026-09-05",
        }).json()
        edited = client.post("/api/v1/strategy-drafts", headers={
            "X-Conversation-Parent-Draft-ID": initial["draft_id"],
        }, json={"utterance": "只改卖出的周期为10", "as_of_date": "2026-09-05"}).json()
        assert edited["status"] == "needs_clarification"
        assert edited["strategy"] is None
        assert edited["diagnostic_code"] == (
            "strategy_edit_clarification" if unchanged else "strategy_edit_unavailable"
        )


@pytest.mark.parametrize(("text", "refs", "accepted"), [
    ("换成贵州茅台试试，买卖规则和区间全部保留，再回测。", ("贵州茅台",), True),
    ("把这版规则放到贵州茅台600519上再测。", ("贵州茅台", "600519"), True),
    ("改成贵州茅台", ("贵州茅台",), True),
    ("贵州茅台300059再试一次", ("贵州茅台", "300059"), False),
    ("贵州茅台300059再试一次", ("贵州茅台", "300059.SZ"), False),
    ("换成贵州茅台600519试试", ("贵州茅台", "600519.SH"), True),
    ("换一只吧", ("贵州茅台",), False),
])
@pytest.mark.parametrize("combined", [False, True])
def test_model_stock_change_resolves_target_and_binds_saved_rules(
    text: str, refs: tuple[str, ...], accepted: bool, combined: bool,
) -> None:
    class Editor(_Editor):
        async def edit(self, request: StrategyEditRequest) -> StrategyEditResult:
            result = await super().edit(request)
            edited = request.strategy.model_copy(update={"exit": FirstOfExit(children=(
                request.strategy.exit.children[0].model_copy(update={
                    "params": {"period": 5, "price_field": "close"},
                }),
            ))}) if combined else None
            return replace(result, disposition="change_instrument", strategy=edited,
                           instrument_refs=refs, run_requested=True, message="已按原规则换股。")

    editor, store, provider = Editor(), FakeRunStore(), _UnexpectedLiveData()
    compiler = StrategyCompiler(
        generator=RuleBasedCandidateGenerator(), catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01", strategy_editor=editor,
        backtest_anchor_date=date(2026, 9, 5),
        instrument_name_resolver=lambda name: {"贵州茅台": "600519.SH"}[name],
    )
    with TestClient(create_app(compiler=compiler, run_store=store,
                              backtest_submission=FakeSubmitter(store),
                              live_market_data=provider, live_finance_data=provider)) as client:
        initial = client.post("/api/v1/strategy-drafts", json={
            "utterance": "创30日新高买入，下穿10日均线卖出，回测近1年，本金10万元",
            "instrument_context": "300059.SZ", "as_of_date": "2026-09-05",
        }).json()
        strategy = StrategySpec.model_validate(initial["strategy"])
        run_id = "run:stock-change-fixture"
        store.seed(make_record(run_id, state=BacktestJobState.SUCCEEDED,
                               strategy_json=canonical_json(strategy),
                               result_json=result_bundle_json(run_id)))
        response = client.post("/api/v1/strategy-drafts", headers={
            "X-Conversation-Parent-Draft-ID": initial["draft_id"],
        }, json={"utterance": text, "as_of_date": "2026-09-05", "related_run_ids": [run_id]})
        assert response.status_code == 201, response.text
        draft = response.json()
        assert len(editor.requests) == 1 and provider.calls == []
        if accepted:
            expected = {**initial["strategy"], "instrument": {
                **initial["strategy"]["instrument"], "symbol": "600519.SH",
            }}
            if combined:
                expected["exit"] = {"op": "first_of", "children": [{
                    **initial["strategy"]["exit"]["children"][0],
                    "params": {"period": 5, "price_field": "close"},
                }]}
            assert draft["status"] == "ready" and draft["strategy"] == expected
            assert draft["run_requested"] is True
        else:
            assert draft["status"] == "needs_clarification" and draft["strategy"] is None
            assert not draft.get("run_requested")
            if len(refs) == 2:
                assert "对应不同股票" in draft["assistant_message"]
                assert "600519.SH" in draft["assistant_message"]
                assert "300059.SZ" in draft["assistant_message"]


@pytest.mark.parametrize(("rerun", "attach_report"), [(False, True), (True, False), (True, True)])
@pytest.mark.parametrize("same_rules", [False, True])
def test_result_discussion_loads_reports_preserves_strategy_and_accepts_the_next_edit(
    rerun: bool, attach_report: bool, same_rules: bool,
) -> None:
    class IntentTransport:
        def __init__(self) -> None:
            self.requests: list[CandidateTransportRequest] = []

        async def generate_json(
            self, request: CandidateTransportRequest,
        ) -> CandidateTransportResponse:
            self.requests.append(request)
            if request.response_schema_name == "initial_dialogue_intent":
                return {"intent": "new_strategy"}
            if request.response_schema_name == "contextual_dialogue_intent":
                # The language model owns meaning; this contract exercises its
                # typed discussion decision, not the legacy 怎么样 keyword path.
                return {"intent": "supplement"}
            if request.user_payload and request.user_payload.get("identityOnly"):
                return {"reply_kind": "preference", "acknowledgement_id": "respect_preference",
                        "natural_reply": "本轮没有另外选择股票。",
                        "instrument_name": None, "instrument_selected": False}
            return {"reply_kind": "unclear", "acknowledgement_id": "ask_rephrase",
                    "natural_reply": "规则已准备好，尚未开始回测。",
                    "recommended_option_ids": []}

    class DiscussEditor(_Editor):
        async def edit(self, request: StrategyEditRequest) -> StrategyEditResult:
            result = await super().edit(request)
            if request.answer in {"这次结果怎么样？", "你这次没有把买入也一起改掉吧？"}:
                return replace(result, disposition="discuss", strategy=None,
                               message="买入仍是上穿20日线；这次只有一笔交易，先比较回撤。")
            return replace(result, run_requested=rerun,
                           strategy=request.strategy if same_rules else result.strategy)

    editor, store, provider = DiscussEditor(), FakeRunStore(), _UnexpectedLiveData()
    transport = IntentTransport()
    dialogue = VibeClarificationDialogueRouter(transport, capability_matrix=
        build_candidate_capability_matrix(
            load_catalog_directory(ROOT / "catalogs"),
            load_coverage_catalog_directory(ROOT / "catalogs" / "coverage"),
        ))
    compiler = StrategyCompiler(
        generator=RuleBasedCandidateGenerator(), catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01", strategy_editor=editor,
        clarification_dialogue_router=dialogue,
        backtest_anchor_date=date(2026, 9, 5),
    )
    with TestClient(create_app(compiler=compiler, run_store=store,
                              backtest_submission=FakeSubmitter(store),
                              live_market_data=provider, live_finance_data=provider)) as client:
        initial = client.post("/api/v1/strategy-drafts", json={
            "utterance": "上穿20日均线买入，下穿20日均线卖出，回测近1年",
            "instrument_context": "300059.SZ", "as_of_date": "2026-09-05",
        }).json()
        strategy = StrategySpec.model_validate(initial["strategy"])
        run_id = "run:discussion-fixture"
        store.seed(make_record(run_id, state=BacktestJobState.SUCCEEDED,
                               strategy_json=canonical_json(strategy),
                               result_json=result_bundle_json(run_id)))
        reply = client.post("/api/v1/strategy-drafts", headers={
            "X-Conversation-Parent-Draft-ID": initial["draft_id"],
        }, json={"utterance": "这次结果怎么样？", "as_of_date": "2026-09-05",
                 "related_run_ids": [run_id]}).json()
        assert reply["diagnostic_code"] == "strategy_discussion"
        assert reply["strategy"] is None
        assert reply["assistant_message"] == "买入仍是上穿20日线；这次只有一笔交易，先比较回撤。"
        facts = editor.requests[-1].backtest_results
        assert len(facts) == 1
        assert facts[0]["runId"] == run_id
        assert facts[0]["strategy"] == initial["strategy"]
        assert facts[0]["summary"]["totalReturn"] == .12  # type: ignore[index]
        assert "dataProvenance" not in facts[0]["summary"]  # type: ignore[operator]
        intent = next(request for request in transport.requests
                      if request.response_schema_name == "contextual_dialogue_intent")
        assert intent.user_payload is not None
        context = intent.user_payload["context"]
        assert isinstance(context, dict)
        assert context["backtest_results"][0]["runId"] == run_id
        path = (f"/api/v1/strategy-drafts/{reply['draft_id']}"
                f"/revisions/{reply['revision']}/clarification-answers")
        followup = client.post(path, json={"answer": "你这次没有把买入也一起改掉吧？",
                                            "related_run_ids": [run_id]}).json()["draft"]
        assert editor.requests[-1].backtest_results == facts
        path = (f"/api/v1/strategy-drafts/{followup['draft_id']}"
                f"/revisions/{followup['revision']}/clarification-answers")
        edited = client.post(path, json={
            "answer": "只改卖出的周期为10，再跑一次" if rerun else "只改卖出的周期为10",
            "related_run_ids": [run_id] if attach_report else [],
        }).json()["draft"]
        if same_rules and not rerun:
            assert edited["status"] == "needs_clarification"
            assert edited["strategy"] is None and not edited.get("run_requested", False)
            return
        assert edited["status"] == "ready"
        assert edited.get("run_requested", False) is rerun
        assert edited["strategy"]["entry"] == initial["strategy"]["entry"]
        assert edited["strategy"]["exit"]["children"][0]["params"]["period"] == (
            20 if same_rules else 10
        )
        assert provider.calls == []


@pytest.mark.parametrize(("disposition", "same_rules", "run_requested"), [
    ("apply", False, True), ("apply", False, False),
    ("apply", True, True), ("apply", True, False),
    ("change_instrument", False, True), ("change_instrument", False, False),
])
def test_fresh_draft_edit_keeps_explicit_run_intent_without_historical_reports(
    disposition: str, same_rules: bool, run_requested: bool,
) -> None:
    class Editor(_Editor):
        async def edit(self, request: StrategyEditRequest) -> StrategyEditResult:
            result = await super().edit(request)
            assert not request.backtest_results
            if disposition == "change_instrument":
                return replace(result, disposition="change_instrument", strategy=None,
                               instrument_refs=("300033.SZ",), run_requested=run_requested)
            return replace(result, strategy=request.strategy if same_rules else result.strategy,
                           run_requested=run_requested)

    editor, provider = Editor(), _UnexpectedLiveData()
    compiler = StrategyCompiler(
        generator=RuleBasedCandidateGenerator(), catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01", strategy_editor=editor,
        backtest_anchor_date=date(2026, 9, 5),
    )
    with TestClient(create_app(compiler=compiler, live_market_data=provider,
                              live_finance_data=provider)) as client:
        initial = client.post("/api/v1/strategy-drafts", json={
            "utterance": "上穿20日均线买入，下穿20日均线卖出，回测近1年",
            "instrument_context": "300059.SZ", "as_of_date": "2026-09-05",
        }).json()
        assert initial["status"] == "ready" and not initial.get("run_requested", False)
        edit_text = "换成300033.SZ" if disposition == "change_instrument" else (
            "卖出周期保持20日" if same_rules else "卖出周期改为10日"
        )
        response = client.post("/api/v1/strategy-drafts", headers={
            "X-Conversation-Parent-Draft-ID": initial["draft_id"],
        }, json={
            "utterance": edit_text + ("，按新条件重新回测" if run_requested else "，先不回测"),
            "edit_current_strategy": True, "as_of_date": "2026-09-05",
        })
        assert response.status_code == 201, response.text
        edited = response.json()
        assert edited.get("run_requested", False) is run_requested
        if same_rules and not run_requested:
            assert edited["status"] == "needs_clarification" and edited["strategy"] is None
        else:
            assert edited["status"] == "ready" and edited["is_strategy_edit"]
            assert edited["strategy"]["entry"] == initial["strategy"]["entry"]
            assert edited["strategy"]["instrument"]["symbol"] == (
                "300033.SZ" if disposition == "change_instrument" else "300059.SZ"
            )
        assert len(editor.requests) == 1 and not provider.calls


@pytest.mark.parametrize("run_state", [None, BacktestJobState.RUNNING_REPORT])
def test_unavailable_report_reference_is_not_fed_to_the_model(
    run_state: BacktestJobState | None,
) -> None:
    editor, store = _Editor(), FakeRunStore()
    compiler = StrategyCompiler(
        generator=RuleBasedCandidateGenerator(), catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals", release_version="2026.09.01", strategy_editor=editor,
    )
    with TestClient(create_app(compiler=compiler, run_store=store,
                              backtest_submission=FakeSubmitter(store))) as client:
        initial = client.post("/api/v1/strategy-drafts", json={
            "utterance": "上穿20日均线买入，下穿20日均线卖出，回测近1年",
            "instrument_context": "300059.SZ", "as_of_date": "2026-09-05",
        }).json()
        if run_state is not None:
            store.seed(make_record("run:pending", state=run_state))
        reply = client.post("/api/v1/strategy-drafts", headers={
            "X-Conversation-Parent-Draft-ID": initial["draft_id"],
        }, json={"utterance": "这次结果如何", "as_of_date": "2026-09-05",
                 "related_run_ids": ["run:pending"]})
        assert reply.status_code == (404 if run_state is None else 409)
        assert editor.requests == []
