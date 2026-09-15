from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from ashare_lab.application.skill_indicator_routes import build_skill_indicator_routes
from ashare_lab.domain.catalog import (
    CatalogSnapshot,
    CoverageCatalogSnapshot,
    load_catalog_directory,
    load_coverage_catalog_directory,
)
from ashare_lab.domain.signals.runtime import STABLE_INDICATOR_EVALUATOR_IDS

ROOT = Path(__file__).resolve().parents[3]
PROVIDER_IDS = {
    "technical.ma_cross", "technical.rsi", "price.close", "price.amplitude",
    "market.amount", "market.turnover_rate", "price.consecutive_up",
}
EXISTING_LOCAL_IDS = {
    "technical.ma", "technical.macd", "technical.donchian",
    "price.rolling_high", "volume.relative",
}
NEW_LOCAL_IDS = {
    "amount.average", "market.volume", "price.return_pct", "price.true_range",
    "technical.adx", "technical.atr", "technical.bbi", "technical.bias",
    "technical.bollinger", "technical.cci", "technical.dmi", "technical.ema",
    "technical.ema_bias", "technical.historical_volatility", "technical.kdj",
    "technical.momentum", "technical.natr", "technical.obv", "technical.return_stddev",
    "technical.roc", "technical.stochastic", "technical.trend_regime",
    "technical.williams_r", "volume.price_confirmation", "volume.price_divergence",
}


@pytest.fixture(scope="module")
def catalogs() -> tuple[CatalogSnapshot, CoverageCatalogSnapshot]:
    return (
        load_catalog_directory(ROOT / "catalogs"),
        load_coverage_catalog_directory(ROOT / "catalogs" / "coverage"),
    )


def test_current_routes_preserve_provider_contracts_and_enable_engine_formulas(
    catalogs: tuple[CatalogSnapshot, CoverageCatalogSnapshot],
) -> None:
    routes = build_skill_indicator_routes(*catalogs, provider_first=False)

    assert set(routes) == STABLE_INDICATOR_EVALUATOR_IDS
    assert Counter(route.source for route in routes.values()) == {
        "skill_ohlcv_python": 30, "provider_indicator": 7,
    }
    assert {key for key, route in routes.items() if route.source == "provider_indicator"} == (
        PROVIDER_IDS
    )
    assert len(NEW_LOCAL_IDS) == 25
    assert set(routes) == EXISTING_LOCAL_IDS | NEW_LOCAL_IDS | PROVIDER_IDS
    for indicator_id in EXISTING_LOCAL_IDS | NEW_LOCAL_IDS:
        assert routes[indicator_id].source == "skill_ohlcv_python"
        assert routes[indicator_id].reason is None


def test_routes_retain_catalog_formula_implementation_and_all_required_fields(
    catalogs: tuple[CatalogSnapshot, CoverageCatalogSnapshot],
) -> None:
    catalog, coverage = catalogs
    routes = build_skill_indicator_routes(catalog, coverage, provider_first=False)
    metrics = {metric.id: metric for metric in coverage.metrics}

    for indicator_id, route in routes.items():
        metric = metrics[indicator_id]
        assert route.formula_summary == metric.formula_summary
        assert route.implementation_ref == metric.implementation_ref
        assert route.required_fields == tuple(sorted({
            field for requirement in metric.data_requirements for field in requirement.fields
        }))


def test_turnover_rate_is_provider_data_not_inferred_from_ohlcv(
    catalogs: tuple[CatalogSnapshot, CoverageCatalogSnapshot],
) -> None:
    route = build_skill_indicator_routes(*catalogs, provider_first=False)["market.turnover_rate"]

    assert route.source == "provider_indicator"
    assert {
        "turnover_rate_pct", "turnover_rate_provider", "turnover_rate_methodology",
    } <= set(route.required_fields)
    assert "原始换手率" in route.formula_summary


@pytest.mark.parametrize("indicator_id", ["technical.ema", "technical.rsi"])
@pytest.mark.parametrize("dependency_change", ["extra_float_field", "extra_event", "intraday"])
def test_new_unconnected_dependency_is_not_opened_by_existing_indicator_identity(
    catalogs: tuple[CatalogSnapshot, CoverageCatalogSnapshot],
    indicator_id: str,
    dependency_change: str,
) -> None:
    catalog, coverage = catalogs
    metric = next(item for item in coverage.metrics if item.id == indicator_id)
    daily = metric.data_requirements[0]
    if dependency_change == "extra_float_field":
        requirements = (
            daily.model_copy(update={"fields": (*daily.fields, "float_shares")}),
        )
    elif dependency_change == "extra_event":
        requirements = (
            *metric.data_requirements,
            daily.model_copy(update={
                "dataset_id": "issuer.events", "frequency": "event",
                "fields": ("instrument_id", "available_at", "event_code"),
            }),
        )
    else:
        requirements = (daily.model_copy(update={"frequency": "1m"}),)
    changed_metric = metric.model_copy(update={"data_requirements": requirements})
    changed_coverage = coverage.model_copy(update={
        "metrics": tuple(
            changed_metric if item.id == indicator_id else item for item in coverage.metrics
        ),
    })

    route = build_skill_indicator_routes(
        catalog, changed_coverage, provider_first=False,
    )[indicator_id]

    assert route.source == "unavailable"
    assert route.reason
    assert route.source_note == route.reason
    assert route.formula_summary == metric.formula_summary
    assert route.implementation_ref == metric.implementation_ref
    if dependency_change == "extra_float_field":
        assert "float_shares" in route.required_fields


def test_unknown_stable_indicator_with_connected_data_but_no_runtime_is_unavailable(
    catalogs: tuple[CatalogSnapshot, CoverageCatalogSnapshot],
) -> None:
    catalog, coverage = catalogs
    indicator_id = "technical.test_without_runtime"
    definition = next(item for item in catalog.indicators if item.id == "technical.ema")
    metric = next(item for item in coverage.metrics if item.id == "technical.ema")
    changed_catalog = catalog.model_copy(update={
        "indicators": (*catalog.indicators, definition.model_copy(update={"id": indicator_id})),
    })
    changed_coverage = coverage.model_copy(update={
        "metrics": (*coverage.metrics, metric.model_copy(update={"id": indicator_id})),
    })

    route = build_skill_indicator_routes(changed_catalog, changed_coverage)[indicator_id]

    assert route.source == "unavailable"
    assert route.reason == "尚无对应的确定性计算实现。"
    assert route.formula_summary == metric.formula_summary
    assert route.implementation_ref == metric.implementation_ref


def test_missing_coverage_cannot_silently_admit_a_stable_indicator(
    catalogs: tuple[CatalogSnapshot, CoverageCatalogSnapshot],
) -> None:
    catalog, coverage = catalogs
    changed_coverage = coverage.model_copy(update={
        "metrics": tuple(item for item in coverage.metrics if item.id != "technical.ema"),
    })

    with pytest.raises(ValueError, match=r"missing stable coverage: technical\.ema"):
        build_skill_indicator_routes(catalog, changed_coverage)


def test_live_skill_profile_prefers_provider_and_limits_fallback_to_existing_formulas(
    catalogs: tuple[CatalogSnapshot, CoverageCatalogSnapshot],
) -> None:
    routes = build_skill_indicator_routes(*catalogs)
    assert set(routes) == STABLE_INDICATOR_EVALUATOR_IDS
    assert {route.source for route in routes.values()} == {"provider_indicator"}
    assert len(routes) == 37
    assert routes["technical.ma"].source == "provider_indicator"
    assert routes["technical.macd"].source == "provider_indicator"
    assert {key for key, route in routes.items() if route.fallback_source} == (
        EXISTING_LOCAL_IDS | NEW_LOCAL_IDS | {"price.consecutive_up", "price.close", "technical.rsi", "technical.ma_cross"}
    )
    assert all(routes[key].fallback_source is None
               for key in PROVIDER_IDS - {"price.consecutive_up", "price.close", "technical.rsi", "technical.ma_cross"})
    assert all(routes[key].implementation_ref and routes[key].formula_summary
               for key in EXISTING_LOCAL_IDS | NEW_LOCAL_IDS)
