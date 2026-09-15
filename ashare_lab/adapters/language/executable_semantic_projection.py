"""A review-only view of parameters that apply to the selected operator.

This is deliberately partial, versioned execution metadata, not a second
natural-language parser. Only parameters confirmed inactive in both the
provider comparison recipe and the local runtime are omitted. Unknown
indicators, versions, operators and parameters remain visible to review.
The stored/compiled candidate is never rewritten by this projection.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import cast

# Shared Catalog parameter forms can contain values for another operator.
# Keep this declaration aligned with provider_catalog._relative_volume and
# runtime._evaluate_relative_volume; runtime parity is covered by tests.
_INACTIVE_TRIGGER_PARAMETERS: Mapping[tuple[str, str, str], frozenset[str]] = {
    ("volume.relative", "1.0.0", trigger): frozenset({"consecutive_days"})
    for trigger in ("gt_multiple", "gte_multiple", "lte_multiple")
}
# Both conditional runtimes read reference_mode only for relative_price.
# Pydantic still serializes its previous_fill default for the other kinds.
_CONDITIONS_WITHOUT_RELATIVE_REFERENCE = frozenset({
    "price", "rebound", "pullback", "take_profit", "stop_loss", "holding_period",
})


def inactive_parameter_names(condition: Mapping[str, object]) -> frozenset[str]:
    key = (
        str(condition.get("indicator_id", "")),
        str(condition.get("definition_version", "")),
        str(condition.get("trigger", "")),
    )
    return _INACTIVE_TRIGGER_PARAMETERS.get(key, frozenset())


def project_executable_semantics(value: object) -> object:
    """Recursively copy a candidate, omitting only proven inactive parameters."""
    if isinstance(value, list):
        return [project_executable_semantics(item) for item in cast(list[object], value)]
    if not isinstance(value, dict):
        return value
    fields = cast(dict[str, object], value)
    projected = {key: project_executable_semantics(child) for key, child in fields.items()}
    # Both conditional runtimes derive all-position sells from actual inventory
    # less the reserve. The schema's fixed-quantity default is not an order size.
    if (isinstance(fields.get("kind"), str)
            and fields.get("kind") in _CONDITIONS_WITHOUT_RELATIVE_REFERENCE | {"relative_price"}
            and fields.get("side") == "sell"
            and fields.get("sizing_mode") == "all_position"):
        projected.pop("quantity", None)
        projected.pop("amount_cny", None)
    if (isinstance(fields.get("kind"), str)
            and fields.get("kind") in _CONDITIONS_WITHOUT_RELATIVE_REFERENCE
            and isinstance(fields.get("side"), str)
            and fields.get("side") in {"buy", "sell"}
            and fields.get("reference_mode") == "previous_fill"):
        projected.pop("reference_mode", None)
    params = projected.get("params")
    if isinstance(params, dict):
        inactive = inactive_parameter_names(fields)
        projected["params"] = {
            name: parameter for name, parameter in cast(dict[str, object], params).items()
            if name not in inactive
        }
    return projected


def _conditions(value: object) -> Iterator[Mapping[str, object]]:
    if isinstance(value, dict):
        fields = cast(dict[str, object], value)
        if isinstance(fields.get("indicator_id"), str):
            yield fields
        for child in fields.values():
            yield from _conditions(child)
    elif isinstance(value, list):
        for child in cast(list[object], value):
            yield from _conditions(child)


def project_indicator_definition(
    definition: Mapping[str, object], candidate: Mapping[str, object],
) -> dict[str, object]:
    """Do not reintroduce an inactive default through the review's Catalog view.

    If another condition uses the parameter, retain its definition. The actual
    candidate view remains per-condition, so mixed operators keep their own
    executable parameters rather than sharing a global exclusion.
    """
    matching = [
        inactive_parameter_names(condition) for condition in _conditions(candidate)
        if condition.get("indicator_id") == definition.get("indicator_id")
    ]
    inactive = matching[0].intersection(*matching[1:]) if matching else frozenset[str]()
    projected = dict(definition)
    parameters = projected.get("parameters")
    if inactive and isinstance(parameters, list):
        projected["parameters"] = [
            parameter for parameter in cast(list[object], parameters)
            if not isinstance(parameter, dict)
            or cast(dict[str, object], parameter).get("name") not in inactive
        ]
    return projected
