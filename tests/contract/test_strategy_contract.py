from __future__ import annotations

import json
import unittest
from pathlib import Path

from ashare_lab.domain.catalog import load_catalog_directory
from ashare_lab.domain.strategy import StrategySpec, validate_strategy_against_catalog
from contracts.export_schemas import build_schemas

ROOT = Path(__file__).resolve().parents[2]


class StrategyContractTests(unittest.TestCase):
    def test_strategy_v2_schema_is_published_without_executable_override(self) -> None:
        schema = build_schemas()["strategy.v2.schema.json"]

        self.assertEqual(schema["properties"]["schema_version"]["const"], "strategy.v2")
        self.assertNotIn("executable", schema["properties"])
        self.assertNotIn("signal", schema["properties"])

    def test_checked_in_schemas_match_authoritative_models(self) -> None:
        for filename, generated in build_schemas().items():
            checked_in = json.loads((ROOT / "contracts" / filename).read_text(encoding="utf-8"))
            self.assertEqual(checked_in, generated, f"regenerate contracts/{filename}")

    def test_published_example_is_executable_by_the_dsl_subset(self) -> None:
        example_path = ROOT / "contracts/examples/strategy.macd-volume.daily.v1.json"
        strategy = StrategySpec.model_validate_json(example_path.read_text(encoding="utf-8"))
        catalog = load_catalog_directory(ROOT / "catalogs")

        validated = validate_strategy_against_catalog(strategy, catalog)

        self.assertEqual(validated.schema_version, "strategy.v1")
        self.assertEqual(validated.catalog.release_version, "2026.09.01")

    def test_event_and_indicator_example_is_executable_by_the_dsl_subset(self) -> None:
        example_path = ROOT / "contracts/examples/strategy.event-forecast-macd.daily.v1.json"
        strategy = StrategySpec.model_validate_json(example_path.read_text(encoding="utf-8"))
        catalog = load_catalog_directory(ROOT / "catalogs")

        validated = validate_strategy_against_catalog(strategy, catalog)

        self.assertEqual(validated.entry.type, "event_condition")
        self.assertEqual(validated.execution.data_capability, "daily_ohlcv_events")


if __name__ == "__main__":
    unittest.main()
