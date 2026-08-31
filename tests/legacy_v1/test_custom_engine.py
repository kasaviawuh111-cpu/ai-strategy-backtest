"""Known legacy behavior for the custom atom-combination engine."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import numpy as np

from astock_backtest.models.backtest_custom import BacktestCustomRequest
from astock_backtest.services.backtest_custom_engine import run_backtest_custom
from astock_backtest.signal_atoms import ATOM_REGISTRY, DataDep, SignalAtom, SignalCategory
from tests.legacy_v1.fakes import STOCK_CODE, date_label, make_ohlcv, make_store

ATOM_ID = "__legacy_trigger_at_index_1__"


def _trigger_at_index_1(ohlcv, **_):
    out = np.zeros(len(ohlcv), dtype=np.int8)
    if len(out) > 1:
        out[1] = 1
    return out


TEST_ATOM = SignalAtom(
    id=ATOM_ID,
    professional_name="Legacy fixture trigger",
    layman_name="Legacy fixture trigger",
    category=SignalCategory.TECHNICAL,
    deps=(DataDep.OHLCV,),
    min_warmup=0,
    compute_fn=_trigger_at_index_1,
)


def run_known_defect_case(future_exit_close: float):
    closes = [10, 10, 11, 14, future_exit_close, 20, 21]
    ohlcv = make_ohlcv(
        opens=[10, 10, 10, 13, 15, 20, 21],
        closes=closes,
    )
    request = BacktestCustomRequest(
        stock_code=STOCK_CODE,
        buy_atoms=[ATOM_ID],
        holding_days=3,
        start_date=date_label(ohlcv, 0),
        end_date=date_label(ohlcv, 3),
        initial_capital=100_000.0,
        commission=0.0,
        stamp_tax=0.0,
        slippage=0.0,
    )
    with patch.dict(ATOM_REGISTRY, {ATOM_ID: TEST_ATOM}):
        result = run_backtest_custom(request, make_store(ohlcv))
    return ohlcv, result


class LegacyCustomEngineCharacterizationTests(unittest.TestCase):
    def test_known_defect_end_date_uses_price_from_later_bar(self):
        """Freeze the current prefix-invariance violation for migration diffing.

        The holding rule exits at full-data index 4, which is after the requested
        end_date at index 3. Legacy code moves the SELL event back to index 3 but
        keeps index 4's close as its price. Changing only that future close thus
        changes the result reported on end_date.
        """

        ohlcv_99, result_99 = run_known_defect_case(99.0)
        _ohlcv_123, result_123 = run_known_defect_case(123.0)

        end_date = date_label(ohlcv_99, 3)
        self.assertEqual(result_99["end_date"], end_date)
        self.assertEqual(result_123["end_date"], end_date)
        self.assertEqual(
            [(trade["date"], trade["action"], trade["price"]) for trade in result_99["trade_log"]],
            [
                (date_label(ohlcv_99, 2), "BUY", 10.0),
                (end_date, "SELL", 99.0),
            ],
        )
        self.assertEqual(result_99["trade_log"][-1]["reason"], "hit_hold_n_days_3")
        self.assertEqual(result_123["trade_log"][-1]["date"], end_date)
        self.assertEqual(result_123["trade_log"][-1]["price"], 123.0)
        self.assertEqual(result_99["equity_curve"][-1], {"date": end_date, "value": 990_000.0})
        self.assertEqual(result_123["equity_curve"][-1], {"date": end_date, "value": 1_230_000.0})


if __name__ == "__main__":
    unittest.main()
