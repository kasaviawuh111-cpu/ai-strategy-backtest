from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

import httpx
import pytest

from ashare_lab.adapters.language.openai_compatible import (
    CandidateProviderTransportError,
    OpenAICompatibleCandidateTransport,
)
from ashare_lab.adapters.language.vibe_candidates import (
    CandidateProviderIdentityView,
    CandidateTransportError,
    CandidateTransportRequest,
    CandidateTransportResponse,
    build_candidate_capability_matrix,
)
from ashare_lab.adapters.language.vibe_strategy_advice import (
    VibeVerifiedFactStrategyAdvisor,
)
from ashare_lab.domain.catalog import load_catalog_directory, load_coverage_catalog_directory
from ashare_lab.ports.idea_routing import IdeaProposal
from ashare_lab.ports.live_market_data import (
    LiveFinanceDataResult,
    LiveMarketDataProvenance,
    LiveMarketDataResult,
)
from ashare_lab.ports.strategy_advice import (
    StockRecommendationAdvisor,
    StockStrategyDataRequest,
    StockStrategyPairingAdvisor,
    VerifiedFactStrategyAdviceRequest,
)

ROOT = Path(__file__).parents[4]


def _decode_prompt_tables(value: object) -> object:
    if isinstance(value, dict):
        table = cast(dict[str, object], value)
        if table.get("encoding") == "columnar.v1":
            return [
                dict(zip(cast(list[str], table["columns"]),
                         map(_decode_prompt_tables, row), strict=True))
                for row in cast(list[list[object]], table["rows"])
            ]
        return {key: _decode_prompt_tables(item) for key, item in table.items()}
    if isinstance(value, list):
        return [_decode_prompt_tables(item) for item in cast(list[object], value)]
    return value


class _Transport:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.requests: list[CandidateTransportRequest] = []

    async def generate_json(
        self,
        request: CandidateTransportRequest,
    ) -> CandidateTransportResponse:
        self.requests.append(request)
        response = self.responses[min(len(self.requests) - 1, len(self.responses) - 1)]
        if isinstance(response, CandidateTransportError):
            raise response
        return cast(CandidateTransportResponse, response)


def _payload() -> dict[str, object]:
    return {
        "analysis": "现价只是一个当前切片，可以分别验证趋势和超跌反转。",
        "hypothesis": "不同参数对这只股票的历史价格路径会给出不同交易节奏。",
        "proposals": [
            {
                "title": "短周期趋势",
                "hypothesis": "用10日线验证较快的趋势反应。",
                "entry_summary": "股价上穿10日均线",
                "exit_summary": "股价跌破10日均线",
                "suggested_utterance": "股价上穿10日均线买入，跌破10日均线卖出，回测近1年",
            },
            {
                "title": "深度超跌",
                "hypothesis": "用更严格的RSI阈值减少一般波动触发。",
                "entry_summary": "RSI低于25",
                "exit_summary": "RSI高于65",
                "suggested_utterance": "RSI低于25买入，高于65卖出，回测近1年",
            },
        ],
    }


@pytest.mark.asyncio
async def test_verified_facts_and_capability_matrix_are_sent_to_bounded_model() -> None:
    transport = _Transport([_payload()])
    matrix = build_candidate_capability_matrix(
        load_catalog_directory(ROOT / "catalogs"),
        load_coverage_catalog_directory(ROOT / "catalogs" / "coverage"),
    )
    advisor = VibeVerifiedFactStrategyAdvisor(
        transport,
        capability_matrix=matrix,
        provider_identity=CandidateProviderIdentityView(
            provider="deepseek",
            model="deepseek-v4-flash",
            prompt_version="candidate.prompt.v1",
            schema_version="candidate.schema.v1",
        ),
    )

    result = await advisor.advise(
        VerifiedFactStrategyAdviceRequest(
            original_utterance="东方财富现价，再给我两种策略方向",
            instrument_symbol="300059.SZ",
            as_of_date=date(2026, 9, 4),
            verified_facts=("东方财富：最新价=19.15",),
        )
    )

    assert result is not None
    assert result.provider == "deepseek"
    assert [item.title for item in result.proposals] == ["短周期趋势", "深度超跌"]
    request = transport.requests[0]
    assert request.user_payload is not None
    assert request.user_payload["verifiedFacts"] == ["东方财富：最新价=19.15"]
    assert request.response_schema["additionalProperties"] is False
    assert "参数可以" in request.system_contract
    assert request.system_footer is not None
    assert request.json_object_contract is not None
    assert "not source" in request.json_object_contract


@pytest.mark.asyncio
async def test_data_only_analysis_is_preserved_with_empty_hypothesis_and_proposals() -> None:
    analysis = "东方财富2026-09-04收盘价19.15元，成交额48.54亿元，换手率1.881%。"
    transport = _Transport([{"analysis": analysis, "hypothesis": "", "proposals": []}])
    matrix = build_candidate_capability_matrix(
        load_catalog_directory(ROOT / "catalogs"),
        load_coverage_catalog_directory(ROOT / "catalogs" / "coverage"),
    )
    advisor = VibeVerifiedFactStrategyAdvisor(
        transport, capability_matrix=matrix,
        provider_identity=CandidateProviderIdentityView(
            provider="deepseek", model="deepseek-v4-flash",
            prompt_version="candidate.prompt.v1", schema_version="candidate.schema.v1",
        ),
    )
    facts = (
        "东方财富：数据日期=2026-09-04", "东方财富：收盘价(元)=19.15",
        "东方财富：成交额(亿元)=48.54", "东方财富：换手率(%)=1.881",
    )
    result = await advisor.advise(VerifiedFactStrategyAdviceRequest(
        original_utterance="东方财富最近一个交易日收盘价、成交额和换手率是多少？",
        instrument_symbol="300059.SZ", as_of_date=date(2026, 9, 5), verified_facts=facts,
    ))

    assert result is not None
    assert result.analysis == analysis
    assert result.hypothesis == "" and result.proposals == ()
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.user_payload is not None
    assert request.user_payload["verifiedFacts"] == list(facts)
    assert request.user_payload["asOfDate"] == "2026-09-05"
    properties = cast(dict[str, dict[str, object]], request.response_schema["properties"])
    assert properties["proposals"]["minItems"] == 0
    assert properties["hypothesis"].get("minLength", 0) == 0
    assert request.response_schema["additionalProperties"] is False


@pytest.mark.asyncio
async def test_incomplete_model_rules_fail_closed_without_blind_retry() -> None:
    invalid = _payload()
    cast(list[dict[str, object]], invalid["proposals"])[0]["suggested_utterance"] = (
        "股价上穿10日均线买入"
    )
    transport = _Transport([invalid, invalid])
    matrix = build_candidate_capability_matrix(
        load_catalog_directory(ROOT / "catalogs"),
        load_coverage_catalog_directory(ROOT / "catalogs" / "coverage"),
    )
    advisor = VibeVerifiedFactStrategyAdvisor(
        transport,
        capability_matrix=matrix,
        provider_identity=CandidateProviderIdentityView(
            provider="deepseek",
            model="deepseek-v4-flash",
            prompt_version="candidate.prompt.v1",
            schema_version="candidate.schema.v1",
        ),
    )

    result = await advisor.advise(
        VerifiedFactStrategyAdviceRequest(
            original_utterance="东方财富现价",
            instrument_symbol="300059.SZ",
            as_of_date=date(2026, 9, 4),
            verified_facts=("东方财富：最新价=19.15",),
        )
    )

    assert result is None
    assert len(transport.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("symbols", "expected"),
    [
        (["002594.SZ", "300059.SZ"], ["002594.SZ", "300059.SZ"]),
        (["002594.SZ", "002594"], ["002594.SZ"]),
        (["300308.SZ"], None),
        (["002594.SZ", "300059.SZ", "600519.SH", "000001.SZ"], None),
        ([], None),
    ],
)
async def test_stock_ranking_is_model_selected_and_limited_to_verified_entities(
    symbols: list[str], expected: list[str] | None
) -> None:
    transport = _Transport(
        [
            {
                "recommendations": [
                    {"symbol": symbol, "reason": "成交额更适合比较流动性。"} for symbol in symbols
                ]
            }
        ]
    )
    matrix = build_candidate_capability_matrix(
        load_catalog_directory(ROOT / "catalogs"),
        load_coverage_catalog_directory(ROOT / "catalogs" / "coverage"),
    )
    advisor = VibeVerifiedFactStrategyAdvisor(
        transport,
        capability_matrix=matrix,
        provider_identity=CandidateProviderIdentityView(
            provider="deepseek",
            model="deepseek-v4-flash",
            prompt_version="candidate.prompt.v1",
            schema_version="candidate.schema.v1",
        ),
    )
    screen = LiveMarketDataResult(
        provider="eastmoney_mx",
        query="筛选成交活跃的 A 股",
        asset_type="stock",
        columns=("代码", "名称", "成交额"),
        rows=(
            {"代码": "300059", "名称": "东方财富", "成交额": 100},
            {"代码": "600519", "名称": "贵州茅台", "成交额": 80},
            {"代码": "000001", "名称": "平安银行", "成交额": 60},
            {"代码": "002594", "名称": "比亚迪", "成交额": 120},
        ),
        provenance=LiveMarketDataProvenance(
            response_sha256="unit-test-only",
            retrieved_at=datetime(2026, 9, 5, tzinfo=UTC),
            schema_version="select-security.v1",
        ),
    )

    assert isinstance(advisor, StockRecommendationAdvisor)
    result = await advisor.recommend_stocks("推荐流动性更好的股票", screen)

    if expected is None:
        assert result is None
    else:
        assert result is not None
        assert [item.symbol for item in result] == expected
        assert result[0].name == "比亚迪"
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.response_schema_name == "verified_stock_recommendations"
    assert request.response_schema["additionalProperties"] is False
    assert request.user_payload is not None
    assert _decode_prompt_tables(request.user_payload["verifiedRows"]) == list(screen.rows)
    assert request.max_candidates == 3
    assert "用户已选策略时围绕该方向" in request.system_contract
    assert "均线上方不等于刚发生金叉" in request.system_contract
    assert "这些是当前选股数据，不是策略回测结果" in request.system_contract
    assert "不能称根据回测结果选出、策略效果更好或收益更优" in request.system_contract
    assert "research-sample.v2" in (request.system_footer or "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        None,
        "unknown_proposal",
        "unknown_stock",
        "duplicate_stock",
        "duplicate_proposal",
        "too_many",
        "extra_dsl",
        "stock_in_introduction",
        "two_questions",
        "transport",
        "empty",
    ],
)
async def test_stock_strategy_pairing_uses_one_model_call_and_only_existing_identities(
    failure: str | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO", logger="ashare_lab.adapters.language.vibe_strategy_advice")
    proposals = tuple(
        IdeaProposal(
            id=f"idea_{index:012d}",
            title=title,
            hypothesis="unit-test-only",
            entry_summary=entry,
            exit_summary=exit_rule,
            suggested_utterance=f"{entry}买入，{exit_rule}卖出",
            capability_ids=(),
            assumptions=(),
            confidence=1,
        )
        for index, (title, entry, exit_rule) in enumerate(
            [
                ("趋势跟随", "上穿10日均线", "下穿10日均线"),
                ("超跌恢复", "RSI低于25", "RSI高于65"),
                ("新高突破", "创80日新高", "跌破20日均线"),
            ]
        )
    )
    pairs = [
        {
            "proposal_id": proposals[2].id,
            "symbol": "002594.SZ",
            "reason": "成交额较高，便于观察突破后的趋势。",
        },
        {
            "proposal_id": proposals[0].id,
            "symbol": "300059.SZ",
            "reason": "成交活跃，可以检验短周期趋势。",
        },
        {
            "proposal_id": proposals[1].id,
            "symbol": "600519.SH",
            "reason": "有成交数据，可以比较超跌后的表现。",
        },
    ]
    introduction = "借秦始皇的果断劲儿，试试这些进退明确的方向？"
    payload: dict[str, object] = {"introduction": introduction, "pairs": pairs}
    if failure == "unknown_proposal":
        pairs[0]["proposal_id"] = "idea_unknown"
    elif failure == "unknown_stock":
        pairs[0]["symbol"] = "300308.SZ"
    elif failure == "duplicate_stock":
        pairs[1]["symbol"] = "002594"
    elif failure == "duplicate_proposal":
        pairs[1]["proposal_id"] = pairs[0]["proposal_id"]
    elif failure == "too_many":
        pairs.append(dict(pairs[0]))
    elif failure == "extra_dsl":
        payload["strategy"] = {"entry": "invented rule"}
    elif failure == "stock_in_introduction":
        payload["introduction"] = "可以先试试比亚迪。"
    elif failure == "two_questions":
        payload["introduction"] = "想试哪个？要马上开始吗？"
    elif failure == "empty":
        payload["pairs"] = []
    transport = _Transport(
        [
            CandidateTransportError("unit-test-only") if failure == "transport" else payload,
        ]
    )
    advisor = VibeVerifiedFactStrategyAdvisor(
        transport,
        capability_matrix=build_candidate_capability_matrix(
            load_catalog_directory(ROOT / "catalogs"),
            load_coverage_catalog_directory(ROOT / "catalogs" / "coverage"),
        ),
        provider_identity=CandidateProviderIdentityView(
            provider="deepseek",
            model="deepseek-v4-flash",
            prompt_version="candidate.prompt.v1",
            schema_version="candidate.schema.v1",
        ),
    )
    screen = LiveMarketDataResult(
        provider="eastmoney_mx",
        query="成交活跃的A股",
        asset_type="A股",
        columns=("代码", "名称", "成交额"),
        rows=(
            {"代码": "300059", "名称": "东方财富", "成交额": 30},
            {"代码": "600519", "名称": "贵州茅台", "成交额": 20},
            {"代码": "000001", "名称": "平安银行", "成交额": 10},
            {"代码": "002594", "名称": "比亚迪", "成交额": 40},
        ),
        provenance=LiveMarketDataProvenance(
            response_sha256="unit-test-only",
            retrieved_at=datetime(2026, 9, 5, tzinfo=UTC),
            schema_version="select-security.v1",
        ),
    )

    assert isinstance(advisor, StockStrategyPairingAdvisor)
    result = await advisor.pair_stock_strategies(
        "我是秦始皇",
        screen,
        proposals,
        understanding="以人物作为策略创作灵感。",
    )

    if failure:
        assert result is None
        if failure == "empty":
            assert (
                "stock_strategy_pairing_empty pair_count=0 candidates=4 proposals=3" in caplog.text
            )
    else:
        assert result is not None
        assert result.introduction == introduction
        assert [(item.proposal_id, item.symbol, item.name) for item in result.pairs] == [
            (proposals[2].id, "002594.SZ", "比亚迪"),
            (proposals[0].id, "300059.SZ", "东方财富"),
            (proposals[1].id, "600519.SH", "贵州茅台"),
        ]
        assert "stock_strategy_pairing_ready pair_count=3 candidates=4 proposals=3" in caplog.text
    assert introduction not in caplog.text
    assert "我是秦始皇" not in caplog.text
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.response_schema_name == "verified_stock_strategy_pairing"
    assert request.response_schema["additionalProperties"] is False
    assert request.user_payload is not None
    assert _decode_prompt_tables(request.user_payload["verifiedRows"]) == list(screen.rows)
    assert request.user_payload["existingProposals"] == [
        {
            "id": proposal.id,
            "title": proposal.title,
            "entrySummary": proposal.entry_summary,
            "exitSummary": proposal.exit_summary,
        }
        for proposal in proposals
    ]
    assert request.max_candidates == 3
    assert "风险承受力" in request.system_contract
    assert "不要求股票今天满足全部入场或退出条件" in request.system_contract
    assert "不能暗示更高收益" in request.system_contract



def _data_pairing_fixture(
    payload: dict[str, object],
) -> tuple[
    VibeVerifiedFactStrategyAdvisor, _Transport, LiveMarketDataResult, tuple[IdeaProposal, ...],
]:
    """Adapter fixtures only; neither a live model nor a data provider is called."""
    transport = _Transport([payload])
    advisor = VibeVerifiedFactStrategyAdvisor(
        transport,
        capability_matrix=build_candidate_capability_matrix(
            load_catalog_directory(ROOT / "catalogs"),
            load_coverage_catalog_directory(ROOT / "catalogs" / "coverage"),
        ),
        provider_identity=CandidateProviderIdentityView(
            provider="deepseek", model="deepseek-v4-flash",
            prompt_version="fixture", schema_version="fixture",
        ),
    )
    screen = LiveMarketDataResult(
        provider="eastmoney_mx_screener", query="fixture", asset_type="A股",
        columns=("代码", "名称"), rows=({"代码": "300059", "名称": "东方财富"},),
        provenance=LiveMarketDataProvenance(
            response_sha256="fixture", retrieved_at=datetime(2026, 9, 5, tzinfo=UTC),
            schema_version="fixture",
        ),
    )
    proposals = (IdeaProposal(
        id="idea_fixture", title="趋势", hypothesis="fixture",
        entry_summary="上穿20日线", exit_summary="下穿20日线",
        suggested_utterance="上穿20日线买入，下穿20日线卖出",
        capability_ids=(), assumptions=(), confidence=1,
    ),)
    return advisor, transport, screen, proposals


def _data_request_payload() -> dict[str, object]:
    return {
        "introduction": "再看一点成交信息，就可以继续比较。",
        "pairs": [],
        "data_request": {
            "symbols": ["300059.SZ"],
            "fields": ["最近交易日成交额"],
            "message": "我再补查一下成交额，继续比较这些方向。",
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source", ["pairing_screen", "recommendation_screen", "pairing_supplement"],
)
async def test_model_factory_compacts_large_tables_losslessly_below_transport_limit(
    source: str, caplog: pytest.LogCaptureFixture,
) -> None:
    """A synthetic wide provider table through real factories, not a public replay."""
    answer: dict[str, object] = {"introduction": "先比较既有方案。", "pairs": [{
        "proposal_id": "idea_fixture", "symbol": "300059.SZ", "reason": "观察成交特征。",
    }], "data_request": None}
    if source == "recommendation_screen":
        answer = {"recommendations": [{"symbol": "300059.SZ", "reason": "观察成交特征。"}]}
    advisor, capture, screen, proposals = _data_pairing_fixture(answer)
    field_names = [f"第{index}项最近20交易日成交额（人民币元，后复权口径，日期2026-09-04）"
                   for index in range(20)]
    rows = [
        {"代码": "300059", "名称": "东方财富", "日期": f"2026-09-{1 + row % 6:02d}",
         **{key: row * 100 + column if (row + column) % 3 else None
            for column, key in enumerate(field_names)}}
        for row in range(150)
    ]
    supplement = LiveFinanceDataResult(
        provider="fixture", query="fixture supplement", indicators="成交额",
        tables=({"unit": "人民币元", "retrievedDate": "2026-09-04", "rows": rows,
                 "otherTable": [{"日期": "2026-09-03"}, {"日期": None}, {}],
                 "rawTable": "原始文字和引号\"\\\n保持不变"},), provenance=screen.provenance,
    )
    snapshots = deepcopy((rows, supplement.tables))
    if source.endswith("screen"):
        screen = replace(screen, rows=tuple(rows), columns=tuple(rows[0]))
    if source == "recommendation_screen":
        result = await advisor.recommend_stocks("完整保留原条件", screen)
    else:
        result = await advisor.pair_stock_strategies(
            "完整保留原条件", screen, proposals, understanding="保留20日线买卖规则",
            supplemental_results=(supplement,) if source == "pairing_supplement" else (),
        )
    assert result is not None and len(capture.requests) == 1
    request = capture.requests[0]
    assert request.user_payload is not None
    original_payload = cast(dict[str, object], _decode_prompt_tables(request.user_payload))
    assert original_payload["utterance" if source != "recommendation_screen" else "query"] == (
        "完整保留原条件"
    )
    if source.endswith("screen"):
        assert original_payload["verifiedRows"] == rows
    else:
        assert cast(list[dict[str, object]], original_payload["supplementalData"])[0]["tables"] == (
            list(supplement.tables)
        )
    assert (rows, supplement.tables) == snapshots
    assert "columnar.v1无损编码" in request.system_contract
    sent: list[httpx.Request] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        sent.append(http_request)
        event = {"choices": [{"delta": {"content": json.dumps(answer, ensure_ascii=False)},
                              "finish_reason": "stop"}]}
        return httpx.Response(
            200, content=f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n".encode(),
            headers={"Content-Type": "text/event-stream"}, request=http_request,
        )

    provider = OpenAICompatibleCandidateTransport(
        endpoint="https://fixture.invalid/chat/completions", provider="deepseek",
        model="deepseek-v4-pro", prompt_version="fixture", schema_version="fixture",
        thinking="enabled", reasoning_effort="high", response_mode="json_object",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(CandidateProviderTransportError, match="request is too large"):
        await provider.generate_json(replace(request, user_payload=original_payload))
    assert sent == []
    assert await provider.generate_json(request) == answer
    assert len(sent) == 1 and len(sent[0].content) < 256 * 1024
    posted = json.loads(sent[0].content)
    assert posted["thinking"] == {"type": "enabled"}
    assert _decode_prompt_tables(json.loads(posted["messages"][1]["content"])) == {
        **original_payload, "responseSchema": request.response_schema,
    }
    assert f"purpose={request.response_schema_name}" in caplog.text
    assert "payload_field_bytes=" in caplog.text
    assert "完整保留原条件" not in caplog.text


@pytest.mark.asyncio
async def test_pairing_introduction_keeps_valuation_preference_and_execution_boundary() -> None:
    understanding = "保留低估值偏好，历史估值条件尚未纳入回测，先给可修改的日线反转方案。"
    reply = "低估值偏好先保留，历史估值条件尚未纳入回测；下面先给你可修改的日线反转组合。"
    advisor, transport, screen, proposals = _data_pairing_fixture({
        "introduction": reply, "pairs": [{
            "proposal_id": "idea_fixture", "symbol": "300059.SZ",
            "reason": "可以用来观察既有日线规则的研究样本。",
        }], "data_request": None,
    })
    result = await advisor.pair_stock_strategies(
        "估值过低的股票反转买", screen, proposals, understanding,
    )
    assert result is not None and result.introduction == reply
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.user_payload is not None
    assert request.user_payload["understanding"] == understanding
    assert "不能把它缩成只有选股流程或选择问题" in request.system_contract
    assert "introduction须保留这层边界" in request.system_contract
    assert "列表首项不是用户已选" in request.system_contract


@pytest.mark.asyncio
async def test_pairing_returns_model_data_request_with_unmerged_supplemental_tables() -> None:
    advisor, transport, screen, proposals = _data_pairing_fixture(_data_request_payload())
    previous = StockStrategyDataRequest(
        symbols=("300059.SZ",), fields=("最近交易日换手率",), message="fixture",
    )
    supplement = LiveFinanceDataResult(
        provider="eastmoney_mx_finance_data", query="fixture supplement",
        indicators="最近交易日换手率",
        tables=({"rawTable": "|代码|换手率(%) 2026.09.04|\n|300059|1.2|"},),
        provenance=screen.provenance,
    )
    result = await advisor.pair_stock_strategies(
        "我是秦始皇", screen, proposals, supplemental_results=(supplement,),
        previous_requests=(previous,), remaining_data_rounds=1,
        data_feedback=("换手率已返回，但成交额仍缺失。",),
    )
    assert result is not None and not result.pairs
    assert result.data_request == StockStrategyDataRequest(
        symbols=("300059.SZ",), fields=("最近交易日成交额",),
        message="我再补查一下成交额，继续比较这些方向。",
    )
    assert len(transport.requests) == 1
    payload = transport.requests[0].user_payload
    assert payload is not None
    assert payload["verifiedRows"] == list(screen.rows)
    assert payload["supplementalData"] == [{
        "provider": supplement.provider, "query": supplement.query,
        "indicators": supplement.indicators, "tables": list(supplement.tables),
        "retrievedAt": supplement.provenance.retrieved_at.isoformat(),
    }]
    assert payload["previousRequests"] == [{
        "symbols": ["300059.SZ"], "fields": ["最近交易日换手率"],
    }]
    assert payload["remainingDataRounds"] == 1
    assert payload["dataFeedback"] == ["换手率已返回，但成交额仍缺失。"]
    assert "不能重复同组股票和字段" in transport.requests[0].system_contract


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unknown_stock", "long_field", "both_pairs", "budget"])
async def test_pairing_rejects_invalid_or_over_budget_data_requests(failure: str) -> None:
    payload = _data_request_payload()
    data_request = cast(dict[str, object], payload["data_request"])
    if failure == "unknown_stock":
        data_request["symbols"] = ["600519.SH"]
    elif failure == "long_field":
        data_request["fields"] = ["长" * 81]
    elif failure == "both_pairs":
        payload["pairs"] = [{
            "proposal_id": "idea_fixture", "symbol": "300059.SZ", "reason": "fixture",
        }]
    advisor, transport, screen, proposals = _data_pairing_fixture(payload)
    result = await advisor.pair_stock_strategies(
        "我是秦始皇", screen, proposals,
        remaining_data_rounds=0 if failure == "budget" else 2,
    )
    assert result is None
    assert len(transport.requests) == 1
    if failure == "budget":
        properties = cast(dict[str, object], transport.requests[0].response_schema["properties"])
        assert properties["data_request"] == {"type": "null"}


@pytest.mark.asyncio
async def test_pairing_rejects_a_previously_attempted_lookup_even_without_results() -> None:
    advisor, transport, screen, proposals = _data_pairing_fixture(_data_request_payload())
    result = await advisor.pair_stock_strategies(
        "我是秦始皇", screen, proposals, remaining_data_rounds=1,
        previous_requests=(StockStrategyDataRequest(
            symbols=("300059",), fields=("最近交易日 成交额",), message="fixture",
        ),),
        data_feedback=("这次查数未返回结果。",),
    )
    assert result is None
    assert len(transport.requests) == 1
