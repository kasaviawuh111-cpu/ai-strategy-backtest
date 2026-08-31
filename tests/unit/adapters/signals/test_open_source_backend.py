from __future__ import annotations

from decimal import Decimal

import pytest

from ashare_lab.adapters.signals import MossPandasIndicatorBackend
from ashare_lab.domain.signals.indicators import (
    exponential_moving_average,
    macd,
    rsi,
)
from ashare_lab.ports.indicator_backend import IndicatorBackend, MacdIndicatorPoint
from tests.unit.signals.conftest import make_bars

TOLERANCE = Decimal("1e-12")


def test_backend_satisfies_provider_protocol_and_is_pinned_to_moss() -> None:
    backend = MossPandasIndicatorBackend()
    closes = tuple(Decimal(value) for value in (1, 2, 3, 4))

    assert isinstance(backend, IndicatorBackend)
    assert "1fce09b03151a01ba1dee230466ec64cdd12fda8" in backend.backend_id
    assert backend.adoption_allowlist == frozenset({"ema", "macd"})
    assert backend.ema(closes, period=3) == (
        None,
        None,
        Decimal("2.25"),
        Decimal("3.125"),
    )
    # The pinned MOSS semantics turn a zero average loss into NaN.  Keeping
    # this visible is why RSI is not in the replacement allowlist.
    assert backend.rsi(closes, period=3) == (None, None, None, None)


def test_moss_ema_and_macd_match_canonical_with_explicit_float_tolerance() -> None:
    backend = MossPandasIndicatorBackend()
    closes = tuple(
        Decimal(100) + Decimal((index * 7) % 13) + Decimal(index) / Decimal(10)
        for index in range(64)
    )
    bars = make_bars(closes)

    _assert_numeric_close(
        backend.ema(closes, period=20),
        exponential_moving_average(bars, 20),
    )

    expected_macd = tuple(
        None
        if point is None
        else MacdIndicatorPoint(
            dif=point.macd_line,
            dea=point.signal_line,
            histogram=point.histogram,
        )
        for point in macd(bars)
    )
    actual_macd = backend.macd(closes)
    _assert_macd_close(actual_macd, expected_macd)
    assert all(
        point is None or point.histogram == Decimal(2) * (point.dif - point.dea)
        for point in actual_macd
    )


def test_moss_rsi_is_an_explicit_differential_oracle_not_an_adopted_definition() -> None:
    backend = MossPandasIndicatorBackend()
    closes = tuple(
        Decimal(100) + Decimal((index * 7) % 13) + Decimal(index) / Decimal(10)
        for index in range(64)
    )
    canonical = rsi(make_bars(closes), 14)
    candidate = backend.rsi(closes, period=14)

    assert "rsi" not in backend.adoption_allowlist
    assert candidate[14] is not None
    assert canonical[14] is not None
    assert abs(candidate[14] - canonical[14]) > Decimal("0.1")


def test_adapter_and_canonical_implementations_are_prefix_stable() -> None:
    backend = MossPandasIndicatorBackend()
    closes = tuple(
        Decimal(50) + Decimal((index * 11) % 17) - Decimal(index % 3) / Decimal(10)
        for index in range(48)
    )
    bars = make_bars(closes)
    adapter_full = (
        backend.ema(closes, period=7),
        backend.rsi(closes, period=6),
        backend.macd(closes, fast=4, slow=9, signal=3),
    )
    canonical_full = (
        exponential_moving_average(bars, 7),
        rsi(bars, 6),
        macd(bars, fast=4, slow=9, signal=3),
    )

    for end in range(1, len(closes) + 1):
        prefix = closes[:end]
        prefix_bars = bars[:end]
        assert backend.ema(prefix, period=7) == adapter_full[0][:end]
        assert backend.rsi(prefix, period=6) == adapter_full[1][:end]
        assert backend.macd(prefix, fast=4, slow=9, signal=3) == adapter_full[2][:end]
        assert exponential_moving_average(prefix_bars, 7) == canonical_full[0][:end]
        assert rsi(prefix_bars, 6) == canonical_full[1][:end]
        assert macd(prefix_bars, fast=4, slow=9, signal=3) == canonical_full[2][:end]


def test_invalid_periods_and_non_decimal_inputs_fail_closed() -> None:
    backend = MossPandasIndicatorBackend()

    with pytest.raises(ValueError, match="positive integer"):
        backend.ema((Decimal(1),), period=0)
    with pytest.raises(ValueError, match="less than slow"):
        backend.macd((Decimal(1), Decimal(2)), fast=2, slow=2)
    with pytest.raises(TypeError, match="Decimal"):
        backend.rsi((1, 2, 3), period=2)  # type: ignore[arg-type]


def _assert_numeric_close(
    actual: tuple[Decimal | None, ...],
    expected: tuple[Decimal | None, ...],
) -> None:
    assert len(actual) == len(expected)
    for candidate, canonical in zip(actual, expected, strict=True):
        assert (candidate is None) is (canonical is None)
        if candidate is not None and canonical is not None:
            assert abs(candidate - canonical) <= TOLERANCE


def _assert_macd_close(
    actual: tuple[MacdIndicatorPoint | None, ...],
    expected: tuple[MacdIndicatorPoint | None, ...],
) -> None:
    assert len(actual) == len(expected)
    for candidate, canonical in zip(actual, expected, strict=True):
        assert (candidate is None) is (canonical is None)
        if candidate is None or canonical is None:
            continue
        assert abs(candidate.dif - canonical.dif) <= TOLERANCE
        assert abs(candidate.dea - canonical.dea) <= TOLERANCE
        assert abs(candidate.histogram - canonical.histogram) <= TOLERANCE
