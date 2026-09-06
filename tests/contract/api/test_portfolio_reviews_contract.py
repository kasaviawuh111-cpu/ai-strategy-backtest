from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from ashare_lab.api import create_app
from ashare_lab.ports.portfolio_highlight_narrative import (
    DriverConfidence,
    PortfolioHighlightNarrative,
    PortfolioLikelyDriver,
    PortfolioNarrativeSource,
)

_PARSE_PATH = "/api/v1/portfolio-reviews/imports/parse"
_ANALYZE_PATH = "/api/v1/portfolio-reviews/analyze"
_NARRATE_PATH = "/api/v1/portfolio-reviews/narrate-highlight"


def _trade_only_review() -> dict[str, Any]:
    return {
        "import_metadata": {"method": "csv_xlsx", "confirmed": True},
        "account": {
            "period_start": "2026-09-01",
            "period_end": "2026-09-04",
            "base_currency": "CNY",
            "timezone": "Asia/Shanghai",
        },
        "trades": [
            {
                "source_id": "trade-only-1",
                "executed_at": "2026-09-03T14:05:00+08:00",
                "market": "CN_A",
                "symbol": "600519",
                "name": "贵州茅台",
                "side": "buy",
                "quantity": "1",
                "price": "1500",
                "fees": "5",
                "currency": "CNY",
            }
        ],
    }


def _complete_review() -> dict[str, Any]:
    return {
        "import_metadata": {"method": "csv_xlsx", "confirmed": True},
        "account": {
            "period_start": "2026-09-01",
            "period_end": "2026-09-04",
            "base_currency": "CNY",
            "timezone": "Asia/Shanghai",
        },
        "holdings": [
            {
                "source_id": "holding-1",
                "as_of": "2026-09-04T16:00:00+08:00",
                "market": "CN_A",
                "symbol": "600519",
                "name": "贵州茅台",
                "quantity": "10",
                "market_price": "1500",
                "average_cost": "1400",
                "currency": "CNY",
            }
        ],
        "trades": [
            {
                "source_id": "trade-1",
                "executed_at": "2026-09-03T14:05:00+08:00",
                "market": "CN_A",
                "symbol": "600519",
                "name": "贵州茅台",
                "side": "sell",
                "quantity": "1",
                "price": "1500",
                "fees": "5",
                "currency": "CNY",
                "realized_pnl": "100",
            }
        ],
        "daily_equity": [
            {
                "source_id": "equity-1",
                "at": "2026-09-01",
                "equity_base": "10000",
            },
            {
                "source_id": "equity-2",
                "at": "2026-09-02",
                "equity_base": "10000",
            },
            {
                "source_id": "equity-3",
                "at": "2026-09-03",
                "equity_base": "10000",
            },
            {
                "source_id": "equity-4",
                "at": "2026-09-04",
                "equity_base": "10200",
            },
        ],
        "market_ticks": [
            {
                "source_id": "market-1",
                "at": "2026-09-03T14:06:00+08:00",
                "granularity": "1m",
                "market": "CN_A",
                "symbol": "600519",
                "last_price": "1501",
                "currency": "CNY",
            }
        ],
    }


def test_parse_chinese_broker_ledger_infers_account_trade_and_closing_holding() -> None:
    with TestClient(create_app()) as client:
        response = client.post(
            _PARSE_PATH,
            json={
                "rows": [
                    {
                        "成交日期": "2026-09-03 14:05:00",
                        "证券代码": "600519",
                        "证券名称": "贵州茅台",
                        "业务名称": "买入",
                        "成交数量": "10",
                        "成交价格": "1400",
                        "手续费": "5",
                        "股份余额": "10",
                        "最新价": "1500",
                        "币种": "人民币",
                    }
                ]
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["security_identity_scope"] == "syntactic_a_h_normalization_only"
    assert payload["import_metadata"] == {"method": "csv_xlsx", "confirmed": False}
    assert payload["inferred_account"] == {
        "period_start": "2026-09-03",
        "period_end": "2026-09-03",
        "base_currency": "CNY",
        "timezone": "Asia/Shanghai",
        "markets": ["CN_A"],
    }
    assert payload["draft"]["trades"][0]["symbol"] == "600519.SH"
    assert payload["draft"]["trades"][0]["side"] == "buy"
    assert payload["draft"]["holdings"][0]["symbol"] == "600519.SH"
    assert payload["draft"]["holdings"][0]["market_price"] == "1500"
    assert payload["reconciliation"] == {
        "status": "ready_for_confirmation",
        "can_analyze_after_confirmation": True,
        "needs_current_holdings": False,
        "needs_opening_positions": False,
        "holding_source": "broker_reported_closing_balance",
        "reasons": [],
    }


def test_analyze_rejects_an_import_that_has_not_been_confirmed() -> None:
    review = _trade_only_review()
    review["import_metadata"]["confirmed"] = False

    with TestClient(create_app()) as client:
        response = client.post(_ANALYZE_PATH, json=review)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_failed"


def test_trade_only_analysis_degrades_snapshot_and_performance_capabilities() -> None:
    with TestClient(create_app()) as client:
        response = client.post(_ANALYZE_PATH, json=_trade_only_review())

    assert response.status_code == 200
    payload = response.json()
    assert "snapshot" not in payload
    assert "performance" not in payload
    assert payload["capabilities"] == {
        "snapshot": False,
        "trade_replay": True,
        "performance": False,
        "attribution": False,
        "market_tick_replay": False,
    }
    assert payload["data_capabilities"]["snapshot"]["grade"] == "unavailable"
    assert payload["trade_replay"]["events"][0]["symbol"] == "600519.SH"
    assert payload["replay_ticks"][0]["kind"] == "buy"


def test_complete_confirmed_review_returns_supported_analysis_outputs() -> None:
    with TestClient(create_app()) as client:
        response = client.post(_ANALYZE_PATH, json=_complete_review())

    assert response.status_code == 200
    payload = response.json()
    assert payload["validity"]["record_count"] == 7
    assert payload["capabilities"] == {
        "snapshot": True,
        "trade_replay": True,
        "performance": True,
        "attribution": True,
        "market_tick_replay": False,
    }
    assert payload["snapshot"]["total_market_value_base"] == "15000"
    assert payload["holdings"][0]["symbol"] == "600519.SH"
    assert payload["summary"]["period_return_pct"] == "2.00"
    assert payload["performance"]["twr"] == "0.02"
    assert payload["attribution"]["verified_pnl_base"] == "100"
    assert payload["highlights"][0]["source_id"] == "trade-1"
    assert payload["narrative_status"]["available"] is False


def test_narrate_highlight_without_server_key_returns_503() -> None:
    with TestClient(create_app()) as client:
        response = client.post(
            _NARRATE_PATH,
            json={"review": _complete_review(), "highlight_index": 0},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "portfolio_narrative_not_configured"


class _FutureSourceNarrator:
    async def narrate(self, _highlight: Any) -> PortfolioHighlightNarrative:
        return PortfolioHighlightNarrative(
            likely_drivers=(
                PortfolioLikelyDriver(
                    reason="最可能：市场预期发生了变化。",
                    confidence=DriverConfidence.MEDIUM,
                    source_ids=("future-source",),
                ),
            ),
            sources=(
                PortfolioNarrativeSource(
                    source_id="future-source",
                    title="事后才发布的报道",
                    url="https://example.test/future",
                    publisher="测试媒体",
                    published_at="2026-09-04T09:00:00+08:00",
                ),
            ),
            unresolved=(),
        )


class _InventedAccountFactNarrator:
    async def narrate(self, _highlight: Any) -> Any:
        return SimpleNamespace(
            headline="账户赚 999999",
            empathetic_summary="这次买入大赚 999999 元。",
            likely_drivers=(),
            sources=(),
            unresolved=("无法从公开信息确认单一原因。",),
        )


def test_narrate_highlight_renders_account_facts_only_from_verified_highlight() -> None:
    app = create_app()
    app.state.portfolio_highlight_narrator = _InventedAccountFactNarrator()

    with TestClient(app) as client:
        response = client.post(
            _NARRATE_PATH,
            json={"review": _complete_review(), "highlight_index": 0},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["headline"] == "贵州茅台 · 卖出并实现盈利"
    assert "2026-09-03" in payload["empathetic_summary"]
    assert "100 CNY" in payload["empathetic_summary"]
    assert "999999" not in payload["headline"]
    assert "999999" not in payload["empathetic_summary"]
    assert "买入" not in payload["empathetic_summary"]
    assert payload["unresolved"] == ["无法从公开信息确认单一原因。"]


class _ConflictingAccountFactNarrator:
    def __init__(self, text: str) -> None:
        self.text = text

    async def narrate(self, _highlight: Any) -> PortfolioHighlightNarrative:
        return PortfolioHighlightNarrative(
            likely_drivers=(),
            sources=(),
            unresolved=(self.text,),
        )


def test_narrate_highlight_rejects_model_authored_numbers_and_actions() -> None:
    for text in ("账户赚了 999999 元。", "用户当时买入了这只股票。"):
        app = create_app()
        app.state.portfolio_highlight_narrator = _ConflictingAccountFactNarrator(text)

        with TestClient(app) as client:
            response = client.post(
                _NARRATE_PATH,
                json={"review": _complete_review(), "highlight_index": 0},
            )

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "portfolio_narrative_unavailable"


def test_narrate_highlight_rejects_a_source_published_after_the_trade() -> None:
    app = create_app()
    app.state.portfolio_highlight_narrator = _FutureSourceNarrator()

    with TestClient(app) as client:
        response = client.post(
            _NARRATE_PATH,
            json={"review": _complete_review(), "highlight_index": 0},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "portfolio_narrative_unavailable"


def test_portfolio_parse_uses_its_bounded_path_specific_body_limit() -> None:
    app = create_app(max_body_bytes=128)
    valid_payload_above_global_limit = {
        "rows": [
            {
                "成交日期": "2026-09-03",
                "证券代码": "600519",
                "证券名称": "贵州茅台",
                "买卖方向": "买入",
                "成交数量": "1",
                "成交价格": "1500",
                "券商备注": "x" * 2048,
            }
        ]
    }
    oversized_body = b'{"oversized":"' + (b"x" * (2 * 1024 * 1024)) + b'"}'

    with TestClient(app) as client:
        accepted = client.post(_PARSE_PATH, json=valid_payload_above_global_limit)
        rejected = client.post(
            _PARSE_PATH,
            content=oversized_body,
            headers={
                "Content-Type": "application/json",
                "X-Request-ID": "portfolio-too-big",
            },
        )

    assert accepted.status_code == 200
    assert rejected.status_code == 413
    assert rejected.json() == {
        "error": {
            "code": "request_body_too_large",
            "message": "Request body exceeds 2097152 bytes",
            "details": [],
        },
        "request_id": "portfolio-too-big",
    }


def _demo_ledger_rows() -> list[dict[str, object]]:
    return [
        {
            "成交日期": "2026-08-29 10:05:00",
            "证券代码": "600519",
            "证券名称": "贵州茅台",
            "交易市场": "沪市",
            "业务名称": "买入",
            "成交数量": "20",
            "成交价格": "1450",
            "手续费": "5",
            "股份余额": "20",
            "最新价": "1450",
            "币种": "人民币",
            "本位币": "CNY",
        },
        {
            "成交日期": "2026-08-29 10:12:00",
            "证券代码": "00700",
            "证券名称": "腾讯控股",
            "交易市场": "香港",
            "业务名称": "买入",
            "成交数量": "100",
            "成交价格": "550",
            "手续费": "10",
            "股份余额": "100",
            "最新价": "550",
            "币种": "港币",
            "本位币": "CNY",
            "折算汇率": "0.92",
        },
        {
            "成交日期": "2026-09-03 14:30:00",
            "证券代码": "600519",
            "证券名称": "贵州茅台",
            "交易市场": "沪市",
            "业务名称": "卖出",
            "成交数量": "10",
            "成交价格": "1550",
            "手续费": "5",
            "已实现盈亏": "995",
            "股份余额": "10",
            "最新价": "1560",
            "币种": "人民币",
            "本位币": "CNY",
        },
        {
            "成交日期": "2026-09-03 14:30:00",
            "证券代码": "00700",
            "证券名称": "腾讯控股",
            "交易市场": "香港",
            "业务名称": "卖出",
            "成交数量": "20",
            "成交价格": "610",
            "手续费": "10",
            "已实现盈亏": "1100",
            "股份余额": "80",
            "最新价": "605",
            "币种": "港币",
            "本位币": "CNY",
            "折算汇率": "0.92",
        },
    ]


def _holding_only_review() -> dict[str, Any]:
    return {
        "import_metadata": {"method": "manual", "confirmed": True},
        "account": {
            "period_start": "2026-09-01",
            "period_end": "2026-09-02",
            "base_currency": "CNY",
            "timezone": "Asia/Shanghai",
        },
        "holdings": [
            {
                "source_id": "holding-only-1",
                "as_of": "2026-09-02T16:00:00+08:00",
                "market": "CN_A",
                "symbol": "600519",
                "name": "贵州茅台",
                "quantity": "10",
                "market_price": "1500",
                "average_cost": "1400",
                "currency": "CNY",
            }
        ],
    }


def _fifo_review(*, buy_lots: tuple[tuple[str, str], ...], sold: str) -> dict[str, Any]:
    buy_trades = [
        {
            "source_id": f"fifo-buy-{index}",
            "executed_at": f"2026-09-01T{9 + index:02d}:00:00+08:00",
            "market": "CN_A",
            "symbol": "000001",
            "name": "平安银行",
            "side": "buy",
            "quantity": quantity,
            "price": price,
            "fees": "0",
            "currency": "CNY",
        }
        for index, (quantity, price) in enumerate(buy_lots, start=1)
    ]
    return {
        "import_metadata": {"method": "csv_xlsx", "confirmed": True},
        "account": {
            "period_start": "2026-09-01",
            "period_end": "2026-09-02",
            "base_currency": "CNY",
            "timezone": "Asia/Shanghai",
        },
        "trades": [
            *buy_trades,
            {
                "source_id": "fifo-sell-1",
                "executed_at": "2026-09-02T10:00:00+08:00",
                "market": "CN_A",
                "symbol": "000001",
                "name": "平安银行",
                "side": "sell",
                "quantity": sold,
                "price": "300",
                "fees": "0",
                "currency": "CNY",
            },
        ],
    }


def test_builtin_demo_parse_then_confirmed_analyze_is_one_working_chain() -> None:
    with TestClient(create_app()) as client:
        parsed = client.post(_PARSE_PATH, json={"rows": _demo_ledger_rows()})
        assert parsed.status_code == 200
        import_result = parsed.json()
        assert import_result["reconciliation"]["can_analyze_after_confirmation"] is True
        review = {
            "profile": import_result["profile"],
            "import_metadata": {
                "method": import_result["import_metadata"]["method"],
                "confirmed": True,
            },
            **import_result["draft"],
        }
        analyzed = client.post(_ANALYZE_PATH, json=review)

    assert analyzed.status_code == 200
    payload = analyzed.json()
    assert len(payload["holdings"]) == 2
    assert len(payload["trade_replay"]["events"]) == 4
    assert payload["capabilities"]["snapshot"] is True
    assert payload["capabilities"]["trade_replay"] is True
    assert payload["capabilities"]["attribution"] is True


def test_parse_normalizes_explicit_shenzhen_short_code_and_rejects_ambiguous_short_code() -> None:
    explicit_shenzhen = {
        "成交日期": "2026-09-03 10:00:00",
        "证券代码": 1,
        "证券名称": "平安银行",
        "交易市场": "深圳",
        "业务名称": "买入",
        "成交数量": "10",
        "成交价格": "10",
        "股份余额": "10",
        "最新价": "10",
    }
    ambiguous_short_code = {
        "成交日期": "2026-09-03 10:01:00",
        "证券代码": 1,
        "证券名称": "未确定证券",
        "业务名称": "买入",
        "成交数量": "10",
        "成交价格": "10",
    }
    unambiguous_control = {
        "成交日期": "2026-09-03 09:59:00",
        "证券代码": "600519",
        "证券名称": "贵州茅台",
        "业务名称": "买入",
        "成交数量": "1",
        "成交价格": "1500",
    }

    with TestClient(create_app()) as client:
        explicit = client.post(_PARSE_PATH, json={"rows": [explicit_shenzhen]})
        ambiguous = client.post(
            _PARSE_PATH,
            json={"rows": [unambiguous_control, ambiguous_short_code]},
        )

    assert explicit.status_code == 200
    assert explicit.json()["draft"]["trades"][0]["symbol"] == "000001.SZ"
    assert explicit.json()["draft"]["holdings"][0]["symbol"] == "000001.SZ"
    assert ambiguous.status_code == 200
    ambiguous_payload = ambiguous.json()
    assert [item["symbol"] for item in ambiguous_payload["draft"]["trades"]] == ["600519.SH"]
    assert ambiguous_payload["unresolved_rows"][0]["source_index"] == 1
    assert "security code/market" in ambiguous_payload["unresolved_rows"][0]["reason"]


def test_reverse_ordered_ledger_does_not_invent_an_opening_position_gap() -> None:
    rows = [
        {
            "成交日期": "2026-09-02 10:00:00",
            "证券代码": "000001",
            "证券名称": "平安银行",
            "业务名称": "卖出",
            "成交数量": "10",
            "成交价格": "12",
        },
        {
            "成交日期": "2026-09-01 10:00:00",
            "证券代码": "000001",
            "证券名称": "平安银行",
            "业务名称": "买入",
            "成交数量": "10",
            "成交价格": "10",
        },
    ]

    with TestClient(create_app()) as client:
        response = client.post(_PARSE_PATH, json={"rows": rows})

    assert response.status_code == 200
    reconciliation = response.json()["reconciliation"]
    assert reconciliation["needs_opening_positions"] is False
    assert all("先卖后买" not in reason for reason in reconciliation["reasons"])


def test_same_second_economically_identical_trades_with_distinct_source_ids_are_retained() -> None:
    review = _trade_only_review()
    first = review["trades"][0]
    review["trades"] = [
        {**first, "source_id": "same-second-1"},
        {**first, "source_id": "same-second-2"},
    ]

    with TestClient(create_app()) as client:
        response = client.post(_ANALYZE_PATH, json=review)

    assert response.status_code == 200
    assert [item["source_id"] for item in response.json()["trade_replay"]["events"]] == [
        "same-second-1",
        "same-second-2",
    ]


def test_negative_fx_and_negative_average_cost_are_validation_errors_not_500s() -> None:
    negative_cost = _holding_only_review()
    negative_cost["holdings"][0]["average_cost"] = "-1"
    negative_fx = _holding_only_review()
    negative_fx["holdings"][0].update(
        {"market": "HK", "symbol": "00700", "currency": "HKD", "fx_to_base": "-0.9"}
    )

    with TestClient(create_app()) as client:
        responses = [
            client.post(_ANALYZE_PATH, json=negative_cost),
            client.post(_ANALYZE_PATH, json=negative_fx),
        ]

    assert [response.status_code for response in responses] == [422, 422]
    assert all(
        response.json()["error"]["code"] == "request_validation_failed" for response in responses
    )


def test_sparse_daily_equity_is_exposed_as_partial_performance_capability() -> None:
    review = {
        "import_metadata": {"method": "csv_xlsx", "confirmed": True},
        "account": {
            "period_start": "2026-09-01",
            "period_end": "2026-09-04",
            "base_currency": "CNY",
            "timezone": "Asia/Shanghai",
        },
        "daily_equity": [
            {"source_id": "sparse-1", "at": "2026-09-01", "equity_base": "100"},
            {"source_id": "sparse-2", "at": "2026-09-04", "equity_base": "102"},
        ],
    }

    with TestClient(create_app()) as client:
        response = client.post(_ANALYZE_PATH, json=review)

    assert response.status_code == 200
    payload = response.json()
    assert payload["data_capabilities"]["performance"]["grade"] == "partial"
    assert payload["data_capabilities"]["performance"]["available"] is True
    assert payload["capabilities"]["performance"] is False
    assert "performance" in payload


def test_daily_equity_external_flow_matches_the_account_summary() -> None:
    review = {
        "import_metadata": {"method": "csv_xlsx", "confirmed": True},
        "account": {
            "period_start": "2026-09-01",
            "period_end": "2026-09-02",
            "base_currency": "CNY",
            "timezone": "Asia/Shanghai",
        },
        "daily_equity": [
            {"source_id": "flow-equity-1", "at": "2026-09-01", "equity_base": "100"},
            {
                "source_id": "flow-equity-2",
                "at": "2026-09-02",
                "equity_base": "160",
                "external_flow_base": "50",
            },
        ],
    }

    with TestClient(create_app()) as client:
        response = client.post(_ANALYZE_PATH, json=review)

    assert response.status_code == 200
    payload = response.json()
    assert payload["performance"]["points"][1]["external_flow_base"] == "50"
    assert payload["summary"]["net_external_flow_base"] == "50"
    assert payload["summary"]["external_flow_base"] == "50"


def test_holdings_only_does_not_present_securities_value_as_ending_account_equity() -> None:
    with TestClient(create_app()) as client:
        response = client.post(_ANALYZE_PATH, json=_holding_only_review())

    assert response.status_code == 200
    summary = response.json()["summary"]
    assert summary["total_market_value_base"] == "15000"
    assert "ending_equity_base" not in summary


def test_margin_and_short_sale_rows_remain_unresolved_in_the_cash_profile() -> None:
    rows = [
        {
            "成交日期": "2026-09-03 10:00:00",
            "证券代码": "600519",
            "证券名称": "贵州茅台",
            "业务名称": "买入",
            "成交数量": "1",
            "成交价格": "1500",
            "股份余额": "1",
            "最新价": "1500",
        },
        {
            "成交日期": "2026-09-03 10:01:00",
            "证券代码": "000001",
            "证券名称": "平安银行",
            "业务名称": "融资买入",
            "成交数量": "10",
            "成交价格": "10",
        },
        {
            "成交日期": "2026-09-03 10:02:00",
            "证券代码": "000001",
            "证券名称": "平安银行",
            "业务名称": "融券卖出",
            "成交数量": "10",
            "成交价格": "10",
        },
    ]

    with TestClient(create_app()) as client:
        response = client.post(_PARSE_PATH, json={"rows": rows})

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["draft"]["trades"]) == 1
    assert [item["source_index"] for item in payload["unresolved_rows"]] == [1, 2]
    assert all("unsupported" in item["reason"] for item in payload["unresolved_rows"])


def test_fifo_derives_realized_pnl_only_when_confirmed_buys_cover_the_sale() -> None:
    with TestClient(create_app()) as client:
        covered = client.post(
            _ANALYZE_PATH,
            json=_fifo_review(buy_lots=(("5", "100"), ("5", "200")), sold="6"),
        )
        uncovered = client.post(
            _ANALYZE_PATH,
            json=_fifo_review(buy_lots=(("2", "100"), ("2", "200")), sold="6"),
        )

    assert covered.status_code == 200
    covered_payload = covered.json()
    assert covered_payload["data_capabilities"]["attribution"]["grade"] == "available"
    # 5 shares use the first 100 CNY lot and 1 share uses the 200 CNY lot.
    assert covered_payload["attribution"]["verified_pnl_base"] == "1100"
    assert covered_payload["attribution"]["entries"][0]["source_id"] == "fifo-sell-1"
    assert covered_payload["highlights"][0]["amount_base"] == "1100"

    assert uncovered.status_code == 200
    uncovered_payload = uncovered.json()
    assert uncovered_payload["data_capabilities"]["attribution"]["grade"] == "unavailable"
    assert "attribution" not in uncovered_payload
    assert uncovered_payload["highlights"] == []


def test_supplemental_holding_overlays_one_symbol_without_dropping_other_broker_balance() -> None:
    supplemental = [
        {
            "持仓日期": "2026-09-03 14:30:00",
            "证券代码": "600519",
            "证券名称": "贵州茅台",
            "交易市场": "沪市",
            "持仓数量": "12",
            "最新价": "1600",
            "币种": "CNY",
        }
    ]
    with TestClient(create_app()) as client:
        parsed = client.post(
            _PARSE_PATH,
            json={"rows": _demo_ledger_rows(), "current_holding_rows": supplemental},
        )
        assert parsed.status_code == 200
        import_result = parsed.json()
        confirmed = {
            "profile": import_result["profile"],
            "import_metadata": {"method": "csv_xlsx", "confirmed": True},
            **import_result["draft"],
        }
        analyzed = client.post(_ANALYZE_PATH, json=confirmed)

    holdings = {item["symbol"]: item for item in import_result["draft"]["holdings"]}
    assert set(holdings) == {"600519.SH", "00700.HK"}
    assert holdings["600519.SH"]["quantity"] == "12"
    assert holdings["600519.SH"]["source_id"] == "holding-row-1"
    assert holdings["00700.HK"]["quantity"] == "80"
    assert holdings["00700.HK"]["source_id"].endswith(":closing-holding")
    assert import_result["reconciliation"]["can_analyze_after_confirmation"] is True
    assert analyzed.status_code == 200


def test_mixed_holding_snapshot_times_are_not_ready_for_confirmation() -> None:
    with TestClient(create_app()) as client:
        response = client.post(
            _PARSE_PATH,
            json={
                "rows": _demo_ledger_rows(),
                "current_holding_rows": [
                    {
                        "持仓日期": "2026-09-04",
                        "证券代码": "600519",
                        "证券名称": "贵州茅台",
                        "交易市场": "沪市",
                        "持仓数量": "12",
                        "最新价": "1600",
                        "币种": "CNY",
                    }
                ],
            },
        )

    assert response.status_code == 200
    reconciliation = response.json()["reconciliation"]
    assert reconciliation["status"] == "needs_supplement"
    assert reconciliation["can_analyze_after_confirmation"] is False
    assert any("同一 as_of" in reason for reason in reconciliation["reasons"])


def test_unresolved_company_action_blocks_confirmation_before_fifo_analysis() -> None:
    rows = [
        {
            "成交日期": "2026-9-1",
            "证券代码": "600519",
            "业务名称": "买入",
            "成交数量": "10",
            "成交价格": "100",
            "股份余额": "10",
            "最新价": "100",
        },
        {
            "成交日期": "2026-9-2",
            "证券代码": "600519",
            "业务名称": "送股",
            "成交数量": "2",
        },
        {
            "成交日期": "2026/9/3",
            "证券代码": "600519",
            "业务名称": "卖出",
            "成交数量": "5",
            "成交价格": "200",
            "股份余额": "5",
            "最新价": "200",
        },
    ]
    with TestClient(create_app()) as client:
        response = client.post(_PARSE_PATH, json={"rows": rows})

    assert response.status_code == 200
    reconciliation = response.json()["reconciliation"]
    assert reconciliation["can_analyze_after_confirmation"] is False
    assert any("公司行动" in reason for reason in reconciliation["reasons"])


def test_same_day_date_only_buy_sell_does_not_derive_fifo_pnl() -> None:
    review = _fifo_review(buy_lots=(("10", "100"),), sold="5")
    for trade in review["trades"]:
        trade["executed_at"] = "2026-09-01T00:00:00+08:00"
        trade["timestamp_precision"] = "date_only"

    with TestClient(create_app()) as client:
        response = client.post(_ANALYZE_PATH, json=review)

    assert response.status_code == 200
    payload = response.json()
    assert payload["data_capabilities"]["attribution"]["grade"] == "unavailable"
    assert "attribution" not in payload
    assert payload["highlights"] == []


def test_summary_keeps_cash_flows_outside_the_equity_observation_window() -> None:
    review = {
        "import_metadata": {"method": "csv_xlsx", "confirmed": True},
        "account": {
            "period_start": "2026-09-01",
            "period_end": "2026-09-03",
            "base_currency": "CNY",
            "timezone": "Asia/Shanghai",
        },
        "cash_flows": [
            {
                "source_id": "pre-equity-deposit",
                "occurred_at": "2026-09-01T09:00:00+08:00",
                "kind": "deposit",
                "amount": "100",
                "currency": "CNY",
            }
        ],
        "daily_equity": [
            {"source_id": "later-equity-1", "at": "2026-09-02", "equity_base": "100"},
            {"source_id": "later-equity-2", "at": "2026-09-03", "equity_base": "101"},
        ],
    }
    with TestClient(create_app()) as client:
        response = client.post(_ANALYZE_PATH, json=review)

    assert response.status_code == 200
    assert response.json()["summary"]["net_external_flow_base"] == "100"


def test_parser_accepts_common_unpadded_slash_and_dash_dates() -> None:
    with TestClient(create_app()) as client:
        response = client.post(
            _PARSE_PATH,
            json={
                "rows": [
                    {
                        "成交日期": "2026/9/3",
                        "证券代码": "600519",
                        "业务名称": "买入",
                        "成交数量": "1",
                        "成交价格": "100",
                    },
                    {
                        "成交日期": "2026-9-3",
                        "证券代码": "600519",
                        "业务名称": "买入",
                        "成交数量": "1",
                        "成交价格": "100",
                    },
                ]
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["draft"]["trades"]) == 2
    assert all(item["timestamp_precision"] == "date_only" for item in payload["draft"]["trades"])
    assert payload["inferred_account"]["period_start"] == "2026-09-03"
    assert payload["inferred_account"]["period_end"] == "2026-09-03"
