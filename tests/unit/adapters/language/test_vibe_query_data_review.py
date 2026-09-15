from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest

from ashare_lab.adapters.language.vibe_candidates import (
    CandidateProviderIdentityView,
    CandidateTransportError,
    CandidateTransportRequest,
    CandidateTransportResponse,
    build_candidate_capability_matrix,
)
from ashare_lab.adapters.language.vibe_strategy_advice import VibeVerifiedFactStrategyAdvisor
from ashare_lab.domain.catalog import load_catalog_directory, load_coverage_catalog_directory
from ashare_lab.ports.strategy_advice import QueryDataReviewAdvisor

ROOT = Path(__file__).parents[4]
QUESTION = "查询A股最近一个交易日成交额最高的3只股票，列出股票名称和成交额。"
INITIAL_QUERY = "全部A股，按最近一个交易日成交额从高到低排序，返回前三只的名称和成交额"
RETRY_QUERY = (
    "全市场A股，按2026年9月4日单日成交额降序取前三只，"
    "返回股票名称及成交额（元），字段须注明单日日期，不使用区间成交额"
)
RANGE_COLUMN = "区间成交额(元) 2026.09.03—2026.09.04"


class _Transport:
    def __init__(self, response: object) -> None:
        self.response = response
        self.requests: list[CandidateTransportRequest] = []

    async def generate_json(self, request: CandidateTransportRequest) -> CandidateTransportResponse:
        self.requests.append(request)
        if len(self.requests) > 1:
            raise AssertionError("the review must not silently retry the model or retrieve data")
        if isinstance(self.response, CandidateTransportError):
            raise self.response
        return cast(CandidateTransportResponse, self.response)


@pytest.fixture
def advisor_factory():
    matrix = build_candidate_capability_matrix(
        load_catalog_directory(ROOT / "catalogs"),
        load_coverage_catalog_directory(ROOT / "catalogs" / "coverage"),
    )

    def make(response: object) -> tuple[VibeVerifiedFactStrategyAdvisor, _Transport]:
        transport = _Transport(response)
        return VibeVerifiedFactStrategyAdvisor(
            transport, capability_matrix=matrix,
            provider_identity=CandidateProviderIdentityView(
                provider="deepseek", model="deepseek-v4-flash",
                prompt_version="candidate.prompt.v1", schema_version="candidate.schema.v1",
            ),
        ), transport

    return make


def _screen_snapshot(*, single_day: bool = False) -> Mapping[str, object]:
    column = "单日成交额(元) 2026.09.04" if single_day else RANGE_COLUMN
    return {
        "kind": "screen", "asset_type": "stock",
        "columns": ["名称", column],
        "rows": [{"名称": "东方财富", column: 300}, {"名称": "同花顺", column: 200},
                 {"名称": "平安银行", column: 100}],
        "row_count": 3,
        "scope": "全部A股",
        "ranking": f"按{column}降序取前三名",
        "retrieved_at": "2026-09-06T10:20:30Z",
    }


def _retry_payload(**overrides: object) -> dict[str, object]:
    return {
        "satisfied": False, "evidence": [RANGE_COLUMN], "retry_query": RETRY_QUERY,
        "message": "这次返回的是区间成交额，我继续核对单日口径并重新筛选全市场。",
        **overrides,
    }


@pytest.mark.asyncio
async def test_query_review_accepts_actual_single_day_ranking_and_preserves_snapshot(
    advisor_factory,
) -> None:
    snapshot = _screen_snapshot(single_day=True)
    advisor, transport = advisor_factory({
        "satisfied": True,
        "evidence": ["全部A股", "按单日成交额(元) 2026.09.04降序取前三名"],
        "retry_query": None, "message": "这次返回的数据符合所需的单日口径和排名范围。",
    })

    assert isinstance(advisor, QueryDataReviewAdvisor)
    result = await advisor.review_query_result(
        question=QUESTION, data_snapshot=snapshot, previous_queries=(INITIAL_QUERY,),
    )

    assert result is not None and result.satisfied and result.retry_query is None
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.response_schema_name == "verified_query_data_review"
    assert request.response_schema["additionalProperties"] is False
    properties = cast(dict[str, dict[str, object]], request.response_schema["properties"])
    evidence_items = cast(dict[str, object], properties["evidence"]["items"])
    choices = cast(list[str], evidence_items["enum"])
    assert "按单日成交额(元) 2026.09.04降序取前三名" in choices
    assert "2026-09-06T10:20:30Z" not in choices
    assert INITIAL_QUERY not in choices
    assert len(choices) == len(set(choices))
    assert all(2 <= len(item) <= 160 for item in choices)
    assert request.user_payload == {
        "question": QUESTION, "dataSnapshot": snapshot,
        "previousQueries": [INITIAL_QUERY], "remainingDataRounds": 1,
    }
    assert "抓取时间" in request.system_contract
    assert "同一资产宇宙" in request.system_contract
    assert "不能只查原来的几只股票" in request.system_contract
    assert result.strategy_requested is False
    assert properties["strategy_requested"]["type"] == "boolean"


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy_requested", [False, True])
async def test_query_review_reports_explicit_strategy_intent_without_another_model_call(
    advisor_factory, strategy_requested: bool,
) -> None:
    question = "查询东方财富最新价，并分析两种交易策略" if strategy_requested else (
        "查询东方财富最新价"
    )
    advisor, transport = advisor_factory({
        "satisfied": True, "evidence": ["最新价(元)"], "retry_query": None,
        "message": "东方财富最新价为19.15元。", "strategy_requested": strategy_requested,
    })
    result = await advisor.review_query_result(
        question=question, data_snapshot={
            "kind": "finance", "columns": ["名称", "最新价(元)"],
            "rows": [{"名称": "东方财富", "最新价(元)": "19.15"}],
        },
    )
    assert result is not None and result.strategy_requested is strategy_requested
    assert len(transport.requests) == 1
    assert "明确同时要求" in transport.requests[0].system_contract
    assert "历史会话含策略" in transport.requests[0].system_contract


@pytest.mark.asyncio
async def test_query_review_returns_one_new_full_market_query_for_period_mismatch(
    advisor_factory,
) -> None:
    advisor, transport = advisor_factory(_retry_payload())
    result = await advisor.review_query_result(
        question=QUESTION, data_snapshot=_screen_snapshot(),
        previous_queries=(INITIAL_QUERY,), remaining_data_rounds=1,
    )

    assert result is not None and not result.satisfied
    assert result.retry_query == RETRY_QUERY
    assert result.evidence == (RANGE_COLUMN,)
    assert result.message == "这次返回的是区间成交额，我继续核对单日口径并重新筛选全市场。"
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_satisfied_query_review_answers_three_stocks_with_original_dates_and_numbers(
    advisor_factory,
) -> None:
    column = "单日成交额(亿元) 2026.09.04"
    reply = (
        "2026.09.04全市场A股成交额前三名依次是：中际旭创（300308）173.780000亿元、"
        "浪潮信息（000977）137.080000亿元、中国船舶（600150）123.930000亿元。"
    )
    snapshot: Mapping[str, object] = {
        "kind": "screen", "asset_type": "stock", "columns": ["代码", "名称", column],
        "rows": [
            {"代码": "300308", "名称": "中际旭创", column: "173.780000"},
            {"代码": "000977", "名称": "浪潮信息", column: "137.080000"},
            {"代码": "600150", "名称": "中国船舶", column: "123.930000"},
        ],
        "provider_metadata": {"totalCondition": {"describe": "全部A股单日成交额降序前三名"}},
        "row_count": 3,
    }
    advisor, transport = advisor_factory({
        "satisfied": True, "evidence": [column, "全部A股单日成交额降序前三名"],
        "retry_query": None, "message": reply,
    })
    result = await advisor.review_query_result(
        question=QUESTION, data_snapshot=snapshot,
        previous_queries=(INITIAL_QUERY, RETRY_QUERY), remaining_data_rounds=0,
    )

    assert result is not None and result.satisfied and result.message == reply
    assert result.retry_query is None and len(transport.requests) == 1
    properties = cast(
        dict[str, dict[str, object]], transport.requests[0].response_schema["properties"],
    )
    assert properties["message"]["maxLength"] == 320
    assert "不会再有其他模型改写或拼接" in transport.requests[0].system_contract


@pytest.mark.asyncio
async def test_query_review_exhausted_budget_keeps_actual_finance_scope_and_no_retry(
    advisor_factory,
) -> None:
    message = "尚未取得所需的单日成交额，本次实际返回的是区间数据。"
    advisor, transport = advisor_factory(_retry_payload(retry_query=None, message=message))
    result = await advisor.review_query_result(
        question="查询东方财富最近一个交易日成交额。",
        data_snapshot={
            "kind": "finance", "table_count": 1,
            "tables": [{"columns": [RANGE_COLUMN], "first_row": {RANGE_COLUMN: 300}}],
            "retrieved_at": "2026-09-06T10:20:30Z",
        },
        previous_queries=(INITIAL_QUERY, RETRY_QUERY), remaining_data_rounds=0,
    )

    assert result is not None and not result.satisfied and result.retry_query is None
    assert result.message == message
    properties = cast(
        dict[str, dict[str, object]], transport.requests[0].response_schema["properties"],
    )
    assert properties["retry_query"] == {"type": "null"}
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_query_review_rejects_retrieval_timestamp_as_data_evidence(advisor_factory) -> None:
    advisor, transport = advisor_factory({
        "satisfied": True, "evidence": ["2026-09-06T10:20:30Z"],
        "retry_query": None, "message": "已核对单日口径。",
    })
    result = await advisor.review_query_result(question=QUESTION, data_snapshot=_screen_snapshot())

    assert result is None
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_query_review_rejects_same_query_with_only_spacing_or_punctuation_changes(
    advisor_factory,
) -> None:
    advisor, transport = advisor_factory(_retry_payload(
        retry_query=" 全部 A 股，按最近一个交易日成交额从高到低排序；返回前三只的名称和成交额。",
    ))
    result = await advisor.review_query_result(
        question=QUESTION, data_snapshot=_screen_snapshot(), previous_queries=(INITIAL_QUERY,),
    )

    assert result is None
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_query_review_fails_closed_on_local_contract_and_transport_errors(
    advisor_factory,
) -> None:
    # Each response exercises one rejection boundary; no retry or provider fallback is permitted.
    responses = (
        CandidateTransportError("unit-test-only"),
        _retry_payload(satisfied="false"),
        _retry_payload(strategy_requested="true"),
        _retry_payload(evidence=["单日成交额 2026.09.04"]),
        _retry_payload(retry_query="请访问https://example.test/data查询成交额"),
        _retry_payload(retry_query="SELECT name FROM stocks ORDER BY amount DESC"),
        _retry_payload(retry_query="查询完成后调用交易工具下单买入东方财富"),
        _retry_payload(message="成交额已核对为999亿元。"),
        _retry_payload(message="回测已经完成。"),
        _retry_payload(satisfied=True),
    )
    for response in responses:
        advisor, transport = advisor_factory(response)
        result = await advisor.review_query_result(
            question=QUESTION, data_snapshot=_screen_snapshot(), previous_queries=(INITIAL_QUERY,),
        )
        assert result is None
        assert len(transport.requests) == 1

    advisor, transport = advisor_factory(_retry_payload())
    assert await advisor.review_query_result(
        question=QUESTION, data_snapshot=_screen_snapshot(), remaining_data_rounds=0,
    ) is None
    assert len(transport.requests) == 1
