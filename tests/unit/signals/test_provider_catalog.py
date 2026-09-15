from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from ashare_lab.domain.signals.provider_catalog import (
    PROVIDER_INDICATOR_BUILDERS,
    RightOperandSource,
    provider_binding_for_condition,
)
from ashare_lab.domain.strategy import IndicatorCondition

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "catalogs/signals/cn_a_technical.v1.manifest.json"


def _manifest_indicators() -> tuple[dict[str, object], ...]:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return tuple(payload["indicators"])


def _condition(indicator: dict[str, object], trigger: dict[str, object]) -> IndicatorCondition:
    params = {
        parameter["name"]: parameter["default"]
        for parameter in indicator["parameters"]
        if "default" in parameter
    }
    requires_value = trigger["value_requirement"] == "required"
    return IndicatorCondition(
        indicator_id=indicator["id"],
        definition_version=indicator["version"],
        params=params,
        trigger=trigger["id"],
        value=1.5 if requires_value else None,
    )


def test_provider_catalog_covers_every_stable_indicator() -> None:
    expected = {
        indicator["id"] for indicator in _manifest_indicators() if indicator["status"] == "stable"
    }

    assert set(PROVIDER_INDICATOR_BUILDERS) == expected
    assert len(expected) == 37


@pytest.mark.parametrize(
    ("indicator", "trigger"),
    [
        (indicator, trigger)
        for indicator in _manifest_indicators()
        if indicator["status"] == "stable"
        for trigger in indicator["triggers"]
    ],
    ids=lambda item: item.get("id", "trigger"),
)
def test_every_stable_trigger_builds_a_provider_only_query(
    indicator: dict[str, object],
    trigger: dict[str, object],
) -> None:
    condition = _condition(indicator, trigger)

    binding = provider_binding_for_condition(condition)

    assert binding.indicator_id == condition.indicator_id
    assert binding.trigger == condition.trigger
    assert binding.provider_indicator_name
    assert binding.value_names
    assert binding.comparisons
    assert "300059" not in binding.provider_indicator_name
    assert "300033" not in binding.provider_indicator_name
    assert set(binding.required_fields).issubset(set(binding.value_names))


def test_kdj_binding_requests_provider_values_instead_of_ohlcv() -> None:
    condition = IndicatorCondition(
        indicator_id="technical.kdj",
        definition_version="1.0.0",
        params={"period": 9, "k_smoothing": 3, "d_smoothing": 3},
        trigger="golden_cross",
    )

    binding = provider_binding_for_condition(condition)

    assert binding.provider_indicator_name == "KDJ(9,3,3)"
    assert binding.value_names == ("K值", "D值", "J值")
    assert "开盘价" not in binding.value_names
    assert "最高价" not in binding.value_names
    assert "最低价" not in binding.value_names
    assert "收盘价" not in binding.value_names


@pytest.mark.parametrize("period", [15, 20, 60])
@pytest.mark.parametrize("basis,label", [("close", "收盘价"), ("high", "最高价")])
def test_rolling_high_compares_price_with_exact_prior_window_not_provider_flag(
    period: int, basis: str, label: str,
) -> None:
    binding = provider_binding_for_condition(IndicatorCondition(
        indicator_id="price.rolling_high", definition_version="1.0.0",
        params={"period": period, "price_field": basis}, trigger="new_high",
    ))
    highest = f"前{period}日最高{label}"
    assert binding.value_names == (label, highest)
    comparison, = binding.comparisons
    assert comparison.left_field == label
    assert comparison.comparator.value == "gt"
    assert comparison.right.source is RightOperandSource.FIELD
    assert comparison.right.field_name == highest
    assert comparison.right.multiplier == Decimal(1)
    assert "近期创阶段新高" not in binding.provider_indicator_name


@pytest.mark.parametrize("period", [15, 20, 60])
@pytest.mark.parametrize("trigger,comparator,consecutive", [
    ("gt_multiple", "gt", 1), ("gte_multiple", "gte", 1),
    ("lte_multiple", "lte", 1), ("consecutive_gte_multiple", "gte", 3),
])
def test_relative_volume_keeps_threshold_and_consecutive_logic_over_exact_prior_average(
    period: int, trigger: str, comparator: str, consecutive: int,
) -> None:
    binding = provider_binding_for_condition(IndicatorCondition(
        indicator_id="volume.relative", definition_version="1.0.0",
        params={"baseline_period": period, "consecutive_days": 3}, trigger=trigger, value=1.5,
    ))
    average = f"前{period}日平均成交量"
    assert binding.value_names == ("成交量", average)
    comparison, = binding.comparisons
    assert comparison.left_field == "成交量"
    assert comparison.comparator.value == comparator
    assert comparison.right.source is RightOperandSource.FIELD
    assert comparison.right.field_name == average
    assert comparison.right.multiplier == Decimal("1.5")
    assert binding.consecutive_sessions == consecutive
    assert "量比" not in binding.provider_indicator_name
