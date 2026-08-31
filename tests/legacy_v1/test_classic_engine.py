"""Golden behavior for the legacy v1 classic strategy engine.

These tests intentionally describe current behavior. They are not approval of
the market-model assumptions. New execution semantics belong in a separate v2
acceptance suite.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from astock_backtest.services.backtest_engine import run_backtest
from astock_backtest.strategies import STRATEGY_REGISTRY
from astock_backtest.strategies.base import Signal
from tests.legacy_v1.fakes import (
    STOCK_CODE,
    ScriptedStrategy,
    date_label,
    make_ohlcv,
    make_store,
    signal_plan,
)

STRATEGY_ID = "__legacy_scripted__"


class LegacyClassicEngineCharacterizationTests(unittest.TestCase):
    def run_scripted(self, ohlcv, signals):
        with patch.dict(STRATEGY_REGISTRY, {STRATEGY_ID: ScriptedStrategy}):
            return run_backtest(
                store=make_store(ohlcv),
                stock_code=STOCK_CODE,
                strategy_id=STRATEGY_ID,
                params={"signals": signal_plan(ohlcv, signals)},
                initial_capital=100_000.0,
                commission=0.0,
                stamp_tax=0.0,
                slippage=0.0,
            )

    def test_normal_trade_uses_next_open_and_full_lot_position(self):
        ohlcv = make_ohlcv(
            opens=[10, 10, 11, 12, 13],
            closes=[10, 10.5, 11, 12.5, 13],
        )
        result = self.run_scripted(
            ohlcv,
            {
                0: Signal.BUY,
                2: Signal.SELL,
            },
        )

        self.assertEqual(
            result["trade_log"],
            [
                {
                    "date": date_label(ohlcv, 1),
                    "action": "BUY",
                    "price": 10.0,
                    "shares": 10_000,
                    "cash_flow": -100_000.0,
                    "pnl": None,
                    "reason": "signal",
                },
                {
                    "date": date_label(ohlcv, 3),
                    "action": "SELL",
                    "price": 12.0,
                    "shares": 10_000,
                    "cash_flow": 120_000.0,
                    "pnl": 20_000.0,
                    "reason": "signal",
                },
            ],
        )
        self.assertEqual(
            [point["value"] for point in result["equity_curve"]],
            [100_000.0, 105_000.0, 110_000.0, 120_000.0, 120_000.0],
        )
        self.assertEqual(result["total_trades"], 1)
        self.assertEqual(result["win_rate"], 1.0)
        self.assertAlmostEqual(result["total_return"], 0.2)
        self.assertEqual(result["warnings"], ["总交易次数 1 偏少，结果可能失真"])

    def test_buy_day_sell_signal_is_dropped_by_legacy_t_plus_one_guard(self):
        """Current engine drops, rather than defers, a buy-day SELL signal."""

        ohlcv = make_ohlcv(
            opens=[10, 10, 10, 10],
            closes=[10, 10, 10, 10],
        )
        result = self.run_scripted(
            ohlcv,
            {
                0: Signal.BUY,
                1: Signal.SELL,
            },
        )

        self.assertEqual([trade["action"] for trade in result["trade_log"]], ["BUY"])
        self.assertEqual(result["trade_log"][0]["date"], date_label(ohlcv, 1))
        self.assertEqual(result["total_trades"], 0)

    def test_limit_up_cancels_pending_buy_instead_of_carrying_it(self):
        ohlcv = make_ohlcv(
            opens=[10, 11, 12],
            closes=[10, 11, 12],
            pct_changes=[0, 10.0, 0],
        )
        result = self.run_scripted(ohlcv, {0: Signal.BUY})

        self.assertEqual(result["trade_log"], [])
        self.assertEqual(
            result["warnings"],
            [
                f"{date_label(ohlcv, 1)} 涨停无法买入，信号作废",
                "总交易次数 0 偏少，结果可能失真",
            ],
        )

    def test_limit_down_carries_pending_sell_until_next_non_limit_day(self):
        ohlcv = make_ohlcv(
            opens=[10, 10, 9, 8, 8],
            closes=[10, 10, 9, 8, 8],
            pct_changes=[0, 0, 0, -10.0, 0],
        )
        result = self.run_scripted(
            ohlcv,
            {
                0: Signal.BUY,
                2: Signal.SELL,
            },
        )

        self.assertEqual(
            [(trade["date"], trade["action"], trade["price"]) for trade in result["trade_log"]],
            [
                (date_label(ohlcv, 1), "BUY", 10.0),
                (date_label(ohlcv, 4), "SELL", 8.0),
            ],
        )
        self.assertEqual(
            result["warnings"],
            [
                f"{date_label(ohlcv, 3)} 跌停无法卖出，信号延后",
                "总交易次数 1 偏少，结果可能失真",
            ],
        )


if __name__ == "__main__":
    unittest.main()
