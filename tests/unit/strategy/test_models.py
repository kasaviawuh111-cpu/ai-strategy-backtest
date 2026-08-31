from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from pydantic import ValidationError

from ashare_lab.domain.strategy import (
    AllCondition,
    AnyCondition,
    NotCondition,
    StrategySpec,
    iter_indicator_conditions,
)

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE = ROOT / "contracts/examples/strategy.macd-volume.daily.v1.json"


class StrategySpecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = json.loads(EXAMPLE.read_text(encoding="utf-8"))

    def test_parses_daily_long_only_subset(self) -> None:
        strategy = StrategySpec.model_validate(self.payload)

        self.assertIsInstance(strategy.entry, AllCondition)
        self.assertEqual(strategy.instrument.symbol, "300059.SZ")
        self.assertEqual(strategy.instrument.position_mode, "long_only")
        self.assertTrue(strategy.execution.t_plus_one)
        self.assertEqual(
            [condition.indicator_id for condition in iter_indicator_conditions(strategy)],
            ["technical.macd", "market.volume", "technical.macd"],
        )

    def test_parses_any_and_not_nodes(self) -> None:
        payload = copy.deepcopy(self.payload)
        macd = payload["entry"]["children"][0]
        volume = payload["entry"]["children"][1]
        payload["entry"] = {
            "type": "any",
            "children": [macd, {"type": "not", "child": volume}],
        }

        strategy = StrategySpec.model_validate(payload)

        self.assertIsInstance(strategy.entry, AnyCondition)
        self.assertIsInstance(strategy.entry.children[1], NotCondition)

    def test_rejects_intraday_timeframe(self) -> None:
        payload = copy.deepcopy(self.payload)
        payload["entry"]["children"][0]["timeframe"] = "1m"

        with self.assertRaises(ValidationError):
            StrategySpec.model_validate(payload)

    def test_rejects_extra_fields(self) -> None:
        payload = copy.deepcopy(self.payload)
        payload["python_code"] = "print('must never run')"

        with self.assertRaises(ValidationError):
            StrategySpec.model_validate(payload)

    def test_first_of_requires_at_least_one_exit(self) -> None:
        payload = copy.deepcopy(self.payload)
        payload["exit"]["children"] = []

        with self.assertRaises(ValidationError):
            StrategySpec.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
