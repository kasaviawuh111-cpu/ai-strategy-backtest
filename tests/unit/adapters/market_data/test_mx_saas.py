from __future__ import annotations

import asyncio
import json
import re
import ssl
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from ashare_lab.adapters.market_data.eastmoney_instrument_search import EastmoneyInstrumentSearch
from ashare_lab.adapters.market_data.mx_saas import (
    MxSaasMarketDataClient,
    MxSaasProviderAuthError,
    MxSaasProviderDataError,
    MxSaasProviderNoDataError,
    MxSaasProviderUnavailableError,
    _provider_indicator_points,
)
from ashare_lab.domain.signals.provider_runtime import _field_value
from ashare_lab.domain.strategy import IndicatorCondition
from ashare_lab.ports.instrument_resolution import InstrumentNameAmbiguous


async def _no_sleep(_delay: float) -> None:
    return None


def _client(handler: httpx.AsyncBaseTransport) -> MxSaasMarketDataClient:
    return MxSaasMarketDataClient(
        api_key="test-provider-key",
        transport=handler,
        clock=lambda: datetime(2026, 9, 2, 10, 0, tzinfo=UTC),
        sleeper=_no_sleep,
    )


@pytest.mark.parametrize("query,symbol", [
    ("dfcf", "300059.SZ"), ("DFCF", "300059.SZ"), ("dongfangcaifu", "300059.SZ"),
    ("gzmt", "600519.SH"), ("THS", "300033.SZ"),
    ("茅台", "600519.SH"), ("宁德", "300750.SZ"), ("怡 亚 通", "002183.SZ"),
])
def test_instrument_resolver_reuses_packaged_initials_without_provider(
    query: str, symbol: str,
) -> None:
    def no_network(_request: httpx.Request) -> httpx.Response:
        pytest.fail("a verified local identity must not issue a provider request")

    transport = httpx.MockTransport(no_network)
    directory = Path(__file__).resolve().parents[4] / "ashare_lab/resources/a_share_directory.json"
    search = EastmoneyInstrumentSearch(transport=transport, directory_path=directory)
    client = MxSaasMarketDataClient(
        api_key="test-provider-key", transport=transport, instrument_search=search,
    )
    assert client.resolve_instrument_name(query) == symbol
    assert not search._refresh_tasks


def test_instrument_resolver_local_partial_returns_candidates_not_a_guess() -> None:
    def no_network(_request: httpx.Request) -> httpx.Response:
        pytest.fail("local ambiguity must not be overridden by a provider")

    transport = httpx.MockTransport(no_network)
    directory = Path(__file__).resolve().parents[4] / "ashare_lab/resources/a_share_directory.json"
    search = EastmoneyInstrumentSearch(transport=transport, directory_path=directory)
    client = MxSaasMarketDataClient(
        api_key="test-provider-key", transport=transport, instrument_search=search,
    )
    with pytest.raises(InstrumentNameAmbiguous) as caught:
        client.resolve_instrument_name("dongfangcaif")
    assert [(item.symbol, item.name, item.source) for item in caught.value.candidates] == [
        ("300059.SZ", "东方财富", "eastmoney_instrument_directory"),
    ]
    assert not search._refresh_tasks


def test_runtime_instrument_resolver_enables_the_local_directory_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_provider(*_args: object, **_kwargs: object) -> None:
        pytest.fail("default runtime resolution must not request a verified local alias")

    monkeypatch.setattr(MxSaasMarketDataClient, "screen", no_provider)
    client = MxSaasMarketDataClient(api_key="test-provider-key")
    assert client.resolve_instrument_name("DFCF") == "300059.SZ"


@pytest.mark.parametrize("query,name,symbol", [
    ("东财", "东方财富", "300059.SZ"),
    ("茅台", "贵州茅台", "600519.SH"),
])
def test_unique_chinese_alias_binds_without_confirmation(
    query: str, name: str, symbol: str,
) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        assert request.url.params["keyword"] == query
        return httpx.Response(200, json={"code": "0", "result": [{
            "code": symbol[:6], "shortName": name,
            "market": 1 if symbol.endswith("SH") else 0,
            "securityTypeName": "沪A" if symbol.endswith("SH") else "深A",
        }]})

    transport = httpx.MockTransport(handler)
    search = EastmoneyInstrumentSearch(transport=transport)
    client = MxSaasMarketDataClient(
        api_key="test-provider-key", transport=transport, instrument_search=search,
    )
    assert client.resolve_instrument_name(query) == symbol
    assert requests == ["/codetable/search/web"]


@pytest.mark.parametrize("query,truncated,multiple", [
    ("平安", False, True), ("东财", True, False),
    ("东", False, False), ("3000", False, False),
])
def test_alias_autobinding_does_not_choose_from_ambiguous_or_incomplete_matches(
    query: str, truncated: bool, multiple: bool,
) -> None:
    from ashare_lab.adapters.market_data.eastmoney_instrument_search import (
        InstrumentSearchResult, SearchInstrument,
    )

    class Search:
        def resolve_local_name(self, _query: str) -> None:
            return None

        async def search(self, keyword: str, *, limit: int) -> InstrumentSearchResult:
            items = (SearchInstrument("000001.SZ", "平安银行", "SZ"),
                     SearchInstrument("601318.SH", "中国平安", "SH")) if multiple else (
                SearchInstrument("300059.SZ", "东方财富", "SZ"),
            )
            return InstrumentSearchResult(keyword, items, datetime.now(UTC), truncated)

    client = MxSaasMarketDataClient(api_key="test-provider-key", instrument_search=Search())
    with pytest.raises(InstrumentNameAmbiguous):
        client.resolve_instrument_name(query)


def test_exact_autocomplete_identity_can_bind_without_finance_screening() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/codetable/search/web"
        return httpx.Response(200, json={"code": "0", "result": [{
            "code": "300059", "shortName": "东方财富", "market": 0,
            "securityTypeName": "深A",
        }]})

    transport = httpx.MockTransport(handler)
    client = MxSaasMarketDataClient(
        api_key="test-provider-key", transport=transport,
        instrument_search=EastmoneyInstrumentSearch(transport=transport),
    )
    assert client.resolve_instrument_name("东方财富") == "300059.SZ"


@pytest.mark.parametrize("search_failure", ["unavailable", "empty", "invalid"])
def test_security_autocomplete_failure_retains_existing_provider_fallback(
    search_failure: str,
) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/codetable/search/web":
            assert "em_api_key" not in request.headers
            if search_failure == "unavailable":
                raise httpx.ConnectError("offline")
            return httpx.Response(200, json={
                "code": "0" if search_failure == "empty" else "1", "result": [],
            })
        assert request.url.path == "/proxy/b/mcp/tool/selectSecurity"
        return httpx.Response(200, json={"code": 0, "data": {"allResults": {"result": {
            "columns": [{"field": "code", "displayName": "证券代码"},
                        {"field": "name", "displayName": "证券简称"}],
            "dataList": [{"code": "300059", "name": "东方财富"}],
        }}}})

    transport = httpx.MockTransport(handler)
    client = MxSaasMarketDataClient(
        api_key="test-provider-key", transport=transport,
        instrument_search=EastmoneyInstrumentSearch(transport=transport),
    )
    assert client.resolve_instrument_name("东方财富") == "300059.SZ"
    assert paths == ["/codetable/search/web", "/proxy/b/mcp/tool/selectSecurity"]


def test_screen_preserves_provider_columns_rows_and_auditable_response_hash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/proxy/b/mcp/tool/selectSecurity"
        assert request.headers["em_api_key"] == "test-provider-key"
        assert request.json() if hasattr(request, "json") else True
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "allResults": {
                        "result": {
                            "columns": [
                                {"field": "code", "displayName": "证券代码"},
                                {"field": "name", "displayName": "证券简称"},
                                {"field": "pct", "displayName": "近一年涨跌幅"},
                            ],
                            "dataList": [{"code": "300059", "name": "东方财富", "pct": "12.34"}],
                        }
                    }
                },
            },
        )

    result = asyncio.run(
        _client(httpx.MockTransport(handler)).screen(
            query="A股近一年涨幅前5只股票",
            asset_type="A股",
        )
    )

    assert result.provider == "eastmoney_mx_screener"
    assert result.asset_type == "A股"
    assert result.rows == ({"证券代码": "300059", "证券简称": "东方财富", "近一年涨跌幅": "12.34"},)
    assert result.provenance.response_sha256.startswith("sha256:")
    assert result.provenance.retrieved_at.tzinfo is UTC


def test_screen_preserves_actual_subset_sort_and_date_metadata_without_request_echo() -> None:
    description = "证券类型包含A股 且 今日涨跌幅大于0 且 今日成交额从大到小排名前3"
    column = {
        "title": "成交额(元)", "key": "TRADING_VOLUMES{2026-09-04}",
        "dateMsg": "14:47", "sortWay": "desc", "unit": "元", "sortable": True,
        "indexName": "TRADING_VOLUMES", "trace": "not-for-model",
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "code": 0,
            "data": {
                "selectType": "A_STOCK",
                "title": "echoed request is not execution evidence",
                "responseConditionList": [{"describe": description, "stockCount": 3,
                                           "ignored": "not-for-model"}],
                "totalCondition": description,
                "allResults": {
                    "market": "HSJ", "query": "another echoed query",
                    "totalCondition": {"describe": description, "stockCount": 3},
                    "result": {
                        "columns": [column],
                        "dataList": [{"TRADING_VOLUMES{2026-09-04}": "fixture-value"}],
                    },
                },
            },
        })

    result = asyncio.run(_client(httpx.MockTransport(handler)).screen(
        query="查询全部A股单日成交额前三名", asset_type="A股",
    ))

    assert result.columns == ("成交额(元) 14:47",)
    assert result.rows == ({"成交额(元) 14:47": "fixture-value"},)
    assert result.provider_metadata == {
        "selectType": "A_STOCK",
        "responseConditionList": [{"describe": description, "stockCount": 3}],
        "totalCondition": description,
        "allResults": {
            "market": "HSJ", "totalCondition": {"describe": description, "stockCount": 3},
        },
        "columns": [{key: value for key, value in column.items() if key != "trace"}],
    }
    assert "今日涨跌幅大于0" in str(result.provider_metadata)
    assert "查询全部A股" not in str(result.provider_metadata)
    assert "echoed" not in str(result.provider_metadata)
    assert "not-for-model" not in str(result.provider_metadata)


def test_screen_preserves_nested_events_without_changing_display_rows() -> None:
    detail = {"title": "股东增减持计划", "time": "2025-09-08 - 2026-09-08",
              "full": False, "colHeads": [{"colName": "首次公告日期", "colProp": "notice"}],
              "data": [{"notice": "2026-09-07", "holder": "测试股东"}]}
    payload = {"code": 0, "data": {"allResults": {"result": {
        "columns": [{"key": "code", "title": "代码"},
                    {"key": "event", "title": "股东增减持计划"}],
        "dataList": [{"code": "300059", "event": "公告摘要...",
                      "MTM_EXTRA|event": json.dumps([detail]),
                      "MTM_EXTRA|undeclared": json.dumps([detail])},
                     {"code": "600519", "event": "另一摘要", "MTM_EXTRA|event": "invalid"}],
    }}}}
    result = asyncio.run(_client(httpx.MockTransport(
        lambda _: httpx.Response(200, json=payload),
    )).screen(query="股东增持计划", asset_type="A股"))
    assert result.rows[0] == {"代码": "300059", "股东增减持计划": "公告摘要..."}
    assert result.rows[1]["代码"] == "600519"
    assert result.provider_metadata["event_tables"] == [
        {"row_index": 0, "source_key": "event", **detail},
    ]


def test_screen_preserves_provider_column_date_context() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "allResults": {
                        "result": {
                            "columns": [
                                {
                                    "field": "roe",
                                    "displayName": "ROE",
                                    "dateMsg": "2025年报",
                                }
                            ],
                            "dataList": [{"roe": "12.34"}],
                        }
                    }
                },
            },
        )

    result = asyncio.run(
        _client(httpx.MockTransport(handler)).screen(
            query="ROE高于10%",
            asset_type="A股",
        )
    )

    assert result.columns == ("ROE 2025年报",)
    assert result.rows == ({"ROE 2025年报": "12.34"},)


def test_screen_falls_back_to_provider_partial_results_table() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "allResults": None,
                    "partialResults": (
                        "|代码|名称|换手率(%)|\n|---|---|---|\n|300059|东方财富|2.37|"
                    ),
                },
            },
        )

    result = asyncio.run(
        _client(httpx.MockTransport(handler)).screen(
            query="东方财富换手率",
            asset_type="A股",
        )
    )

    assert result.columns == ("代码", "名称", "换手率(%)")
    assert result.rows == ({"代码": "300059", "名称": "东方财富", "换手率(%)": "2.37"},)


def test_screen_reports_an_empty_provider_result_as_no_data() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "securityCount": 0,
                    "allResults": {
                        "result": {
                            "columns": [
                                {"field": "code", "displayName": "证券代码"},
                            ],
                            "dataList": [],
                        }
                    },
                },
            },
        )

    with pytest.raises(MxSaasProviderNoDataError):
        asyncio.run(
            _client(httpx.MockTransport(handler)).screen(
                query="不存在的股票",
                asset_type="A股",
            )
        )


@pytest.mark.parametrize(
    "message",
    [
        "检测到您的数据范围较大，现为您返回精简后的部分数据",
        "请求数据量已达到上限",
    ],
)
def test_screen_rejects_provider_partial_or_truncated_business_result(message: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "message": message,
                    "allResults": {
                        "result": {
                            "columns": [
                                {"field": "code", "displayName": "证券代码"},
                            ],
                            "dataList": [{"code": "300059"}],
                        }
                    },
                },
            },
        )

    with pytest.raises(MxSaasProviderDataError):
        asyncio.run(
            _client(httpx.MockTransport(handler)).screen(
                query="全部A股",
                asset_type="A股",
            )
        )


def test_screen_rejects_provider_auth_failure_without_fallback() -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(403, json={"code": 403, "message": "expired"})

    with pytest.raises(MxSaasProviderAuthError):
        asyncio.run(
            _client(httpx.MockTransport(handler)).screen(
                query="A股涨幅前5",
                asset_type="A股",
            )
        )
    assert attempts == 1


def test_screen_retries_one_transient_transport_failure() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("temporary disconnect", request=request)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "allResults": {
                        "result": {
                            "columns": [
                                {"field": "code", "displayName": "证券代码"},
                            ],
                            "dataList": [{"code": "300059"}],
                        }
                    }
                },
            },
        )

    result = asyncio.run(
        _client(httpx.MockTransport(handler)).screen(
            query="A股涨幅前1",
            asset_type="A股",
        )
    )

    assert attempts == 2
    assert result.rows == ({"证券代码": "300059"},)


def test_screen_retries_transport_failures_with_exponential_backoff() -> None:
    attempts = 0
    delays: list[float] = []

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.ConnectError("temporary disconnect", request=request)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "allResults": {
                        "result": {
                            "columns": [
                                {"field": "code", "displayName": "证券代码"},
                            ],
                            "dataList": [{"code": "300059"}],
                        }
                    }
                },
            },
        )

    client = MxSaasMarketDataClient(
        api_key="test-provider-key",
        transport=httpx.MockTransport(handler),
        clock=lambda: datetime(2026, 9, 2, 10, 0, tzinfo=UTC),
        sleeper=sleeper,
    )
    result = asyncio.run(client.screen(query="A股涨幅前1", asset_type="A股"))

    assert attempts == 3
    assert delays == [1.0, 2.0]
    assert result.rows == ({"证券代码": "300059"},)


@pytest.mark.parametrize("transient_status", [429, 503])
def test_screen_retries_retryable_http_status(transient_status: int) -> None:
    attempts = 0
    delays: list[float] = []

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                transient_status,
                json={"code": transient_status, "message": "temporary provider detail"},
            )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "allResults": {
                        "result": {
                            "columns": [
                                {"field": "code", "displayName": "证券代码"},
                            ],
                            "dataList": [{"code": "300059"}],
                        }
                    }
                },
            },
        )

    client = MxSaasMarketDataClient(
        api_key="test-provider-key",
        transport=httpx.MockTransport(handler),
        clock=lambda: datetime(2026, 9, 2, 10, 0, tzinfo=UTC),
        sleeper=sleeper,
    )
    result = asyncio.run(client.screen(query="A股涨幅前1", asset_type="A股"))

    assert attempts == 2
    assert delays == [1.0]
    assert result.rows == ({"证券代码": "300059"},)


def test_screen_stops_after_bounded_retryable_http_failures() -> None:
    attempts = 0
    delays: list[float] = []

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            503,
            text="private provider response detail",
        )

    client = MxSaasMarketDataClient(
        api_key="test-provider-key",
        transport=httpx.MockTransport(handler),
        clock=lambda: datetime(2026, 9, 2, 10, 0, tzinfo=UTC),
        sleeper=sleeper,
    )
    with pytest.raises(MxSaasProviderUnavailableError) as error:
        asyncio.run(client.screen(query="A股涨幅前1", asset_type="A股"))

    assert attempts == 3
    assert delays == [1.0, 2.0]
    assert "private provider response detail" not in str(error.value)
    assert "test-provider-key" not in str(error.value)


def test_http_client_does_not_inherit_environment_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_options: list[dict[str, object]] = []
    original_async_client = httpx.AsyncClient

    def client_factory(
        *,
        timeout: httpx.Timeout,
        transport: httpx.AsyncBaseTransport | None,
        follow_redirects: bool,
        trust_env: bool,
    ) -> httpx.AsyncClient:
        captured_options.append(
            {
                "timeout": timeout,
                "transport": transport,
                "follow_redirects": follow_redirects,
                "trust_env": trust_env,
            }
        )
        return original_async_client(
            timeout=timeout,
            transport=transport,
            follow_redirects=follow_redirects,
            trust_env=trust_env,
        )

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)

    result = asyncio.run(
        _client(
            httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "allResults": {
                                "result": {
                                    "columns": [
                                        {"field": "code", "displayName": "证券代码"},
                                    ],
                                    "dataList": [{"code": "300059"}],
                                }
                            }
                        },
                    },
                )
            )
        ).screen(query="A股涨幅前1", asset_type="A股")
    )

    assert result.rows == ({"证券代码": "300059"},)
    assert captured_options[0]["trust_env"] is False
    assert captured_options[0]["follow_redirects"] is False
    timeout = captured_options[0]["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == 120.0
    assert timeout.connect == 10.0


@pytest.mark.parametrize("failure", ["read_timeout", "connect_timeout", "http_error"])
def test_finance_transport_failure_keeps_safe_cause_and_bounded_retries(failure: str) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if failure == "http_error":
            return httpx.Response(503, text="private gateway response")
        error = httpx.ReadTimeout if failure == "read_timeout" else httpx.ConnectTimeout
        raise error("private request detail", request=request)

    with pytest.raises(MxSaasProviderUnavailableError) as captured:
        asyncio.run(_client(httpx.MockTransport(handler)).query_finance(
            query="美的集团成交额", indicators=None,
        ))

    assert attempts == 3
    assert captured.value.tool == "searchData"
    assert captured.value.reason == failure
    assert captured.value.http_status == (503 if failure == "http_error" else None)
    assert "private" not in str(captured.value)
    assert "test-provider-key" not in str(captured.value)


def test_indicator_transport_retains_its_safe_call_id(caplog: pytest.LogCaptureFixture) -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path.rsplit("/", 1)[-1],
                      json.loads(request.content)["toolContext"]["callId"]))
        raise httpx.ReadTimeout("https://private.invalid/?token=secret", request=request)

    with pytest.raises(MxSaasProviderUnavailableError) as caught:
        asyncio.run(_client(httpx.MockTransport(handler)).query_indicator_history(
            instrument_id="300059.SZ", indicator_id="technical.rsi",
            provider_indicator_name="RSI", value_names=("RSI",),
            start=date(2026, 9, 2), end=date(2026, 9, 3),
        ))
    finance_ids = [call_id for tool, call_id in calls if tool == "searchData"]
    screen_ids = [call_id for tool, call_id in calls if tool == "selectSecurity"]
    # Each channel has its own bounded transport retry loop. Recovery tries
    # the alternate Skill once; it cannot re-enter the finance channel.
    assert [tool for tool, _ in calls] == ["searchData"] * 3 + ["selectSecurity"] * 3
    assert len(set(finance_ids)) == len(set(screen_ids)) == 1
    assert set(finance_ids).isdisjoint(screen_ids)
    assert re.fullmatch(r"indicator_history_[0-9a-f]{32}", caught.value.call_id or "")
    assert re.fullmatch(r"screen_[0-9a-f]{32}", screen_ids[0])
    assert caught.value.call_id == finance_ids[0]
    assert caught.value.tool == "searchData" and caught.value.reason == "read_timeout"
    assert caught.value.attempts == 3
    alternate = caught.value.__cause__
    assert isinstance(alternate, MxSaasProviderUnavailableError)
    assert alternate.tool == "selectSecurity" and alternate.call_id == screen_ids[0]
    assert alternate.reason == "read_timeout" and alternate.attempts == 3
    assert caught.value.call_id in caplog.text
    assert screen_ids[0] in caplog.text
    assert "private.invalid" not in str(caught.value) + caplog.text
    assert "token=secret" not in str(caught.value) + caplog.text


@pytest.mark.parametrize("auth_channel", ["searchData", "selectSecurity"])
@pytest.mark.parametrize("status", [401, 403])
def test_indicator_auth_failure_is_terminal_in_its_own_channel(
    auth_channel: str, status: int,
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        channel = request.url.path.rsplit("/", 1)[-1]
        calls.append(channel)
        if channel == auth_channel:
            return httpx.Response(status, text="private auth response")
        raise httpx.ReadTimeout("private request detail", request=request)

    with pytest.raises(MxSaasProviderAuthError) as caught:
        asyncio.run(_client(httpx.MockTransport(handler)).query_indicator_history(
            instrument_id="300059.SZ", indicator_id="technical.rsi",
            provider_indicator_name="RSI", value_names=("RSI",),
            start=date(2026, 9, 2), end=date(2026, 9, 3),
        ))
    assert calls == (["searchData"] if auth_channel == "searchData"
                     else ["searchData"] * 3 + ["selectSecurity"])
    assert caught.value.tool == auth_channel and caught.value.http_status == status
    assert caught.value.attempts == 1
    assert "private" not in str(caught.value)


@pytest.mark.parametrize(("payload", "reason"), [
    ({"dataTableDTOList": [{"entityCodes": ["300059.SZ"]}]}, "protocol_raw_table_missing"),
    ({"dataTableDTOList": [{"entityCodes": ["300059.SZ"], "rawTable": {
        "headName": ["2026-09-02", "2026-09-02"],
    }}]}, "data_dates_mismatch"),
    ({"dataTableDTOList": [{"entityCodes": ["600519.SH"]}]}, "data_security_mismatch"),
    ({"code": 500, "message": "https://private.invalid/?token=secret"}, "provider_query_rejected"),
    ({"data": {}}, "protocol_tables_missing"),
])
def test_indicator_bad_data_has_safe_reason_and_bounded_cause_specific_retry(
    payload: dict[str, object], reason: str,
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=payload)

    with pytest.raises(MxSaasProviderDataError) as caught:
        asyncio.run(_client(httpx.MockTransport(handler)).query_indicator_history(
            instrument_id="300059.SZ", indicator_id="technical.rsi",
            provider_indicator_name="RSI", value_names=("RSI",),
            start=date(2026, 9, 2), end=date(2026, 9, 3),
        ))
    assert caught.value.data_reason == reason
    assert caught.value.tool == "searchData"
    assert re.fullmatch(r"indicator_history_[0-9a-f]{32}", caught.value.call_id or "")
    assert "private.invalid" not in str(caught.value) + caught.value.data_reason
    assert "test-provider-key" not in str(caught.value)
    # Query execution errors now retry twice. Identity correction already
    # retried once in the baseline, but never accepts the wrong security.
    assert calls == {"provider_query_rejected": 3, "data_security_mismatch": 2}.get(reason, 1)


def test_indicator_unknown_validation_failure_retries_exact_query_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    parse_calls = 0
    queries: list[str] = []
    valid_payload = {
        "code": 200,
        "data": {"searchDataResultDTO": {"dataTableDTOList": [{
            "entityCodes": ["300059.SZ"],
            "rawTable": {"rsi": ["42.5"], "headName": ["2026-09-02"]},
            "fieldSet": [{"returnCode": "rsi", "returnName": "RSI"}],
        }]}},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        queries.append(str(json.loads(request.content)["query"]))
        return httpx.Response(200, json=valid_payload)

    original_parser = _provider_indicator_points

    def flaky_parser(**kwargs: object):
        nonlocal parse_calls
        parse_calls += 1
        if parse_calls == 1:
            raise MxSaasProviderDataError("transient provider shape")
        return original_parser(**kwargs)

    monkeypatch.setattr(
        "ashare_lab.adapters.market_data.mx_saas._provider_indicator_points",
        flaky_parser,
    )

    result = asyncio.run(_client(httpx.MockTransport(handler)).query_indicator_history(
        instrument_id="300059.SZ", indicator_id="technical.rsi",
        provider_indicator_name="RSI", value_names=("RSI",),
        start=date(2026, 9, 2), end=date(2026, 9, 2),
    ))

    assert calls == 2
    assert parse_calls == 2
    assert queries[0] == queries[1]
    assert result.points[0].values[0].value == Decimal("42.5")


def test_indicator_unknown_validation_failure_stays_fail_closed_after_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"code": 200, "data": {
            "searchDataResultDTO": {"dataTableDTOList": []},
        }})

    def always_invalid(**_kwargs: object):
        raise MxSaasProviderDataError("transient provider shape")

    monkeypatch.setattr(
        "ashare_lab.adapters.market_data.mx_saas._provider_indicator_points",
        always_invalid,
    )
    with pytest.raises(MxSaasProviderDataError) as caught:
        asyncio.run(_client(httpx.MockTransport(handler)).query_indicator_history(
            instrument_id="300059.SZ", indicator_id="technical.rsi",
            provider_indicator_name="RSI", value_names=("RSI",),
            start=date(2026, 9, 2), end=date(2026, 9, 2),
        ))

    assert calls == 2
    assert caught.value.data_reason == "data_validation_failed"
    assert isinstance(caught.value.__cause__, MxSaasProviderDataError)


def test_non_json_auth_failure_is_not_mistaken_for_bad_data() -> None:
    with pytest.raises(MxSaasProviderAuthError) as captured:
        asyncio.run(_client(httpx.MockTransport(
            lambda _request: httpx.Response(401, text="private gateway response")
        )).query_finance(query="美的集团成交额", indicators=None))
    assert captured.value.tool == "searchData"
    assert captured.value.http_status == 401
    assert "private" not in str(captured.value)


def test_default_transport_binds_provider_calls_to_ipv4(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_options: list[dict[str, object]] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "allResults": {
                        "result": {
                            "columns": [{"field": "code", "displayName": "证券代码"}],
                            "dataList": [{"code": "300059"}],
                        }
                    }
                },
            },
        )

    def transport_factory(
        *,
        local_address: str,
        verify: ssl.SSLContext,
    ) -> httpx.AsyncBaseTransport:
        captured_options.append({"local_address": local_address, "verify": verify})
        return httpx.MockTransport(handler)

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", transport_factory)
    client = MxSaasMarketDataClient(
        api_key="test-provider-key",
        clock=lambda: datetime(2026, 9, 2, 10, 0, tzinfo=UTC),
        sleeper=_no_sleep,
    )

    result = asyncio.run(client.screen(query="A股涨幅前1", asset_type="A股"))

    assert result.rows == ({"证券代码": "300059"},)
    assert captured_options[0]["local_address"] == "0.0.0.0"
    tls = captured_options[0]["verify"]
    assert isinstance(tls, ssl.SSLContext)
    assert tls.minimum_version is ssl.TLSVersion.TLSv1_2
    assert tls.maximum_version is ssl.TLSVersion.TLSv1_2


def test_finance_query_preserves_provider_tables_and_auditable_response_hash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/proxy/b/mcp/tool/searchData"
        assert request.headers["em_api_key"] == "test-provider-key"
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "searchDataResultDTO": {
                        "dataTableDTOList": [
                            {
                                "title": "东方财富市盈率",
                                "rawTable": {"headers": ["市盈率"], "data": [["35.2"]]},
                            }
                        ]
                    }
                },
            },
        )

    result = asyncio.run(
        _client(httpx.MockTransport(handler)).query_finance(
            query="查询东方财富当前市盈率",
            indicators="市盈率",
        )
    )

    assert result.provider == "eastmoney_mx_finance_data"
    assert result.indicators == "市盈率"
    assert result.tables[0]["title"] == "东方财富市盈率"
    assert result.provenance.response_sha256.startswith("sha256:")
    assert result.provenance.retrieved_at.tzinfo is UTC


def test_indicator_history_uses_provider_kdj_values_without_requesting_ohlcv() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "searchDataResultDTO": {
                        "dataTableDTOList": [
                            {
                                "code": "300033.SZ",
                                "entityName": "同花顺(300033.SZ)",
                                "entityCodes": ["300033.SZ"],
                                "rawTable": {
                                    "k": ["68.5", "24.1"],
                                    "d": ["60.2", "31.7"],
                                    "j": ["85.1", "8.9"],
                                    "headName": ["2026-09-03", "2026-09-02"],
                                },
                                "nameMap": {
                                    "k": "KDJ K值",
                                    "d": "KDJ D值",
                                    "j": "KDJ J值",
                                },
                                "fieldSet": [
                                    {
                                        "returnCode": "k",
                                        "returnName": "KDJ K值",
                                        "returnSourceCode": "KDJ_K",
                                        "returnSourceName": "KDJ K值",
                                    },
                                    {
                                        "returnCode": "d",
                                        "returnName": "KDJ D值",
                                        "returnSourceCode": "KDJ_D",
                                        "returnSourceName": "KDJ D值",
                                    },
                                    {
                                        "returnCode": "j",
                                        "returnName": "KDJ J值",
                                        "returnSourceCode": "KDJ_J",
                                        "returnSourceName": "KDJ J值",
                                    },
                                ],
                            }
                        ]
                    }
                },
            },
        )

    result = asyncio.run(
        _client(httpx.MockTransport(handler)).query_indicator_history(
            instrument_id="300033.SZ",
            indicator_id="technical.kdj",
            provider_indicator_name="KDJ",
            value_names=("K值", "D值", "J值"),
            start=date(2026, 9, 2),
            end=date(2026, 9, 3),
        )
    )

    assert "KDJ指标K值、D值、J值" in str(requests[0]["query"])
    assert all(term not in str(requests[0]["query"]) for term in ("开盘价", "收盘价", "成交量"))
    assert result.instrument_id == "300033.SZ"
    assert result.indicator_id == "technical.kdj"
    assert tuple(point.session_date for point in result.points) == (
        date(2026, 9, 2),
        date(2026, 9, 3),
    )
    assert tuple(value.value for value in result.points[-1].values) == (
        Decimal("68.5"),
        Decimal("60.2"),
        Decimal("85.1"),
    )
    assert result.response_sha256.startswith("sha256:")


def test_indicator_mapping_preserves_source_direction_and_rejects_range_dates() -> None:
    parameters = "N=14,N1=6,Dmi=1,AdjustFlag=2,period=1"
    raw_table = {"headName": ["2026-09-03T15:00:00+08:00", "2026-09-04T15:00:00+08:00"],
                 "pdi": ["20", "21"], "mdi": ["10", "11"]}
    table = {
        "entityCodes": ["300059.SZ"],
        "rawTable": raw_table,
        "table": {"headName": ["2026-09-03", "2026-09-04"],
                  "pdi": ["20.00%", "21.00%"], "mdi": ["10.00%", "11.00%"]},
        "fieldSet": [
            {
                "returnCode": code,
                "returnName": f"DMI({direction}DI值)",
                "unitName": "100%",
                "fixedParamValue": parameters,
            }
            for code, direction in (("pdi", "+"), ("mdi", "-"))
        ],
    }

    def parse():
        return _provider_indicator_points(
            tables=(table,),
            instrument_id="300059.SZ",
            provider_indicator_name="DMI(14)",
            value_names=("+DI值", "-DI值"),
            start=date(2026, 9, 3),
            end=date(2026, 9, 4),
        )

    point, _ = parse()
    assert tuple(value.field_name for value in point.values) == ("+DI值", "-DI值")
    assert tuple(value.source_field_name for value in point.values) == ("DMI(+DI值)", "DMI(-DI值)")
    assert all(value.source_unit == "100%" and value.unit == "%" for value in point.values)
    assert all(value.source_parameters == parameters for value in point.values)
    assert _field_value(point, "+DI值") == Decimal("20")
    assert _field_value(point, "-DI值") == Decimal("10")

    raw_table["headName"] = ["2026-08-24至2026-09-04"]
    with pytest.raises(MxSaasProviderDataError, match="only non-daily observations"):
        parse()


def test_ma_cross_binding_matches_real_provider_field_labels() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "searchDataResultDTO": {
                        "dataTableDTOList": [
                            {
                                "code": "300059.SZ",
                                "entityCodes": ["300059.SZ"],
                                "rawTable": {
                                    "ma5": ["19.17", "19.23"],
                                    "ma20": ["18.90", "19.10"],
                                    "headName": ["2026-09-03", "2026-09-04"],
                                },
                                "nameMap": {
                                    "ma5": "5日MA简单移动平均",
                                    "ma20": "20日MA简单移动平均",
                                },
                                "fieldSet": [
                                    {
                                        "returnCode": "ma5",
                                        "returnName": "5日MA简单移动平均",
                                        "returnSourceCode": "MAJDYDPJ5",
                                        "returnSourceName": "MA简单移动平均",
                                    },
                                    {
                                        "returnCode": "ma20",
                                        "returnName": "20日MA简单移动平均",
                                        "returnSourceCode": "MAJDYDPJ20",
                                        "returnSourceName": "MA简单移动平均",
                                    },
                                ],
                            }
                        ]
                    }
                },
            },
        )

    result = asyncio.run(
        _client(httpx.MockTransport(handler)).query_condition_history(
            instrument_id="300059.SZ",
            condition=IndicatorCondition(
                indicator_id="technical.ma_cross",
                definition_version="1.0.0",
                params={"fast_period": 5, "slow_period": 20, "price_field": "close"},
                trigger="golden_cross",
            ),
            start=date(2026, 9, 3),
            end=date(2026, 9, 4),
        )
    )

    assert "5日MA简单移动平均、20日MA简单移动平均" in str(requests[0]["query"])
    assert tuple(value.value for value in result.points[-1].values) == (
        Decimal("19.23"),
        Decimal("19.10"),
    )


def test_indicator_history_rejects_wrong_security_instead_of_using_first_table() -> None:
    payload = {
        "code": 200,
        "data": {
            "searchDataResultDTO": {
                "dataTableDTOList": [
                    {
                        "code": "300059.SZ",
                        "entityCodes": ["300059.SZ"],
                        "rawTable": {
                            "k": ["1"],
                            "d": ["2"],
                            "j": ["3"],
                            "headName": ["2026-09-03"],
                        },
                        "fieldSet": [
                            {"returnCode": "k", "returnName": "KDJ K值"},
                            {"returnCode": "d", "returnName": "KDJ D值"},
                            {"returnCode": "j", "returnName": "KDJ J值"},
                        ],
                    }
                ]
            }
        },
    }
    client = _client(httpx.MockTransport(lambda _request: httpx.Response(200, json=payload)))

    with pytest.raises(MxSaasProviderDataError, match="exactly one requested security"):
        asyncio.run(
            client.query_indicator_history(
                instrument_id="300033.SZ",
                indicator_id="technical.kdj",
                provider_indicator_name="KDJ",
                value_names=("K值", "D值", "J值"),
                start=date(2026, 9, 3),
                end=date(2026, 9, 3),
            )
        )


def test_indicator_history_selects_unique_longest_table_for_same_security() -> None:
    payload = {
        "code": 200,
        "data": {
            "searchDataResultDTO": {
                "dataTableDTOList": [
                    {
                        "code": "300033.SZ",
                        "entityCodes": ["300033.SZ"],
                        "rawTable": {
                            "k": ["1"],
                            "d": ["2"],
                            "headName": ["2026-09-03"],
                        },
                        "fieldSet": [
                            {"returnCode": "k", "returnName": "KDJ K值"},
                            {"returnCode": "d", "returnName": "KDJ D值"},
                        ],
                    },
                    {
                        "code": "300033.SZ",
                        "entityCodes": ["300033.SZ"],
                        "rawTable": {
                            "k": ["68.5", "24.1"],
                            "d": ["60.2", "31.7"],
                            "j": ["85.1", "8.9"],
                            "headName": ["2026-09-03", "2026-09-02"],
                        },
                        "fieldSet": [
                            {"returnCode": "k", "returnName": "KDJ K值"},
                            {"returnCode": "d", "returnName": "KDJ D值"},
                            {"returnCode": "j", "returnName": "KDJ J值"},
                        ],
                    },
                ]
            }
        },
    }
    client = _client(httpx.MockTransport(lambda _request: httpx.Response(200, json=payload)))

    result = asyncio.run(
        client.query_indicator_history(
            instrument_id="300033.SZ",
            indicator_id="technical.kdj",
            provider_indicator_name="KDJ",
            value_names=("K值", "D值", "J值"),
            start=date(2026, 9, 2),
            end=date(2026, 9, 3),
        )
    )

    assert tuple(point.session_date for point in result.points) == (
        date(2026, 9, 2),
        date(2026, 9, 3),
    )
    assert tuple(value.value for value in result.points[-1].values) == (
        Decimal("68.5"),
        Decimal("60.2"),
        Decimal("85.1"),
    )


def test_indicator_history_merges_same_security_tables_with_same_date_axis() -> None:
    payload = {
        "code": 200,
        "data": {
            "searchDataResultDTO": {
                "dataTableDTOList": [
                    {
                        "code": "300033.SZ",
                        "entityCodes": ["300033.SZ"],
                        "rawTable": {
                            "close": ["19.20", "19.05"],
                            "headName": ["2026-09-02", "2026-09-03"],
                        },
                        "fieldSet": [
                            {"returnCode": "close", "returnName": "收盘价"},
                        ],
                    },
                    {
                        "code": "300033.SZ",
                        "entityCodes": ["300033.SZ"],
                        "rawTable": {
                            "ma20": ["18.90", "19.10"],
                            "headName": ["2026-09-02", "2026-09-03"],
                        },
                        "fieldSet": [
                            {"returnCode": "ma20", "returnName": "20日MA简单移动平均"},
                        ],
                    },
                ]
            }
        },
    }
    client = _client(httpx.MockTransport(lambda _request: httpx.Response(200, json=payload)))

    result = asyncio.run(
        client.query_indicator_history(
            instrument_id="300033.SZ",
            indicator_id="technical.ma",
            provider_indicator_name="20日移动平均线",
            value_names=("收盘价", "20日MA简单移动平均"),
            start=date(2026, 9, 2),
            end=date(2026, 9, 3),
        )
    )

    assert tuple(point.session_date for point in result.points) == (
        date(2026, 9, 2),
        date(2026, 9, 3),
    )
    assert tuple(value.value for value in result.points[-1].values) == (
        Decimal("19.05"),
        Decimal("19.10"),
    )


def test_indicator_history_accepts_provider_boolean_text_values() -> None:
    payload = {
        "code": 200,
        "data": {
            "searchDataResultDTO": {
                "dataTableDTOList": [
                    {
                        "code": "300033.SZ",
                        "entityCodes": ["300033.SZ"],
                        "rawTable": {
                            "flag": ["否", "是"],
                            "headName": ["2026-09-02", "2026-09-03"],
                        },
                        "fieldSet": [
                            {"returnCode": "flag", "returnName": "近期创阶段新高"},
                        ],
                    }
                ]
            }
        },
    }
    client = _client(httpx.MockTransport(lambda _request: httpx.Response(200, json=payload)))

    result = asyncio.run(
        client.query_indicator_history(
            instrument_id="300033.SZ",
            indicator_id="price.rolling_high",
            provider_indicator_name="近期创阶段新高",
            value_names=("近期创阶段新高",),
            start=date(2026, 9, 2),
            end=date(2026, 9, 3),
        )
    )

    assert [point.values[0].value for point in result.points] == [Decimal(0), Decimal(1)]


def test_condition_history_uses_the_shared_provider_catalog() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "searchDataResultDTO": {
                        "dataTableDTOList": [
                            {
                                "code": "300033.SZ",
                                "entityCodes": ["300033.SZ"],
                                "rawTable": {
                                    "k": ["30"],
                                    "d": ["25"],
                                    "j": ["40"],
                                    "headName": ["2026-09-03"],
                                },
                                "fieldSet": [
                                    {"returnCode": "k", "returnName": "KDJ K值"},
                                    {"returnCode": "d", "returnName": "KDJ D值"},
                                    {"returnCode": "j", "returnName": "KDJ J值"},
                                ],
                            }
                        ]
                    }
                },
            },
        )

    condition = IndicatorCondition(
        indicator_id="technical.kdj",
        definition_version="1.0.0",
        params={"period": 9, "k_smoothing": 3, "d_smoothing": 3},
        trigger="golden_cross",
    )
    result = asyncio.run(
        _client(httpx.MockTransport(handler)).query_condition_history(
            instrument_id="300033.SZ",
            condition=condition,
            start=date(2026, 9, 3),
            end=date(2026, 9, 3),
        )
    )

    assert "KDJ(9,3,3)指标K值、D值、J值" in str(requests[0]["query"])
    assert result.indicator_id == "technical.kdj"
    assert tuple(value.field_name for value in result.points[0].values) == ("K值", "D值", "J值")
    assert tuple(value.source_field_name for value in result.points[0].values) == (
        "KDJ K值",
        "KDJ D值",
        "KDJ J值",
    )


@pytest.mark.parametrize("table_location", ["root", "data", "nested"])
def test_finance_query_accepts_all_provider_table_locations(table_location: str) -> None:
    tables = [{"title": "东方财富市盈率", "rawTable": {"data": [["35.2"]]}}]
    if table_location == "root":
        payload = {"code": 200, "dataTableDTOList": tables}
    elif table_location == "data":
        payload = {"code": 200, "data": {"dataTableDTOList": tables}}
    else:
        payload = {
            "code": 200,
            "data": {"searchDataResultDTO": {"dataTableDTOList": tables}},
        }

    client = _client(httpx.MockTransport(lambda _request: httpx.Response(200, json=payload)))
    result = asyncio.run(
        client.query_finance(
            query="查询东方财富当前市盈率",
            indicators="市盈率",
        )
    )

    assert result.tables == tuple(tables)


@pytest.mark.parametrize(
    ("payload", "expected_error"),
    [
        ({"code": 200, "status": 500, "message": "failed"}, MxSaasProviderDataError),
        ({"code": 200, "status": 403, "message": "expired"}, MxSaasProviderAuthError),
        (
            {
                "code": 200,
                "data": {
                    "message": "检测到您的数据范围较大，现为您返回的是精简后的部分数据",
                    "searchDataResultDTO": {
                        "dataTableDTOList": [{"rawTable": {"data": [["35.2"]]}}]
                    },
                },
            },
            MxSaasProviderDataError,
        ),
    ],
)
def test_finance_query_rejects_business_error_or_partial_result(
    payload: dict[str, object],
    expected_error: type[Exception],
) -> None:
    client = _client(httpx.MockTransport(lambda _request: httpx.Response(200, json=payload)))

    with pytest.raises(expected_error):
        asyncio.run(
            client.query_finance(
                query="查询东方财富当前市盈率",
                indicators="市盈率",
            )
        )


def test_finance_query_uses_indicator_hint_when_query_omits_it() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "searchDataResultDTO": {
                        "dataTableDTOList": [
                            {"title": "东方财富市盈率", "rawTable": {"data": [["35.2"]]}}
                        ]
                    }
                },
            },
        )

    result = asyncio.run(
        _client(httpx.MockTransport(handler)).query_finance(
            query="查询东方财富",
            indicators="当前市盈率",
        )
    )

    assert requests[0]["query"] == "查询东方财富；获取当前市盈率"
    assert result.query == "查询东方财富；获取当前市盈率"
    assert result.indicators == "当前市盈率"


def test_finance_query_distinguishes_successful_empty_response() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {"searchDataResultDTO": {"dataTableDTOList": []}},
            },
        )

    with pytest.raises(MxSaasProviderNoDataError):
        asyncio.run(
            _client(httpx.MockTransport(handler)).query_finance(
                query="查询不存在的股票",
                indicators="市盈率",
            )
        )


def test_screen_then_query_finance_queries_up_to_five_entities_directly() -> None:
    search_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path.endswith("/selectSecurity"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "allResults": {
                            "result": {
                                "columns": [
                                    {"field": "code", "displayName": "证券代码"},
                                    {"field": "name", "displayName": "证券简称"},
                                    {"field": "pct", "displayName": "涨跌幅"},
                                ],
                                "dataList": [
                                    {"code": "300059", "name": "东方财富", "pct": "1.23"},
                                    {"code": "300059", "name": "东方财富", "pct": "1.23"},
                                    {"code": "300033", "name": "同花顺", "pct": "2.34"},
                                ],
                            }
                        }
                    },
                },
            )
        search_queries.append(body["query"])
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "searchDataResultDTO": {
                        "dataTableDTOList": [
                            {
                                "title": "选定实体近十年归母净利润",
                                "entityName2TagMap": {
                                    "东方财富": {"secuCode": "300059.SZ"},
                                    "同花顺": {"secuCode": "300033.SZ"},
                                },
                                "rawTable": {"data": [["300059", "100"], ["300033", "200"]]},
                            }
                        ]
                    }
                },
            },
        )

    result = asyncio.run(
        _client(httpx.MockTransport(handler)).screen_then_query_finance(
            screening_query="上涨的股票",
            asset_type="A股",
            indicators="近10年的归母净利润",
        )
    )

    assert search_queries == ["查询东方财富(300059)、同花顺(300033)；获取近10年的归母净利润"]
    assert tuple(entity.code for entity in result.entities) == ("300059", "300033")
    assert len(result.batches) == 1
    assert result.screen.rows[0]["涨跌幅"] == "1.23"
    assert result.batches[0].tables[0]["rawTable"]["data"][0] == ["300059", "100"]
    assert result.screen.provenance.response_sha256.startswith("sha256:")
    assert result.batches[0].provenance.response_sha256.startswith("sha256:")


@pytest.mark.parametrize(
    ("asset_type", "code", "name"),
    [
        ("ETF", "510300", "沪深300ETF"),
        ("基金", "000001", "华夏成长混合"),
    ],
)
def test_screen_then_query_finance_preserves_etf_and_fund_identity(
    asset_type: str,
    code: str,
    name: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path.endswith("/selectSecurity"):
            assert body["selectType"] == asset_type
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "allResults": {
                            "result": {
                                "columns": [
                                    {"field": "code", "displayName": "基金代码"},
                                    {"field": "name", "displayName": "基金简称"},
                                ],
                                "dataList": [{"code": code, "name": name}],
                            }
                        }
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "searchDataResultDTO": {
                        "dataTableDTOList": [
                            {
                                "title": f"{name}最新数据",
                                "entityCodes": [code],
                                "rawTable": {"value": ["1.23"]},
                            }
                        ]
                    }
                },
            },
        )

    result = asyncio.run(
        _client(httpx.MockTransport(handler)).screen_then_query_finance(
            screening_query=f"筛选{name}",
            asset_type=asset_type,
            indicators="最新数据",
        )
    )

    assert result.entities[0].code == code
    assert result.entities[0].name == name
    assert result.entities[0].asset_type == asset_type
    assert f"{name}({code})" in result.batches[0].query


def test_screen_then_query_finance_batches_more_than_five_entities() -> None:
    search_queries: list[str] = []
    rows = [{"code": f"{600000 + index:06d}", "name": f"股票{index}"} for index in range(12)]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path.endswith("/selectSecurity"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "allResults": {
                            "result": {
                                "columns": [
                                    {"field": "code", "displayName": "证券代码"},
                                    {"field": "name", "displayName": "证券简称"},
                                ],
                                "dataList": rows,
                            }
                        }
                    },
                },
            )
        search_queries.append(body["query"])
        entity_codes = re.findall(r"\((\d{6})\)", body["query"])
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "searchDataResultDTO": {
                        "dataTableDTOList": [
                            {
                                "title": "批次",
                                "entityCodes": entity_codes,
                                "rawTable": {"data": [[body["query"]]]},
                            }
                        ]
                    }
                },
            },
        )

    result = asyncio.run(
        _client(httpx.MockTransport(handler)).screen_then_query_finance(
            screening_query="上涨的股票",
            asset_type="A股",
            indicators="近10年的归母净利润",
        )
    )

    assert len(search_queries) == 3
    assert [query.count("(") for query in search_queries] == [5, 5, 2]
    assert all("近10年的归母净利润" in query for query in search_queries)
    assert len(result.entities) == 12
    assert len(result.batches) == 3


def test_screen_then_query_finance_uses_bounded_parallel_batches() -> None:
    active = 0
    max_active = 0
    rows = [{"code": f"{600000 + index:06d}", "name": f"股票{index}"} for index in range(10)]

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, max_active
        body = json.loads(request.content)
        if request.url.path.endswith("/selectSecurity"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "allResults": {
                            "result": {
                                "columns": [
                                    {"field": "code", "displayName": "证券代码"},
                                    {"field": "name", "displayName": "证券简称"},
                                ],
                                "dataList": rows,
                            }
                        }
                    },
                },
            )
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        entity_codes = re.findall(r"\((\d{6})\)", body["query"])
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "searchDataResultDTO": {
                        "dataTableDTOList": [
                            {
                                "title": "批次",
                                "entityCodes": entity_codes,
                                "rawTable": {"data": [[body["query"]]]},
                            }
                        ]
                    }
                },
            },
        )

    result = asyncio.run(
        _client(httpx.MockTransport(handler)).screen_then_query_finance(
            screening_query="上涨的股票",
            asset_type="A股",
            indicators="最新市盈率",
        )
    )

    assert len(result.batches) == 2
    assert max_active == 2


@pytest.mark.parametrize(
    "table",
    [
        {
            "title": "选定实体近十年归母净利润",
            "entityCodes": ["300059"],
            "rawTable": {"data": [["300059", "100"]]},
        },
        {
            "title": "选定实体近十年归母净利润",
            "rawTable": {"data": [["100"], ["200"]]},
        },
        {
            "title": "选定实体近十年归母净利润",
            "entityCodes": ["300059", "300033", "600000"],
            "rawTable": {"data": [["300059", "100"], ["300033", "200"]]},
        },
    ],
)
def test_screen_then_query_finance_rejects_missing_or_unprovable_entity_coverage(
    table: dict[str, object],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/selectSecurity"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "allResults": {
                            "result": {
                                "columns": [
                                    {"field": "code", "displayName": "证券代码"},
                                    {"field": "name", "displayName": "证券简称"},
                                ],
                                "dataList": [
                                    {"code": "300059", "name": "东方财富"},
                                    {"code": "300033", "name": "同花顺"},
                                ],
                            }
                        }
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "searchDataResultDTO": {
                        "dataTableDTOList": [table],
                    }
                },
            },
        )

    with pytest.raises(MxSaasProviderDataError, match="entity coverage"):
        asyncio.run(
            _client(httpx.MockTransport(handler)).screen_then_query_finance(
                screening_query="上涨的股票",
                asset_type="A股",
                indicators="近10年的归母净利润",
            )
        )


def test_screen_then_query_finance_accepts_explicit_code_column_coverage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/selectSecurity"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "allResults": {
                            "result": {
                                "columns": [
                                    {"field": "code", "displayName": "证券代码"},
                                    {"field": "name", "displayName": "证券简称"},
                                ],
                                "dataList": [
                                    {"code": "000001", "name": "平安银行"},
                                    {"code": "300059", "name": "东方财富"},
                                ],
                            }
                        }
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "searchDataResultDTO": {
                        "dataTableDTOList": [
                            {
                                "title": "批次",
                                "rawTable": {
                                    "headers": ["证券代码", "市盈率"],
                                    "data": [[1, "7.2"], ["300059.SZ", "35.2"]],
                                },
                            }
                        ]
                    }
                },
            },
        )

    result = asyncio.run(
        _client(httpx.MockTransport(handler)).screen_then_query_finance(
            screening_query="查询平安银行和东方财富",
            asset_type="A股",
            indicators="市盈率",
        )
    )

    assert len(result.entities) == 2
    assert len(result.batches) == 1


def test_screen_then_query_finance_rejects_more_than_five_hundred_entities() -> None:
    rows = [{"code": f"{index:06d}", "name": f"股票{index}"} for index in range(501)]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/selectSecurity")
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "allResults": {
                        "result": {
                            "columns": [
                                {"field": "code", "displayName": "证券代码"},
                                {"field": "name", "displayName": "证券简称"},
                            ],
                            "dataList": rows,
                        }
                    }
                },
            },
        )

    with pytest.raises(MxSaasProviderDataError, match="500"):
        asyncio.run(
            _client(httpx.MockTransport(handler)).screen_then_query_finance(
                screening_query="全部A股",
                asset_type="A股",
                indicators="市盈率",
            )
        )


def test_screen_then_query_finance_reuses_existing_screen_columns_over_limit() -> None:
    rows = [
        {
            "code": f"{index:06d}",
            "name": f"股票{index}",
            "price": "10.25",
            "turnover": "2.37",
        }
        for index in range(501)
    ]
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        assert request.url.path.endswith("/selectSecurity")
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "allResults": {
                        "result": {
                            "columns": [
                                {"field": "code", "displayName": "证券代码"},
                                {"field": "name", "displayName": "证券简称"},
                                {"field": "price", "displayName": "最新价(元)"},
                                {"field": "turnover", "displayName": "换手率(%)"},
                            ],
                            "dataList": rows,
                        }
                    }
                },
            },
        )

    result = asyncio.run(
        _client(httpx.MockTransport(handler)).screen_then_query_finance(
            screening_query="A股今日上涨的股票",
            asset_type="A股",
            indicators="最新价和换手率",
        )
    )

    assert requested_paths == ["/proxy/b/mcp/tool/selectSecurity"]
    assert len(result.entities) == 501
    assert len(result.screen.rows) == 501
    assert result.screen.rows[0]["最新价(元)"] == "10.25"
    assert result.screen.rows[0]["换手率(%)"] == "2.37"
    assert result.batches == ()


def test_screen_then_query_finance_does_not_substitute_a_different_metric() -> None:
    rows = [{"code": f"{index:06d}", "name": f"股票{index}", "pe": "20.5"} for index in range(501)]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/selectSecurity")
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "allResults": {
                        "result": {
                            "columns": [
                                {"field": "code", "displayName": "证券代码"},
                                {"field": "name", "displayName": "证券简称"},
                                {"field": "pe", "displayName": "市盈率(动)(倍)"},
                            ],
                            "dataList": rows,
                        }
                    }
                },
            },
        )

    with pytest.raises(MxSaasProviderDataError, match="500"):
        asyncio.run(
            _client(httpx.MockTransport(handler)).screen_then_query_finance(
                screening_query="全部A股",
                asset_type="A股",
                indicators="市盈率",
            )
        )


def test_screen_then_query_finance_distinguishes_no_entities() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/selectSecurity")
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "allResults": {
                        "result": {
                            "columns": [
                                {"field": "code", "displayName": "证券代码"},
                                {"field": "name", "displayName": "证券简称"},
                            ],
                            "dataList": [],
                        }
                    }
                },
            },
        )

    with pytest.raises(MxSaasProviderNoDataError):
        asyncio.run(
            _client(httpx.MockTransport(handler)).screen_then_query_finance(
                screening_query="不存在的股票",
                asset_type="A股",
                indicators="市盈率",
            )
        )


def test_exact_unique_name_can_be_resolved_to_a_canonical_symbol() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "allResults": {
                        "result": {
                            "columns": [
                                {"field": "code", "displayName": "证券代码"},
                                {"field": "name", "displayName": "证券简称"},
                            ],
                            "dataList": [{"code": "300033", "name": "同花顺"}],
                        }
                    }
                },
            },
        )

    assert _client(httpx.MockTransport(handler)).resolve_instrument_name("同花顺") == "300033.SZ"


def test_name_resolver_rejects_fuzzy_or_ambiguous_provider_rows() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "allResults": {
                        "result": {
                            "columns": [
                                {"field": "code", "displayName": "证券代码"},
                                {"field": "name", "displayName": "证券简称"},
                            ],
                            "dataList": [
                                {"code": "300033", "name": "同花顺"},
                                {"code": "300059", "name": "东方财富"},
                            ],
                        }
                    }
                },
            },
        )

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(LookupError):
        client.resolve_instrument_name("顺")


def test_abbreviation_returns_verified_choices_without_binding_first_result() -> None:
    from ashare_lab.ports.instrument_resolution import InstrumentNameAmbiguous

    def handler(request: httpx.Request) -> httpx.Response:
        assert "证券简称包含中金" in request.content.decode()
        return httpx.Response(200, json={"code": 0, "data": {"allResults": {"result": {
            "columns": [{"field": "code", "displayName": "证券代码"},
                        {"field": "name", "displayName": "证券简称"}],
            "dataList": [{"code": "601995", "name": "中金公司"},
                         {"code": "600489", "name": "中金黄金"},
                         {"code": "000060", "name": "中金岭南"},
                         {"code": "300059", "name": "东方财富"},
                         {"code": "bad-code", "name": "中金无效"}],
        }}}})

    with pytest.raises(InstrumentNameAmbiguous) as caught:
        _client(httpx.MockTransport(handler)).resolve_instrument_name("中金")
    assert {(item.name, item.symbol) for item in caught.value.candidates} == {
        ("中金公司", "601995.SH"), ("中金黄金", "600489.SH"), ("中金岭南", "000060.SZ"),
    }


def test_exact_name_wins_over_other_containing_names() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"allResults": {"result": {
            "columns": [{"field": "code", "displayName": "证券代码"},
                        {"field": "name", "displayName": "证券简称"}],
            "dataList": [{"code": "300059", "name": "东方财富测试"},
                         {"code": "601995", "name": "中金公司"}],
        }}}})

    assert _client(httpx.MockTransport(handler)).resolve_instrument_name("中金公司") == "601995.SH"
