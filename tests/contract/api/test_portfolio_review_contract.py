from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from ashare_lab.api import create_app
from ashare_lab.domain.market_data import DailyBar, DataSnapshotRef
from ashare_lab.domain.shared import InstrumentId, Price, Quantity, StrongId
from ashare_lab.ports.portfolio_highlight_narrative import (
    DriverConfidence,
    PortfolioHighlightNarrative,
    PortfolioLikelyDriver,
    PortfolioNarrativeSource,
)


def _trade_review() -> dict[str, object]:
    return {
        "profile": "CN_A_HK_CASH_V1",
        "import_metadata": {"method": "csv_xlsx", "confirmed": True},
        "account": {
            "period_start": "2026-08-29",
            "period_end": "2026-09-03",
            "base_currency": "CNY",
            "timezone": "Asia/Shanghai",
        },
        "trades": [
            {
                "source_id": "broker-row-1",
                "content_sha256": "sha256:" + "a" * 64,
                "executed_at": "2026-09-02T14:30:00+08:00",
                "market": "CN_A",
                "symbol": "600519",
                "name": "贵州茅台",
                "side": "SELL",
                "quantity": "10",
                "price": "1500",
                "fees": "5",
                "currency": "CNY",
                "realized_pnl": "1000",
            }
        ],
    }


def test_parse_broker_rows_infers_account_and_does_not_invent_holdings() -> None:
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/portfolio-reviews/imports/parse",
            json={
                "rows": [
                    {
                        "成交日期": "2026-08-29",
                        "证券代码": "600519",
                        "证券名称": "贵州茅台",
                        "买卖方向": "买入",
                        "成交数量": "100",
                        "成交价格": "1000",
                        "手续费": "5",
                    },
                    {
                        "成交日期": "2026-09-03 14:30:00",
                        "证券代码": "600519",
                        "证券名称": "贵州茅台",
                        "买卖方向": "卖出",
                        "成交数量": "100",
                        "成交价格": "1100",
                        "已实现盈亏": "9990",
                    },
                ]
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["inferred_account"] == {
        "period_start": "2026-08-29",
        "period_end": "2026-09-03",
        "base_currency": "CNY",
        "timezone": "Asia/Shanghai",
        "markets": ["CN_A"],
    }
    assert payload["draft"]["trades"][0]["symbol"] == "600519.SH"
    assert payload["draft"]["trades"][0]["timestamp_precision"] == "date_only"
    assert payload["draft"]["holdings"] == []
    assert payload["reconciliation"]["can_analyze_after_confirmation"] is True
    assert payload["reconciliation"]["needs_current_holdings"] is True


def test_parse_reconstructs_only_broker_reported_closing_balance_with_source_identity() -> None:
    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/portfolio-reviews/imports/parse",
            json={
                "rows": [
                    {
                        "成交日期": "2026-09-03 15:00:00",
                        "证券代码": "600519",
                        "证券名称": "贵州茅台",
                        "买卖方向": "买入",
                        "成交数量": "100",
                        "成交价格": "1000",
                        "股份余额": "100",
                        "最新价": "1100",
                    }
                ]
            },
        )

    assert response.status_code == 200
    payload = response.json()
    holding = payload["draft"]["holdings"][0]
    balance = payload["position_balances"][0]
    assert holding["quantity"] == "100"
    assert holding["market_price"] == "1100"
    assert holding["source_id"] == "broker-row-1:closing-holding"
    assert holding["content_sha256"].startswith("sha256:")
    assert balance["source_id"] == "broker-row-1"
    assert balance["content_sha256"] != holding["content_sha256"]
    assert payload["reconciliation"]["holding_source"] == "broker_reported_closing_balance"


def test_import_contract_is_broker_first_and_holdings_are_supplemental() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/api/v1/portfolio-reviews/import-contract")

    assert response.status_code == 200
    payload = response.json()
    assert payload["parse_limits"]["max_request_bytes"] == 2 * 1024 * 1024
    assert payload["required_csv_types"] == []
    holding_contract = next(item for item in payload["csv_types"] if item["name"] == "holdings")
    assert holding_contract["required"] is False
    assert any("Broker trade/delivery rows are the primary" in item for item in payload["rules"])


def test_analyze_accepts_trade_only_and_returns_only_supported_results() -> None:
    with TestClient(create_app()) as client:
        response = client.post("/api/v1/portfolio-reviews/analyze", json=_trade_review())

    assert response.status_code == 200
    payload = response.json()
    assert "snapshot" not in payload
    assert "performance" not in payload
    assert payload["capabilities"] == {
        "snapshot": False,
        "trade_replay": True,
        "performance": False,
        "attribution": True,
        "market_tick_replay": False,
    }
    assert payload["trade_replay"]["events"][0]["source_id"] == "broker-row-1"
    assert payload["highlights"][0]["source_id"] == "broker-row-1"
    assert payload["narrative_status"]["available"] is False


def test_imported_tick_label_stays_unverified_and_narration_without_key_is_503() -> None:
    review = _trade_review()
    review["market_ticks"] = [
        {
            "at": "2026-09-02T14:30:00+08:00",
            "granularity": "tick",
            "market": "CN_A",
            "symbol": "600519",
            "last_price": "1500",
            "currency": "CNY",
        }
    ]
    with TestClient(create_app()) as client:
        analysis = client.post("/api/v1/portfolio-reviews/analyze", json=review)
        narrative = client.post(
            "/api/v1/portfolio-reviews/narrate-highlight",
            json={"review": review, "highlight_index": 0},
        )

    assert analysis.status_code == 200
    payload = analysis.json()
    assert payload["capabilities"]["market_tick_replay"] is False
    assert payload["data_capabilities"]["market_tick_replay"]["grade"] == "partial"
    assert "customer-imported" in payload["data_capabilities"]["market_tick_replay"]["reason"]
    assert narrative.status_code == 503
    assert narrative.json()["error"]["code"] == "portfolio_narrative_not_configured"


class _CapturingNarrator:
    def __init__(self, *, published_at: str) -> None:
        self.published_at = published_at
        self.evidence: tuple[str, ...] = ()

    async def narrate(self, highlight: Any) -> PortfolioHighlightNarrative:
        performance_evidence = highlight.performance_evidence
        self.evidence = tuple(item.statement for item in performance_evidence)
        return PortfolioHighlightNarrative(
            likely_drivers=(
                PortfolioLikelyDriver(
                    reason="最可能：事件前公开信息影响了市场预期。",
                    confidence=DriverConfidence.MEDIUM,
                    source_ids=("src-1",),
                ),
            ),
            sources=(
                PortfolioNarrativeSource(
                    source_id="src-1",
                    title="事件前公告",
                    url="https://example.com/pre-event",
                    publisher="交易所",
                    published_at=self.published_at,
                ),
            ),
            unresolved=("无法确认单一因果。",),
        )


class _PITMarketData:
    def __init__(self) -> None:
        self.period_end: date | None = None

    def pin_snapshot(self, requirements: object, period: Any) -> DataSnapshotRef:
        self.period_end = period.end
        return DataSnapshotRef(
            snapshot_id=StrongId("snapshot:test"),
            checksum="sha256:" + "b" * 64,
            schema_version="test.v1",
            created_at=datetime(2026, 9, 1, tzinfo=UTC),
        )

    def load_daily_bars(
        self,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: object,
    ) -> tuple[DailyBar, ...]:
        del snapshot, period

        def bar(day: date, price: str, available_at: datetime) -> DailyBar:
            value = Decimal(price)
            return DailyBar(
                instrument_id=instrument_id,
                session_date=day,
                open=Price(value),
                high=Price(value),
                low=Price(value),
                close=Price(value),
                volume=Quantity(100),
                turnover=value * Decimal("100"),
                available_at=available_at,
            )

        return (
            bar(
                date(2026, 9, 1),
                "1400",
                datetime(2026, 9, 1, 15, tzinfo=UTC),
            ),
            bar(
                date(2026, 9, 2),
                "1500",
                datetime(2026, 9, 2, 15, tzinfo=UTC),
            ),
            bar(
                date(2026, 9, 3),
                "1600",
                datetime(2026, 9, 3, 15, tzinfo=UTC),
            ),
        )


def test_narrative_uses_only_market_facts_known_by_highlight_time() -> None:
    narrator = _CapturingNarrator(published_at="2026-09-01T09:00:00+08:00")
    market = _PITMarketData()
    app = create_app()
    app.state.portfolio_highlight_narrator = narrator
    app.state.portfolio_market_data = market

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/portfolio-reviews/narrate-highlight",
            json={"review": _trade_review(), "highlight_index": 0},
        )

    assert response.status_code == 200
    assert market.period_end == date(2026, 9, 2)
    assert response.json()["historical_market_evidence_count"] == 1
    assert any("1400" in item for item in narrator.evidence)
    assert all("1500" not in item and "1600" not in item for item in narrator.evidence[1:])


def test_narrative_rejects_source_published_after_highlight() -> None:
    narrator = _CapturingNarrator(published_at="2026-09-02T15:00:00+08:00")
    app = create_app()
    app.state.portfolio_highlight_narrator = narrator

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/portfolio-reviews/narrate-highlight",
            json={"review": _trade_review(), "highlight_index": 0},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "portfolio_narrative_unavailable"
