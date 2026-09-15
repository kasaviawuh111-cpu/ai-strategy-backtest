"""Composition evidence only: no model or financial-data request is submitted."""

# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from collections import Counter
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from ashare_lab.adapters.language.openai_compatible import OpenAICompatibleCandidateTransport
from ashare_lab.adapters.market_data.mx_saas import MxSaasMarketDataClient
from ashare_lab.api.skill_app import create_skill_app
from ashare_lab.application.skill_indicator_routes import (
    build_skill_indicator_routes,
    default_skill_indicator_routes,
)
from ashare_lab.application.skill_numeric_catalog import extend_skill_numeric_catalogs
from ashare_lab.domain.catalog import (
    CatalogSnapshot,
    CoverageCatalogSnapshot,
    load_catalog_directory,
    load_coverage_catalog_directory,
)
from ashare_lab.domain.signals.runtime import STABLE_INDICATOR_EVALUATOR_IDS
from ashare_lab.domain.signals.skill_numeric import (
    SKILL_NUMERIC_ID,
    SKILL_NUMERIC_TRIGGERS,
    SKILL_SERIES_COMPARE_ID,
)
from ashare_lab.domain.strategy.canonical import canonical_hash
from ashare_lab.settings import AppSettings


@pytest.mark.parametrize("fast_model,deep_model", [
    ("test-model", "test-deep-model"), ("deepseek-v4-flash", "deepseek-v4-pro"),
])
def test_skill_composition_gives_all_model_entries_the_same_connected_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fast_model: str, deep_model: str,
) -> None:
    unavailable = AsyncMock(side_effect=AssertionError("wiring test must not request a provider"))
    monkeypatch.setattr(OpenAICompatibleCandidateTransport, "generate_json", unavailable)
    monkeypatch.setattr(MxSaasMarketDataClient, "query_finance", unavailable)
    monkeypatch.setattr(MxSaasMarketDataClient, "screen", unavailable)
    settings = AppSettings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        app_env="test", market_data_profile="eastmoney_skill",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'ashare.db'}",
        initialize_schema=True, queue_backend="thread", local_worker_threads=1,
        candidate_provider_mode="openai_compatible", candidate_provider_name="deepseek",
        candidate_provider_endpoint="https://model.example.test/v1",
        candidate_provider_model=fast_model, candidate_provider_api_key="test-not-real",
        plan_deep_provider_mode="openai_compatible", plan_deep_provider_name="deepseek",
        plan_deep_provider_endpoint="https://model.example.test/v1",
        plan_deep_provider_model=deep_model, plan_deep_provider_api_key="test-not-real",
        mx_saas_api_key="test-not-real",
        skill_history_cache_root=tmp_path / "history",
        provider_indicator_cache_root=tmp_path / "indicators",
        code_revision="test-skill-route-composition", web_dist_root=None,
    )
    app = create_skill_app(settings)
    with TestClient(app) as client:
        container = app.state.container
        compiler = container.compiler
        assert (
            compiler._idea_router._transport._timeout_seconds
            == settings.candidate_provider_timeout_seconds
        )
        assert (
            compiler._idea_router._repair_transport._timeout_seconds
            == settings.plan_deep_provider_timeout_seconds
        )
        assert compiler._idea_router._transport.identity.model == fast_model
        assert compiler._idea_router._repair_transport.identity.model == deep_model
        fallback = compiler._idea_router._transport.timeout_fallback_identity
        assert (fallback.model if fallback is not None else None) == (
            deep_model if fast_model == "deepseek-v4-flash" else None
        )
        assert compiler._idea_router._model_semantic_review is True
        matrices = {
            "candidate": compiler._generator._bounded_fallback._capability_matrix,
            "clarification": compiler._clarification_dialogue_router._capability_matrix,
            "ideas": compiler._idea_router._capability_matrix,
            "editing": compiler._strategy_editor._matrix,
            "advice": container.strategy_advisor._capability_matrix,
            "review": container.backtest_review_advisor._capability_matrix,
        }
        routes = container.backtest_submission.indicator_routes
        public = client.get("/api/v1/capabilities").json()
        public_indicators = {item["indicator_id"]: item for item in public["indicators"]}
        assert public["skill_data_discovery"]["available"] is True
        assert public["skill_data_discovery"]["requires_catalog_indicator"] is False
        assert public["skill_data_discovery"]["automatic_backtest_binding"] is True
        assert len(routes) == 39
        assert Counter(route.source for route in routes.values()) == {
            "provider_indicator": 37, "skill_numeric_history": 2,
        }
        interpretation_entries = {"candidate", "clarification", "editing"}
        assert len({matrices[name].content_hash for name in interpretation_entries}) == 1
        assert matrices["candidate"].content_hash != matrices["ideas"].content_hash
        for entry, matrix in matrices.items():
            indicators = {item.indicator_id: item for item in matrix.indicators}
            assert set(indicators) == set(routes), entry
            if entry in interpretation_entries:
                assert matrix.events, entry
            else:
                assert matrix.events == (), entry
            assert matrix.data_discovery is not None, entry
            assert matrix.data_discovery.requires_catalog_indicator is False, entry
            assert matrix.data_discovery.automatic_backtest_binding is True, entry
            assert matrix.data_discovery.endpoint == public["skill_data_discovery"]["endpoint"]
            for indicator_id, route in routes.items():
                item = indicators[indicator_id]
                assert item.data_source == route.source, entry
                assert item.data_source_note == route.source_note, entry
                assert item.formula_summary == route.formula_summary, entry
                assert public_indicators[indicator_id]["data_source"] == item.data_source
                assert public_indicators[indicator_id]["formula_summary"] == item.formula_summary
            assert indicators["technical.ema"].data_source == "provider_indicator", entry
            assert indicators["technical.atr"].data_source == "provider_indicator", entry
            assert indicators["technical.ma"].data_source == "provider_indicator", entry
            assert indicators["technical.rsi"].data_source == "provider_indicator", entry
            assert indicators["price.close"].data_source == "provider_indicator", entry
            generic = indicators[SKILL_NUMERIC_ID]
            assert generic.data_source == "skill_numeric_history", entry
            assert generic.definition_version == "1.0.0", entry
            assert {item.id for item in generic.triggers} == set(SKILL_NUMERIC_TRIGGERS), entry
            assert all(item.value_requirement == "required" for item in generic.triggers), entry
            assert {item.name for item in generic.parameters} == {"metric_query", "unit"}, entry
            assert all(
                item.value_type == "string" and item.required and item.default is None
                and item.choices == () for item in generic.parameters
            ), entry
            assert "按日线收盘条件进行研究回放" in generic.data_source_note, entry
            assert "未验证" not in generic.data_source_note, entry
            assert "PIT" not in generic.data_source_note, entry
            comparison = indicators[SKILL_SERIES_COMPARE_ID]
            assert comparison.data_source == "skill_numeric_history", entry
            assert {item.name for item in comparison.parameters} == {
                "left_metric_query", "right_metric_query", "unit",
            }, entry
            assert {item.id for item in comparison.triggers} == set(SKILL_NUMERIC_TRIGGERS), entry
            assert all(item.value_requirement == "forbidden" for item in comparison.triggers), entry
        assert container.backtest_submission.numeric_series_enabled is True
        assert container.backtest_submission.numeric_binding_reviewer is not None
        assert container.backtest_submission.numeric_discovery._provider is (
            container.live_finance_data
        )
        projection_hash = app.state.candidate_capability_projection["hash"]
        assert projection_hash == matrices["candidate"].content_hash
    unavailable.assert_not_awaited()


def test_skill_numeric_overlay_is_isolated_and_hashes_its_actual_definitions() -> None:
    root = Path(__file__).resolve().parents[3] / "catalogs"
    base_catalog = load_catalog_directory(root)
    base_coverage = load_coverage_catalog_directory(root / "coverage")
    catalog_before = base_catalog.model_dump_json()
    coverage_before = base_coverage.model_dump_json()

    catalog, coverage = extend_skill_numeric_catalogs(base_catalog, base_coverage)

    assert base_catalog.model_dump_json() == catalog_before
    assert base_coverage.model_dump_json() == coverage_before
    assert len(base_catalog.indicators) == len(STABLE_INDICATOR_EVALUATOR_IDS) == 37
    assert base_catalog.resolve_indicator(SKILL_NUMERIC_ID) is None
    assert base_coverage.resolve_metric(SKILL_NUMERIC_ID) is None
    assert SKILL_NUMERIC_ID not in STABLE_INDICATOR_EVALUATOR_IDS
    assert SKILL_NUMERIC_ID not in default_skill_indicator_routes()
    assert len(catalog.indicators) == 39
    assert base_catalog.resolve_indicator(SKILL_SERIES_COMPARE_ID) is None
    assert base_coverage.resolve_metric(SKILL_SERIES_COMPARE_ID) is None
    assert SKILL_SERIES_COMPARE_ID not in STABLE_INDICATOR_EVALUATOR_IDS
    assert SKILL_SERIES_COMPARE_ID not in default_skill_indicator_routes()
    assert catalog.content_hash != base_catalog.content_hash
    assert coverage.content_hash != base_coverage.content_hash
    assert catalog.content_hash == canonical_hash({
        "manifests": [item.model_dump(mode="json") for item in catalog.manifests],
    })
    assert coverage.content_hash == canonical_hash({
        "releases": [item.model_dump(mode="json") for item in coverage.releases],
    })
    assert CatalogSnapshot.model_validate_json(catalog.model_dump_json()) == catalog
    assert CoverageCatalogSnapshot.model_validate_json(coverage.model_dump_json()) == coverage
    assert coverage.events == base_coverage.events
    assert len({item.id for release in coverage.releases for item in release.events}) == len(
        coverage.events
    )
    metric = coverage.resolve_metric(SKILL_NUMERIC_ID)
    assert metric is not None
    requirement = metric.data_requirements[0]
    assert requirement.dataset_id == "market.skill.numeric"
    assert requirement.frequency == "1d"
    assert requirement.point_in_time_required is False
    assert "available_at" not in requirement.required_time_fields
    assert metric.time_semantics.date_only_policy == "conservative_after_close"
    assert "不视为已通过 PIT 验证" in metric.time_semantics.notes


def test_skill_numeric_route_does_not_claim_an_ohlcv_or_unconnected_data_path() -> None:
    root = Path(__file__).resolve().parents[3] / "catalogs"
    catalog, coverage = extend_skill_numeric_catalogs(
        load_catalog_directory(root), load_coverage_catalog_directory(root / "coverage"),
    )
    metric = coverage.resolve_metric(SKILL_NUMERIC_ID)
    assert metric is not None
    changed = metric.model_copy(update={"data_requirements": (
        metric.data_requirements[0].model_copy(update={"frequency": "1m"}),
    )})
    changed_coverage = coverage.model_copy(update={"metrics": tuple(
        changed if item.id == SKILL_NUMERIC_ID else item for item in coverage.metrics
    )})

    assert build_skill_indicator_routes(catalog, coverage)[SKILL_NUMERIC_ID].source == (
        "skill_numeric_history"
    )
    assert build_skill_indicator_routes(catalog, changed_coverage)[SKILL_NUMERIC_ID].source == (
        "unavailable"
    )
