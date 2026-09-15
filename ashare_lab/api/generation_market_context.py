"""Bounded, read-only reuse of the real historical execution anchor loader."""

import asyncio
from decimal import Decimal
from typing import TYPE_CHECKING

from ashare_lab.domain.strategy import BacktestConfig, CatalogRef, Instrument, StrategySpec, execution_for_price_plan
from ashare_lab.domain.strategy.price_plans import GridParameters, GridPlan

if TYPE_CHECKING:
    from ashare_lab.application.skill_backtest_service import SkillBacktestService


async def load_generation_market_context(
    symbol: str, backtest: BacktestConfig, *,
    service: "SkillBacktestService", catalog: CatalogRef,
) -> dict[str, object]:
    # This neutral plan only requests a historical anchor. It is never saved,
    # suggested to the user or submitted to execution.
    params = GridParameters(anchor_mode="previous_close", lower_price=Decimal("0.01"), upper_price=Decimal(1_000_000),
                            initial_cash_cny=Decimal(backtest.initial_cash_cny))
    plan = GridPlan(parameters=params)
    strategy = StrategySpec(catalog=catalog, instrument=Instrument(symbol=symbol), backtest=backtest,
                            trading_plan=plan, execution=execution_for_price_plan(plan))

    async def historical() -> dict[str, object]:
        try:
            async with asyncio.timeout(30):
                bound = await service.resolve_grid_anchor(strategy)
            if not isinstance(bound.trading_plan, GridPlan):
                raise ValueError("historical grid anchor was not bound")
            pinned = bound.trading_plan.parameters
            return {"status": "ready", "price": str(pinned.resolved_anchor), "unit": "CNY",
                    "basis": "backtest_start_previous_close", "date": pinned.anchor_quote_time_label,
                    "source": pinned.anchor_quote_source}
        except (TimeoutError, ValueError, RuntimeError, OSError):
            # No raw exceptions/credentials/provider text enter the prompt.
            return {"status": "unavailable", "reason": "historical_anchor_not_ready"}

    anchor = await historical()
    return {"symbol": symbol, "backtest": {"start": backtest.start.isoformat(), "end": backtest.end.isoformat()},
            "initialGridAnchor": anchor,
            "initialCashCny": str(backtest.initial_cash_cny), "priceTickCny": "0.01",
            "tPlusOne": True, "positionsKnown": False}
