"""Catalog-backed semantic validation for a parsed StrategySpec."""

from __future__ import annotations

from dataclasses import dataclass

from ashare_lab.domain.catalog.models import (
    CatalogSnapshot,
    IndicatorDefinition,
    ParameterDefinition,
)
from ashare_lab.domain.events.catalog import DOCUMENT_TEXT_EVENT_CODES, resolve_executable_event

from .models import (
    EventCondition,
    IndicatorCondition,
    JsonScalar,
    StrategySpec,
    iter_event_conditions,
    iter_indicator_conditions,
)


@dataclass(frozen=True, slots=True)
class StrategyCatalogIssue:
    code: str
    path: str
    message: str


class StrategyCatalogError(ValueError):
    def __init__(self, issues: list[StrategyCatalogIssue]):
        self.issues = tuple(issues)
        super().__init__(
            "; ".join(f"{issue.code} at {issue.path}: {issue.message}" for issue in issues)
        )


def validate_strategy_against_catalog(
    strategy: StrategySpec,
    catalog: CatalogSnapshot,
) -> StrategySpec:
    """Validate all indicator leaves against one exact Catalog snapshot."""

    issues: list[StrategyCatalogIssue] = []
    if not catalog.contains_release(strategy.catalog.catalog_id, strategy.catalog.release_version):
        issues.append(
            StrategyCatalogIssue(
                code="catalog_release_mismatch",
                path="/catalog",
                message=(
                    f"{strategy.catalog.catalog_id}@{strategy.catalog.release_version} "
                    "is not present in the loaded snapshot"
                ),
            )
        )

    for index, condition in enumerate(iter_indicator_conditions(strategy)):
        _validate_condition(condition, catalog, f"/conditions/{index}", issues)
    for index, condition in enumerate(iter_event_conditions(strategy)):
        _validate_event_condition(condition, f"/event_conditions/{index}", issues)

    if issues:
        raise StrategyCatalogError(issues)
    return strategy


def _validate_event_condition(
    condition: EventCondition,
    path: str,
    issues: list[StrategyCatalogIssue],
) -> None:
    definition = resolve_executable_event(condition.event_code)
    if definition is None:
        issues.append(
            StrategyCatalogIssue(
                code="event_not_executable",
                path=f"{path}/event_code",
                message=f"event {condition.event_code!r} is not in the executable event release",
            )
        )
        return
    if condition.definition_version != definition.definition_version:
        issues.append(
            StrategyCatalogIssue(
                code="event_version_mismatch",
                path=f"{path}/definition_version",
                message=f"expected {definition.definition_version!r}",
            )
        )
    unknown_attributes = sorted(set(condition.attributes) - set(definition.allowed_attributes))
    for name in unknown_attributes:
        issues.append(
            StrategyCatalogIssue(
                code="unknown_event_attribute",
                path=f"{path}/attributes/{name}",
                message=f"attribute is not declared by {definition.event_code}",
            )
        )
    if (
        condition.document_text is not None
        and condition.event_code not in DOCUMENT_TEXT_EVENT_CODES
    ):
        issues.append(
            StrategyCatalogIssue(
                code="event_document_text_not_executable",
                path=f"{path}/document_text",
                message=(
                    f"full-document text metrics are not published for {condition.event_code}"
                ),
            )
        )


def _validate_condition(
    condition: IndicatorCondition,
    catalog: CatalogSnapshot,
    path: str,
    issues: list[StrategyCatalogIssue],
) -> None:
    definition = catalog.resolve_indicator(condition.indicator_id)
    if definition is None:
        issues.append(
            StrategyCatalogIssue(
                code="indicator_not_found",
                path=f"{path}/indicator_id",
                message=f"indicator {condition.indicator_id!r} is not in the active Catalog",
            )
        )
        return
    if definition.status != "stable":
        issues.append(
            StrategyCatalogIssue(
                code="indicator_not_stable",
                path=f"{path}/indicator_id",
                message=f"indicator status is {definition.status!r}",
            )
        )
    if condition.definition_version != definition.version:
        issues.append(
            StrategyCatalogIssue(
                code="indicator_version_mismatch",
                path=f"{path}/definition_version",
                message=f"expected {definition.version!r}",
            )
        )
    if condition.timeframe not in definition.timeframes:
        issues.append(
            StrategyCatalogIssue(
                code="unsupported_timeframe",
                path=f"{path}/timeframe",
                message=f"{condition.timeframe!r} is unsupported",
            )
        )
    if condition.evaluation_mode not in definition.evaluation_modes:
        issues.append(
            StrategyCatalogIssue(
                code="unsupported_evaluation_mode",
                path=f"{path}/evaluation_mode",
                message=f"{condition.evaluation_mode!r} is unsupported",
            )
        )
    _validate_parameters(condition, definition, path, issues)
    _validate_trigger(condition, definition, path, issues)


def _validate_parameters(
    condition: IndicatorCondition,
    definition: IndicatorDefinition,
    path: str,
    issues: list[StrategyCatalogIssue],
) -> None:
    definitions = {parameter.name: parameter for parameter in definition.parameters}
    for name in sorted(set(condition.params) - set(definitions)):
        issues.append(
            StrategyCatalogIssue(
                code="unknown_parameter",
                path=f"{path}/params/{name}",
                message=f"parameter is not declared by {definition.id}",
            )
        )
    for name, parameter in definitions.items():
        if parameter.required and name not in condition.params:
            issues.append(
                StrategyCatalogIssue(
                    code="missing_parameter",
                    path=f"{path}/params/{name}",
                    message="compiled DSL must contain every required parameter",
                )
            )
        elif name in condition.params:
            _validate_parameter_value(
                condition.params[name], parameter, f"{path}/params/{name}", issues
            )

    for relation in definition.parameter_relations:
        if relation.left not in condition.params or relation.right not in condition.params:
            continue
        left = condition.params[relation.left]
        right = condition.params[relation.right]
        if isinstance(left, bool) or isinstance(right, bool):
            continue
        if not isinstance(left, int | float) or not isinstance(right, int | float):
            continue
        relation_holds = {
            "lt": left < right,
            "lte": left <= right,
            "gt": left > right,
            "gte": left >= right,
        }[relation.op]
        if not relation_holds:
            issues.append(
                StrategyCatalogIssue(
                    code="parameter_relation_failed",
                    path=f"{path}/params",
                    message=f"requires {relation.left} {relation.op} {relation.right}",
                )
            )


def _validate_parameter_value(
    value: JsonScalar,
    definition: ParameterDefinition,
    path: str,
    issues: list[StrategyCatalogIssue],
) -> None:
    valid_type = {
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, int | float) and not isinstance(value, bool),
        "string": isinstance(value, str),
    }[definition.value_type]
    if not valid_type:
        issues.append(
            StrategyCatalogIssue(
                code="parameter_type_mismatch",
                path=path,
                message=f"expected {definition.value_type}",
            )
        )
        return
    if definition.choices and value not in definition.choices:
        issues.append(
            StrategyCatalogIssue(
                code="parameter_not_in_choices", path=path, message="unsupported value"
            )
        )
    if isinstance(value, int | float) and not isinstance(value, bool):
        if definition.minimum is not None and value < definition.minimum:
            issues.append(
                StrategyCatalogIssue(
                    code="parameter_below_minimum", path=path, message="value is too small"
                )
            )
        if definition.maximum is not None and value > definition.maximum:
            issues.append(
                StrategyCatalogIssue(
                    code="parameter_above_maximum", path=path, message="value is too large"
                )
            )


def _validate_trigger(
    condition: IndicatorCondition,
    definition: IndicatorDefinition,
    path: str,
    issues: list[StrategyCatalogIssue],
) -> None:
    trigger = next((item for item in definition.triggers if item.id == condition.trigger), None)
    if trigger is None:
        issues.append(
            StrategyCatalogIssue(
                code="unsupported_trigger",
                path=f"{path}/trigger",
                message=f"trigger {condition.trigger!r} is not declared by {definition.id}",
            )
        )
        return
    if trigger.value_requirement == "required" and condition.value is None:
        issues.append(
            StrategyCatalogIssue(
                code="trigger_value_required",
                path=f"{path}/value",
                message=f"trigger {trigger.id!r} requires a numeric value",
            )
        )
        return
    if trigger.value_requirement == "forbidden" and condition.value is not None:
        issues.append(
            StrategyCatalogIssue(
                code="trigger_value_forbidden",
                path=f"{path}/value",
                message=f"trigger {trigger.id!r} does not accept a value",
            )
        )
        return
    if condition.value is None:
        return
    if trigger.minimum is not None:
        invalid = (
            condition.value <= trigger.minimum
            if trigger.exclusive_minimum
            else condition.value < trigger.minimum
        )
        if invalid:
            issues.append(
                StrategyCatalogIssue(
                    code="trigger_value_below_minimum",
                    path=f"{path}/value",
                    message="value is too small",
                )
            )
    if trigger.maximum is not None:
        invalid = (
            condition.value >= trigger.maximum
            if trigger.exclusive_maximum
            else condition.value > trigger.maximum
        )
        if invalid:
            issues.append(
                StrategyCatalogIssue(
                    code="trigger_value_above_maximum",
                    path=f"{path}/value",
                    message="value is too large",
                )
            )
