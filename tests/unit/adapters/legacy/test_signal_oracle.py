from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

import pytest

from ashare_lab.adapters.legacy import LegacyAstockSignalOracle
from ashare_lab.domain.signals import macd, moving_average_cross, relative_volume, rsi

from ...signals.conftest import make_bars


def _decimals(values: Sequence[int | str]) -> tuple[Decimal, ...]:
    return tuple(Decimal(str(value)) for value in values)


def _ma_cross_up(closes: tuple[Decimal, ...]) -> tuple[bool, ...]:
    points = moving_average_cross(make_bars(closes), fast_period=5, slow_period=20)
    result = [False] * len(points)
    for index in range(1, len(points)):
        previous = points[index - 1]
        current = points[index]
        if previous is not None and current is not None:
            result[index] = previous.fast <= previous.slow and current.fast > current.slow
    return tuple(result)


def _macd_cross_up(closes: tuple[Decimal, ...]) -> tuple[bool, ...]:
    points = macd(make_bars(closes))
    result = [False] * len(points)
    for index in range(1, len(points)):
        previous = points[index - 1]
        current = points[index]
        if previous is not None and current is not None:
            result[index] = previous.histogram <= 0 and current.histogram > 0
    return tuple(result)


def _rsi_below_30(closes: tuple[Decimal, ...]) -> tuple[bool, ...]:
    return tuple(value is not None and value < 30 for value in rsi(make_bars(closes)))


def _relative_volume_2x(
    closes: tuple[Decimal, ...],
    volumes: tuple[int, ...],
) -> tuple[bool, ...]:
    values = relative_volume(make_bars(closes, volumes=volumes), period=20)
    return tuple(value is not None and value >= 2 for value in values)


def test_ma_and_macd_events_match_after_their_conservative_common_warmups() -> None:
    closes = _decimals(
        [30] * 25
        + list(range(29, 9, -1))
        + list(range(10, 50))
        + list(range(49, 19, -1))
        + list(range(20, 70))
    )
    oracle = LegacyAstockSignalOracle()

    ma = oracle.compare(
        "ma_5_cross_up_20",
        closes=closes,
        canonical_values=_ma_cross_up(closes),
    )
    macd_result = oracle.compare(
        "macd_golden_cross",
        closes=closes,
        canonical_values=_macd_cross_up(closes),
    )

    assert ma.values_equal_after_start and ma.semantically_eligible
    assert macd_result.values_equal_after_start and macd_result.semantically_eligible
    assert ma.comparison_start == 21
    assert macd_result.comparison_start == 37
    assert any(ma.legacy_values[ma.comparison_start :])
    assert any(macd_result.legacy_values[macd_result.comparison_start :])
    assert "2*(DIF-DEA)" in " ".join(macd_result.notes)


def test_rsi_is_characterization_only_because_the_seed_and_recurrence_differ() -> None:
    closes = _decimals(
        (
            100,
            98,
            96,
            94,
            92,
            90,
            88,
            86,
            84,
            82,
            80,
            78,
            76,
            74,
            72,
            73,
            71,
            72,
            69,
            70,
            68,
            67,
            69,
            66,
            65,
            67,
            64,
            63,
            66,
            62,
            64,
            61,
            60,
            63,
            59,
            58,
            62,
            57,
            56,
            60,
        )
    )
    result = LegacyAstockSignalOracle().compare(
        "rsi_below_30",
        closes=closes,
        canonical_values=_rsi_below_30(closes),
    )

    assert not result.semantically_eligible
    assert result.mismatch_indices
    assert "Wilder" in " ".join(result.notes)


def test_volume_is_characterization_only_and_suspension_rows_fail_equivalence() -> None:
    closes = _decimals([10] * 26)
    volumes = (0,) + (100,) * 20 + (205, 100, 100, 100, 100)
    result = LegacyAstockSignalOracle().compare(
        "volume_2x_ma20",
        closes=closes,
        volumes=volumes,
        canonical_values=_relative_volume_2x(closes, volumes),
    )

    assert result.contains_zero_volume_rows
    assert not result.semantically_eligible
    assert result.mismatch_indices == (21,)
    assert "trading-status" in " ".join(result.notes)


@pytest.mark.parametrize(
    ("signal_id", "volumes"),
    [
        ("ma_5_cross_up_20", None),
        ("macd_golden_cross", None),
        ("rsi_below_30", None),
        ("volume_2x_ma20", tuple(100 + (index % 7) * 50 for index in range(100))),
    ],
)
def test_selected_legacy_atoms_are_prefix_invariant(
    signal_id: str,
    volumes: tuple[int, ...] | None,
) -> None:
    closes = _decimals([100 + ((index * 7) % 19) - (index % 5) for index in range(100)])
    oracle = LegacyAstockSignalOracle()
    full = oracle.compute(signal_id, closes=closes, volumes=volumes)

    for length in (1, 14, 21, 37, 63, 99):
        prefix_volumes = None if volumes is None else volumes[:length]
        assert (
            oracle.compute(
                signal_id,
                closes=closes[:length],
                volumes=prefix_volumes,
            )
            == full[:length]
        )


def test_oracle_rejects_unselected_atoms_and_misaligned_or_non_decimal_inputs() -> None:
    oracle = LegacyAstockSignalOracle()

    with pytest.raises(ValueError, match="unsupported legacy shadow signal"):
        oracle.compute("kdj_golden_cross_low", closes=_decimals([1, 2, 3]))
    with pytest.raises(ValueError, match="align with closes"):
        oracle.compute("ma_5_cross_up_20", closes=_decimals([1, 2]), volumes=[100])
    with pytest.raises(TypeError, match="Decimal"):
        oracle.compute("ma_5_cross_up_20", closes=[Decimal(1), 2])  # type: ignore[list-item]
