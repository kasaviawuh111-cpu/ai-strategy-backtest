"""Dependency discovery only; these leg views are not executable StrategySpecs."""
from types import SimpleNamespace

import pytest

from ashare_lab.domain.financials.models import FinancialMetricId, FinancialUnit
from ashare_lab.domain.strategy.models import (
    AllCondition, EventCondition, FinancialCondition, FirstOfExit, IndicatorCondition,
    iter_event_conditions, iter_financial_conditions, iter_indicator_conditions,
)


@pytest.mark.parametrize("side", ["entry", "exit", "both"])
def test_dependency_discovery_does_not_require_the_other_leg(side):
    leaves = (
        IndicatorCondition(indicator_id="technical.macd", definition_version="1.0.0",
                           params={}, trigger="golden_cross"),
        EventCondition(event_code="event.company.repurchase", definition_version="1.0.0"),
        FinancialCondition(metric_id=FinancialMetricId.ROE, comparator="gt", value=1,
                           unit=FinancialUnit.PERCENT, statement_scope="consolidated"),
    )
    root = AllCondition(children=leaves)
    view = SimpleNamespace(entry=root if side != "exit" else None,
                           exit=FirstOfExit(children=(root,)) if side != "entry" else None)
    count = 2 if side == "both" else 1
    for iterator, expected in zip(
        (iter_indicator_conditions, iter_event_conditions, iter_financial_conditions), leaves,
        strict=True,
    ):
        assert tuple(iterator(view)) == (expected,) * count
