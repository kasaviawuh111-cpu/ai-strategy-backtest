from __future__ import annotations

from decimal import Decimal

import pytest

from ashare_lab.domain.signals.comparators import Comparator, evaluate_comparator
from ashare_lab.domain.signals.provider_catalog import ProviderCatalogError, RightOperandSource
from ashare_lab.domain.signals.skill_numeric import (
    SKILL_NUMERIC_ID,
    SKILL_NUMERIC_VERSION,
    SKILL_SERIES_COMPARE_ID,
    skill_comparison_binding,
    skill_numeric_binding,
    validate_skill_comparison_condition,
    validate_skill_numeric_condition,
)
from ashare_lab.domain.strategy import IndicatorCondition


def _condition(**changes: object) -> IndicatorCondition:
    condition = IndicatorCondition(
        indicator_id=SKILL_NUMERIC_ID,
        definition_version=SKILL_NUMERIC_VERSION,
        params={"metric_query": "历史日线指标", "unit": "百分点"},
        trigger="above",
        value=5,
    )
    # Invalid copies deliberately test this operator's validation independently
    # of the outer IndicatorCondition JSON parser.
    return condition.model_copy(update=changes)


def test_six_supported_triggers_use_server_owned_comparison_and_value_field() -> None:
    expected = {
        "above": Comparator.GT,
        "below": Comparator.LT,
        "at_least": Comparator.GTE,
        "at_most": Comparator.LTE,
        "crosses_above": Comparator.CROSSES_ABOVE,
        "crosses_below": Comparator.CROSSES_BELOW,
    }
    for trigger, comparator in expected.items():
        condition = _condition(trigger=trigger)
        assert validate_skill_numeric_condition(condition) is condition
        binding = skill_numeric_binding(condition)
        assert binding.indicator_id == SKILL_NUMERIC_ID
        assert binding.trigger == trigger
        assert binding.provider_indicator_name == condition.params["metric_query"]
        assert binding.value_names == binding.required_fields == ("value",)
        assert binding.consecutive_sessions == 1
        assert len(binding.comparisons) == 1
        comparison = binding.comparisons[0]
        assert comparison.left_field == "value"
        assert comparison.comparator is comparator
        assert comparison.right.source is RightOperandSource.CONDITION_VALUE
        assert comparison.right.field_name is None
        assert comparison.right.constant is None
        assert comparison.right.multiplier == 1


def test_query_and_original_unit_are_preserved_without_conversion() -> None:
    query = " 任意供应商数值查询 "
    unit = " 万元/原始比较单位 "
    condition = _condition(params={"metric_query": query, "unit": unit}, value=2.5)
    original = condition.model_dump()

    assert skill_numeric_binding(condition).provider_indicator_name == query
    assert condition.model_dump() == original
    assert condition.params["unit"] == unit
    assert condition.value == 2.5
    assert skill_numeric_binding(_condition(params={
        "metric_query": "数" * 200, "unit": "原单位",
    })).provider_indicator_name == "数" * 200


def test_metric_query_is_required_nonblank_string_with_bounded_length() -> None:
    for query in ("", " \n\t", "数" * 201, 10, False):
        with pytest.raises(ProviderCatalogError, match="metric_query"):
            skill_numeric_binding(_condition(params={"metric_query": query, "unit": "%"}))
    with pytest.raises(ProviderCatalogError, match="params"):
        validate_skill_numeric_condition(_condition(params={"unit": "%"}))


def test_unit_is_required_nonblank_string_not_an_engine_guessed_default() -> None:
    for unit in ("", " \t", 100, False):
        with pytest.raises(ProviderCatalogError, match="unit"):
            skill_numeric_binding(_condition(params={"metric_query": "任意指标", "unit": unit}))
    with pytest.raises(ProviderCatalogError, match="params"):
        validate_skill_numeric_condition(_condition(params={"metric_query": "任意指标"}))


def test_model_cannot_supply_field_codes_or_other_evidence_parameters() -> None:
    for key in ("fieldCode", "field_code", "source_parameters", "available_at", "schema_version"):
        with pytest.raises(ProviderCatalogError, match="params"):
            skill_numeric_binding(_condition(params={
                "metric_query": "任意指标", "unit": "%", key: "model_supplied_claim",
            }))


def test_wrong_identity_version_timeframe_or_trigger_is_rejected() -> None:
    for changes in (
        {"indicator_id": "technical.rsi"},
        {"definition_version": "2.0.0"},
        {"timeframe": "1m"},
        {"evaluation_mode": "intraday"},
        {"trigger": "equals"},
    ):
        with pytest.raises(ProviderCatalogError):
            skill_numeric_binding(_condition(**changes))


def test_threshold_is_required_numeric_and_finite_even_for_unsafe_copies() -> None:
    for value in (None, float("nan"), float("inf"), float("-inf"), True, "5"):
        with pytest.raises(ProviderCatalogError, match="numeric and finite"):
            skill_numeric_binding(_condition(value=value))


def test_crossing_retains_existing_previous_observation_semantics() -> None:
    above = skill_numeric_binding(_condition(trigger="crosses_above")).comparisons[0].comparator
    below = skill_numeric_binding(_condition(trigger="crosses_below")).comparisons[0].comparator
    threshold = Decimal(5)
    assert evaluate_comparator(
        above, Decimal(6), threshold, previous_left=threshold, previous_right=threshold,
    )
    assert not evaluate_comparator(
        above, Decimal(6), threshold, previous_left=Decimal(6), previous_right=threshold,
    )
    assert evaluate_comparator(
        below, Decimal(4), threshold, previous_left=threshold, previous_right=threshold,
    )
    with pytest.raises(ValueError, match="requires both previous operands"):
        evaluate_comparator(above, Decimal(6), threshold)


@pytest.mark.parametrize("trigger", [
    "above", "below", "at_least", "at_most", "crosses_above", "crosses_below",
])
def test_series_comparison_binds_two_server_owned_fields_without_threshold(trigger: str) -> None:
    condition = _condition(
        indicator_id=SKILL_SERIES_COMPARE_ID, trigger=trigger, value=None,
        params={"left_metric_query": " 指标甲 ", "right_metric_query": "指标乙", "unit": "倍"},
    )
    assert validate_skill_comparison_condition(condition) is condition
    binding = skill_comparison_binding(condition)
    assert binding.required_fields == ("left_value", "right_value")
    comparison = binding.comparisons[0]
    assert comparison.left_field == "left_value"
    assert comparison.right.source is RightOperandSource.FIELD
    assert comparison.right.field_name == "right_value"
    scalar = skill_numeric_binding(_condition(trigger=trigger)).comparisons[0]
    assert comparison.comparator == scalar.comparator
    assert condition.params["left_metric_query"] == " 指标甲 "


@pytest.mark.parametrize("changes", [
    {"value": 0}, {"value": False}, {"value": float("nan")},
    {"params": {
        "left_metric_query": "甲", "right_metric_query": "乙", "unit": "倍", "fieldCode": "x",
    }},
    {"params": {"left_metric_query": "甲", "right_metric_query": " ", "unit": "倍"}},
    {"params": {"left_metric_query": "甲", "right_metric_query": "乙" * 201, "unit": "倍"}},
    {"params": {"left_metric_query": "甲", "right_metric_query": "乙", "unit": " "}},
    {"params": {"left_metric_query": "甲", "unit": "倍"}},
    {"trigger": "equals"}, {"timeframe": "1m"}, {"evaluation_mode": "intraday"},
    {"definition_version": "2.0.0"},
])
def test_series_comparison_rejects_incomplete_operands_or_model_field_evidence(
    changes: dict[str, object],
) -> None:
    condition = _condition(
        indicator_id=SKILL_SERIES_COMPARE_ID, value=None,
        params={"left_metric_query": "甲", "right_metric_query": "乙", "unit": "倍"},
    )
    with pytest.raises(ProviderCatalogError):
        skill_comparison_binding(condition.model_copy(update=changes))
