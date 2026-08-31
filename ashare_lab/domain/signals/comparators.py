"""Explicit comparison semantics shared by every indicator trigger."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum


class Comparator(StrEnum):
    CROSSES_ABOVE = "crosses_above"
    CROSSES_BELOW = "crosses_below"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"


def evaluate_comparator(
    comparator: Comparator,
    left: Decimal,
    right: Decimal,
    *,
    previous_left: Decimal | None = None,
    previous_right: Decimal | None = None,
) -> bool:
    """Evaluate a comparison with deterministic crossover boundaries.

    A crossover includes equality on the previous observation and requires a
    strict inequality now.  It therefore fires once when two lines separate,
    but does not repeatedly fire while they remain on the same side.
    """

    if comparator is Comparator.GT:
        return left > right
    if comparator is Comparator.GTE:
        return left >= right
    if comparator is Comparator.LT:
        return left < right
    if comparator is Comparator.LTE:
        return left <= right
    if previous_left is None or previous_right is None:
        raise ValueError(f"{comparator.value} requires both previous operands")
    if comparator is Comparator.CROSSES_ABOVE:
        return previous_left <= previous_right and left > right
    if comparator is Comparator.CROSSES_BELOW:
        return previous_left >= previous_right and left < right
    raise ValueError(f"unsupported comparator: {comparator!r}")
