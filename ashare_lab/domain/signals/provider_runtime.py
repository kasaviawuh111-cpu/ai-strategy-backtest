"""Evaluate DSL conditions over provider-supplied indicator values.

The provider owns every indicator value.  This runtime only applies the DSL's
comparison, crossover and consecutive-session semantics; it never derives an
indicator from OHLCV and never falls back to the legacy formula runtime.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from datetime import date
from decimal import Decimal, InvalidOperation

from ashare_lab.domain.historical_units import unit_definition, unit_text
from ashare_lab.domain.shared import DomainValidationError, InstrumentId
from ashare_lab.domain.strategy.models import (
    AllCondition,
    AnyCondition,
    Condition,
    IndicatorCondition,
    NotCondition,
)
from ashare_lab.ports.provider_indicator_data import (
    ProviderIndicatorPoint,
    ProviderIndicatorSeries,
    ProviderIndicatorValue,
)

from .comparators import Comparator, evaluate_comparator
from .models import SignalEvidence, SignalFact
from .provider_catalog import (
    ProviderCatalogError,
    ProviderComparisonBinding,
    ProviderIndicatorBinding,
    ProviderRightOperand,
    RightOperandSource,
    provider_binding_for_condition,
)


class ProviderSignalRuntimeError(DomainValidationError):
    """Provider data cannot safely satisfy the requested DSL condition."""


def binding_for_provider_condition(condition: IndicatorCondition) -> ProviderIndicatorBinding:
    """Return the provider-owned fields and deterministic comparison recipe."""

    try:
        return provider_binding_for_condition(condition)
    except ProviderCatalogError as exc:
        raise ProviderSignalRuntimeError(str(exc)) from exc


def provider_condition_leaves(
    condition: Condition,
    *,
    path: str = "$",
) -> tuple[tuple[str, IndicatorCondition], ...]:
    """Return stable paths for every provider-owned indicator leaf.

    Acquisition uses these paths as identities, so two conditions for the same
    indicator and different parameters cannot accidentally share a series.
    Financial and event leaves are deliberately rejected by this technical-only
    provider runtime.
    """

    if isinstance(condition, IndicatorCondition):
        return ((path, condition),)
    if isinstance(condition, (AllCondition, AnyCondition)):
        return tuple(
            leaf
            for index, child in enumerate(condition.children)
            for leaf in provider_condition_leaves(
                child,
                path=f"{path}.children[{index}]",
            )
        )
    if isinstance(condition, NotCondition):
        return provider_condition_leaves(condition.child, path=f"{path}.child")
    raise ProviderSignalRuntimeError(
        "provider technical runtime cannot evaluate financial or event conditions"
    )


def evaluate_provider_condition_tree_aligned(
    condition: Condition,
    series_by_path: Mapping[str, ProviderIndicatorSeries],
    session_dates: tuple[date, ...],
) -> tuple[SignalFact | None, ...]:
    """Evaluate a complete technical condition tree from provider values only."""

    leaves = provider_condition_leaves(condition)
    expected_paths = {path for path, _ in leaves}
    actual_paths = set(series_by_path)
    if actual_paths != expected_paths:
        missing = sorted(expected_paths - actual_paths)
        extra = sorted(actual_paths - expected_paths)
        raise ProviderSignalRuntimeError(
            f"provider series paths do not match condition leaves: "
            f"missing={missing[:3]}, extra={extra[:3]}"
        )
    leaf_timelines = {
        path: evaluate_provider_indicator_aligned(
            leaf,
            series_by_path[path],
            session_dates,
        )
        for path, leaf in leaves
    }
    return combine_condition_timelines(condition, leaf_timelines, session_dates)


def combine_condition_timelines(
    condition: Condition,
    leaf_timelines: Mapping[str, tuple[SignalFact | None, ...]],
    session_dates: tuple[date, ...],
) -> tuple[SignalFact | None, ...]:
    """Combine already evaluated leaves without changing their source evidence."""

    if set(leaf_timelines) != {path for path, _ in provider_condition_leaves(condition)}:
        raise ProviderSignalRuntimeError("signal timeline paths do not match condition leaves")
    for timeline in leaf_timelines.values():
        if len(timeline) != len(session_dates) or any(
            fact is not None and fact.session_date != day
            for fact, day in zip(timeline, session_dates, strict=True)
        ):
            raise ProviderSignalRuntimeError("signal timelines are not aligned")
    return _evaluate_provider_tree(
        condition,
        path="$",
        leaf_timelines=leaf_timelines,
        session_dates=session_dates,
    )


def evaluate_provider_indicator_aligned(
    condition: IndicatorCondition,
    series: ProviderIndicatorSeries,
    session_dates: tuple[date, ...],
    *,
    binding: ProviderIndicatorBinding | None = None,
) -> tuple[SignalFact | None, ...]:
    """Evaluate one condition using only exact values in ``series``.

    A provider can legitimately publish one calculated indicator later than
    the underlying daily bar.  A missing session is therefore represented as
    ``None`` (no signal), never zero-filled or carried forward.  Provider rows
    outside the trusted market-data session axis still fail closed.
    """

    resolved = binding or binding_for_provider_condition(condition)
    if (
        series.indicator_id != condition.indicator_id
        or resolved.indicator_id != condition.indicator_id
    ):
        raise ProviderSignalRuntimeError("provider indicator identity does not match the DSL")
    if resolved.trigger != condition.trigger:
        raise ProviderSignalRuntimeError("provider trigger binding does not match the DSL")
    if not session_dates or session_dates != tuple(sorted(set(session_dates))):
        raise ProviderSignalRuntimeError("expected sessions must be unique and ascending")
    points_by_date = {point.session_date: point for point in series.points}
    extra = sorted(set(points_by_date) - set(session_dates))
    if extra:
        raise ProviderSignalRuntimeError(
            f"provider indicator history contains sessions outside the trusted axis: "
            f"extra={extra[:3]}"
        )

    instrument_id = InstrumentId(series.instrument_id)
    ordered_points = tuple(points_by_date.get(item) for item in session_dates)
    row_results: list[bool | None] = []
    row_operands: list[tuple[Decimal, Decimal] | None] = []
    row_reasons: list[str | None] = []
    for index, point in enumerate(ordered_points):
        if point is None:
            row_results.append(None)
            row_operands.append(None)
            row_reasons.append(None)
            continue
        previous = ordered_points[index - 1] if index else None
        evaluations: list[bool] = []
        reasons: list[str] = []
        display_operands: tuple[Decimal, Decimal] | None = None
        unavailable = False
        for comparison in resolved.comparisons:
            result = _evaluate_comparison(
                comparison,
                condition=condition,
                point=point,
                previous_point=previous,
            )
            if result is None:
                unavailable = True
                break
            triggered, left, right = result
            evaluations.append(triggered)
            display_operands = display_operands or (left, right)
            reasons.append(
                f"{comparison.left_field} {comparison.comparator.value} "
                f"{_right_label(comparison.right)}"
            )
        row_results.append(None if unavailable else all(evaluations))
        row_operands.append(None if unavailable else display_operands)
        row_reasons.append(None if unavailable else " AND ".join(reasons))

    timeline: list[SignalFact | None] = []
    required_streak = resolved.consecutive_sessions
    for index, (session_date, point) in enumerate(zip(session_dates, ordered_points, strict=True)):
        if point is None:
            timeline.append(None)
            continue
        window_start = index - required_streak + 1
        if window_start < 0 or any(
            result is None for result in row_results[window_start : index + 1]
        ):
            timeline.append(None)
            continue
        triggered = all(result is True for result in row_results[window_start : index + 1])
        operands = row_operands[index]
        reason = row_reasons[index]
        assert operands is not None and reason is not None
        if required_streak > 1:
            reason = f"{reason}; consecutive_sessions={required_streak}"
        timeline.append(
            SignalFact(
                instrument_id=instrument_id,
                session_date=session_date,
                condition_ref=(
                    f"{condition.indicator_id}@{condition.definition_version}:{condition.trigger}"
                ),
                triggered=triggered,
                observed_at=point.observed_at,
                available_at=point.first_available_at,
                reason=f"provider:{series.provider}:{reason}",
                left_value=operands[0],
                right_value=operands[1],
                evidence=(
                    SignalEvidence(
                        evidence_type="provider_indicator",
                        evidence_id=(
                            f"{series.provider}:{series.indicator_id}:{session_date.isoformat()}"
                        ),
                        available_at=point.first_available_at,
                        provider=series.provider,
                        timestamp_precision="second",
                        validation_status="provider_value_validated",
                        raw_response_sha256=series.response_sha256,
                    ),
                ),
            )
        )
    return tuple(timeline)


def _evaluate_provider_tree(
    condition: Condition,
    *,
    path: str,
    leaf_timelines: Mapping[str, tuple[SignalFact | None, ...]],
    session_dates: tuple[date, ...],
) -> tuple[SignalFact | None, ...]:
    if isinstance(condition, IndicatorCondition):
        return leaf_timelines[path]
    if isinstance(condition, (AllCondition, AnyCondition)):
        child_timelines = tuple(
            _evaluate_provider_tree(
                child,
                path=f"{path}.children[{index}]",
                leaf_timelines=leaf_timelines,
                session_dates=session_dates,
            )
            for index, child in enumerate(condition.children)
        )
        return _combine_provider_facts(condition.type, child_timelines, session_dates)
    if isinstance(condition, NotCondition):
        child_timeline = _evaluate_provider_tree(
            condition.child,
            path=f"{path}.child",
            leaf_timelines=leaf_timelines,
            session_dates=session_dates,
        )
        return _combine_provider_facts("not", (child_timeline,), session_dates)
    raise ProviderSignalRuntimeError("provider condition type is unsupported")


def _combine_provider_facts(
    operator: str,
    child_timelines: tuple[tuple[SignalFact | None, ...], ...],
    session_dates: tuple[date, ...],
) -> tuple[SignalFact | None, ...]:
    if any(len(timeline) != len(session_dates) for timeline in child_timelines):
        raise ProviderSignalRuntimeError("provider condition timelines are not aligned")
    aligned: list[SignalFact | None] = []
    for index, session_date in enumerate(session_dates):
        children = tuple(timeline[index] for timeline in child_timelines)
        facts = tuple(child for child in children if child is not None)
        states = tuple(None if child is None else child.triggered for child in children)
        if operator == "all":
            if False in states:
                triggered = False
            elif all(state is True for state in states):
                triggered = True
            else:
                aligned.append(None)
                continue
        elif operator == "any":
            if True in states:
                triggered = True
            elif all(state is False for state in states):
                triggered = False
            else:
                aligned.append(None)
                continue
        elif operator == "not":
            if not facts or states[0] is None:
                aligned.append(None)
                continue
            triggered = not facts[0].triggered
        else:  # pragma: no cover - internal exhaustive guard.
            raise ProviderSignalRuntimeError(f"unsupported provider boolean operator {operator!r}")
        instrument_ids = {fact.instrument_id for fact in facts}
        if len(instrument_ids) != 1 or any(
            fact.session_date != session_date for fact in facts
        ):
            raise ProviderSignalRuntimeError("provider condition facts have inconsistent identity")
        observed_at = max(fact.observed_at for fact in facts)
        available_at = max(fact.available_at for fact in facts)
        child_results = ",".join(
            "unknown" if state is None else "true" if state else "false" for state in states
        )
        aligned.append(
            SignalFact(
                instrument_id=next(iter(instrument_ids)),
                session_date=session_date,
                condition_ref=operator,
                triggered=triggered,
                observed_at=observed_at,
                available_at=max(observed_at, available_at),
                reason=f"conditions:{operator}[{child_results}] => "
                f"{'true' if triggered else 'false'}",
                children=facts,
                evidence=_merge_provider_evidence(facts),
            )
        )
    return tuple(aligned)


def _merge_provider_evidence(facts: tuple[SignalFact, ...]) -> tuple[SignalEvidence, ...]:
    merged: dict[tuple[str, str, str | None], SignalEvidence] = {}
    for fact in facts:
        for item in fact.evidence:
            key = (item.evidence_type, item.evidence_id, item.source_event_id)
            merged[key] = item
    return tuple(merged[key] for key in sorted(merged, key=lambda item: str(item)))


def _evaluate_comparison(
    comparison: ProviderComparisonBinding,
    *,
    condition: IndicatorCondition,
    point: ProviderIndicatorPoint,
    previous_point: ProviderIndicatorPoint | None,
) -> tuple[bool, Decimal, Decimal] | None:
    if comparison.right.source in {RightOperandSource.FIELD, RightOperandSource.PREVIOUS_FIELD}:
        right_point = (
            point if comparison.right.source is RightOperandSource.FIELD else previous_point
        )
        if right_point is not None:
            assert comparison.right.field_name is not None
            _require_compatible_field_units(
                _field(point, comparison.left_field),
                _field(right_point, comparison.right.field_name),
            )
            if condition.indicator_id in {"volume.relative", "volume.price_confirmation"} and (
                _field_value(point, comparison.left_field) <= 0
                or _field_value(right_point, comparison.right.field_name) <= 0
            ):
                # Rewriting volume / baseline > multiplier as volume >
                # baseline * multiplier is equivalent only for a positive
                # baseline. Suspensions / zero means remain unavailable, just
                # as in the existing relative-volume formula evaluator.
                return None
    left = _field_value(point, comparison.left_field)
    right = _right_value(
        comparison.right,
        condition=condition,
        point=point,
        previous_point=previous_point,
    )
    if right is None:
        return None
    previous_left: Decimal | None = None
    previous_right: Decimal | None = None
    if comparison.comparator in {
        Comparator.CROSSES_ABOVE,
        Comparator.CROSSES_BELOW,
    }:
        if previous_point is None:
            return None
        _require_compatible_field_units(
            _field(point, comparison.left_field), _field(previous_point, comparison.left_field),
        )
        previous_left = _field_value(previous_point, comparison.left_field)
        previous_right = _right_value(
            comparison.right,
            condition=condition,
            point=previous_point,
            previous_point=None,
        )
        if previous_right is None:
            return None
    return (
        evaluate_comparator(
            comparison.comparator,
            left,
            right,
            previous_left=previous_left,
            previous_right=previous_right,
        ),
        left,
        right,
    )


def _right_value(
    operand: ProviderRightOperand,
    *,
    condition: IndicatorCondition,
    point: ProviderIndicatorPoint,
    previous_point: ProviderIndicatorPoint | None,
) -> Decimal | None:
    if operand.source is RightOperandSource.FIELD:
        assert operand.field_name is not None
        value = _field_value(point, operand.field_name)
    elif operand.source is RightOperandSource.PREVIOUS_FIELD:
        if previous_point is None:
            return None
        assert operand.field_name is not None
        value = _field_value(previous_point, operand.field_name)
    elif operand.source is RightOperandSource.CONDITION_VALUE:
        if condition.value is None:
            raise ProviderSignalRuntimeError("provider threshold condition has no value")
        value = _decimal(condition.value, "condition value")
    elif operand.source is RightOperandSource.PARAMETER:
        assert operand.parameter_name is not None
        if operand.parameter_name not in condition.params:
            raise ProviderSignalRuntimeError(
                f"provider condition is missing parameter {operand.parameter_name!r}"
            )
        value = _decimal(
            condition.params[operand.parameter_name],
            f"parameter {operand.parameter_name}",
        )
    elif operand.source is RightOperandSource.CONSTANT:
        assert operand.constant is not None
        value = operand.constant
    else:  # pragma: no cover - exhaustive enum guard.
        raise ProviderSignalRuntimeError("provider right operand source is unsupported")
    return value * operand.multiplier


def _decimal(value: object, label: str) -> Decimal:
    if isinstance(value, bool):
        raise ProviderSignalRuntimeError(f"provider {label} must be numeric")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ProviderSignalRuntimeError(f"provider {label} must be numeric") from exc
    if not result.is_finite():
        raise ProviderSignalRuntimeError(f"provider {label} must be finite")
    return result


def _field(point: ProviderIndicatorPoint, requested_name: str) -> ProviderIndicatorValue:
    matches = tuple(
        item
        for item in point.values
        if _field_token(item.field_name) == _field_token(requested_name)
    )
    if len(matches) != 1:
        raise ProviderSignalRuntimeError(
            f"provider field {requested_name!r} is missing or ambiguous on {point.session_date}"
        )
    return matches[0]


def _field_value(point: ProviderIndicatorPoint, requested_name: str) -> Decimal:
    return _field(point, requested_name).value


def _require_compatible_field_units(
    left: ProviderIndicatorValue, right: ProviderIndicatorValue,
) -> None:
    # Acquisition owns normalization. Do not infer missing units for historic
    # fixtures or reinterpret raw values here; reject explicit dimension OR
    # scale mismatches before comparing, including previous-session operands.
    if left.unit is None or right.unit is None:
        return
    if unit_definition(unit_text(left.unit)) != unit_definition(unit_text(right.unit)):
        raise ProviderSignalRuntimeError("provider comparison fields have incompatible units")


def _field_token(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        character for character in normalized if character.isalnum() or character in "+-"
    )


def _right_label(operand: ProviderRightOperand) -> str:
    if operand.source in {RightOperandSource.FIELD, RightOperandSource.PREVIOUS_FIELD}:
        return str(operand.field_name)
    if operand.source is RightOperandSource.PARAMETER:
        return f"param:{operand.parameter_name}"
    if operand.source is RightOperandSource.CONDITION_VALUE:
        return "condition.value"
    return str(operand.constant)
