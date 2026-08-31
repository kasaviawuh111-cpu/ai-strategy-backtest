from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from ashare_lab.domain.strategy import StrategySpec, canonical_hash, canonical_json

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE = ROOT / "contracts/examples/strategy.macd-volume.daily.v1.json"


class CanonicalSerializationTests(unittest.TestCase):
    def test_parameter_key_order_does_not_change_hash(self) -> None:
        first = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        second = copy.deepcopy(first)
        second["entry"]["children"][0]["params"] = {"signal": 9, "slow": 26, "fast": 12}

        first_spec = StrategySpec.model_validate(first)
        second_spec = StrategySpec.model_validate(second)

        self.assertEqual(canonical_json(first_spec), canonical_json(second_spec))
        self.assertEqual(canonical_hash(first_spec), canonical_hash(second_spec))

    def test_integral_float_has_one_canonical_representation(self) -> None:
        self.assertEqual(canonical_json({"value": 2}), canonical_json({"value": 2.0}))

    def test_hash_has_algorithm_prefix(self) -> None:
        digest = canonical_hash({"schema_version": "strategy.v1"})
        self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
