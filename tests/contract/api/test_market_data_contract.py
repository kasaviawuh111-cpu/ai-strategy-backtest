from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from ashare_lab.adapters.market_data.mx_saas import (
    MxSaasProviderAuthError,
    MxSaasProviderNoDataError,
)
from ashare_lab.api import create_app
from ashare_lab.ports.live_market_data import (
    LiveFinanceDataResult,
    LiveMarketDataProvenance,
    LiveMarketDataResult,
    LiveScreenedFinanceDataResult,
    LiveSecurityEntity,
)


class FakeLiveMarketData:
    async def screen(self, *, query: str, asset_type: str) -> LiveMarketDataResult:
        assert query == "A股近一年涨幅前5只股票"
        assert asset_type == "A股"
        return LiveMarketDataResult(
            provider="eastmoney_mx_screener",
            query=query,
            asset_type=asset_type,
            columns=("证券代码", "证券简称", "近一年涨跌幅"),
            rows=({"证券代码": "300059", "证券简称": "东方财富", "近一年涨跌幅": "12.34"},),
            provenance=LiveMarketDataProvenance(
                response_sha256="sha256:" + "a" * 64,
                retrieved_at=datetime(2026, 9, 2, 10, 0, tzinfo=UTC),
                schema_version="eastmoney-mx.select-security.v1",
            ),
        )


class FakeLiveFinanceData:
    async def query_finance(
        self,
        *,
        query: str,
        indicators: str | None,
    ) -> LiveFinanceDataResult:
        assert query == "查询东方财富当前市盈率"
        assert indicators == "市盈率"
        return LiveFinanceDataResult(
            provider="eastmoney_mx_finance_data",
            query=query,
            indicators=indicators,
            tables=({"title": "东方财富市盈率", "rawTable": {"data": [["35.2"]]}},),
            provenance=LiveMarketDataProvenance(
                response_sha256="sha256:" + "b" * 64,
                retrieved_at=datetime(2026, 9, 2, 10, 0, tzinfo=UTC),
                schema_version="eastmoney-mx.search-data.v1",
            ),
        )


class EmptyLiveFinanceData:
    async def query_finance(
        self,
        *,
        query: str,
        indicators: str | None,
    ) -> LiveFinanceDataResult:
        del query, indicators
        raise MxSaasProviderNoDataError("no matching rows")


def test_empty_screen_is_not_reported_as_a_parser_failure() -> None:
    class EmptyScreen:
        async def screen(self, *, query: str, asset_type: str) -> LiveMarketDataResult:
            raise MxSaasProviderNoDataError("no matching rows")

    with TestClient(create_app(live_market_data=EmptyScreen())) as client:
        response = client.post(
            "/api/v1/market/screen",
            json={"query": "A股非ST成交额前1", "asset_type": "A股"},
        )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "live_market_data_no_results"
    assert "未找到符合本次条件" in response.json()["error"]["message"]


@pytest.mark.parametrize("empty_result", [True, False])
def test_missing_stock_offers_an_active_sample_only_after_empty_results(empty_result: bool) -> None:
    class SampleScreener:
        def __init__(self) -> None:
            self.queries: list[str] = []

        async def screen(self, *, query: str, asset_type: str) -> LiveMarketDataResult:
            self.queries.append(query)
            if len(self.queries) == 1:
                if empty_result:
                    raise MxSaasProviderNoDataError("no current entry matches")
                raise MxSaasProviderAuthError("credential rejected")
            return LiveMarketDataResult(
                provider="eastmoney_mx_screener", query=query, asset_type=asset_type,
                columns=("代码", "名称"), rows=({"代码": "300059", "名称": "东方财富"},),
                provenance=LiveMarketDataProvenance(
                    response_sha256="sha256:" + "a" * 64,
                    retrieved_at=datetime(2026, 9, 5, tzinfo=UTC), schema_version="test.v1",
                ),
            )

    provider = SampleScreener()
    with TestClient(create_app(live_market_data=provider)) as client:
        response = client.post("/api/v1/strategy-drafts", json={
            "utterance": "股价上穿20日均线买入，股价跌破20日均线卖出，回测近1年",
            "as_of_date": "2026-09-05",
        })
    assert response.status_code == 201
    draft = response.json()
    assert draft["status"] == "needs_clarification"
    assert draft["strategy"] is None
    if empty_result:
        assert len(provider.queries) == 2
        assert "日线策略" in provider.queries[0]
        assert "最近交易日成交额排名前10" in provider.queries[1]
        assert draft["instrument_suggestion"]["symbol"] == "300059.SZ"
        assert draft["instrument_suggestion"]["name"] == "东方财富"
        assert "你想用哪只股票试试" in draft["clarification"]
    else:
        assert len(provider.queries) == 1
        assert draft["instrument_suggestion"] is None
        assert "东方财富选股 Skill授权失败" in draft["clarification"]


class FakeScreenedFinanceData(FakeLiveMarketData):
    async def screen_then_query_finance(
        self,
        *,
        screening_query: str,
        asset_type: str,
        indicators: str,
    ) -> LiveScreenedFinanceDataResult:
        assert screening_query == "A股今日上涨"
        assert asset_type == "A股"
        assert indicators == "近10年的归母净利润"
        screen = LiveMarketDataResult(
            provider="eastmoney_mx_screener",
            query=screening_query,
            asset_type=asset_type,
            columns=("证券代码", "证券简称", "涨跌幅"),
            rows=(
                {"证券代码": "300059", "证券简称": "东方财富", "涨跌幅": "1.25"},
                {"证券代码": "300033", "证券简称": "同花顺", "涨跌幅": "2.50"},
            ),
            provenance=LiveMarketDataProvenance(
                response_sha256="sha256:" + "c" * 64,
                retrieved_at=datetime(2026, 9, 2, 10, 1, tzinfo=UTC),
                schema_version="eastmoney-mx.select-security.v1",
            ),
        )
        finance = LiveFinanceDataResult(
            provider="eastmoney_mx_finance_data",
            query="查询东方财富(300059)、同花顺(300033)；获取近10年的归母净利润",
            indicators=indicators,
            tables=(
                {
                    "title": "归母净利润",
                    "rawTable": {
                        "columns": ["证券代码", "报告期", "归母净利润"],
                        "data": [["300059", "2025", "provider-raw-value"]],
                    },
                },
            ),
            provenance=LiveMarketDataProvenance(
                response_sha256="sha256:" + "d" * 64,
                retrieved_at=datetime(2026, 9, 2, 10, 2, tzinfo=UTC),
                schema_version="eastmoney-mx.search-data.v1",
            ),
        )
        return LiveScreenedFinanceDataResult(
            screen=screen,
            entities=(
                LiveSecurityEntity(code="300059", name="东方财富", asset_type="A股"),
                LiveSecurityEntity(code="300033", name="同花顺", asset_type="A股"),
            ),
            batches=(finance,),
        )


def test_live_market_screen_is_explicitly_unavailable_without_provider() -> None:
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/market/screen",
            json={"query": "A股近一年涨幅前5只股票", "asset_type": "A股"},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "live_market_data_unavailable"
    assert response.json()["error"]["message"] == "东方财富选股 Skill 尚未配置，暂时无法选股。"


def test_live_market_screen_rejects_non_ashare_product_types() -> None:
    with TestClient(create_app(live_market_data=FakeLiveMarketData())) as client:
        response = client.post(
            "/api/v1/market/screen",
            json={"query": "纳斯达克市值前十", "asset_type": "美股"},
        )

    assert response.status_code == 422


def test_live_market_screen_returns_provider_result_with_provenance() -> None:
    with TestClient(create_app(live_market_data=FakeLiveMarketData())) as client:
        response = client.post(
            "/api/v1/market/screen",
            json={"query": "A股近一年涨幅前5只股票", "asset_type": "A股"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "provider": "eastmoney_mx_screener",
        "query": "A股近一年涨幅前5只股票",
        "asset_type": "A股",
        "columns": ["证券代码", "证券简称", "近一年涨跌幅"],
        "rows": [{"证券代码": "300059", "证券简称": "东方财富", "近一年涨跌幅": "12.34"}],
        "provider_metadata": {},
        "provenance": {
            "response_sha256": "sha256:" + "a" * 64,
            "retrieved_at": "2026-09-02T10:00:00Z",
            "schema_version": "eastmoney-mx.select-security.v1",
        },
    }


def test_live_finance_query_is_explicitly_unavailable_without_provider() -> None:
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/market/query",
            json={"query": "查询东方财富当前市盈率", "indicators": "市盈率"},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "live_market_data_unavailable"
    assert response.json()["error"]["message"] == "东方财富查数 Skill 尚未配置，暂时无法查询数据。"


def test_live_finance_query_returns_provider_tables_with_provenance() -> None:
    with TestClient(create_app(live_finance_data=FakeLiveFinanceData())) as client:
        response = client.post(
            "/api/v1/market/query",
            json={"query": "查询东方财富当前市盈率", "indicators": "市盈率"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "provider": "eastmoney_mx_finance_data",
        "query": "查询东方财富当前市盈率",
        "indicators": "市盈率",
        "tables": [{"title": "东方财富市盈率", "rawTable": {"data": [["35.2"]]}}],
        "provenance": {
            "response_sha256": "sha256:" + "b" * 64,
            "retrieved_at": "2026-09-02T10:00:00Z",
            "schema_version": "eastmoney-mx.search-data.v1",
        },
    }


def test_live_finance_query_distinguishes_no_matching_data() -> None:
    with TestClient(create_app(live_finance_data=EmptyLiveFinanceData())) as client:
        response = client.post(
            "/api/v1/market/query",
            json={"query": "查询不存在的股票", "indicators": "市盈率"},
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "live_market_data_no_results"


def test_screen_then_query_finance_is_current_only_and_preserves_provenance() -> None:
    with TestClient(create_app(live_market_data=FakeScreenedFinanceData())) as client:
        response = client.post(
            "/api/v1/market/screen-query",
            json={
                "screening_query": "A股今日上涨",
                "asset_type": "A股",
                "indicators": "近10年的归母净利润",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["usage_scope"] == "current_query_only"
    assert payload["historical_backtest_eligible"] is False
    assert payload["entities"] == [
        {"code": "300059", "name": "东方财富", "asset_type": "A股"},
        {"code": "300033", "name": "同花顺", "asset_type": "A股"},
    ]
    assert payload["screen"]["rows"][0]["涨跌幅"] == "1.25"
    assert payload["screen"]["provenance"]["response_sha256"] == "sha256:" + "c" * 64
    assert payload["batches"][0]["tables"][0]["rawTable"]["data"][0][-1] == (
        "provider-raw-value"
    )
    assert payload["batches"][0]["provenance"]["response_sha256"] == "sha256:" + "d" * 64
    assert "api_key" not in response.text.casefold()


def test_screen_then_query_finance_rejects_and_never_echoes_client_credentials() -> None:
    fake_secret = "must-not-be-echoed"
    with TestClient(create_app(live_market_data=FakeScreenedFinanceData())) as client:
        response = client.post(
            "/api/v1/market/screen-query",
            json={
                "screening_query": "A股今日上涨",
                "asset_type": "A股",
                "indicators": "近10年的归母净利润",
                "api_key": fake_secret,
            },
        )
        openapi = client.get("/api/v1/openapi.json").json()

    assert response.status_code == 422
    assert fake_secret not in response.text
    request_schema = openapi["components"]["schemas"]["LiveScreenedFinanceQueryRequest"]
    assert "api_key" not in request_schema["properties"]
