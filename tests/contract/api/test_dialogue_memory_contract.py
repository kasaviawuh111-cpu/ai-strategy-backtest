# Starlette's TestClient currently exposes partially unknown httpx method types to Pyright.
# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from ashare_lab.adapters.language import RuleBasedCandidateGenerator
from ashare_lab.adapters.language.vibe_candidates import (
    CandidateTransportRequest,
    CandidateTransportResponse,
    HybridCandidateGenerator,
    build_candidate_capability_matrix,
)
from ashare_lab.adapters.language.vibe_clarification import VibeClarificationDialogueRouter
from ashare_lab.api import create_app
from ashare_lab.application.compile_strategy import StrategyCompiler
from ashare_lab.domain.catalog import load_catalog_directory, load_coverage_catalog_directory
from ashare_lab.ports.candidate_generation import CandidateAst, CompileInput
from ashare_lab.ports.idea_routing import IdeaAssetMapping, IdeaProposal, IdeaRoute, IdeaRouter
from ashare_lab.ports.live_market_data import LiveFinanceDataResult, LiveMarketDataProvenance

ROOT = Path(__file__).resolve().parents[3]


class _UnexpectedFallback:
    async def generate(self, request: CompileInput) -> tuple[CandidateAst, ...]:
        raise AssertionError(f"bounded fallback must not run: {request!r}")


class _RecordingFinanceData:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def query_finance(
        self,
        *,
        query: str,
        indicators: str | None,
    ) -> LiveFinanceDataResult:
        self.calls.append((query, indicators))
        return LiveFinanceDataResult(
            provider="test_finance",
            query=query,
            indicators=indicators,
            tables=(
                {
                    "title": "换手率",
                    "rawTable": {
                        "headers": ["日期", "换手率(%)"],
                        "data": [["2026-09-02", 2.37]],
                    },
                },
            ),
            provenance=LiveMarketDataProvenance(
                response_sha256="sha256:" + "a" * 64,
                retrieved_at=datetime(2026, 9, 3, 10, 0, tzinfo=UTC),
                schema_version="test.live.v1",
            ),
        )


class _RecordingTransport:
    def __init__(self) -> None:
        self.requests: list[CandidateTransportRequest] = []

    async def generate_json(
        self,
        request: CandidateTransportRequest,
    ) -> CandidateTransportResponse:
        self.requests.append(request)
        return {
            "reply_kind": "off_topic",
            "acknowledgement_id": "light_redirect",
            "natural_reply": "我知道你是在开玩笑，这句先不作为策略条件。",
            "recommended_option_ids": [],
        }


@pytest.mark.parametrize("returned_code", ["600183", "600519"])
def test_code_identity_is_provider_verified_and_reused_in_dialogue(returned_code: str) -> None:
    class IdentityFinance(_RecordingFinanceData):
        async def query_finance(
            self, *, query: str, indicators: str | None,
        ) -> LiveFinanceDataResult:
            result = await super().query_finance(query=query, indicators=indicators)
            from dataclasses import replace

            return replace(result, provider="eastmoney_mx_finance_data", tables=({
                "entityCodes": [returned_code],
                "rawTable": {"100000000000870": ["生益科技"], "headName": [" "]},
                "nameMap": {"100000000000870": "股票简称"},
            }, {
                "code": returned_code, "entityName": "报告期",
                "rawTable": {"REPORTDATE": ["2025-12-31"]},
                "nameMap": {"REPORTDATE": "报告期"},
            }))

    finance = IdentityFinance()
    with TestClient(create_app(live_finance_data=finance)) as client:
        first = client.post("/api/v1/strategy-drafts", json={
            "utterance": "600183 MACD金叉买入，MACD死叉卖出，近一年",
            "as_of_date": "2026-09-05",
        })
        assert first.status_code == 201, first.text
        draft = first.json()
        assert draft["status"] == "ready"
        assert draft["verified_instrument"]["symbol"] == "600183.SH"
        assert draft["verified_instrument"]["name"] == (
            "生益科技" if returned_code == "600183" else None
        )
        if returned_code == "600183":
            second = client.post("/api/v1/strategy-drafts", json={
                "utterance": "600183 MACD金叉买入，MACD死叉卖出，近一年",
                "as_of_date": "2026-09-05",
            }, headers={"X-Conversation-Parent-Draft-ID": draft["draft_id"]})
            assert second.status_code == 201, second.text
            assert second.json()["verified_instrument"]["name"] == "生益科技"
            assert len(finance.calls) == 1


def test_code_identity_is_included_after_clarifying_the_instrument() -> None:
    class IdentityFinance(_RecordingFinanceData):
        async def query_finance(
            self, *, query: str, indicators: str | None,
        ) -> LiveFinanceDataResult:
            result = await super().query_finance(query=query, indicators=indicators)
            from dataclasses import replace

            return replace(result, tables=({"entityCodes": ["600183"],
                                           "rawTable": {"股票简称": ["生益科技"]}},))

    finance = IdentityFinance()
    with TestClient(create_app(live_finance_data=finance)) as client:
        first = client.post("/api/v1/strategy-drafts", json={
            "utterance": "MACD金叉买入，MACD死叉卖出，近一年", "as_of_date": "2026-09-05",
        }).json()
        completed = _answer(client, first, "600183")
        assert completed["draft"]["status"] == "ready"
        assert completed["draft"]["verified_instrument"]["name"] == "生益科技"
        assert len(finance.calls) == 1


class _RecordingIdeaRouter:
    def __init__(self) -> None:
        self.requests: list[CompileInput] = []

    async def route(self, request: CompileInput) -> IdeaRoute:
        self.requests.append(request)
        assert request.instrument_context is not None
        strategies = (
            (
                "趋势跟随",
                "股价上穿 20 日均线",
                "股价跌破 20 日均线",
                "股价上穿20日均线买入，股价跌破20日均线卖出，回测近1年",
            ),
            (
                "超卖反转",
                "RSI 低于 30",
                "RSI 高于 70",
                "RSI低于30买入，RSI高于70卖出，回测近1年",
            ),
            (
                "动量交叉",
                "MACD 金叉",
                "MACD 死叉",
                "MACD金叉买入，MACD死叉卖出，回测近1年",
            ),
        )
        proposals = tuple(
            IdeaProposal(
                id=f"idea_{index:012x}",
                title=title,
                hypothesis="只用可验证的量价信号检验观点。",
                entry_summary=entry_summary,
                exit_summary=exit_summary,
                suggested_utterance=suggested_utterance,
                capability_ids=(),
                assumptions=("不证明因果关系。",),
                confidence=0.75,
                instrument_symbol=request.instrument_context,
            )
            for index, (title, entry_summary, exit_summary, suggested_utterance) in enumerate(
                strategies,
                start=1,
            )
        )
        return IdeaRoute(
            understanding="用户补充了一条负面观点。",
            hypothesis="两条观点需要合并后再选择可回测方向。",
            asset_mapping=IdeaAssetMapping(
                instrument_symbol=request.instrument_context,
                relation="current_page_proxy",
                rationale="使用服务器已确认的当前标的。",
                evidence_status="host_context_only",
            ),
            proposals=proposals,
        )


def _answer(client: TestClient, draft: dict[str, Any], answer: str) -> dict[str, Any]:
    response = client.post(
        (
            f"/api/v1/strategy-drafts/{draft['draft_id']}"
            f"/revisions/{draft['revision']}/clarification-answers"
        ),
        json={"answer": answer},
    )
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json())


def _compiler(
    *,
    instrument_name_resolver: Callable[[str], str] | None = None,
    clarification_router: VibeClarificationDialogueRouter | None = None,
    idea_router: IdeaRouter | None = None,
) -> StrategyCompiler:
    generator = HybridCandidateGenerator(
        deterministic=RuleBasedCandidateGenerator(),
        bounded_fallback=_UnexpectedFallback(),
        instrument_name_resolver=instrument_name_resolver,
    )
    return StrategyCompiler(
        generator=generator,
        catalog=load_catalog_directory(ROOT / "catalogs"),
        catalog_id="cn_a.signals",
        release_version="2026.09.01",
        clarification_dialogue_router=clarification_router,
        instrument_name_resolver=instrument_name_resolver,
        idea_router=idea_router,
    )


def test_ambiguous_latin_preference_does_not_merge_or_answer_instrument() -> None:
    idea_router = _RecordingIdeaRouter()
    resolved_names: list[str] = []

    def resolver(name: str) -> str:
        resolved_names.append(name)
        raise LookupError(name)

    compiler = _compiler(
        instrument_name_resolver=resolver,
        idea_router=idea_router,
    )
    with TestClient(create_app(compiler=compiler)) as client:
        created_response = client.post(
            "/api/v1/strategy-drafts",
            json={
                "utterance": "我讨厌特朗普",
                "instrument_context": "300059.SZ",
                "as_of_date": "2026-08-20",
            },
        )
        assert created_response.status_code == 201, created_response.text
        created = cast(dict[str, Any], created_response.json())

        continued = _answer(client, created, "而且我讨厌wash")

    assert continued["reply_kind"] == "clarification"
    assert continued["draft"]["revision"] == 1
    assert continued["draft"]["diagnostic_code"] == "idea_guidance_required"
    assert "放在一起" not in continued["assistant_message"]
    assert [item.utterance for item in idea_router.requests] == ["我讨厌特朗普"]
    assert resolved_names == []


@pytest.mark.parametrize(
    "pending_utterance",
    [
        "MACD金叉买入，MACD死叉卖出",
        "300059.SZ MACD金叉买入",
    ],
)
def test_complete_new_rule_replaces_stale_missing_slot(
    client: TestClient,
    pending_utterance: str,
) -> None:
    created = cast(
        dict[str, Any],
        client.post(
            "/api/v1/strategy-drafts",
            json={"utterance": pending_utterance, "as_of_date": "2026-08-20"},
        ).json(),
    )

    result = _answer(
        client,
        created,
        "300033.SZ RSI低于30买入，RSI高于70卖出，回测近1年",
    )

    draft = cast(dict[str, Any], result["draft"])
    strategy = cast(dict[str, Any], draft["strategy"])
    assert result["reply_kind"] == "accepted"
    assert draft["status"] == "ready"
    assert strategy["instrument"]["symbol"] == "300033.SZ"
    assert strategy["entry"]["indicator_id"] == "technical.rsi"
    assert strategy["exit"]["children"][0]["indicator_id"] == "technical.rsi"
    assert "technical.macd" not in str(strategy)


def test_buy_then_sell_then_instrument_completes_one_pending_strategy(
    client: TestClient,
) -> None:
    created = cast(
        dict[str, Any],
        client.post(
            "/api/v1/strategy-drafts",
            json={"utterance": "MACD", "as_of_date": "2026-08-20"},
        ).json(),
    )

    buy = _answer(client, created, "MACD金叉买入")
    sell = _answer(client, cast(dict[str, Any], buy["draft"]), "MACD死叉卖出")
    completed = _answer(client, cast(dict[str, Any], sell["draft"]), "300059.SZ")

    draft = cast(dict[str, Any], completed["draft"])
    strategy = cast(dict[str, Any], draft["strategy"])
    assert cast(dict[str, Any], buy["draft"])["diagnostic_code"] == ("exit_rule_not_recognized")
    assert cast(dict[str, Any], sell["draft"])["diagnostic_code"] == "instrument_required"
    assert draft["revision"] == 4
    assert draft["status"] == "ready"
    assert strategy["instrument"]["symbol"] == "300059.SZ"
    assert strategy["entry"]["trigger"] == "golden_cross"
    assert strategy["exit"]["children"][0]["trigger"] == "death_cross"


def test_instrument_can_be_supplied_out_of_order_without_losing_the_pending_rule() -> None:
    compiler = _compiler(
        instrument_name_resolver=lambda name: {"同花顺": "300033.SZ"}[name],
    )
    with TestClient(create_app(compiler=compiler)) as client:
        created = cast(
            dict[str, Any],
            client.post(
                "/api/v1/strategy-drafts",
                json={"utterance": "MACD", "as_of_date": "2026-08-20"},
            ).json(),
        )
        buy = _answer(client, created, "MACD金叉买入")
        instrument = _answer(client, cast(dict[str, Any], buy["draft"]), "同花顺")
        completed = _answer(
            client,
            cast(dict[str, Any], instrument["draft"]),
            "MACD死叉卖出",
        )

    instrument_draft = cast(dict[str, Any], instrument["draft"])
    completed_draft = cast(dict[str, Any], completed["draft"])
    assert instrument["reply_kind"] == "accepted"
    assert instrument_draft["revision"] == 3
    assert instrument_draft["diagnostic_code"] == "exit_rule_not_recognized"
    assert completed_draft["status"] == "ready"
    assert completed_draft["strategy"]["instrument"]["symbol"] == "300033.SZ"
    assert completed_draft["strategy"]["entry"]["trigger"] == "golden_cross"
    assert completed_draft["strategy"]["exit"]["children"][0]["trigger"] == "death_cross"


def test_data_query_detour_preserves_pending_strategy_until_it_can_complete() -> None:
    finance = _RecordingFinanceData()
    with TestClient(create_app(live_finance_data=finance)) as client:
        created = cast(
            dict[str, Any],
            client.post(
                "/api/v1/strategy-drafts",
                json={
                    "utterance": "300059.SZ MACD金叉买入",
                    "as_of_date": "2026-08-20",
                },
            ).json(),
        )
        queried = _answer(client, created, "昨天换手率多少")
        completed = _answer(client, created, "MACD死叉卖出")

    queried_draft = cast(dict[str, Any], queried["draft"])
    completed_draft = cast(dict[str, Any], completed["draft"])
    assert queried_draft["revision"] == created["revision"]
    assert queried_draft["diagnostic_code"] == "exit_rule_not_recognized"
    assert queried["data"]["kind"] == "finance"
    assert ("300059.SZ；昨天换手率多少", "昨天换手率") in finance.calls
    assert all(query == "300059.SZ；昨天换手率多少" or indicators == "证券代码和股票简称"
               for query, indicators in finance.calls)
    assert completed_draft["status"] == "ready"
    assert completed_draft["strategy"]["instrument"]["symbol"] == "300059.SZ"


@pytest.mark.parametrize(
    "utterance",
    [
        "同花顺MACD金叉买入",
        "300033.SZ MACD金叉买入",
    ],
)
def test_omitted_entity_query_reuses_the_previously_verified_instrument(
    utterance: str,
) -> None:
    finance = _RecordingFinanceData()
    compiler = _compiler(
        instrument_name_resolver=lambda name: {"同花顺": "300033.SZ"}[name],
    )
    with TestClient(create_app(compiler=compiler, live_finance_data=finance)) as client:
        created = cast(
            dict[str, Any],
            client.post(
                "/api/v1/strategy-drafts",
                json={"utterance": utterance, "as_of_date": "2026-08-20"},
            ).json(),
        )
        queried = _answer(client, created, "昨天换手率多少")

    assert created["diagnostic_code"] == "exit_rule_not_recognized"
    assert queried["draft"]["revision"] == created["revision"]
    assert finance.calls == [("300033.SZ；昨天换手率多少", "昨天换手率")]


@pytest.mark.parametrize("choice", ["1", "2", "3"])
def test_bare_ordinal_selects_only_a_server_validated_option(
    client: TestClient,
    choice: str,
) -> None:
    with_options = cast(
        dict[str, Any],
        client.post(
            "/api/v1/strategy-drafts",
            json={
                "utterance": "东方财富MACD金叉买入",
                "instrument_context": "300059.SZ",
                "as_of_date": "2026-08-20",
            },
        ).json(),
    )
    proposals = cast(dict[str, Any], with_options["idea_route"])["proposals"]
    selected = _answer(client, with_options, choice)
    selected_sentence = cast(dict[str, Any], proposals[int(choice) - 1])["suggested_utterance"]
    independently_compiled = cast(
        dict[str, Any],
        client.post(
            "/api/v1/strategy-drafts",
            json={
                "utterance": selected_sentence,
                "instrument_context": "300059.SZ",
                "as_of_date": "2026-08-20",
            },
        ).json(),
    )

    without_options = cast(
        dict[str, Any],
        client.post(
            "/api/v1/strategy-drafts",
            json={"utterance": "MACD", "as_of_date": "2026-08-20"},
        ).json(),
    )
    ignored = _answer(client, without_options, choice)

    selected_draft = cast(dict[str, Any], selected["draft"])
    ignored_draft = cast(dict[str, Any], ignored["draft"])
    assert len(proposals) >= 2
    assert selected_draft["status"] == "ready"
    assert selected_draft["strategy"]["instrument"]["symbol"] == "300059.SZ"
    assert selected_draft["strategy_hash"] == independently_compiled["strategy_hash"]
    assert ignored["reply_kind"] == "clarification"
    assert ignored_draft["revision"] == without_options["revision"]
    assert ignored_draft["diagnostic_code"] == "strategy_rule_incomplete"
    assert ignored_draft["strategy"] is None


def test_contract_passes_only_the_latest_twenty_turns_to_the_dialogue_model() -> None:
    transport = _RecordingTransport()
    capability_matrix = build_candidate_capability_matrix(
        load_catalog_directory(ROOT / "catalogs"),
        load_coverage_catalog_directory(ROOT / "catalogs" / "coverage"),
    )
    clarification_router = VibeClarificationDialogueRouter(
        transport,
        capability_matrix=capability_matrix,
    )
    compiler = _compiler(clarification_router=clarification_router)
    with TestClient(create_app(compiler=compiler)) as client:
        created = cast(
            dict[str, Any],
            client.post(
                "/api/v1/strategy-drafts",
                json={
                    "utterance": "东方财富MACD金叉买入",
                    "instrument_context": "300059.SZ",
                    "as_of_date": "2026-08-20",
                },
            ).json(),
        )
        for index in range(21):
            unresolved = _answer(client, created, f"你好第{index}轮")
            assert unresolved["draft"]["revision"] == created["revision"]

    payload = transport.requests[-1].user_payload
    recent_turns = cast(list[dict[str, object]], payload["recentTurns"])
    assert len(recent_turns) == 20
    assert recent_turns[0]["userText"] == "你好第0轮"
    assert recent_turns[-1]["userText"] == "你好第19轮"
    assert all(item["userText"] != "东方财富MACD金叉买入" for item in recent_turns)


def test_verified_instrument_can_be_reused_only_after_confirmation_within_twenty_turns() -> None:
    with TestClient(create_app()) as client:
        created = cast(
            dict[str, Any],
            client.post(
                "/api/v1/strategy-drafts",
                json={
                    "utterance": "300059.SZ MACD金叉买入",
                    "as_of_date": "2026-08-20",
                },
            ).json(),
        )
        for index in range(19):
            unresolved = _answer(client, created, f"你好第{index}轮")
            assert unresolved["draft"]["revision"] == created["revision"]

        offered = _answer(
            client,
            created,
            "RSI低于30买入，高于70卖出，回测近1年",
        )
        confirmed = _answer(client, cast(dict[str, Any], offered["draft"]), "沿用")

    assert offered["draft"]["diagnostic_code"] == "instrument_reuse_confirmation"
    assert "300059.SZ" in offered["assistant_message"]
    assert offered["draft"]["strategy"] is None
    assert confirmed["draft"]["status"] == "ready"
    assert confirmed["draft"]["strategy"]["instrument"]["symbol"] == "300059.SZ"


def test_verified_instrument_expires_after_it_leaves_the_twenty_turn_window() -> None:
    with TestClient(create_app()) as client:
        created = cast(
            dict[str, Any],
            client.post(
                "/api/v1/strategy-drafts",
                json={
                    "utterance": "300059.SZ MACD金叉买入",
                    "as_of_date": "2026-08-20",
                },
            ).json(),
        )
        for index in range(20):
            unresolved = _answer(client, created, f"你好第{index}轮")
            assert unresolved["draft"]["revision"] == created["revision"]

        replaced = _answer(
            client,
            created,
            "RSI低于30买入，高于70卖出，回测近1年",
        )

    assert replaced["draft"]["diagnostic_code"] == "instrument_required"
    assert replaced["draft"]["strategy"] is None
    assert "沿用上次" not in replaced["assistant_message"]


def test_rejecting_instrument_reuse_never_binds_the_previous_symbol() -> None:
    with TestClient(create_app()) as client:
        created = cast(
            dict[str, Any],
            client.post(
                "/api/v1/strategy-drafts",
                json={
                    "utterance": "300059.SZ MACD金叉买入",
                    "as_of_date": "2026-08-20",
                },
            ).json(),
        )
        offered = _answer(
            client,
            created,
            "RSI低于30买入，高于70卖出，回测近1年",
        )
        rejected = _answer(client, cast(dict[str, Any], offered["draft"]), "不沿用")
        rebound = _answer(client, cast(dict[str, Any], rejected["draft"]), "300033.SZ")

    assert rejected["draft"]["diagnostic_code"] == "instrument_required"
    assert rejected["draft"]["strategy"] is None
    assert "股票" in rejected["assistant_message"] and "名称" in rejected["assistant_message"]
    assert rebound["draft"]["status"] == "ready"
    assert rebound["draft"]["strategy"]["instrument"]["symbol"] == "300033.SZ"


def test_emotional_text_does_not_pollute_verified_instrument_memory() -> None:
    resolved_names: list[str] = []

    def resolver(name: str) -> str:
        resolved_names.append(name)
        raise LookupError(name)

    idea_router = _RecordingIdeaRouter()
    compiler = _compiler(instrument_name_resolver=resolver, idea_router=idea_router)
    with TestClient(create_app(compiler=compiler)) as client:
        created = cast(
            dict[str, Any],
            client.post(
                "/api/v1/strategy-drafts",
                json={
                    "utterance": "300059.SZ MACD金叉买入",
                    "as_of_date": "2026-08-20",
                },
            ).json(),
            )
        calls_before_emotional_turn = len(idea_router.requests)
        emotional = _answer(client, created, "我讨厌特朗普")
        confirmed = _answer(client, cast(dict[str, Any], emotional["draft"]), "沿用")

    assert emotional["draft"]["diagnostic_code"] == "instrument_reuse_confirmation"
    assert "300059.SZ" in emotional["assistant_message"]
    assert "特朗普（" not in emotional["assistant_message"]
    assert emotional["draft"]["strategy"] is None
    assert len(idea_router.requests) == calls_before_emotional_turn + 1
    assert confirmed["draft"]["diagnostic_code"] == "idea_guidance_required"
    assert all(
        proposal["instrument_symbol"] == "300059.SZ"
        for proposal in confirmed["draft"]["idea_route"]["proposals"]
    )
    assert resolved_names == []


def test_verified_instrument_memory_never_crosses_draft_boundaries() -> None:
    with TestClient(create_app()) as client:
        first = cast(
            dict[str, Any],
            client.post(
                "/api/v1/strategy-drafts",
                json={
                    "utterance": "300059.SZ MACD金叉买入",
                    "as_of_date": "2026-08-20",
                },
            ).json(),
        )
        second = cast(
            dict[str, Any],
            client.post(
                "/api/v1/strategy-drafts",
                json={"utterance": "MACD", "as_of_date": "2026-08-20"},
            ).json(),
        )
        replaced = _answer(
            client,
            second,
            "RSI低于30买入，高于70卖出，回测近1年",
        )

    assert first["draft_id"] != second["draft_id"]
    assert replaced["draft"]["diagnostic_code"] == "instrument_required"
    assert "300059.SZ" not in replaced["assistant_message"]
    assert "沿用上次" not in replaced["assistant_message"]


def test_parent_draft_header_links_memory_and_fails_closed() -> None:
    with TestClient(create_app()) as client:
        first_response = client.post(
            "/api/v1/strategy-drafts",
            json={
                "utterance": "300059.SZ MACD金叉买入，MACD死叉卖出，回测近1年",
                "as_of_date": "2026-08-20",
            },
        )
        assert first_response.status_code == 201, first_response.text
        first = cast(dict[str, Any], first_response.json())

        linked_response = client.post(
            "/api/v1/strategy-drafts",
            headers={"X-Conversation-Parent-Draft-ID": first["draft_id"]},
            json={
                "utterance": "RSI低于30买入，RSI高于70卖出，回测近1年",
                "as_of_date": "2026-08-20",
            },
        )
        assert linked_response.status_code == 201, linked_response.text
        linked = cast(dict[str, Any], linked_response.json())
        assert linked["draft_id"] != first["draft_id"]
        assert linked["diagnostic_code"] == "instrument_reuse_confirmation"
        assert "300059.SZ" in linked["assistant_message"]

        confirmed = _answer(client, linked, "沿用")
        assert confirmed["draft"]["status"] == "ready"
        assert confirmed["draft"]["strategy"]["instrument"]["symbol"] == "300059.SZ"

        malformed = client.post(
            "/api/v1/strategy-drafts",
            headers={"X-Conversation-Parent-Draft-ID": "not-a-uuid"},
            json={"utterance": "MACD", "as_of_date": "2026-08-20"},
        )
        missing = client.post(
            "/api/v1/strategy-drafts",
            headers={
                "X-Conversation-Parent-Draft-ID": "00000000-0000-0000-0000-000000000000"
            },
            json={"utterance": "MACD", "as_of_date": "2026-08-20"},
        )
        assert malformed.status_code == 422
        assert malformed.json()["error"]["code"] == "request_validation_failed"
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "conversation_parent_draft_not_found"

        explicit = {
            "utterance": "600519.SH MACD金叉买入，MACD死叉卖出，回测近1年",
            "instrument_context": "600519.SH",
            "as_of_date": "2026-08-20",
        }
        idempotency_headers = {
            "Idempotency-Key": "conversation-parent-001",
            "X-Conversation-Parent-Draft-ID": first["draft_id"],
        }
        accepted = client.post(
            "/api/v1/strategy-drafts",
            headers=idempotency_headers,
            json=explicit,
        )
        idempotency_headers["X-Conversation-Parent-Draft-ID"] = linked["draft_id"]
        conflict = client.post(
            "/api/v1/strategy-drafts",
            headers=idempotency_headers,
            json=explicit,
        )
        assert accepted.status_code == 201, accepted.text
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "idempotency_key_conflict"
