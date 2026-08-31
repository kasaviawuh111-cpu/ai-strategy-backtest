from datetime import date, timedelta
from decimal import Decimal
from math import isinf

import pytest

from ashare_lab.domain.analytics import EquityPoint, RoundTrip, calculate_metrics
from ashare_lab.domain.shared import DomainValidationError


def point(day: int, equity: str, benchmark: str | None = None) -> EquityPoint:
    return EquityPoint(
        session_date=date(2024, 1, 1) + timedelta(days=day),
        equity=Decimal(equity),
        benchmark=None if benchmark is None else Decimal(benchmark),
    )


def test_core_returns_drawdown_benchmark_and_trade_statistics() -> None:
    curve = (
        point(0, "100", "100"),
        point(1, "120", "105"),
        point(2, "90", "103"),
        point(3, "110", "108"),
    )
    trades = (
        RoundTrip(date(2024, 1, 1), date(2024, 1, 2), Decimal("20")),
        RoundTrip(date(2024, 1, 2), date(2024, 1, 4), Decimal("-10")),
    )

    metrics = calculate_metrics(curve, trades)

    assert metrics.total_return == pytest.approx(0.10)
    assert metrics.maximum_drawdown == pytest.approx(-0.25)
    assert metrics.benchmark_return == pytest.approx(0.08)
    assert metrics.excess_return == pytest.approx(0.02)
    assert metrics.win_rate == 0.5
    assert metrics.profit_factor == 2.0
    assert metrics.average_holding_days == 1.5
    assert metrics.statistical_warning == "sample_too_small_for_strong_inference"


def test_all_winners_have_infinite_profit_factor_without_division_error() -> None:
    metrics = calculate_metrics(
        (point(0, "100"), point(1, "101")),
        (RoundTrip(date(2024, 1, 1), date(2024, 1, 2), Decimal("1")),),
    )

    assert metrics.profit_factor is not None and isinf(metrics.profit_factor)


def test_missing_benchmark_is_not_silently_partially_calculated() -> None:
    metrics = calculate_metrics((point(0, "100", "100"), point(1, "101")))

    assert metrics.benchmark_return is None
    assert metrics.excess_return is None


def test_curve_dates_must_be_unique_and_sorted() -> None:
    with pytest.raises(DomainValidationError, match="strictly increasing"):
        calculate_metrics((point(1, "100"), point(0, "101")))
