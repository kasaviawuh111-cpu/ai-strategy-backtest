from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import cast
from zoneinfo import ZoneInfo

from ashare_lab.application.portfolio_review import (
    CashFlowRecord,
    DailyEquityRecord,
    HoldingRecord,
    PortfolioReviewInput,
    ReviewAccount,
    analyze_portfolio_review,
)

TZ = ZoneInfo("Asia/Shanghai")


def _account() -> ReviewAccount:
    return ReviewAccount(
        period_start=date(2026, 9, 1),
        period_end=date(2026, 9, 2),
        base_currency="CNY",
        timezone="Asia/Shanghai",
    )


def test_ttwror_separates_external_inflow_and_outflow() -> None:
    source = PortfolioReviewInput(
        profile="CN_A_HK_CASH_V1",
        import_method="csv_xlsx",
        confirmed=True,
        account=_account(),
        holdings=(),
        trades=(),
        cash_flows=(
            CashFlowRecord(
                source_id="flow-in",
                content_sha256=None,
                occurred_at=datetime(2026, 9, 2, 9, tzinfo=TZ),
                kind="deposit",
                amount=Decimal("10"),
                currency="CNY",
                fx_to_base=None,
                external=True,
            ),
            CashFlowRecord(
                source_id="flow-out",
                content_sha256=None,
                occurred_at=datetime(2026, 9, 2, 14, tzinfo=TZ),
                kind="withdrawal",
                amount=Decimal("5"),
                currency="CNY",
                fx_to_base=None,
                external=True,
            ),
        ),
        daily_equity=(
            DailyEquityRecord(
                source_id="equity-1",
                content_sha256=None,
                at=date(2026, 9, 1),
                equity_base=Decimal("100"),
                external_inflow_base=None,
                external_outflow_base=None,
            ),
            DailyEquityRecord(
                source_id="equity-2",
                content_sha256=None,
                at=date(2026, 9, 2),
                equity_base=Decimal("110"),
                external_inflow_base=None,
                external_outflow_base=None,
            ),
        ),
        market_ticks=(),
    )

    payload = analyze_portfolio_review(source).payload

    performance = payload["performance"]
    assert isinstance(performance, dict)
    assert performance["twr"] == Decimal("115") / Decimal("110") - Decimal("1")
    points = performance["points"]
    assert isinstance(points, list)
    typed_points = cast(list[dict[str, object]], points)
    assert typed_points[1]["external_inflow_base"] == Decimal("10")
    assert typed_points[1]["external_outflow_base"] == Decimal("5")
    assert payload["snapshot"] is None


def test_holdings_only_never_creates_historical_attribution_or_highlight() -> None:
    source = PortfolioReviewInput(
        profile="CN_A_HK_CASH_V1",
        import_method="manual",
        confirmed=True,
        account=_account(),
        holdings=(
            HoldingRecord(
                source_id="holding-1",
                content_sha256=None,
                as_of=datetime(2026, 9, 2, 16, tzinfo=TZ),
                market="CN_A",
                symbol="600519.SH",
                name="贵州茅台",
                quantity=Decimal("100"),
                market_price=Decimal("1500"),
                average_cost=Decimal("1200"),
                currency="CNY",
                fx_to_base=None,
            ),
        ),
        trades=(),
        cash_flows=(),
        daily_equity=(),
        market_ticks=(),
    )

    payload = analyze_portfolio_review(source).payload

    assert payload["snapshot"] is not None
    assert payload["attribution"] is None
    assert payload["highlights"] == []
    capabilities = payload["capabilities"]
    assert isinstance(capabilities, dict)
    assert capabilities["snapshot"] is True
    assert capabilities["performance"] is False
