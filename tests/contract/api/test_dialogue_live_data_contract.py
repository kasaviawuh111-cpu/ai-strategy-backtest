"""Contract tests for current-data answers inside the strategy conversation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from ashare_lab.api import create_app
from ashare_lab.ports.live_market_data import (
    LiveFinanceDataResult,
    LiveMarketDataProvenance,
    LiveMarketDataResult,
    LiveScreenedFinanceDataResult,
    LiveSecurityEntity,
)
from ashare_lab.ports.strategy_advice import (
    QueryDataReview,
    StockRecommendation,
    StrategyAdviceCandidate,
    VerifiedFactStrategyAdvice,
    VerifiedFactStrategyAdviceRequest,
)


class _CompilerMustNotRun:
    async def compile(self, compile_input: object) -> object:
        del compile_input
        raise AssertionError("a current-data question must bypass strategy compilation")

    async def resolve_instrument_context(self, value: str) -> str:
        assert value == "300059.SZ"
        return value

    async def compose_dialogue_response(self, **kwargs: object) -> str:
        # A response-only model is permitted; only strategy compilation is forbidden.
        assert kwargs.get("context")
        return "以下是本轮已比较的股票候选。"


class _RecordingCurrentData:
    def __init__(self) -> None:
        self.screen_calls: list[tuple[str, str]] = []
        self.finance_calls: list[tuple[str, str | None]] = []
        self.screen_finance_calls: list[tuple[str, str, str]] = []
        self.provenance = LiveMarketDataProvenance(
            response_sha256="sha256:" + "a" * 64,
            retrieved_at=datetime(2026, 9, 3, 10, 0, tzinfo=UTC),
            schema_version="test.current-data.v1",
        )

    async def screen(self, *, query: str, asset_type: str) -> LiveMarketDataResult:
        self.screen_calls.append((query, asset_type))
        return self._screen_result(query=query, asset_type=asset_type)

    async def query_finance(
        self,
        *,
        query: str,
        indicators: str | None,
    ) -> LiveFinanceDataResult:
        self.finance_calls.append((query, indicators))
        return self._finance_result(query=query, indicators=indicators)

    async def screen_then_query_finance(
        self,
        *,
        screening_query: str,
        asset_type: str,
        indicators: str,
    ) -> LiveScreenedFinanceDataResult:
        self.screen_finance_calls.append((screening_query, asset_type, indicators))
        return LiveScreenedFinanceDataResult(
            screen=self._screen_result(query=screening_query, asset_type=asset_type),
            entities=(
                LiveSecurityEntity(code="300059", name="东方财富", asset_type="A股"),
                LiveSecurityEntity(code="300033", name="同花顺", asset_type="A股"),
            ),
            batches=(
                self._finance_result(
                    query="查询东方财富(300059)、同花顺(300033)；获取近10年的归母净利润",
                    indicators=indicators,
                ),
            ),
        )

    def _screen_result(self, *, query: str, asset_type: str) -> LiveMarketDataResult:
        return LiveMarketDataResult(
            provider="test_screener",
            query=query,
            asset_type=asset_type,
            columns=("证券代码", "证券简称", "涨跌幅"),
            rows=(
                {"证券代码": "300059", "证券简称": "东方财富", "涨跌幅": "1.25"},
                {"证券代码": "300033", "证券简称": "同花顺", "涨跌幅": "2.50"},
            ),
            provenance=self.provenance,
        )

    def _finance_result(
        self,
        *,
        query: str,
        indicators: str | None,
    ) -> LiveFinanceDataResult:
        return LiveFinanceDataResult(
            provider="test_finance",
            query=query,
            indicators=indicators,
            tables=(
                {
                    "title": "查询结果",
                    "rawTable": {
                        "headers": ["证券代码", "值"],
                        "data": [["300059", "provider-raw-value"]],
                    },
                },
            ),
            provenance=self.provenance,
        )


class _FinancePayloadCurrentData(_RecordingCurrentData):
    def __init__(self, payload: dict[str, object]) -> None:
        super().__init__()
        self._payload = payload

    async def query_finance(
        self,
        *,
        query: str,
        indicators: str | None,
    ) -> LiveFinanceDataResult:
        self.finance_calls.append((query, indicators))
        return LiveFinanceDataResult(
            provider="test_finance",
            query=query,
            indicators=indicators,
            tables=({"title": "查询结果", **self._payload},),
            provenance=self.provenance,
        )


class _FlexibleAdvisor:
    def __init__(
        self, *, analysis_only: str | None = None, strategy_requested: bool = False,
        expected_data_rounds: int = 1,
    ) -> None:
        self.requests: list[VerifiedFactStrategyAdviceRequest] = []
        self.analysis_only = analysis_only
        self.strategy_requested = strategy_requested
        self.expected_data_rounds = expected_data_rounds
        self.review_snapshots: list[Mapping[str, object]] = []

    async def review_query_result(
        self, *, question: str, data_snapshot: Mapping[str, object],
        previous_queries: tuple[str, ...] = (), remaining_data_rounds: int = 1,
    ) -> QueryDataReview:
        # This fixture exercises the API's data-only response contract.
        # Actual review semantics and re-query bounds have separate adapter tests.
        assert question and previous_queries and remaining_data_rounds == self.expected_data_rounds
        self.review_snapshots.append(data_snapshot)
        return QueryDataReview(
            satisfied=True, evidence=(), retry_query=None,
            message=self.analysis_only or "已返回本轮实际数据。",
            strategy_requested=self.strategy_requested,
        )

    async def advise(
        self,
        request: VerifiedFactStrategyAdviceRequest,
    ) -> VerifiedFactStrategyAdvice:
        self.requests.append(request)
        advice = VerifiedFactStrategyAdvice(
            analysis="现价只是一个切片，可以用较快趋势和深度超跌两种假设验证。",
            hypothesis="不同参数会对这只股票的历史价格路径给出不同交易节奏。",
            proposals=(
                StrategyAdviceCandidate(
                    title="短周期趋势",
                    hypothesis="用10日线验证较快的趋势反应。",
                    entry_summary="股价上穿10日均线",
                    exit_summary="股价跌破10日均线",
                    suggested_utterance="股价上穿10日均线买入，跌破10日均线卖出，回测近1年",
                ),
                StrategyAdviceCandidate(
                    title="深度超跌",
                    hypothesis="用更严格的RSI阈值减少一般波动触发。",
                    entry_summary="RSI低于25",
                    exit_summary="RSI高于65",
                    suggested_utterance="RSI低于25买入，高于65卖出，回测近1年",
                ),
            ),
            provider="deepseek",
            model="deepseek-v4-flash",
            prompt_version="verified-fact-strategy-advice.prompt.v1",
            schema_version="verified-fact-strategy-advice.v1",
        )
        return (replace(advice, analysis=self.analysis_only, hypothesis="", proposals=())
                if self.analysis_only is not None else advice)


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


def test_first_turn_known_instrument_lookup_calls_live_finance_provider() -> None:
    provider = _RecordingCurrentData()
    with TestClient(
        create_app(
            compiler=cast(Any, _CompilerMustNotRun()),
            live_market_data=provider,
            live_finance_data=provider,
        )
    ) as client:
        response = client.post(
            "/api/v1/strategy-drafts",
            json={
                "utterance": "东方财富昨天的换手率是多少",
                "as_of_date": "2026-09-03",
            },
        )

    assert response.status_code == 201, response.text
    payload = response.json()
    assert provider.finance_calls == [("东方财富昨天的换手率是多少", "昨天的换手率")]
    assert provider.screen_finance_calls == []
    assert "数据已返回" in payload["assistant_message"]
    assert "provider-raw-value" in payload["assistant_message"]
    assert "未完成" not in payload["assistant_message"]
    assert payload["data"]["kind"] == "finance"
    assert payload["data"]["historical_backtest_eligible"] is False
    assert payload["data"]["finance"]["tables"][0]["rawTable"]["data"] == [
        ["300059", "provider-raw-value"],
    ]
    assert payload["strategy"] is None and payload["idea_route"] is None


def test_finance_lookup_sends_dated_provider_values_and_returns_model_answer_verbatim() -> None:
    provider = _FinancePayloadCurrentData(
        {
            "code": "300059.SZ", "entityName": "东方财富",
            "rawTable": {
                "headers": ["证券简称", "日期", "换手率(%)"],
                "data": [["东方财富", "2026-09-02", 2.37]],
            }
        }
    )
    analysis = "东方财富2026-09-02的换手率为2.37%。"
    advisor = _FlexibleAdvisor(analysis_only=analysis)
    with TestClient(
        create_app(
            compiler=cast(Any, _CompilerMustNotRun()),
            live_market_data=provider,
            live_finance_data=provider,
            strategy_advisor=advisor,
        )
    ) as client:
        response = client.post(
            "/api/v1/strategy-drafts",
            json={
                "utterance": "东方财富昨天的换手率是多少",
                "as_of_date": "2026-09-03",
            },
        )

    assert response.status_code == 201, response.text
    message = response.json()["assistant_message"]
    assert message == analysis
    assert not advisor.requests
    assert "2026-09-02" in str(advisor.review_snapshots)
    assert "换手率(%)" in str(advisor.review_snapshots)
    assert "2.37" in str(advisor.review_snapshots)
    assert provider.screen_calls == [] and provider.screen_finance_calls == []
    assert response.json()["strategy"] is None and response.json()["idea_route"] is None


@pytest.mark.parametrize("payload_key", ["rawTable", "table"])
def test_finance_lookup_accepts_column_oriented_provider_table(
    payload_key: str,
) -> None:
    provider = _FinancePayloadCurrentData(
        {
            "code": "300059.SZ", "entityName": "东方财富",
            payload_key: {
                "headName": ["最新交易日"],
                "最新价": [18.88],
                "换手率": [2.37],
            }
        }
    )
    analysis = "东方财富最新价18.88元，换手率2.37%；接口未提供明确的数据日期。"
    advisor = _FlexibleAdvisor(analysis_only=analysis)
    with TestClient(
        create_app(
            compiler=cast(Any, _CompilerMustNotRun()),
            live_market_data=provider,
            live_finance_data=provider,
            strategy_advisor=advisor,
        )
    ) as client:
        response = client.post(
            "/api/v1/strategy-drafts",
            json={
                "utterance": "东方财富最新价和换手率是多少",
                "as_of_date": "2026-09-03",
            },
        )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["assistant_message"] == analysis
    assert not advisor.requests
    assert "18.88" in str(advisor.review_snapshots)
    assert "2.37" in str(advisor.review_snapshots)
    assert "数据日期" not in str(advisor.review_snapshots)
    assert provider.screen_calls == [] and provider.screen_finance_calls == []
    assert body["strategy"] is None and body["idea_route"] is None
    assert body["data"]["finance"]["tables"][0][payload_key] == provider._payload[payload_key]


def test_verified_finance_answer_does_not_replace_missing_model_with_fixed_rules() -> None:
    provider = _FinancePayloadCurrentData(
        {
            "code": "300059.SZ",
            "entityName": "东方财富(300059.SZ)",
            "entityCodes": ["300059.SZ"],
            "rawTable": {
                "ZXJ_f2_3": [19.15],
                "headName": ["2026-09-03"],
            },
            "nameMap": {"ZXJ_f2_3": "最新价"},
            "fieldSet": [
                {
                    "returnCode": "ZXJ_f2_3",
                    "returnName": "最新价",
                    "unitName": "元",
                }
            ],
        }
    )
    with TestClient(
        create_app(
            live_market_data=provider,
            live_finance_data=provider,
        )
    ) as client:
        response = client.post(
            "/api/v1/strategy-drafts",
            json={
                "utterance": "东方财富现价",
                "as_of_date": "2026-09-03",
            },
        )

    assert response.status_code == 201, response.text
    body = response.json()
    message = body["assistant_message"]
    assert "数据已返回" in message and "最新价(元)=19.15" in message
    assert "ZXJ_f2_3" not in message
    assert "test_finance" not in message
    assert "sha256:" not in message
    assert "2026-09-03" in message
    assert body["data"]["finance"]["tables"][0]["rawTable"]["ZXJ_f2_3"] == [19.15]
    assert body["data"]["finance"]["provenance"]["response_sha256"].startswith("sha256:")
    assert body["idea_route"] is None


@pytest.mark.parametrize("strategy_requested", [False, True])
def test_finance_strategy_advice_requires_explicit_model_understood_request(
    strategy_requested: bool,
) -> None:
    provider = _FinancePayloadCurrentData(
        {
            "code": "300059.SZ",
            "entityName": "东方财富(300059.SZ)",
            "rawTable": {"ZXJ_f2_3": [19.15], "headName": ["2026-09-03"]},
            "nameMap": {"ZXJ_f2_3": "最新价"},
            "fieldSet": [
                {
                    "returnCode": "ZXJ_f2_3",
                    "returnName": "最新价",
                    "unitName": "元",
                }
            ],
        }
    )
    advisor = _FlexibleAdvisor(strategy_requested=strategy_requested)
    with TestClient(
        create_app(
            live_market_data=provider,
            live_finance_data=provider,
            strategy_advisor=advisor,
        )
    ) as client:
        response = client.post(
            "/api/v1/strategy-drafts",
            json={
                "utterance": (
                    "查询东方财富现价，并分析两种策略" if strategy_requested else "查询东方财富现价"
                ),
                "as_of_date": "2026-09-03",
            },
        )

    assert response.status_code == 201, response.text
    body = response.json()
    if strategy_requested:
        assert body["assistant_message"] == (
            "现价只是一个切片，可以用较快趋势和深度超跌两种假设验证。"
        )
        assert [item["title"] for item in body["idea_route"]["proposals"]] == [
            "短周期趋势", "深度超跌",
        ]
        assert [item["entry_summary"] for item in body["idea_route"]["proposals"]] == [
            "股价上穿10日均线", "RSI低于25",
        ]
        assert all(item["suggested_utterance"].startswith("300059.SZ ")
                   for item in body["idea_route"]["proposals"])
        assert len(advisor.requests) == 1
    else:
        assert body["assistant_message"] == "已返回本轮实际数据。"
        assert body["idea_route"] is None and body["strategy"] is None
        assert not advisor.requests
    assert "19.15" in str(advisor.review_snapshots)


@pytest.mark.parametrize("payload_key", ["rawTable", "table"])
@pytest.mark.parametrize("newest_first", [False, True])
def test_finance_date_axis_keeps_latest_date_and_each_column_value_aligned(
    payload_key: str, newest_first: bool,
) -> None:
    columns = {
        "headName": ["2026-09-03", "2026-09-04"],
        "CLOSE_f2": [18.88, 19.15],
        "AMOUNT_f6": [40.01, 48.54],
        "TURNOVER_f8": [1.23, 1.881],
    }
    if newest_first:
        columns = {key: list(reversed(values)) for key, values in columns.items()}
    provider = _FinancePayloadCurrentData({
        "code": "300059.SZ", "entityName": "东方财富", "entityCodes": ["300059.SZ"],
        payload_key: columns,
        "nameMap": {
            "CLOSE_f2": "收盘价", "AMOUNT_f6": "成交额", "TURNOVER_f8": "换手率",
        },
        "fieldSet": [
            {"returnCode": "CLOSE_f2", "returnName": "收盘价", "unitName": "元"},
            {"returnCode": "AMOUNT_f6", "returnName": "成交额", "unitName": "亿元"},
            {"returnCode": "TURNOVER_f8", "returnName": "换手率", "unitName": "%"},
        ],
    })
    analysis = "东方财富2026-09-04收盘价19.15元，成交额48.54亿元，换手率1.881%。"
    advisor = _FlexibleAdvisor(analysis_only=analysis)
    with TestClient(create_app(
        compiler=cast(Any, _CompilerMustNotRun()), live_market_data=provider,
        live_finance_data=provider, strategy_advisor=advisor,
    )) as client:
        response = client.post("/api/v1/strategy-drafts", json={
            "utterance": "东方财富最近一个交易日收盘价、成交额和换手率是多少？",
            "as_of_date": "2026-09-05",
        })

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["assistant_message"] == analysis
    assert not advisor.requests
    snapshot_tables = cast(list[dict[str, object]], advisor.review_snapshots[0]["tables"])
    assert snapshot_tables[0]["fields_and_first_row"] == {
        "数据日期": "2026-09-04", "收盘价(元)": "19.15",
        "成交额(亿元)": "48.54", "换手率(%)": "1.881",
    }
    assert len(provider.finance_calls) == 1
    assert provider.screen_calls == [] and provider.screen_finance_calls == []
    assert body["strategy"] is None and body["idea_route"] is None
    assert body["diagnostic_code"] == "data_query_only"
    assert body["data"]["finance"]["tables"][0][payload_key] == columns
    assert body["data"]["historical_backtest_eligible"] is False


@pytest.mark.parametrize("payload_key", ["rawTable", "table"])
def test_finance_lookup_treats_metadata_without_values_as_empty(payload_key: str) -> None:
    class EmptyCurrentData(_FinancePayloadCurrentData):
        def _screen_result(self, *, query: str, asset_type: str) -> LiveMarketDataResult:
            return replace(super()._screen_result(query=query, asset_type=asset_type), rows=())

    provider = EmptyCurrentData(
        {
            payload_key: {
                "headName": ["最新交易日"],
                "最新价": [],
                "换手率": [],
            }
        }
    )
    with TestClient(
        create_app(
            compiler=cast(Any, _CompilerMustNotRun()),
            live_market_data=provider,
            live_finance_data=provider,
        )
    ) as client:
        response = client.post(
            "/api/v1/strategy-drafts",
            json={
                "utterance": "东方财富最新价和换手率是多少",
                "as_of_date": "2026-09-03",
            },
        )

    assert response.status_code == 201, response.text
    body = response.json()
    assert "未返回匹配数据" in body["assistant_message"]
    assert len(provider.finance_calls) == 1
    assert provider.screen_calls == [("东方财富最新价和换手率是多少", "A股")]
    assert body["data"]["kind"] == "screen" and body["data"]["screen"]["rows"] == []
    assert body["strategy"] is None and body["idea_route"] is None


@pytest.mark.parametrize("payload_key", ["rawTable", "table"])
def test_empty_finance_recovers_requested_security_and_fields_from_screen(payload_key: str) -> None:
    actual_row = {"证券代码": "300059", "证券简称": "东方财富", "最新价(元)": 19.15,
                  "换手率(%)": 1.881}

    class RecoverableCurrentData(_FinancePayloadCurrentData):
        def _screen_result(self, *, query: str, asset_type: str) -> LiveMarketDataResult:
            return replace(
                super()._screen_result(query=query, asset_type=asset_type),
                columns=tuple(actual_row), rows=(actual_row,),
            )

    provider = RecoverableCurrentData({payload_key: {"headName": ["最新交易日"],
                                                   "最新价": [], "换手率": []}})
    analysis = "东方财富最新价19.15元，换手率1.881%。"
    advisor = _FlexibleAdvisor(analysis_only=analysis, expected_data_rounds=0)
    question = "东方财富最新价和换手率是多少"
    with TestClient(create_app(
        compiler=cast(Any, _CompilerMustNotRun()), live_market_data=provider,
        live_finance_data=provider, strategy_advisor=advisor,
    )) as client:
        response = client.post("/api/v1/strategy-drafts", json={
            "utterance": question, "as_of_date": "2026-09-03",
        })

    assert response.status_code == 201, response.text
    body = response.json()
    assert len(provider.finance_calls) == 1 and provider.finance_calls[0][0] == question
    assert provider.screen_calls == [(question, "A股")]
    assert not provider.screen_finance_calls and not advisor.requests
    assert body["assistant_message"] == analysis
    assert body["data"]["kind"] == "screen"
    assert body["data"]["screen"]["rows"] == [actual_row]
    assert body["data"]["screen"]["provenance"]["response_sha256"] == (
        provider.provenance.response_sha256
    )
    assert advisor.review_snapshots[0]["rows"] == [actual_row]
    assert body["strategy"] is None and body["idea_route"] is None
    assert body["data"]["historical_backtest_eligible"] is False


def test_first_turn_screened_finance_splits_filter_from_requested_values() -> None:
    provider = _RecordingCurrentData()
    with TestClient(
        create_app(
            compiler=cast(Any, _CompilerMustNotRun()),
            live_market_data=provider,
            live_finance_data=provider,
            strategy_advisor=_FlexibleAdvisor(),
        )
    ) as client:
        response = client.post(
            "/api/v1/strategy-drafts",
            json={
                "utterance": "A股今日上涨的股票；获取最新价和换手率",
                "as_of_date": "2026-09-03",
            },
        )

    assert response.status_code == 201, response.text
    payload = response.json()
    assert provider.screen_finance_calls == [("A股今日上涨的股票", "A股", "最新价和换手率")]
    assert provider.screen_calls == []
    assert provider.finance_calls == []
    assert payload["diagnostic_code"] == "data_query_only"
    assert payload["data"]["kind"] == "screened_finance"


def test_first_turn_pure_screen_does_not_trigger_a_followup_finance_query() -> None:
    provider = _RecordingCurrentData()
    with TestClient(
        create_app(
            compiler=cast(Any, _CompilerMustNotRun()),
            live_market_data=provider,
            live_finance_data=provider,
        )
    ) as client:
        response = client.post(
            "/api/v1/strategy-drafts",
            json={
                "utterance": "A股近一年涨幅前5只股票",
                "as_of_date": "2026-09-03",
            },
        )

    assert response.status_code == 201, response.text
    payload = response.json()
    assert provider.screen_calls == [("A股近一年涨幅前5只股票", "A股")]
    assert provider.screen_finance_calls == []
    assert provider.finance_calls == []
    assert payload["data"]["kind"] == "screen"


@pytest.mark.parametrize("analysis_available", [True, False])
def test_stock_recommendation_shows_only_model_ranked_shortlist_or_no_table(
    analysis_available: bool,
) -> None:
    class RankedProvider(_RecordingCurrentData):
        def _screen_result(self, *, query: str, asset_type: str) -> LiveMarketDataResult:
            return LiveMarketDataResult(
                provider="test_screener",
                query=query,
                asset_type=asset_type,
                columns=("证券代码", "证券简称", "成交额"),
                rows=(
                    {"证券代码": "300059", "证券简称": "东方财富", "成交额": 10},
                    {"证券代码": "300033", "证券简称": "同花顺", "成交额": 20},
                    {"证券代码": "000001", "证券简称": "平安银行", "成交额": 30},
                    {"证券代码": "002594", "证券简称": "比亚迪", "成交额": 40},
                ),
                provenance=self.provenance,
            )

    class RankingAdvisor(_FlexibleAdvisor):
        def __init__(self) -> None:
            super().__init__()
            self.screened: LiveMarketDataResult | None = None

        async def recommend_stocks(
            self,
            query: str,
            result: LiveMarketDataResult,
        ) -> tuple[StockRecommendation, ...] | None:
            assert query == "帮我推荐成交活跃的A股股票"
            self.screened = result
            if not analysis_available:
                return None
            # Deliberately exceed the adapter contract to test the route's
            # display bound independently of model-schema validation.
            return (
                StockRecommendation("002594.SZ", "比亚迪", "成交額在候选中较高。"),
                StockRecommendation("000001.SZ", "平安银行", "成交额居第二。"),
                StockRecommendation("300033.SZ", "同花顺", "成交额居第三。"),
                StockRecommendation("300059.SZ", "东方财富", "成交额居第四。"),
            )

    provider = RankedProvider()
    advisor = RankingAdvisor()
    with TestClient(
        create_app(
            compiler=cast(Any, _CompilerMustNotRun()),
            live_market_data=provider,
            live_finance_data=provider,
            strategy_advisor=advisor,
        )
    ) as client:
        response = client.post(
            "/api/v1/strategy-drafts",
            json={"utterance": "帮我推荐成交活跃的A股股票", "as_of_date": "2026-09-03"},
        )

    assert response.status_code == 201, response.text
    assert advisor.screened is not None
    assert len(advisor.screened.rows) == 4
    assert provider.finance_calls == []
    assert provider.screen_finance_calls == []
    body = response.json()
    if analysis_available:
        screen = body["data"]["screen"]
        assert screen["columns"] == ["股票", "代码", "选择理由"]
        assert [row["代码"] for row in screen["rows"]] == [
            "002594.SZ",
            "000001.SZ",
            "300033.SZ",
        ]
        assert all(row["选择理由"] for row in screen["rows"])
        assert "查到了" not in body["assistant_message"]
    else:
        assert body.get("data") is None
        assert "还没有生成有充分依据的股票推荐" in body["assistant_message"]
        assert "东方财富" not in body["assistant_message"]


@pytest.mark.parametrize(
    "utterance",
    ["港股涨幅前5只股票", "美股市值前10只股票", "混合型基金收益前5"],
)
def test_explicit_unsupported_product_does_not_call_live_provider(utterance: str) -> None:
    provider = _RecordingCurrentData()
    with TestClient(
        create_app(
            compiler=cast(Any, _CompilerMustNotRun()),
            live_market_data=provider,
            live_finance_data=provider,
        )
    ) as client:
        response = client.post(
            "/api/v1/strategy-drafts",
            json={"utterance": utterance, "as_of_date": "2026-09-03"},
        )

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload.get("data") is None
    assert "A 股股票和场内 ETF" in payload["assistant_message"]
    assert provider.screen_calls == []
    assert provider.finance_calls == []
    assert provider.screen_finance_calls == []


def test_complete_strategy_replaces_a_first_turn_data_query() -> None:
    provider = _RecordingCurrentData()
    with TestClient(create_app(live_market_data=provider, live_finance_data=provider)) as client:
        created_response = client.post(
            "/api/v1/strategy-drafts",
            json={
                "utterance": "东方财富昨天的换手率是多少",
                "as_of_date": "2026-09-03",
            },
        )
        assert created_response.status_code == 201, created_response.text
        created = cast(dict[str, Any], created_response.json())
        completed = _answer(
            client,
            created,
            "300059.SZ MACD金叉买入，MACD死叉卖出，回测近1年",
        )

    assert created["status"] == "needs_clarification"
    assert created["diagnostic_code"] == "data_query_only"
    assert completed["draft"]["status"] == "ready"
    assert completed["draft"]["revision"] == 2
    assert completed["draft"]["strategy"]["instrument"]["symbol"] == "300059.SZ"


def test_screen_with_requested_metric_uses_composed_provider_and_keeps_pending_rule() -> None:
    provider = _RecordingCurrentData()
    with TestClient(create_app(live_market_data=provider, live_finance_data=provider)) as client:
        created = cast(
            dict[str, Any],
            client.post(
                "/api/v1/strategy-drafts",
                json={
                    "utterance": "300059.SZ MACD金叉买入",
                    "as_of_date": "2026-09-03",
                },
            ).json(),
        )
        queried = _answer(client, created, "上涨的股票；获取近10年的归母净利润")
        completed = _answer(client, created, "MACD死叉卖出")

    assert provider.screen_finance_calls == [("上涨的股票", "A股", "近10年的归母净利润")]
    assert provider.screen_calls == []
    assert provider.finance_calls == [
        ("查询A股300059.SZ的证券代码和股票简称", "证券代码和股票简称"),
        ("查询A股300059.SZ的证券代码和股票简称", "证券代码和股票简称"),
    ]
    assert queried["draft"]["revision"] == created["revision"]
    assert queried["data"]["kind"] == "screened_finance"
    assert queried["data"]["screened_finance"]["historical_backtest_eligible"] is False
    assert queried["data"]["screened_finance"]["entities"][1]["name"] == "同花顺"
    assert completed["draft"]["status"] == "ready"
