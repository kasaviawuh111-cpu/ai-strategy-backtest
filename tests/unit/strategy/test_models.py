from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from pydantic import ValidationError

from ashare_lab.domain.strategy import (
    AllCondition,
    AnyCondition,
    FirstOfExit,
    NotCondition,
    StrategySpec,
    iter_indicator_conditions,
)

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE = ROOT / "contracts/examples/strategy.macd-volume.daily.v1.json"


class StrategySpecTests(unittest.TestCase):
    def test_minute_protection_requires_explicit_hybrid_not_silent_daily_default(self):
        from ashare_lab.domain.strategy import HybridExecutionPolicy, MinuteProtectionExit
        payload = copy.deepcopy(self.payload)
        payload["exit"] = {"op": "first_of", "children": [
            {"type": "minute_protection_exit", "take_profit_pct": "5", "stop_loss_pct": "3"},
            {"type": "holding_period_exit", "sessions": 5},
        ]}
        with self.assertRaises(ValidationError):
            StrategySpec.model_validate(payload)
        payload["execution"] = HybridExecutionPolicy().model_dump()
        parsed = StrategySpec.model_validate(payload)
        self.assertIsInstance(parsed.exit.children[0], MinuteProtectionExit)
        self.assertEqual(len(tuple(iter_indicator_conditions(parsed))), 2)
        self.assertEqual(parsed.entry.model_dump(mode="json"), self.payload["entry"])
        self.assertEqual(StrategySpec.model_validate_json(parsed.model_dump_json()), parsed)
        original = StrategySpec.model_validate(self.payload)
        self.assertEqual(original.execution.execution_resolution, "1d")

    def test_minute_protection_rejects_ambiguous_or_empty_combinations(self):
        from ashare_lab.domain.strategy import MinuteProtectionExit
        for values in ({}, {"stop_loss_pct": "100"}, {"take_profit_pct": "NaN"}, {"limit_price_cny": "-1", "stop_loss_pct": 3}):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                MinuteProtectionExit.model_validate(values)
        minute = {"type": "minute_protection_exit", "stop_loss_pct": 3}
        for values in (
            {"op": "all", "children": [minute, {"type": "holding_period_exit", "sessions": 5}]},
            {"op": "first_of", "children": [minute, minute]},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                FirstOfExit.model_validate(values)
        mixed_clock = FirstOfExit.model_validate({
            "op": "first_of", "children": [
                minute,
                {"type": "position_return_exit", "trigger": "take_profit", "threshold_pct": 5},
            ],
        })
        self.assertEqual(len(mixed_clock.children), 2)

    def test_equivalent_single_exit_and_group_alias_keep_canonical_payload(self) -> None:
        rule = {"type": "holding_period_exit", "sessions": 5}
        canonical = FirstOfExit.model_validate({"op": "first_of", "children": [rule]})
        for payload in (rule, {"type": "first_of", "children": [rule]}):
            original = copy.deepcopy(payload)
            self.assertEqual(FirstOfExit.model_validate(payload), canonical)
            self.assertEqual(payload, original)

    def test_exit_shape_recovery_does_not_drop_conflicts_or_invalid_parameters(self) -> None:
        for payload in (
            {"type": "holding_period_exit", "sessions": 0},
            {"type": "holding_period_exit", "sessions": 5, "unknown": 1},
            {"type": "first_of", "op": "all", "children": [{"type": "holding_period_exit", "sessions": 5}]},
            {"type": "unsupported_exit", "sessions": 5},
            {"type": {"unexpected": True}, "sessions": 5},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                FirstOfExit.model_validate(payload)

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

    def test_all_rejects_simultaneous_profit_and_loss_on_the_same_closing_return(self) -> None:
        exits = [
            {"type": "position_return_exit", "trigger": "take_profit", "threshold_pct": 20},
            {"type": "position_return_exit", "trigger": "stop_loss", "threshold_pct": 5},
        ]
        for children in (exits, list(reversed(exits))):
            with self.subTest(children=children):
                payload = copy.deepcopy(self.payload)
                payload["exit"] = {"op": "all", "children": children}
                with self.assertRaisesRegex(ValidationError, "same entry-anchored closing return"):
                    StrategySpec.model_validate(payload)

    def test_first_of_accepts_profit_or_loss_as_alternative_exits(self) -> None:
        exits = FirstOfExit.model_validate({
            "op": "first_of",
            "children": [
                {"type": "position_return_exit", "trigger": "take_profit", "threshold_pct": 20},
                {"type": "position_return_exit", "trigger": "stop_loss", "threshold_pct": 5},
            ],
        })
        self.assertEqual(exits.op, "first_of")
        self.assertEqual(len(exits.children), 2)


if __name__ == "__main__":
    unittest.main()
