from __future__ import annotations

import json
from collections import Counter
from importlib import import_module
from pathlib import Path

import pytest

from ashare_lab.domain.catalog import (
    CoverageCatalogLoadError,
    CoverageCatalogRelease,
    load_catalog_directory,
    load_coverage_catalog_directory,
)
from ashare_lab.domain.events import EXECUTABLE_EVENT_DEFINITIONS
from astock_backtest.signal_atoms import ATOM_REGISTRY
from catalogs.tools.build_coverage import expected_artifacts

ROOT = Path(__file__).resolve().parents[3]
COVERAGE_ROOT = ROOT / "catalogs/coverage"


def test_coverage_release_has_required_counts_and_unique_ids() -> None:
    snapshot = load_coverage_catalog_directory(COVERAGE_ROOT)

    assert len(snapshot.releases) == 1
    assert len(snapshot.metrics) == 182
    assert len(snapshot.events) == 160
    assert len({item.id for item in snapshot.metrics}) == 182
    assert len({item.id for item in snapshot.events}) == 160
    assert snapshot.content_hash.startswith("sha256:")


def test_metric_family_and_status_counts_are_explicit() -> None:
    snapshot = load_coverage_catalog_directory(COVERAGE_ROOT)

    assert Counter(item.family for item in snapshot.metrics) == {
        "technical": 63,
        "price_action": 39,
        "volume_liquidity": 30,
        "fundamental_valuation": 50,
    }
    assert Counter(item.status for item in snapshot.metrics) == {
        "stable": 37,
        "research_only": 74,
        "unavailable": 71,
    }


def test_only_authoritative_runtime_indicators_are_stable() -> None:
    coverage = load_coverage_catalog_directory(COVERAGE_ROOT)
    executable = load_catalog_directory(ROOT / "catalogs")
    stable_ids = {item.id for item in coverage.metrics if item.status == "stable"}

    assert stable_ids == {item.id for item in executable.indicators if item.status == "stable"}
    assert stable_ids == {
        "amount.average",
        "market.amount",
        "market.turnover_rate",
        "market.volume",
        "price.amplitude",
        "price.close",
        "price.consecutive_up",
        "price.return_pct",
        "price.rolling_high",
        "price.true_range",
        "technical.adx",
        "technical.atr",
        "technical.bbi",
        "technical.bias",
        "technical.bollinger",
        "technical.cci",
        "technical.ema",
        "technical.ema_bias",
        "technical.dmi",
        "technical.donchian",
        "technical.historical_volatility",
        "technical.kdj",
        "technical.ma",
        "technical.ma_cross",
        "technical.macd",
        "technical.momentum",
        "technical.natr",
        "technical.rsi",
        "technical.roc",
        "technical.return_stddev",
        "technical.stochastic",
        "technical.obv",
        "technical.trend_regime",
        "technical.williams_r",
        "volume.price_confirmation",
        "volume.price_divergence",
        "volume.relative",
    }
    for item in coverage.metrics:
        if item.status == "stable":
            assert item.implementation_ref
            assert item.formula_summary
            assert not item.blockers
        else:
            assert item.blockers
            assert item.implementation_ref is None


def test_stable_implementation_references_resolve_to_real_python_members() -> None:
    snapshot = load_coverage_catalog_directory(COVERAGE_ROOT)

    for item in snapshot.metrics:
        if item.status != "stable":
            continue
        assert item.implementation_ref is not None
        module_name, member_ref = item.implementation_ref.split(":", maxsplit=1)
        member_name = member_ref.split("[", maxsplit=1)[0]
        assert hasattr(import_module(module_name), member_name), item.implementation_ref


def test_only_in_scope_legacy_atoms_are_mapped_without_claiming_old_code_is_stable() -> None:
    snapshot = load_coverage_catalog_directory(COVERAGE_ROOT)
    mapped = [legacy_id for item in snapshot.metrics for legacy_id in item.legacy_atom_ids]

    assert len(ATOM_REGISTRY) == 85
    assert len(mapped) == 85
    assert set(mapped) == set(ATOM_REGISTRY)
    assert len(mapped) == len(set(mapped))


def test_opening_gap_keeps_legacy_semantics_without_claiming_stable_runtime() -> None:
    snapshot = load_coverage_catalog_directory(COVERAGE_ROOT)
    opening_gap = next(item for item in snapshot.metrics if item.id == "price.opening_gap")

    assert opening_gap.status == "research_only"
    assert opening_gap.parameters == ()
    assert opening_gap.triggers == ("gap_up", "gap_down")
    assert opening_gap.warmup_bars == 2
    assert opening_gap.legacy_atom_ids == ("gap_down", "gap_up")
    assert opening_gap.implementation_ref is None


def test_event_taxonomy_has_sixteen_families_and_explicit_statuses() -> None:
    snapshot = load_coverage_catalog_directory(COVERAGE_ROOT)
    category_counts = Counter(item.category for item in snapshot.events)

    assert len(category_counts) == 16
    assert set(category_counts.values()) == {10}
    assert Counter(item.status for item in snapshot.events) == {
        "stable": 69,
        "research_only": 29,
        "unavailable": 62,
    }
    assert {item.id for item in snapshot.events if item.status == "stable"} == set(
        EXECUTABLE_EVENT_DEFINITIONS
    )
    required_codes = {
        "event.financial_results.annual_report",
        "event.financial_results.earnings_forecast_published",
        "event.dividends_corporate_actions.cash_dividend_ex_date",
        "event.repurchase_capital.repurchase_proposal",
        "event.shareholder_holdings.major_holder_decrease_plan",
        "event.restricted_shares_pledges.restricted_shares_unlock",
        "event.trading_status.suspension",
        "event.regulation_risk.investigation_opened",
        "event.market_activity.dragon_tiger_buy",
        "event.financing_securities.margin_balance_jump",
        "event.index_passive.index_rebalance_effective",
        "event.contracts_orders.major_contract_won",
        "event.capacity_operations.capacity_mass_production",
    }
    assert required_codes <= {item.id for item in snapshot.events}


def test_executable_web_events_require_original_documents_and_external_facts() -> None:
    snapshot = load_coverage_catalog_directory(COVERAGE_ROOT)
    event_codes = {
        "event.contracts_orders.major_contract_won",
        "event.macro_policy_industry.license_approval",
    }

    for event_code in event_codes:
        event = snapshot.resolve_event(event_code)
        assert event is not None
        assert event.status == "stable"
        assert {item.dataset_id for item in event.data_requirements} == {
            "external.events.point_in_time",
            "web.documents.point_in_time",
        }
        external = next(
            item
            for item in event.data_requirements
            if item.dataset_id == "external.events.point_in_time"
        )
        assert {"external_fact_id", "entity_match_method"} <= set(external.fields)
        assert "external_fact_id" in event.dedupe_keys
        assert "搜索摘要" in event.description


def test_every_event_uses_point_in_time_availability_and_conservative_daily_fill() -> None:
    snapshot = load_coverage_catalog_directory(COVERAGE_ROOT)

    for event in snapshot.events:
        assert event.time_semantics.observation_basis == "available_at"
        assert event.time_semantics.date_only_policy == (
            "reject" if event.status == "stable" else "conservative_after_close"
        )
        assert event.time_semantics.default_order_time == "next_tradable_session_open"
        assert event.time_semantics.daily_bar_fallback == "next_tradable_session_open"
        assert "announced" in event.lifecycle_states
        assert "amended" in event.lifecycle_states
        assert "cancelled" in event.lifecycle_states
        assert "source_event_id" in event.dedupe_keys
        requirement = event.data_requirements[0]
        assert requirement.point_in_time_required
        assert set(requirement.required_time_fields) == {
            "fact_at",
            "available_at",
            "ingested_at",
            "revision_id",
        }
        if event.status == "stable":
            assert event.implementation_ref
            assert not event.blockers
        else:
            assert event.implementation_ref is None
            assert event.blockers


def test_checked_in_catalog_and_schema_match_deterministic_builder() -> None:
    for path, expected in expected_artifacts().items():
        assert path.read_text(encoding="utf-8") == expected

    schema = json.loads((ROOT / "contracts/catalog-coverage.v1.schema.json").read_text())
    assert schema["$id"].endswith("/catalog-coverage.v1.schema.json")
    assert schema["properties"]["schema_version"]["const"] == "catalog-coverage.v1"


def test_loader_rejects_multiple_active_releases(tmp_path: Path) -> None:
    original = CoverageCatalogRelease.model_validate_json(
        (COVERAGE_ROOT / "cn_a.v1.catalog.json").read_text(encoding="utf-8")
    )
    first = original.model_dump(mode="json")
    second = original.model_copy(update={"release_version": "2026.08.31"}).model_dump(mode="json")
    (tmp_path / "first.catalog.json").write_text(json.dumps(first), encoding="utf-8")
    (tmp_path / "second.catalog.json").write_text(json.dumps(second), encoding="utf-8")

    with pytest.raises(CoverageCatalogLoadError, match="multiple active coverage releases"):
        load_coverage_catalog_directory(tmp_path)


def test_loader_rejects_duplicate_definition_ids(tmp_path: Path) -> None:
    payload = json.loads((COVERAGE_ROOT / "cn_a.v1.catalog.json").read_text(encoding="utf-8"))
    payload["metrics"].append(payload["metrics"][0])
    (tmp_path / "bad.catalog.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CoverageCatalogLoadError, match="duplicate metric ids"):
        load_coverage_catalog_directory(tmp_path)
