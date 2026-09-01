from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from ashare_lab.domain.signals import SignalRuntime, SignalRuntimeError
from ashare_lab.domain.strategy import IndicatorCondition

from .conftest import make_bars


def _condition(*, trigger: str = "above", value: float = 3.0) -> IndicatorCondition:
    return IndicatorCondition(
        indicator_id="market.turnover_rate",
        definition_version="1.0.0",
        trigger=trigger,
        value=value,
    )


def _bars_with_provider_turnover_rates() -> tuple:
    return tuple(
        replace(
            bar,
            turnover_rate_pct=Decimal(str(rate)),
            turnover_rate_provider="eastmoney_push2his_public",
            turnover_rate_methodology=(
                "eastmoney_push2his.f61.provider_reported_turnover_rate_pct.v1"
            ),
        )
        for bar, rate in zip(make_bars([10, 11]), (2.5, 3.25), strict=True)
    )


def test_turnover_rate_uses_supplier_percent_points_without_deriving_from_volume() -> None:
    facts = SignalRuntime().evaluate(_condition(), _bars_with_provider_turnover_rates())

    assert [fact.triggered for fact in facts] == [False, True]
    assert facts[-1].left_value == Decimal("3.25")
    assert facts[-1].right_value == Decimal("3.0")
    assert "turnover_rate_pct=3.25" in facts[-1].reason


def test_turnover_rate_fails_closed_when_raw_supplier_field_is_absent() -> None:
    with pytest.raises(SignalRuntimeError, match="provider-supplied turnover_rate_pct"):
        SignalRuntime().evaluate(_condition(), make_bars([10, 11]))
