from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from ashare_lab.domain.catalog import CatalogLoadError, load_catalog_directory
from ashare_lab.domain.strategy import (
    StrategyCatalogError,
    StrategySpec,
    validate_strategy_against_catalog,
)

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE = ROOT / "contracts/examples/strategy.macd-volume.daily.v1.json"
MANIFEST = ROOT / "catalogs/signals/cn_a_technical.v1.manifest.json"


class CatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = load_catalog_directory(ROOT / "catalogs")
        self.payload = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        self.payload["catalog"]["release_version"] = "2026.09.01"

    def test_loads_one_active_release_with_thirty_seven_indicators(self) -> None:
        self.assertEqual(len(self.catalog.manifests), 1)
        self.assertEqual(
            {item.id for item in self.catalog.indicators},
            {
                "amount.average",
                "market.amount",
                "market.turnover_rate",
                "market.volume",
                "price.amplitude",
                "price.close",
                "price.consecutive_up",
                "price.return_pct",
                "price.rolling_high",
                "price.true_range",
                "technical.adx",
                "technical.atr",
                "technical.bbi",
                "technical.bias",
                "technical.bollinger",
                "technical.cci",
                "technical.ema",
                "technical.ema_bias",
                "technical.dmi",
                "technical.donchian",
                "technical.historical_volatility",
                "technical.kdj",
                "technical.ma",
                "technical.ma_cross",
                "technical.macd",
                "technical.momentum",
                "technical.natr",
                "technical.rsi",
                "technical.roc",
                "technical.return_stddev",
                "technical.stochastic",
                "technical.obv",
                "technical.trend_regime",
                "technical.williams_r",
                "volume.price_confirmation",
                "volume.price_divergence",
                "volume.relative",
            },
        )
        self.assertRegex(self.catalog.content_hash, r"^sha256:[0-9a-f]{64}$")

    def test_static_warmups_cover_default_confirmation_and_crossing(self) -> None:
        expected = {
            "technical.ma": 21,
            "technical.rsi": 16,
            "volume.relative": 23,
            "volume.price_divergence": 84,
            "technical.trend_regime": 121,
            "price.true_range": 3,
            "technical.atr": 16,
            "technical.natr": 16,
            "technical.adx": 29,
            "technical.dmi": 29,
            "technical.bias": 21,
            "technical.roc": 14,
            "technical.momentum": 12,
            "technical.stochastic": 17,
            "technical.williams_r": 15,
            "technical.donchian": 22,
            "technical.return_stddev": 22,
            "technical.historical_volatility": 22,
        }

        for indicator_id, warmup in expected.items():
            with self.subTest(indicator_id=indicator_id):
                definition = self.catalog.resolve_indicator(indicator_id)
                self.assertIsNotNone(definition)
                self.assertEqual(definition.warmup_bars, warmup)  # type: ignore[union-attr]

    def test_validates_example_against_exact_release(self) -> None:
        strategy = StrategySpec.model_validate(self.payload)
        self.assertIs(validate_strategy_against_catalog(strategy, self.catalog), strategy)

    def test_validates_each_indicator_in_the_first_subset(self) -> None:
        conditions: tuple[dict[str, object], ...] = (
            {
                "type": "indicator_condition",
                "indicator_id": "technical.macd",
                "definition_version": "1.0.0",
                "params": {"fast": 12, "slow": 26, "signal": 9},
                "timeframe": "1d",
                "evaluation_mode": "bar_close_confirmed",
                "trigger": "golden_cross",
                "value": None,
            },
            {
                "type": "indicator_condition",
                "indicator_id": "technical.ma",
                "definition_version": "1.0.0",
                "params": {"period": 20, "price_field": "close"},
                "timeframe": "1d",
                "evaluation_mode": "bar_close_confirmed",
                "trigger": "price_crosses_above",
                "value": None,
            },
            {
                "type": "indicator_condition",
                "indicator_id": "technical.rsi",
                "definition_version": "1.0.0",
                "params": {"period": 14},
                "timeframe": "1d",
                "evaluation_mode": "bar_close_confirmed",
                "trigger": "crosses_below",
                "value": 30,
            },
            {
                "type": "indicator_condition",
                "indicator_id": "market.volume",
                "definition_version": "1.0.0",
                "params": {"baseline_period": 20},
                "timeframe": "1d",
                "evaluation_mode": "bar_close_confirmed",
                "trigger": "gte_multiple",
                "value": 2,
            },
            {
                "type": "indicator_condition",
                "indicator_id": "technical.ema",
                "definition_version": "1.0.0",
                "params": {"period": 28, "price_field": "close"},
                "timeframe": "1d",
                "evaluation_mode": "bar_close_confirmed",
                "trigger": "price_crosses_above",
                "value": None,
            },
            {
                "type": "indicator_condition",
                "indicator_id": "technical.ma_cross",
                "definition_version": "1.0.0",
                "params": {"fast_period": 5, "slow_period": 20, "price_field": "close"},
                "timeframe": "1d",
                "evaluation_mode": "bar_close_confirmed",
                "trigger": "golden_cross",
                "value": None,
            },
            {
                "type": "indicator_condition",
                "indicator_id": "technical.bollinger",
                "definition_version": "1.0.0",
                "params": {
                    "period": 20,
                    "stddev_multiplier": 2.0,
                    "price_field": "close",
                },
                "timeframe": "1d",
                "evaluation_mode": "bar_close_confirmed",
                "trigger": "price_crosses_above_upper",
                "value": None,
            },
            {
                "type": "indicator_condition",
                "indicator_id": "technical.kdj",
                "definition_version": "1.0.0",
                "params": {"period": 9, "k_smoothing": 3, "d_smoothing": 3},
                "timeframe": "1d",
                "evaluation_mode": "bar_close_confirmed",
                "trigger": "j_below",
                "value": 20,
            },
            {
                "type": "indicator_condition",
                "indicator_id": "technical.cci",
                "definition_version": "1.0.0",
                "params": {"period": 14, "constant": 0.015},
                "timeframe": "1d",
                "evaluation_mode": "bar_close_confirmed",
                "trigger": "below",
                "value": -100,
            },
            {
                "type": "indicator_condition",
                "indicator_id": "technical.bbi",
                "definition_version": "1.0.0",
                "params": {
                    "period_1": 3,
                    "period_2": 6,
                    "period_3": 12,
                    "period_4": 24,
                    "price_field": "close",
                },
                "timeframe": "1d",
                "evaluation_mode": "bar_close_confirmed",
                "trigger": "price_crosses_above",
                "value": None,
            },
            {
                "type": "indicator_condition",
                "indicator_id": "technical.ema_bias",
                "definition_version": "1.0.0",
                "params": {"period": 28, "price_field": "close"},
                "timeframe": "1d",
                "evaluation_mode": "bar_close_confirmed",
                "trigger": "below",
                "value": -5,
            },
            {
                "type": "indicator_condition",
                "indicator_id": "price.close",
                "definition_version": "1.0.0",
                "params": {},
                "timeframe": "1d",
                "evaluation_mode": "bar_close_confirmed",
                "trigger": "crosses_above",
                "value": 19,
            },
            {
                "type": "indicator_condition",
                "indicator_id": "price.return_pct",
                "definition_version": "1.0.0",
                "params": {"period": 5, "price_field": "close"},
                "timeframe": "1d",
                "evaluation_mode": "bar_close_confirmed",
                "trigger": "above",
                "value": 10,
            },
            {
                "type": "indicator_condition",
                "indicator_id": "price.rolling_high",
                "definition_version": "1.0.0",
                "params": {"period": 20, "price_field": "close"},
                "timeframe": "1d",
                "evaluation_mode": "bar_close_confirmed",
                "trigger": "new_high",
                "value": None,
            },
            {
                "type": "indicator_condition",
                "indicator_id": "price.consecutive_up",
                "definition_version": "1.0.0",
                "params": {"days": 3},
                "timeframe": "1d",
                "evaluation_mode": "bar_close_confirmed",
                "trigger": "at_least",
                "value": None,
            },
            {
                "type": "indicator_condition",
                "indicator_id": "price.amplitude",
                "definition_version": "1.0.0",
                "params": {},
                "timeframe": "1d",
                "evaluation_mode": "bar_close_confirmed",
                "trigger": "above",
                "value": 5,
            },
            {
                "type": "indicator_condition",
                "indicator_id": "market.amount",
                "definition_version": "1.0.0",
                "params": {},
                "timeframe": "1d",
                "evaluation_mode": "bar_close_confirmed",
                "trigger": "crosses_above",
                "value": 100_000_000,
            },
            {
                "type": "indicator_condition",
                "indicator_id": "market.turnover_rate",
                "definition_version": "1.0.0",
                "params": {},
                "timeframe": "1d",
                "evaluation_mode": "bar_close_confirmed",
                "trigger": "above",
                "value": 3,
            },
            {
                "type": "indicator_condition",
                "indicator_id": "amount.average",
                "definition_version": "1.0.0",
                "params": {"period": 5},
                "timeframe": "1d",
                "evaluation_mode": "bar_close_confirmed",
                "trigger": "above",
                "value": 100_000_000,
            },
            {
                "type": "indicator_condition",
                "indicator_id": "volume.relative",
                "definition_version": "1.0.0",
                "params": {"baseline_period": 20, "consecutive_days": 3},
                "timeframe": "1d",
                "evaluation_mode": "bar_close_confirmed",
                "trigger": "gte_multiple",
                "value": 1.5,
            },
            {
                "type": "indicator_condition",
                "indicator_id": "volume.price_confirmation",
                "definition_version": "1.0.0",
                "params": {
                    "baseline_period": 20,
                    "volume_multiple": 1.5,
                    "return_threshold_pct": 5,
                },
                "timeframe": "1d",
                "evaluation_mode": "bar_close_confirmed",
                "trigger": "surge_up",
                "value": None,
            },
            {
                "type": "indicator_condition",
                "indicator_id": "technical.obv",
                "definition_version": "1.0.0",
                "params": {},
                "timeframe": "1d",
                "evaluation_mode": "bar_close_confirmed",
                "trigger": "rising",
                "value": None,
            },
            {
                "type": "indicator_condition",
                "indicator_id": "volume.price_divergence",
                "definition_version": "1.0.0",
                "params": {
                    "left_bars": 3,
                    "right_bars": 3,
                    "min_separation": 5,
                    "max_separation": 60,
                    "price_threshold_pct": 2,
                    "obv_threshold_adv": 1,
                    "average_volume_period": 20,
                },
                "timeframe": "1d",
                "evaluation_mode": "bar_close_confirmed",
                "trigger": "bearish",
                "value": None,
            },
            {
                "type": "indicator_condition",
                "indicator_id": "technical.trend_regime",
                "definition_version": "1.0.0",
                "params": {
                    "short_period": 20,
                    "long_period": 60,
                    "slope_lookback": 5,
                    "adx_period": 14,
                    "adx_threshold": 25,
                    "confirmation_days": 2,
                    "stability_bars": 120,
                },
                "timeframe": "1d",
                "evaluation_mode": "bar_close_confirmed",
                "trigger": "uptrend",
                "value": None,
            },
        )
        for condition in conditions:
            with self.subTest(indicator_id=condition["indicator_id"]):
                payload = copy.deepcopy(self.payload)
                payload["entry"] = condition
                strategy = StrategySpec.model_validate(payload)
                validate_strategy_against_catalog(strategy, self.catalog)

    def test_validates_each_p1_transparent_daily_indicator(self) -> None:
        cases: tuple[tuple[str, dict[str, object], str, float | None], ...] = (
            ("price.true_range", {}, "above", 1),
            ("technical.atr", {"period": 14}, "above", 1),
            ("technical.natr", {"period": 14}, "above", 1),
            ("technical.adx", {"period": 14}, "above", 25),
            ("technical.dmi", {"period": 14}, "plus_above_minus", None),
            (
                "technical.bias",
                {"period": 20, "price_field": "close"},
                "below",
                -5,
            ),
            (
                "technical.roc",
                {"period": 12, "price_field": "close"},
                "above",
                0,
            ),
            (
                "technical.momentum",
                {"period": 10, "price_field": "close"},
                "above",
                0,
            ),
            (
                "technical.stochastic",
                {"k_period": 14, "d_period": 3},
                "k_crosses_above_d",
                None,
            ),
            ("technical.williams_r", {"period": 14}, "below", -80),
            ("technical.donchian", {"period": 20}, "price_crosses_above_upper", None),
            (
                "technical.return_stddev",
                {"period": 20, "price_field": "close"},
                "above",
                2,
            ),
            (
                "technical.historical_volatility",
                {"period": 20, "annualization_sessions": 252, "price_field": "close"},
                "above",
                30,
            ),
        )
        for indicator_id, params, trigger, value in cases:
            with self.subTest(indicator_id=indicator_id):
                payload = copy.deepcopy(self.payload)
                payload["entry"] = {
                    "type": "indicator_condition",
                    "indicator_id": indicator_id,
                    "definition_version": "1.0.0",
                    "params": params,
                    "timeframe": "1d",
                    "evaluation_mode": "bar_close_confirmed",
                    "trigger": trigger,
                    "value": value,
                }
                strategy = StrategySpec.model_validate(payload)
                validate_strategy_against_catalog(strategy, self.catalog)

    def test_rejects_p1_indicator_parameters_outside_published_boundaries(self) -> None:
        cases: tuple[tuple[str, dict[str, object], str, float | None], ...] = (
            ("price.true_range", {}, "above", -1),
            ("technical.atr", {"period": 0}, "above", 1),
            ("technical.natr", {"period": 0}, "above", 1),
            ("technical.adx", {"period": 1}, "above", 25),
            ("technical.dmi", {"period": 1}, "plus_above_minus", None),
            (
                "technical.bias",
                {"period": 0, "price_field": "close"},
                "above",
                0,
            ),
            (
                "technical.roc",
                {"period": 0, "price_field": "close"},
                "above",
                0,
            ),
            (
                "technical.momentum",
                {"period": 0, "price_field": "close"},
                "above",
                0,
            ),
            (
                "technical.stochastic",
                {"k_period": 1, "d_period": 3},
                "k_above",
                50,
            ),
            ("technical.williams_r", {"period": 14}, "above", 1),
            ("technical.donchian", {"period": 0}, "price_above_upper", None),
            (
                "technical.return_stddev",
                {"period": 1, "price_field": "close"},
                "above",
                1,
            ),
            (
                "technical.historical_volatility",
                {"period": 20, "annualization_sessions": 0, "price_field": "close"},
                "above",
                20,
            ),
        )
        for indicator_id, params, trigger, value in cases:
            with self.subTest(indicator_id=indicator_id):
                payload = copy.deepcopy(self.payload)
                payload["entry"] = {
                    "type": "indicator_condition",
                    "indicator_id": indicator_id,
                    "definition_version": "1.0.0",
                    "params": params,
                    "timeframe": "1d",
                    "evaluation_mode": "bar_close_confirmed",
                    "trigger": trigger,
                    "value": value,
                }
                strategy = StrategySpec.model_validate(payload)

                with self.assertRaises(StrategyCatalogError):
                    validate_strategy_against_catalog(strategy, self.catalog)

    def test_rejects_macd_fast_not_less_than_slow(self) -> None:
        payload = copy.deepcopy(self.payload)
        payload["entry"]["children"][0]["params"]["fast"] = 30
        strategy = StrategySpec.model_validate(payload)

        with self.assertRaises(StrategyCatalogError) as context:
            validate_strategy_against_catalog(strategy, self.catalog)

        self.assertIn(
            "parameter_relation_failed", {issue.code for issue in context.exception.issues}
        )

    def test_rejects_rsi_without_threshold(self) -> None:
        payload = copy.deepcopy(self.payload)
        payload["entry"] = {
            "type": "indicator_condition",
            "indicator_id": "technical.rsi",
            "definition_version": "1.0.0",
            "params": {"period": 14},
            "timeframe": "1d",
            "evaluation_mode": "bar_close_confirmed",
            "trigger": "crosses_below",
            "value": None,
        }
        strategy = StrategySpec.model_validate(payload)

        with self.assertRaises(StrategyCatalogError) as context:
            validate_strategy_against_catalog(strategy, self.catalog)

        self.assertIn("trigger_value_required", {issue.code for issue in context.exception.issues})

    def test_rejects_invalid_parameter_relations(self) -> None:
        invalid_params = (
            (
                "technical.ma_cross",
                {"fast_period": 20, "slow_period": 5, "price_field": "close"},
                "golden_cross",
            ),
            (
                "technical.bbi",
                {
                    "period_1": 3,
                    "period_2": 12,
                    "period_3": 6,
                    "period_4": 24,
                    "price_field": "close",
                },
                "price_crosses_above",
            ),
            (
                "volume.price_divergence",
                {
                    "left_bars": 3,
                    "right_bars": 3,
                    "min_separation": 60,
                    "max_separation": 5,
                    "price_threshold_pct": 2,
                    "obv_threshold_adv": 1,
                    "average_volume_period": 20,
                },
                "bearish",
            ),
            (
                "technical.trend_regime",
                {
                    "short_period": 60,
                    "long_period": 20,
                    "slope_lookback": 5,
                    "adx_period": 14,
                    "adx_threshold": 25,
                    "confirmation_days": 2,
                    "stability_bars": 120,
                },
                "uptrend",
            ),
        )
        for indicator_id, params, trigger in invalid_params:
            with self.subTest(indicator_id=indicator_id):
                payload = copy.deepcopy(self.payload)
                payload["entry"] = {
                    "type": "indicator_condition",
                    "indicator_id": indicator_id,
                    "definition_version": "1.0.0",
                    "params": params,
                    "timeframe": "1d",
                    "evaluation_mode": "bar_close_confirmed",
                    "trigger": trigger,
                    "value": None,
                }
                strategy = StrategySpec.model_validate(payload)

                with self.assertRaises(StrategyCatalogError) as context:
                    validate_strategy_against_catalog(strategy, self.catalog)

                self.assertIn(
                    "parameter_relation_failed",
                    {issue.code for issue in context.exception.issues},
                )

    def test_rejects_multiple_active_versions(self) -> None:
        first = json.loads(MANIFEST.read_text(encoding="utf-8"))
        second = copy.deepcopy(first)
        second["release_version"] = "2026.08.31"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "first.manifest.json").write_text(json.dumps(first), encoding="utf-8")
            (root / "second.manifest.json").write_text(json.dumps(second), encoding="utf-8")

            with self.assertRaisesRegex(CatalogLoadError, "multiple active releases"):
                load_catalog_directory(root)


if __name__ == "__main__":
    unittest.main()
