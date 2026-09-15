"""Generic comparison definition; not evidence that a metric can be backtested.

The data adapter must separately verify the returned field, stock, dates,
availability clock, and unit. A query description is only a request identifier;
neither it nor this server-built binding proves historical data availability.
"""

from __future__ import annotations

from decimal import Decimal
from typing import cast

from ashare_lab.domain.strategy import IndicatorCondition

from .comparators import Comparator
from .provider_catalog import (
    ProviderCatalogError,
    ProviderComparisonBinding,
    ProviderIndicatorBinding,
    ProviderRightOperand,
    RightOperandSource,
)

SKILL_NUMERIC_ID = "provider.numeric"
SKILL_NUMERIC_VERSION = "1.0.0"
SKILL_SERIES_COMPARE_ID = "provider.series_compare"
SKILL_COMPARISON_IDS = frozenset({SKILL_NUMERIC_ID, SKILL_SERIES_COMPARE_ID})
_COMPARATORS = {
    "above": Comparator.GT,
    "below": Comparator.LT,
    "at_least": Comparator.GTE,
    "at_most": Comparator.LTE,
    "crosses_above": Comparator.CROSSES_ABOVE,
    "crosses_below": Comparator.CROSSES_BELOW,
}
SKILL_NUMERIC_TRIGGERS = tuple(_COMPARATORS)


def validate_skill_numeric_condition(condition: IndicatorCondition) -> IndicatorCondition:
    """Validate operator shape without interpreting Chinese or converting units.

    Preserve ``metric_query`` and the user's comparison ``unit`` verbatim. Only
    those two parameters are admitted: provider field codes, timestamps, or
    claimed provenance supplied by a model are not trusted data bindings.
    Historical identity, time alignment, and actual unit agreement belong to
    the data layer and are not established by this validation.
    """
    if condition.indicator_id != SKILL_NUMERIC_ID:
        raise ProviderCatalogError("expected provider.numeric indicator")
    if condition.definition_version != SKILL_NUMERIC_VERSION:
        raise ProviderCatalogError("provider.numeric requires definition_version 1.0.0")
    if condition.timeframe != "1d" or condition.evaluation_mode != "bar_close_confirmed":
        raise ProviderCatalogError("provider.numeric requires daily close-confirmed conditions")
    if set(condition.params) != {"metric_query", "unit"}:
        raise ProviderCatalogError(
            "provider.numeric params must contain only metric_query and unit"
        )
    query = condition.params["metric_query"]
    if not isinstance(query, str) or not query.strip() or len(query) > 200:
        raise ProviderCatalogError(
            "provider.numeric metric_query must be nonblank and at most 200 characters"
        )
    unit = condition.params["unit"]
    if not isinstance(unit, str) or not unit.strip():
        raise ProviderCatalogError("provider.numeric unit must be a nonblank string")
    if condition.trigger not in _COMPARATORS:
        raise ProviderCatalogError("provider.numeric trigger is not supported")
    value = condition.value
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not Decimal(str(value)).is_finite()
    ):
        raise ProviderCatalogError("provider.numeric comparison value must be numeric and finite")
    return condition


def skill_numeric_binding(condition: IndicatorCondition) -> ProviderIndicatorBinding:
    """Build the comparison server-side, never from model-supplied field evidence.

    The data layer must bind its independently verified numeric series to the
    fixed name ``value`` and check the original unit, stock, dates, and known-at
    times. Crossovers use the existing Comparator's previous-observation rules;
    this function does not invent a previous observation or fetch any data.
    """
    validate_skill_numeric_condition(condition)
    return ProviderIndicatorBinding(
        indicator_id=SKILL_NUMERIC_ID,
        trigger=condition.trigger,
        provider_indicator_name=cast(str, condition.params["metric_query"]),
        value_names=("value",),
        comparisons=(ProviderComparisonBinding(
            left_field="value",
            comparator=_COMPARATORS[condition.trigger],
            right=ProviderRightOperand(source=RightOperandSource.CONDITION_VALUE),
        ),),
    )


def validate_skill_series_compare_condition(
    condition: IndicatorCondition,
) -> IndicatorCondition:
    """Validate two query operands without guessing their fields or numeric values."""
    if condition.indicator_id != SKILL_SERIES_COMPARE_ID:
        raise ProviderCatalogError("expected provider.series_compare indicator")
    if condition.definition_version != SKILL_NUMERIC_VERSION:
        raise ProviderCatalogError("provider.series_compare requires definition_version 1.0.0")
    if condition.timeframe != "1d" or condition.evaluation_mode != "bar_close_confirmed":
        raise ProviderCatalogError(
            "provider.series_compare requires daily close-confirmed conditions"
        )
    if set(condition.params) != {"left_metric_query", "right_metric_query", "unit"}:
        raise ProviderCatalogError(
            "provider.series_compare params must contain only left_metric_query, "
            "right_metric_query and unit"
        )
    for name in ("left_metric_query", "right_metric_query"):
        query = condition.params[name]
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise ProviderCatalogError(f"{name} must be nonblank and at most 200 characters")
    unit = condition.params["unit"]
    if not isinstance(unit, str) or not unit.strip():
        raise ProviderCatalogError("provider.series_compare unit must be a nonblank string")
    if condition.trigger not in _COMPARATORS:
        raise ProviderCatalogError("provider.series_compare trigger is not supported")
    if condition.value is not None:
        raise ProviderCatalogError("provider.series_compare forbids a scalar comparison value")
    return condition


def skill_series_compare_binding(condition: IndicatorCondition) -> ProviderIndicatorBinding:
    """Compare two independently bound histories using existing field semantics."""
    validate_skill_series_compare_condition(condition)
    return ProviderIndicatorBinding(
        indicator_id=SKILL_SERIES_COMPARE_ID,
        trigger=condition.trigger,
        provider_indicator_name=(
            f"{condition.params['left_metric_query']} / {condition.params['right_metric_query']}"
        ),
        value_names=("left_value", "right_value"),
        comparisons=(ProviderComparisonBinding(
            left_field="left_value", comparator=_COMPARATORS[condition.trigger],
            right=ProviderRightOperand(source=RightOperandSource.FIELD, field_name="right_value"),
        ),),
    )


def validate_skill_comparison_condition(condition: IndicatorCondition) -> IndicatorCondition:
    if condition.indicator_id == SKILL_SERIES_COMPARE_ID:
        return validate_skill_series_compare_condition(condition)
    return validate_skill_numeric_condition(condition)


def skill_comparison_binding(condition: IndicatorCondition) -> ProviderIndicatorBinding:
    if condition.indicator_id == SKILL_SERIES_COMPARE_ID:
        return skill_series_compare_binding(condition)
    return skill_numeric_binding(condition)
