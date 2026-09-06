from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from ashare_lab.adapters.language.vibe_candidates import (
    CandidateCapabilityMatrix,
    CandidateTransportError,
    CandidateTransportRequest,
    CandidateTransportResponse,
    build_candidate_capability_matrix,
)
from ashare_lab.adapters.language.vibe_clarification import VibeClarificationDialogueRouter
from ashare_lab.domain.catalog import load_catalog_directory, load_coverage_catalog_directory
from ashare_lab.ports.clarification_dialogue import (
    ClarificationDialogueRequest,
    ClarificationDialogueTurn,
    ClarificationOption,
)
from ashare_lab.ports.current_fact_research import (
    CurrentFactResearchResult,
    ResearchPurpose,
    ResearchSource,
)

ROOT = Path(__file__).parents[4]


class _RecordingTransport:
    def __init__(self, response: object) -> None:
        self.response = response
        self.requests: list[CandidateTransportRequest] = []

    async def generate_json(
        self,
        request: CandidateTransportRequest,
    ) -> CandidateTransportResponse:
        self.requests.append(request)
        if isinstance(self.response, CandidateTransportError):
            raise self.response
        return cast(CandidateTransportResponse, self.response)


@pytest.fixture
def capability_matrix() -> CandidateCapabilityMatrix:
    return build_candidate_capability_matrix(
        load_catalog_directory(ROOT / "catalogs"),
        load_coverage_catalog_directory(ROOT / "catalogs" / "coverage"),
    )


def _request() -> ClarificationDialogueRequest:
    return ClarificationDialogueRequest(
        answer="我是你爸",
        prior_utterance="东方财富MACD金叉买入",
        diagnostic_code="exit_rule_not_recognized",
        question="什么时候卖？",
        context_summary="用户已经说清了进场条件。",
        options=(
            ClarificationOption(id="idea_000000000001", title="动能转弱", preview="候选一"),
            ClarificationOption(id="idea_000000000002", title="持有到期", preview="候选二"),
        ),
    )


def _identity_request(answer: str) -> ClarificationDialogueRequest:
    return replace(
        _request(), answer=answer, prior_utterance="", question="", options=(),
        diagnostic_code="non_daily_timeframe_not_supported",
        context_summary="分钟线执行不受支持，只保留原文明确的股票身份。",
        identity_only=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["东方财富", "300059"])
async def test_identity_only_extracts_exact_stock_in_an_unsupported_complete_strategy(
    capability_matrix: CandidateCapabilityMatrix, name: str,
) -> None:
    # This internal acknowledgement is intentionally not accepted as an ordinary
    # chat reply; identity extraction must not depend on user-facing prose rules.
    reply = f"已选择{name}。"
    transport = _RecordingTransport({
        "reply_kind": "preference", "acknowledgement_id": "respect_preference",
        "natural_reply": reply, "instrument_name": name, "instrument_selected": True,
    })
    request = _identity_request(f"{name}用5分钟K线，5均线上穿20均线买，下穿卖，测最近一年。")
    result = await VibeClarificationDialogueRouter(
        transport, capability_matrix=capability_matrix,
    ).assess(request)
    assert result is not None and result.instrument_selected and result.instrument_name == name
    assert len(transport.requests) == 1
    submitted = transport.requests[0]
    assert submitted.user_payload["identityOnly"] is True
    assert "只做本轮原文中的股票身份提取" in submitted.system_contract
    assert "responseOnly=false 时先判断" not in submitted.system_contract
    assert "IDENTITY ONLY" in submitted.system_footer
    properties = submitted.response_schema["properties"]
    assert properties["reply_kind"]["enum"] == ["preference"]
    assert properties["strategy_inspiration"] == {"type": "null"}
    assert properties["source_ids"]["maxItems"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [
    "不要用东方财富，暂时不选股票。", "东方财富或者贵州茅台，还没想好用哪只。",
    "用5分钟K线回测，股票还没选。",
])
async def test_identity_only_can_leave_negated_multiple_or_missing_stock_unselected(
    capability_matrix: CandidateCapabilityMatrix, answer: str,
) -> None:
    transport = _RecordingTransport({
        "reply_kind": "preference", "acknowledgement_id": "respect_preference",
        "natural_reply": "本句没有唯一明确的股票选择。",
        "instrument_name": None, "instrument_selected": False,
    })
    result = await VibeClarificationDialogueRouter(
        transport, capability_matrix=capability_matrix,
    ).assess(_identity_request(answer))
    assert result is not None and not result.instrument_selected and result.instrument_name is None
    assert len(transport.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [
    {"instrument_name": "300059.SZ"},
    {"instrument_selected": "true"},
    {"instrument_selected": False},
    {"strategy_inspiration": "围绕趋势形成策略"},
    {"recommended_option_ids": ["idea_000000000001"]},
    {"selected_option_id": "idea_000000000001"},
    {"source_answer": True},
    {"source_ids": ["invented_source"]},
    {"source_temporal_status": "dated"},
    {"requires_new_data": True, "instrument_selected": False, "instrument_name": None},
    {"reply_kind": "question", "acknowledgement_id": "answer_question",
     "instrument_selected": False, "instrument_name": None},
])
async def test_identity_only_rejects_inferred_names_and_unrelated_authority(
    capability_matrix: CandidateCapabilityMatrix, extra: dict[str, object],
) -> None:
    transport = _RecordingTransport({
        "reply_kind": "preference", "acknowledgement_id": "respect_preference",
        "natural_reply": "识别到东方财富。",
        "instrument_name": "东方财富", "instrument_selected": True, **extra,
    })
    result = await VibeClarificationDialogueRouter(
        transport, capability_matrix=capability_matrix,
    ).assess(_identity_request("东方财富用5分钟K线回测。"))
    assert result is None and len(transport.requests) == 1


@pytest.mark.asyncio
async def test_identity_only_cannot_also_request_response_only_mode(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    transport = _RecordingTransport(None)
    result = await VibeClarificationDialogueRouter(
        transport, capability_matrix=capability_matrix,
    ).assess(replace(_identity_request("东方财富用5分钟K线回测。"), response_only=True))
    assert result is None and not transport.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_data_query", [False, True])
async def test_new_data_decision_requires_an_explicit_routing_context(
    capability_matrix: CandidateCapabilityMatrix, allow_data_query: bool,
) -> None:
    transport = _RecordingTransport({
        "reply_kind": "question", "acknowledgement_id": "answer_question",
        "natural_reply": "需要补查最新换手率。", "recommended_option_ids": [],
        "requires_new_data": True,
    })
    result = await VibeClarificationDialogueRouter(
        transport, capability_matrix=capability_matrix,
    ).assess(replace(
        _request(), answer="查询中际旭创最新换手率。", allow_data_query=allow_data_query,
    ))
    assert (result is not None) is allow_data_query
    if result is not None:
        assert result.requires_new_data
    assert transport.requests[0].user_payload["allowDataQuery"] is allow_data_query


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [
    {"recommended_option_ids": ["idea_000000000001"]},
    {"strategy_inspiration": "趋势交易风格"},
    {"selected_option_id": "idea_000000000001"},
])
async def test_new_data_decision_cannot_also_rank_create_or_select_options(
    capability_matrix: CandidateCapabilityMatrix, extra: dict[str, object],
) -> None:
    transport = _RecordingTransport({
        "reply_kind": "preference", "acknowledgement_id": "respect_preference",
        "natural_reply": "需要补查最新换手率。", "recommended_option_ids": [],
        "requires_new_data": True, **extra,
    })
    result = await VibeClarificationDialogueRouter(
        transport, capability_matrix=capability_matrix,
    ).assess(replace(
        _request(), answer="查询最新换手率。", allow_data_query=True,
    ))
    assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("verified", [True, False])
@pytest.mark.parametrize("wrapper", [" {} ", "（{}）"])
async def test_source_reply_receives_stored_research_and_only_allows_its_urls(
    capability_matrix: CandidateCapabilityMatrix, verified: bool, wrapper: str,
) -> None:
    now = datetime(2026, 9, 5, tzinfo=UTC)
    source = ResearchSource(
        "source1", "新闻专题", "https://example.test/news?a=1&b=2", "示例", None,
    )
    research = CurrentFactResearchResult(
        provider="test", model="none", provider_response_id="test", query="新闻",
        purpose=ResearchPurpose.VIEWPOINT, as_of=now, summary="搜索摘要，尚未逐页核验。",
        facts=(), sources=(source,), unresolved_questions=(), retrieved_at=now,
        response_sha256="sha256:" + "a" * 64, search_call_count=1,
    )
    url = source.url if verified else source.url + "&invented=3"
    reply = f"此前参考的是新闻专题{wrapper.format(url)}，仅有搜索摘要，尚未逐页核验。"
    transport = _RecordingTransport({
        "reply_kind": "question", "acknowledgement_id": "answer_question",
        "natural_reply": reply, "recommended_option_ids": [],
        "source_answer": True, "source_ids": ["source1"],
        "source_temporal_status": "unverified",
    })
    result = await VibeClarificationDialogueRouter(
        transport, capability_matrix=capability_matrix,
    ).assess(replace(_request(), answer="刚才新闻的出处？", research=research))
    assert (result is not None) is verified
    if result:
        assert result.natural_reply == reply
    payload = transport.requests[0].user_payload
    assert payload is not None and payload["research"] is not None
    assert str(payload["research"]).find(source.url) >= 0


def _source_request() -> ClarificationDialogueRequest:
    now = datetime(2026, 9, 5, tzinfo=UTC)
    sources = (
        ResearchSource("web_a", "报道甲", "https://www.example.test/news?ref=" + "a" * 140,
                       "示例甲", None),
        ResearchSource("web_b", "报道乙", "https://www.example.test/topic?ref=" + "b" * 140,
                       "示例乙", None),
    )
    research = CurrentFactResearchResult(
        provider="test", model="none", provider_response_id="test", query="公开消息",
        purpose=ResearchPurpose.VIEWPOINT, as_of=now, summary="搜索摘要，尚未逐页核验。",
        facts=(), sources=sources, unresolved_questions=(), retrieved_at=now,
        response_sha256="sha256:" + "b" * 64, search_call_count=1,
    )
    return replace(_request(), answer="你说的近期事情有出处吗？只给两条最相关的。",
                   research=research)


def _source_response(request: ClarificationDialogueRequest) -> dict[str, object]:
    assert request.research is not None
    return {
        "reply_kind": "question", "acknowledgement_id": "answer_question",
        "natural_reply": "日期未核实，不能确认是近期消息。\n" + "\n".join(
            f"{source.title}：{source.url}" for source in request.research.sources
        ),
        "source_answer": True,
        "source_ids": [source.source_id for source in request.research.sources],
        "source_temporal_status": "unverified",
    }


@pytest.mark.asyncio
async def test_source_answer_selects_original_ids_and_keeps_two_full_urls_verbatim(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    request = _source_request()
    response = _source_response(request)
    assert len(str(response["natural_reply"])) > 320
    transport = _RecordingTransport(response)
    result = await VibeClarificationDialogueRouter(
        transport, capability_matrix=capability_matrix,
    ).assess(request)
    assert result is not None and result.natural_reply == response["natural_reply"]
    assert len(transport.requests) == 1
    schema = transport.requests[0].response_schema
    assert schema["properties"]["source_ids"]["items"]["enum"] == ["web_a", "web_b"]
    assert {"source_answer", "source_ids", "source_temporal_status"} <= set(schema["required"])


@pytest.mark.asyncio
@pytest.mark.parametrize("intro", [
    "关于近期事情的说法尚未核实，下面只是供核对的相关来源。",
    "这些资料是不是最新的，我还无法核实；发布日期也没有确认。",
])
async def test_unverified_source_dates_do_not_reject_natural_uncertainty_wording(
    capability_matrix: CandidateCapabilityMatrix, intro: str,
) -> None:
    request = _source_request()
    response = _source_response(request)
    response["natural_reply"] = intro + "\n" + str(response["natural_reply"]).split("\n", 1)[1]
    transport = _RecordingTransport(response)
    result = await VibeClarificationDialogueRouter(
        transport, capability_matrix=capability_matrix,
    ).assess(request)
    assert result is not None and result.natural_reply == response["natural_reply"]
    assert len(transport.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("published_at,status", [
    (None, "unverified"), ("", "unverified"), ("昨天", "unverified"),
    ("2025-08-30", "dated"), ("2026-09-05T08:30:00+08:00", "dated"),
])
async def test_source_temporal_status_uses_only_the_supplied_publication_date(
    capability_matrix: CandidateCapabilityMatrix, published_at: str | None, status: str,
) -> None:
    request = _source_request()
    assert request.research is not None
    request = replace(request, research=replace(
        request.research,
        sources=tuple(replace(source, published_at=published_at)
                      for source in request.research.sources),
    ))
    response = _source_response(request)
    response["source_temporal_status"] = status
    if status == "dated":
        response["natural_reply"] = f"来源提供的发布日期为{published_at}。\n" + str(
            response["natural_reply"]
        ).split("\n", 1)[1]
    transport = _RecordingTransport(response)
    result = await VibeClarificationDialogueRouter(
        transport, capability_matrix=capability_matrix,
    ).assess(request)
    assert result is not None and result.natural_reply == response["natural_reply"]
    supplied_sources = transport.requests[0].user_payload["research"]["sources"]
    assert all(source["published_at"] == published_at for source in supplied_sources)
    assert all(source["temporal_status"] == status for source in supplied_sources)
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_one_undated_selected_source_keeps_the_answer_temporality_unverified(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    request = _source_request()
    assert request.research is not None
    request = replace(request, research=replace(request.research, sources=(
        replace(request.research.sources[0], published_at="2026-09-04"),
        request.research.sources[1],
    )))
    response = _source_response(request)
    transport = _RecordingTransport(response)
    result = await VibeClarificationDialogueRouter(
        transport, capability_matrix=capability_matrix,
    ).assess(request)
    assert result is not None and result.natural_reply == response["natural_reply"]
    assert len(transport.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    "empty_citations", "missing_url", "shortened_url", "unknown_id", "missing_classification",
    "wrong_classification", "new_data", "wrong_temporal_status",
])
async def test_source_answer_contract_failures_are_repaired_once_not_post_composed(
    capability_matrix: CandidateCapabilityMatrix, failure: str,
) -> None:
    request = _source_request()
    repaired = _source_response(request)
    invalid = dict(repaired)
    if failure == "empty_citations":
        invalid.update(source_ids=[], natural_reply="此前参考的是报道甲和报道乙。")
    elif failure == "missing_url":
        invalid["natural_reply"] = str(repaired["natural_reply"]).split("\n报道乙")[0]
    elif failure == "shortened_url":
        invalid["natural_reply"] = str(repaired["natural_reply"]).replace("www.example", "example")
    elif failure == "unknown_id":
        invalid["source_ids"] = ["invented_id"]
    elif failure == "missing_classification":
        invalid.pop("source_answer")
        invalid["natural_reply"] = "此前参考的是报道甲和报道乙。"
    elif failure == "wrong_classification":
        invalid.update(source_answer=False, natural_reply="此前参考的是报道甲和报道乙。")
    elif failure == "new_data":
        invalid["requires_new_data"] = True
    elif failure == "wrong_temporal_status":
        invalid["source_temporal_status"] = "dated"

    class InvalidThenRepaired(_RecordingTransport):
        async def generate_json(
            self, submitted: CandidateTransportRequest,
        ) -> CandidateTransportResponse:
            self.requests.append(submitted)
            return cast(
                CandidateTransportResponse, invalid if len(self.requests) == 1 else repaired,
            )

    transport = InvalidThenRepaired(None)
    result = await VibeClarificationDialogueRouter(
        transport, capability_matrix=capability_matrix,
    ).assess(request)
    assert result is not None and result.natural_reply == repaired["natural_reply"]
    assert len(transport.requests) == 2
    assert transport.requests[1].user_payload["research"] == transport.requests[0].user_payload[
        "research"
    ]
    assert transport.requests[1].user_payload["sourceAnswerRepair"]["reason"].startswith(
        "source_answer_"
    )


@pytest.mark.asyncio
async def test_source_answer_cannot_accept_empty_citations_after_repair(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    transport = _RecordingTransport({
        "reply_kind": "question", "acknowledgement_id": "answer_question",
        "natural_reply": "此前参考的是报道甲和报道乙。", "source_answer": True, "source_ids": [],
    })
    result = await VibeClarificationDialogueRouter(
        transport, capability_matrix=capability_matrix,
    ).assess(_source_request())
    assert result is None and len(transport.requests) == 2


@pytest.mark.asyncio
async def test_source_repair_and_transport_retry_share_a_single_retry_budget(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    class InterruptedThenMissing(_RecordingTransport):
        async def generate_json(
            self, request: CandidateTransportRequest,
        ) -> CandidateTransportResponse:
            if not self.requests:
                self.requests.append(request)
                raise CandidateTransportError("interrupted")
            return await super().generate_json(request)

    transport = InterruptedThenMissing({
        "reply_kind": "question", "acknowledgement_id": "answer_question",
        "natural_reply": "此前参考的是报道甲和报道乙。", "source_answer": True, "source_ids": [],
    })
    result = await VibeClarificationDialogueRouter(
        transport, capability_matrix=capability_matrix,
    ).assess(_source_request())
    assert result is None and len(transport.requests) == 2


@pytest.mark.asyncio
async def test_source_request_without_research_can_admit_no_evidence_without_inventing_urls(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    reply = "这次没有可提供的检索来源。"
    transport = _RecordingTransport({
        "reply_kind": "question", "acknowledgement_id": "answer_question",
        "natural_reply": reply, "source_answer": True, "source_ids": [],
        "source_temporal_status": "unverified",
    })
    result = await VibeClarificationDialogueRouter(
        transport, capability_matrix=capability_matrix,
    ).assess(replace(_source_request(), research=None))
    assert result is not None and result.natural_reply == reply


@pytest.mark.asyncio
async def test_ordinary_question_with_research_does_not_require_citations_or_extra_call(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    reply = "你想什么时候卖出？"
    transport = _RecordingTransport({
        "reply_kind": "question", "acknowledgement_id": "answer_question",
        "natural_reply": reply, "source_answer": False, "source_ids": [],
    })
    result = await VibeClarificationDialogueRouter(
        transport, capability_matrix=capability_matrix,
    ).assess(replace(_source_request(), answer="卖出还需要补什么？"))
    assert result is not None and result.natural_reply == reply
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_ordinary_reply_keeps_its_original_short_length_limit(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    transport = _RecordingTransport({
        "reply_kind": "question", "acknowledgement_id": "answer_question",
        "natural_reply": "什么时候卖出。" * 60, "source_answer": False, "source_ids": [],
    })
    result = await VibeClarificationDialogueRouter(
        transport, capability_matrix=capability_matrix,
    ).assess(_request())
    assert result is None and len(transport.requests) == 1


@pytest.mark.asyncio
async def test_interrupted_dialogue_transport_retries_once_and_returns_model_reply_verbatim(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    reply = "你想什么时候卖出？"

    class InterruptedOnce(_RecordingTransport):
        async def generate_json(
            self, request: CandidateTransportRequest,
        ) -> CandidateTransportResponse:
            if not self.requests:
                self.requests.append(request)
                raise CandidateTransportError("stream interrupted")
            return await super().generate_json(request)

    transport = InterruptedOnce({
        "reply_kind": "question", "acknowledgement_id": "answer_question",
        "natural_reply": reply, "recommended_option_ids": [],
    })
    router = VibeClarificationDialogueRouter(transport, capability_matrix=capability_matrix)
    result = await router.assess(_request())
    assert result is not None and result.natural_reply == reply
    assert len(transport.requests) == 2
    assert transport.requests[0] == replace(
        transport.requests[1], system_footer=transport.requests[0].system_footer,
    )
    assert "invalid JSON" in (transport.requests[1].system_footer or "")


@pytest.mark.asyncio
async def test_verified_candidate_reply_can_offer_switching_to_the_users_own_stock(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    reply = "可以先用东方财富，也可以换成自己的股票。你想用哪只？"
    transport = _RecordingTransport({
        "reply_kind": "question", "acknowledgement_id": "answer_question",
        "natural_reply": reply, "recommended_option_ids": [],
    })
    router = VibeClarificationDialogueRouter(transport, capability_matrix=capability_matrix)
    result = await router.assess(replace(
        _request(), answer="帮我挑一只股票", response_only=True, question="",
        context_summary="已核验的候选股票：东方财富；用户也可使用自己的股票。",
    ))
    assert result is not None and result.natural_reply == reply
    assert len(transport.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("repaired", [True, False])
async def test_response_only_numeric_repair_uses_original_facts_and_stops_after_two_calls(
    capability_matrix: CandidateCapabilityMatrix, repaired: bool,
) -> None:
    original_context = "东方财富成交额为13708345678元，数据日期为2026-09-04。"
    invalid_reply = "东方财富成交额为137.08亿元。"
    corrected_reply = "东方财富在2026-09-04的成交额为13708345678元。"
    request = replace(
        _request(), answer="查询东方财富最近一个交易日的成交额。",
        prior_utterance="", question="", options=(), response_only=True,
        context_summary=original_context,
    )

    class NumericReplySequence(_RecordingTransport):
        async def generate_json(
            self, submitted: CandidateTransportRequest,
        ) -> CandidateTransportResponse:
            self.requests.append(submitted)
            assert len(self.requests) <= 2, "grounding repair must not request a third response"
            reply = corrected_reply if repaired and len(self.requests) == 2 else invalid_reply
            return {
                "reply_kind": "unclear", "acknowledgement_id": "ask_rephrase",
                "natural_reply": reply,
            }

    transport = NumericReplySequence(None)
    result = await VibeClarificationDialogueRouter(
        transport, capability_matrix=capability_matrix,
    ).assess(request)

    assert len(transport.requests) == 2
    if repaired:
        assert result is not None and result.natural_reply == corrected_reply
    else:
        # Repeating the rejected reply must not promote it into a trusted fact.
        assert result is None
    first_payload = transport.requests[0].user_payload
    second_payload = transport.requests[1].user_payload
    assert first_payload is not None and second_payload is not None
    for key in (
        "answer", "priorUtterance", "question", "contextSummary", "allowedOptions",
        "recentTurns", "research",
    ):
        assert first_payload[key] == second_payload[key]
        assert "137.08" not in str(second_payload[key])
    assert second_payload["contextSummary"] == original_context
    assert second_payload["responseOnly"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("repaired", [True, False])
async def test_response_only_security_name_must_match_supplied_code_after_one_repair(
    capability_matrix: CandidateCapabilityMatrix, repaired: bool,
) -> None:
    wrong_reply = "中芯集成（688825）成交额为500元。"
    correct_reply = "长鑫科技（688825）成交额为500元。"
    request = replace(
        _request(), answer="列出股票名称、代码和成交额。", prior_utterance="",
        question="", options=(), response_only=True,
        context_summary="真实查询返回：688825，长鑫科技，成交额500元。",
        verified_instruments=(("688825", "长鑫科技"),),
    )

    class SecurityReplySequence(_RecordingTransport):
        async def generate_json(
            self, submitted: CandidateTransportRequest,
        ) -> CandidateTransportResponse:
            self.requests.append(submitted)
            assert len(self.requests) <= 2
            return {
                "reply_kind": "unclear", "acknowledgement_id": "ask_rephrase",
                "natural_reply": correct_reply
                if repaired and len(self.requests) == 2 else wrong_reply,
            }

    transport = SecurityReplySequence(None)
    result = await VibeClarificationDialogueRouter(
        transport, capability_matrix=capability_matrix,
    ).assess(request)

    assert len(transport.requests) == 2
    if repaired:
        assert result is not None and result.natural_reply == correct_reply
    else:
        assert result is None
    first_payload = transport.requests[0].user_payload
    assert first_payload is not None
    assert "question为空时不要提出任何新问题" in transport.requests[0].system_contract
    assert "不要求用户重述已经明确的字段或日期口径" in transport.requests[0].system_contract
    for submitted in transport.requests:
        assert submitted.user_payload is not None
        assert submitted.user_payload["verifiedInstruments"] == [
            {"code": "688825", "name": "长鑫科技"},
        ]
        assert submitted.user_payload["contextSummary"] == request.context_summary
        assert "中芯集成" not in str(submitted.user_payload)
    assert transport.requests[1].user_payload == first_payload


@pytest.mark.asyncio
async def test_unselected_directions_keep_persona_and_do_not_request_another_choice(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    context = "用户尚未选择任何策略。方向理解：借秦始皇的果断劲儿，先给可修改的趋势方案。"
    reply = "借秦始皇的果断劲儿，先给你可修改的趋势方案，下面的方向都可以继续调整。"
    transport = _RecordingTransport({
        "reply_kind": "unclear", "acknowledgement_id": "ask_rephrase",
        "natural_reply": reply, "recommended_option_ids": [],
    })
    result = await VibeClarificationDialogueRouter(
        transport, capability_matrix=capability_matrix,
    ).assess(replace(
        _request(), answer="我是秦始皇", prior_utterance="", options=(),
        response_only=True, question="", context_summary=context,
    ))
    assert result is not None and result.natural_reply == reply
    assert result.selected_option_id is None and not result.instrument_selected
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.user_payload is not None and request.user_payload["contextSummary"] == context
    assert "列表首项也不代表用户的选择" in request.system_contract
    assert "保留人物、比喻或风格与策略方向的联系" in request.system_contract
    assert "question为空时不要提出任何新问题" in request.system_contract


@pytest.mark.asyncio
async def test_selected_strategy_stock_reply_uses_current_facts_and_keeps_full_model_text(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    reply = (
        "那就沿着放量创高突破这条思路试试：生益科技成交更活跃，适合观察量价配合；"
        "东方财富价格在20日均线上方，可检验趋势延续；平安银行波动较小，可作对照。"
        "这些只是待测候选，尚未回测，你想选哪只，也可以直接输入自己的股票？"
    )
    assert 80 <= len(reply) <= 140
    context = (
        "用户选了放量创高突破。买入为放量且收盘价创20日新高，卖出为跌破20日均线。"
        "当前只完成真实选股，尚未执行回测。候选事实：生益科技成交额高于另外两只，"
        "适合观察量价配合；东方财富价格在20日均线上方，可观察趋势延续；"
        "平安银行波动较小，可作对照。用户也可以直接输入自己的股票。"
    )
    transport = _RecordingTransport({
        "reply_kind": "unclear", "acknowledgement_id": "ask_rephrase",
        "natural_reply": reply, "recommended_option_ids": [],
    })
    result = await VibeClarificationDialogueRouter(
        transport, capability_matrix=capability_matrix,
    ).assess(replace(
        _request(), answer="放量创高突破", prior_utterance="", options=(),
        response_only=True, question="想用哪只股票试试？", context_summary=context,
    ))

    assert result is not None and result.natural_reply == reply
    assert result.selected_option_id is None and not result.instrument_selected
    assert result.strategy_inspiration is None and not result.requires_new_data
    assert len(transport.requests) == 1
    submitted = transport.requests[0]
    assert submitted.user_payload is not None
    assert submitted.user_payload["contextSummary"] == context
    assert "完整回复80–140字" in submitted.system_contract
    assert "自然承接给定的所选策略方向" in submitted.system_contract
    assert "每只保留名称及一项有依据的量价或均线等特征" in submitted.system_contract
    assert "不能说根据回测结果推荐、效果更好或收益更优" in submitted.system_contract
    assert "也可直接输入自己的股票" in submitted.system_contract


@pytest.mark.asyncio
@pytest.mark.parametrize("response_only,name,accepted", [
    (False, "生益科技", True), (True, "生益科技", False), (False, None, False),
])
async def test_explicit_stock_selection_is_model_authored_and_requires_current_name(
    capability_matrix: CandidateCapabilityMatrix,
    response_only: bool, name: str | None, accepted: bool,
) -> None:
    transport = _RecordingTransport({
        "reply_kind": "preference", "acknowledgement_id": "respect_preference",
        "natural_reply": "生益科技已收到。", "instrument_name": name,
        "instrument_selected": True,
    })
    router = VibeClarificationDialogueRouter(transport, capability_matrix=capability_matrix)
    result = await router.assess(replace(
        _request(), answer="用生益科技。", response_only=response_only,
        context_summary="等待用户选择股票，原策略方向已提供。",
    ))
    assert (result is not None) is accepted
    if result is not None:
        assert result.instrument_selected and result.instrument_name == name


@pytest.mark.asyncio
@pytest.mark.parametrize(("run_requested", "evidence", "response_only", "accepted"), [
    (False, "先别跑", False, True),
    (True, "立即回测", False, False),
    (True, None, False, False),
    (False, "先别跑", True, False),
])
async def test_stock_confirmation_run_intent_requires_exact_current_evidence(
    capability_matrix: CandidateCapabilityMatrix,
    run_requested: bool, evidence: str | None, response_only: bool, accepted: bool,
) -> None:
    transport = _RecordingTransport({
        "reply_kind": "preference", "acknowledgement_id": "respect_preference",
        "natural_reply": "收到，先准备东方财富的规则。", "instrument_name": "东方财富",
        "instrument_selected": True, "run_requested": run_requested,
        "run_request_evidence": evidence,
    })
    result = await VibeClarificationDialogueRouter(
        transport, capability_matrix=capability_matrix,
    ).assess(replace(
        _request(), answer="东方财富，先别跑", options=(), response_only=response_only,
    ))
    assert (result is not None) is accepted
    if result is not None:
        assert result.instrument_name == "东方财富" and result.instrument_selected
        assert result.run_requested is False
        assert result.run_request_evidence == "先别跑"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reply", "accepted"),
    [("可以试试有止损的短线思路。", True), ("可以试试有8%止损的短线思路。", False)],
)
async def test_creative_style_can_mention_generic_exits_but_not_invent_numeric_thresholds(
    capability_matrix: CandidateCapabilityMatrix, reply: str, accepted: bool,
) -> None:
    transport = _RecordingTransport({
        "reply_kind": "preference", "acknowledgement_id": "respect_preference",
        "natural_reply": reply, "recommended_option_ids": [],
        "strategy_inspiration": "将人物意象作为待验证的短线风格灵感。",
    })
    router = VibeClarificationDialogueRouter(transport, capability_matrix=capability_matrix)
    result = await router.assess(replace(
        _request(), answer="我是秦始皇", prior_utterance="", question="",
        context_summary="", options=(),
    ))
    assert (result is not None) is accepted
    if result is not None:
        assert result.natural_reply == reply and result.strategy_inspiration is not None
    assert len(transport.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["东方财富", "贵州茅台"])
async def test_style_inspiration_is_non_executable_and_stock_name_must_match_this_turn(
    capability_matrix: CandidateCapabilityMatrix,
    name: str,
) -> None:
    router = VibeClarificationDialogueRouter(
        _RecordingTransport(
            {
                "reply_kind": "question",
                "acknowledgement_id": "answer_question",
                "natural_reply": "我们可以把这个比喻作为待验证的交易风格。",
                "recommended_option_ids": [],
                "strategy_inspiration": "如果想表达更积极的短线风格，可以探索动量与明确风险退出。",
                "instrument_name": name,
            }
        ),
        capability_matrix=capability_matrix,
    )
    result = await router.assess(replace(_request(), answer="好吧，东方财富怎么交易"))
    if name != "东方财富":
        assert result is None
    else:
        assert result is not None
        assert result.strategy_inspiration is not None
        assert result.instrument_name == name


@pytest.mark.asyncio
async def test_dialogue_provider_can_only_acknowledge_and_rank_server_option_ids(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    transport = _RecordingTransport(
        {
            "reply_kind": "off_topic",
            "acknowledgement_id": "light_redirect",
            "natural_reply": "我知道你是在开玩笑，这句先不放进策略里。",
            "recommended_option_ids": ["idea_000000000002", "idea_000000000001"],
        }
    )
    router = VibeClarificationDialogueRouter(
        transport,
        capability_matrix=capability_matrix,
    )

    result = await router.assess(_request())

    assert result is not None
    assert result.reply_kind == "off_topic"
    assert result.natural_reply == "我知道你是在开玩笑，这句先不放进策略里。"
    assert result.recommended_option_ids == (
        "idea_000000000002",
        "idea_000000000001",
    )
    request = transport.requests[0]
    schema = cast(dict[str, object], request.response_schema)
    properties = cast(dict[str, object], schema["properties"])
    assert request.response_schema_name == "ashare_clarification_dialogue"
    assert schema["additionalProperties"] is False
    assert "strategy" not in properties


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {
            "reply_kind": "off_topic",
            "acknowledgement_id": "light_redirect",
            "natural_reply": "这句先不放进策略里。",
            "acknowledgement": "东方财富300059用PE进场",
            "recommended_option_ids": [],
        },
        {
            "reply_kind": "preference",
            "acknowledgement_id": "respect_preference",
            "natural_reply": "我会保留你的偏好，但不会替你补规则。",
            "recommended_option_ids": ["idea_not_server_owned"],
        },
        {
            "reply_kind": "question",
            "acknowledgement_id": "answer_question",
            "natural_reply": "我先回答你的问题，不把它当成确认。",
            "recommended_option_ids": [],
            "dsl": {"executable": True},
        },
        {
            "reply_kind": "off_topic",
            "acknowledgement_id": "light_redirect",
            "natural_reply": "建议在18元立即买入。",
            "recommended_option_ids": [],
        },
        {
            "reply_kind": "preference",
            "acknowledgement_id": "respect_preference",
            "natural_reply": "我理解你想用RSI超卖。",
            "recommended_option_ids": [],
        },
        {
            "reply_kind": "preference",
            "acknowledgement_id": "respect_preference",
            "natural_reply": "我理解你说的股票是贵州茅台。",
            "recommended_option_ids": [],
        },
        {
            "reply_kind": "preference",
            "acknowledgement_id": "respect_preference",
            "natural_reply": "我理解你的阈值是八个点。",
            "recommended_option_ids": [],
        },
        {
            "reply_kind": "preference",
            "acknowledgement_id": "respect_preference",
            "natural_reply": "我理解你已经确定要持有到期卖出。",
            "recommended_option_ids": [],
        },
        {
            "reply_kind": "unclear",
            "acknowledgement_id": "ask_rephrase",
            "natural_reply": "。。",
            "recommended_option_ids": [],
        },
        {
            "reply_kind": "unclear",
            "acknowledgement_id": "ask_rephrase",
            "natural_reply": "我会保留你已经说清的部分，接下来只差卖出条件。",
            "recommended_option_ids": [],
        },
        {
            "reply_kind": "off_topic",
            "acknowledgement_id": "light_redirect",
            "natural_reply": "我会把这句当作无关消息处理。",
            "recommended_option_ids": [],
        },
        {
            "reply_kind": "unclear",
            "acknowledgement_id": "ask_rephrase",
            "natural_reply": "你可以从下面选，也可以在输入框里告诉我。",
            "recommended_option_ids": [],
        },
        {
            "reply_kind": "preference",
            "acknowledgement_id": "respect_preference",
            "natural_reply": "我理解你想用MACD死叉买入。",
            "recommended_option_ids": [],
        },
        {
            "reply_kind": "question",
            "acknowledgement_id": "answer_question",
            "natural_reply": "你想在收益达到8%时卖出吗？",
            "recommended_option_ids": [],
        },
        {
            "reply_kind": "question",
            "acknowledgement_id": "answer_question",
            "natural_reply": "什么时候卖？准备持有多久？",
            "recommended_option_ids": [],
        },
    ],
)
async def test_dialogue_provider_strategy_authority_is_rejected(
    capability_matrix: CandidateCapabilityMatrix,
    payload: dict[str, object],
) -> None:
    router = VibeClarificationDialogueRouter(
        _RecordingTransport(payload),
        capability_matrix=capability_matrix,
    )

    assert await router.assess(_request()) is None


@pytest.mark.asyncio
async def test_dialogue_provider_reply_may_reference_only_server_supplied_strategy_facts(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    router = VibeClarificationDialogueRouter(
        _RecordingTransport(
            {
                "reply_kind": "question",
                "acknowledgement_id": "answer_question",
                "natural_reply": "你是在确认MACD是否适合作为卖出条件，我先不把提问当成决定。",
                "recommended_option_ids": [],
            }
        ),
        capability_matrix=capability_matrix,
    )

    result = await router.assess(_request())

    assert result is not None
    assert "MACD" in result.natural_reply


@pytest.mark.asyncio
async def test_complete_followup_preserves_punctuation_and_can_reference_supplied_options(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    reply = "你想在动能转弱时卖出，还是持有到期再退出？"
    transport = _RecordingTransport(
        {
            "reply_kind": "question",
            "acknowledgement_id": "answer_question",
            "natural_reply": reply,
            "recommended_option_ids": ["idea_000000000001", "idea_000000000002"],
        }
    )
    router = VibeClarificationDialogueRouter(transport, capability_matrix=capability_matrix)

    result = await router.assess(_request())

    assert _request().question
    assert result is not None
    assert result.natural_reply == reply
    assert result.natural_reply.count("？") == 1
    assert _request().question not in result.natural_reply
    assert len(transport.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("inspiration", "instrument", "accepted"),
    [
        ("可以围绕更积极的短线风格探索交易方向。", None, False),
        (None, "东方财富", False),
        (None, None, True),
    ],
)
async def test_response_only_cannot_launch_inspiration_or_extract_an_instrument(
    capability_matrix: CandidateCapabilityMatrix,
    inspiration: str | None,
    instrument: str | None,
    accepted: bool,
) -> None:
    reply = "这次结果已经出来了，可以继续看看不同条件的表现。"
    transport = _RecordingTransport(
        {
            "reply_kind": "question",
            "acknowledgement_id": "answer_question",
            "natural_reply": reply,
            "recommended_option_ids": [],
            "strategy_inspiration": inspiration,
            "instrument_name": instrument,
        }
    )
    router = VibeClarificationDialogueRouter(transport, capability_matrix=capability_matrix)
    request = replace(
        _request(),
        answer="看看东方财富的结果",
        question="",
        response_only=True,
    )

    result = await router.assess(request)

    assert (result is not None) is accepted
    if result is not None:
        assert result.natural_reply == reply
        assert result.strategy_inspiration is None
        assert result.instrument_name is None
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_dialogue_provider_receives_only_the_latest_twenty_server_saved_turns(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    transport = _RecordingTransport(
        {
            "reply_kind": "off_topic",
            "acknowledgement_id": "light_redirect",
            "natural_reply": "我知道你是在开玩笑，这句先不放进策略里。",
            "recommended_option_ids": [],
        }
    )
    router = VibeClarificationDialogueRouter(
        transport,
        capability_matrix=capability_matrix,
    )
    turns = tuple(
        ClarificationDialogueTurn(
            user_text=f"user-{index}",
            assistant_text=f"assistant-{index}",
            intent="casual",
            revision=1,
            created_at=datetime(2026, 9, 3, index % 24, tzinfo=UTC),
        )
        for index in range(25)
    )

    result = await router.assess(
        ClarificationDialogueRequest(
            answer=_request().answer,
            prior_utterance=_request().prior_utterance,
            diagnostic_code=_request().diagnostic_code,
            question=_request().question,
            context_summary=_request().context_summary,
            options=_request().options,
            recent_turns=turns,
        )
    )

    assert result is not None
    payload = transport.requests[0].user_payload
    recent_turns = cast(list[dict[str, object]], payload["recentTurns"])
    assert len(recent_turns) == 20
    assert recent_turns[0]["userText"] == "user-5"
    assert recent_turns[-1]["assistantText"] == "assistant-24"


@pytest.mark.asyncio
async def test_dialogue_provider_unavailable_returns_none_for_server_fallback(
    capability_matrix: CandidateCapabilityMatrix,
) -> None:
    router = VibeClarificationDialogueRouter(
        _RecordingTransport(CandidateTransportError("timeout")),
        capability_matrix=capability_matrix,
    )

    assert await router.assess(_request()) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply,accepted",
    [
        ("这个称呼先放一边，今天想聊点什么？", True),
        ("可以在18元买入贵州茅台，你想试试吗？", False),
    ],
)
async def test_pure_conversation_can_ask_without_inventing_strategy(
    capability_matrix: CandidateCapabilityMatrix,
    reply: str,
    accepted: bool,
) -> None:
    transport = _RecordingTransport(
        {
            "reply_kind": "off_topic",
            "acknowledgement_id": "light_redirect",
            "natural_reply": reply,
            "recommended_option_ids": [],
        }
    )
    router = VibeClarificationDialogueRouter(transport, capability_matrix=capability_matrix)
    request = replace(
        _request(),
        prior_utterance="",
        question="",
        context_summary="首次对话",
        options=(),
    )
    result = await router.assess(request)
    assert (result is not None) is accepted
    if result is not None:
        assert result.natural_reply == reply
