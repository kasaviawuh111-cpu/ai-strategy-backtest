from __future__ import annotations

from decimal import Decimal

import pytest

from ashare_lab.domain.signals import Comparator, evaluate_comparator


@pytest.mark.parametrize(
    ("comparator", "left", "right", "expected"),
    [
        (Comparator.GT, "2", "1", True),
        (Comparator.GT, "1", "1", False),
        (Comparator.GTE, "1", "1", True),
        (Comparator.LT, "1", "2", True),
        (Comparator.LT, "1", "1", False),
        (Comparator.LTE, "1", "1", True),
    ],
)
def test_level_comparison_boundaries(
    comparator: Comparator,
    left: str,
    right: str,
    expected: bool,
) -> None:
    assert evaluate_comparator(comparator, Decimal(left), Decimal(right)) is expected


def test_cross_includes_previous_equality_but_requires_strict_current_side() -> None:
    assert evaluate_comparator(
        Comparator.CROSSES_ABOVE,
        Decimal("2"),
        Decimal("1"),
        previous_left=Decimal("1"),
        previous_right=Decimal("1"),
    )
    assert not evaluate_comparator(
        Comparator.CROSSES_ABOVE,
        Decimal("1"),
        Decimal("1"),
        previous_left=Decimal("0"),
        previous_right=Decimal("1"),
    )
    assert evaluate_comparator(
        Comparator.CROSSES_BELOW,
        Decimal("0"),
        Decimal("1"),
        previous_left=Decimal("1"),
        previous_right=Decimal("1"),
    )


def test_cross_requires_previous_operands() -> None:
    with pytest.raises(ValueError, match="requires both previous operands"):
        evaluate_comparator(Comparator.CROSSES_ABOVE, Decimal("2"), Decimal("1"))
