"""Local strategy-only composition: real models plus Eastmoney Skills."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI

from ashare_lab.adapters.language.independent_web_research import (
    BingRssResearcher,
    DuckDuckGoHtmlResearcher,
    FailoverWebResearcher,
    QueryPlanningCurrentFactResearcher,
)
from ashare_lab.adapters.language.openai_compatible import OpenAICompatibleCandidateTransport
from ashare_lab.adapters.language.skill_metric_binding_review import SkillMetricBindingReviewer
from ashare_lab.adapters.language.tencent_web_research import TencentWebSearchResearcher
from ashare_lab.adapters.language.vibe_backtest_review import VibeBacktestReviewAdvisor
from ashare_lab.adapters.language.vibe_candidates import (
    SkillMetricDiscoveryCapability,
    build_candidate_capability_matrix,
)
from ashare_lab.adapters.language.vibe_strategy_advice import VibeVerifiedFactStrategyAdvisor
from ashare_lab.adapters.market_data.mx_daily_history import (
    MxDailyHistoryClient,
)
from ashare_lab.adapters.market_data.mx_finance_history_format import MxFinanceHistoryDecoder
from ashare_lab.adapters.market_data.mx_saas import MxSaasMarketDataClient
from ashare_lab.adapters.market_data.provider_indicator_cache import (
    FileCachedHistoricalIndicatorData,
)
from ashare_lab.adapters.persistence.backtest_runs import SQLAlchemyBacktestRunStore
from ashare_lab.application.backtest_submission import SubmissionVersions, latest_stable_a_share_data_date
from ashare_lab.application.skill_backtest_service import SkillBacktestService
from ashare_lab.application.local_minute_grid import LocalMinuteGrid
from ashare_lab.adapters.market_data.eastmoney_minute_acquirer import EastmoneyMinuteAcquirer
from ashare_lab.adapters.market_data.local_price_plan_actions import PreparingPricePlanActions
from ashare_lab.adapters.market_data.minute_availability import MinuteAvailability
from ashare_lab.application.skill_indicator_routes import build_skill_indicator_routes
from ashare_lab.application.skill_numeric_catalog import extend_skill_numeric_catalogs
from ashare_lab.domain.catalog import load_catalog_directory, load_coverage_catalog_directory
from ashare_lab.settings import AppSettings

from .app import build_hybrid_candidate_compiler, create_app
from .dialogue_progress import install_dialogue_progress
from .persistent_store import SQLAlchemyDraftStore


def create_skill_app(settings: AppSettings) -> FastAPI:
    if settings.is_production or settings.queue_backend != "thread":
        raise ValueError("eastmoney_skill is a local acceptance profile; not a public deployment")
    return _compose_skill_app(settings, include_model_reasoning=False)


def _compose_skill_app(
    settings: AppSettings,
    *,
    include_model_reasoning: bool,
    max_pending: int | None = None,
) -> FastAPI:
    """Internal composition shared by the guarded local and private preview entrypoints."""
    # Reuse model transport composition; do not build the legacy data runtime.
    from ashare_lab.bootstrap import (
        _build_candidate_transport,  # pyright: ignore[reportPrivateUsage]
        _build_plan_deep_transport,  # pyright: ignore[reportPrivateUsage]
        _configure_candidate_timeout_fallback,  # pyright: ignore[reportPrivateUsage]
        _language_transport_diagnostic,  # pyright: ignore[reportPrivateUsage]
        _prepare_sqlite_parent,  # pyright: ignore[reportPrivateUsage]
    )

    api_key = settings.resolved_mx_saas_api_key()
    if api_key is None:
        raise ValueError("Eastmoney Skill credential is required")
    root = Path(__file__).resolve().parents[2]
    catalog_root = root / settings.catalog_root
    catalog, coverage = extend_skill_numeric_catalogs(
        load_catalog_directory(catalog_root),
        load_coverage_catalog_directory(catalog_root / "coverage"),
    )
    indicator_routes = build_skill_indicator_routes(catalog, coverage)
    matrix = build_candidate_capability_matrix(catalog, coverage)
    understood_events = matrix.events
    # Understand the user's requested indicator even when its Skill history is
    # unavailable; only propose/optimize within the executable subset.
    matrix = matrix.model_copy(
        update={
            "indicators": tuple(item.model_copy(update={
                "data_source": indicator_routes[item.indicator_id].source,
                "data_source_note": indicator_routes[item.indicator_id].source_note,
                "formula_summary": indicator_routes[item.indicator_id].formula_summary,
            }) for item in matrix.indicators),
            "events": (),
            "data_discovery": SkillMetricDiscoveryCapability(automatic_backtest_binding=True),
            "exit_kinds": ("indicator", "holding_period", "position_return", "trailing_drawdown"),
        }
    )
    # Parsing a user-owned event rule is not a claim that its history is ready.
    # Execution still runs the service preflight; recommendations stay restricted.
    interpretation_matrix = matrix.model_copy(update={"events": understood_events})
    matrix = matrix.model_copy(update={
        "indicators": tuple(
            item for item in matrix.indicators
            if indicator_routes[item.indicator_id].source != "unavailable"
        ),
    })
    # Keep the configured per-profile inactivity budgets. Active DeepSeek
    # streams may outlive these budgets; empty heartbeats must not hold a slot.
    candidate = _build_candidate_transport(settings)
    planner = _build_plan_deep_transport(
        settings, extract_fast_transport=candidate,
    )
    if not isinstance(candidate, OpenAICompatibleCandidateTransport) or not isinstance(
        planner, OpenAICompatibleCandidateTransport
    ):
        raise ValueError("Local acceptance requires real models; rule fallback is disabled")
    if candidate.identity.provider != "deepseek" or planner.identity.provider != "deepseek":
        raise ValueError("Local acceptance requires DeepSeek for both model profiles")
    _configure_candidate_timeout_fallback(candidate, planner)
    # Search is independent from dialogue generation. Tencent WSA is the
    # preferred channel when its dedicated credential is configured. Keep the
    # existing public sources as ordered availability fallbacks.
    public_fallback = FailoverWebResearcher(
        DuckDuckGoHtmlResearcher(report_failure=False), BingRssResearcher(),
    )
    primary_search = (
        TencentWebSearchResearcher(
            api_key=settings.research_provider_api_key,
            timeout_seconds=settings.research_provider_timeout_seconds,
        )
        if settings.research_provider_mode == "tencent_web_search"
        and settings.research_provider_api_key is not None
        else public_fallback
    )
    researcher = QueryPlanningCurrentFactResearcher(
        inner=(
            FailoverWebResearcher(primary_search, public_fallback)
            if primary_search is not public_fallback else public_fallback
        ),
        transport=candidate,
        # Keep short query rewriting fast; evidence interpretation needs the
        # existing reasoning profile to separate actors and disclosure dates.
        analysis_transport=planner,
        analysis_model=planner.identity.model,
    )
    mx = MxSaasMarketDataClient(
        api_key=api_key,
        timeout_seconds=settings.mx_saas_timeout_seconds,
        strict_indicator_contracts=True,
    )
    minute_availability = (MinuteAvailability(settings.external_minute_root, require_index=settings.is_production)
                           if settings.external_minute_root is not None else None)
    def available_data_end():
        imported = minute_availability.latest_date() if minute_availability else None
        stable = latest_stable_a_share_data_date(datetime.now(UTC))
        return min(imported, stable) if imported else stable

    # Production reads the import index, never enumerates the COS mount.
    available_data_end()

    async def generation_prices(symbol, backtest):
        from .generation_market_context import load_generation_market_context
        from ashare_lab.domain.strategy import CatalogRef
        manifest = next(item for item in catalog.manifests if item.catalog_id == "cn_a.signals")
        return await load_generation_market_context(
            symbol, backtest, service=service,
            catalog=CatalogRef(catalog_id=manifest.catalog_id, release_version=manifest.release_version),
        )

    compiler = build_hybrid_candidate_compiler(
        catalog,
        candidate_transport=candidate,
        # Try the existing fast model first; the same schema, Catalog, identity,
        # semantic and execution gates still apply. A rejected generation gets
        # the existing bounded deep repair, not a rule-based fallback.
        idea_transport=candidate,
        idea_repair_transport=planner,
        capability_matrix=interpretation_matrix,
        idea_capability_matrix=matrix,
        backtest_anchor_date=available_data_end,
        instrument_name_resolver=mx.resolve_instrument_name,
        researcher=researcher,
        idea_direct_dsl=True,
        candidate_repair_invalid_output=True,
        candidate_model_semantic_review=True,
        inspiration_market_data=mx,
        generation_market_context_loader=generation_prices,
    )
    _prepare_sqlite_parent(settings.database_url)
    store = SQLAlchemyBacktestRunStore(
        settings.database_url, initialize_schema=settings.initialize_schema
    )
    draft_store = SQLAlchemyDraftStore(
        store.engine, initialize_schema=settings.initialize_schema
    )
    runtime_versions = SubmissionVersions(
        catalog_hash=catalog.content_hash,
        code_revision=settings.code_revision,
        engine_version=settings.engine_version,
    )
    corporate_loader = PreparingPricePlanActions(
        root / (settings.price_plan_corporate_action_root or "var/cache/price-plan-corporate-actions")
    )
    service = SkillBacktestService(
        available_data_end=available_data_end,
        runtime_evidence=dict(
            catalog_hash=catalog.content_hash,
            code_revision=settings.code_revision,
            engine_version=settings.engine_version,
            opening_auction_policy=runtime_versions.opening_auction_policy,
            benchmark_policy=runtime_versions.benchmark_policy,
            corporate_action_policy=runtime_versions.corporate_action_policy,
            dividend_tax_policy=runtime_versions.dividend_tax_policy,
            rights_issue_policy=runtime_versions.rights_issue_policy,
        ),
        signal_corporate_loader=corporate_loader,
        market_calendar_loader=LocalMinuteGrid(root / settings.minute_snapshot_root,
                                              root / settings.minute_market_calendar_path).load_calendar,
        minute_grid=(LocalMinuteGrid(root / settings.minute_snapshot_root,
                                    root / settings.minute_market_calendar_path,
                                    corporate_loader,
                                    minute_acquirer=EastmoneyMinuteAcquirer(root / settings.minute_snapshot_root),
                                    external_minute_root=settings.external_minute_root)
                     if settings.minute_grid_enabled else None),
        finance=mx, finance_decoder=MxFinanceHistoryDecoder(),
        numeric_binding_reviewer=SkillMetricBindingReviewer(
            candidate, provider_identity=candidate.identity,
        ),
        history=MxDailyHistoryClient(
            client=mx, cache_root=root / settings.skill_history_cache_root,
            persistent_instruments=None,
            cache_ttl=timedelta(seconds=settings.provider_indicator_cache_ttl_seconds),
            disk_max_entries=512,
        ),
        indicators=FileCachedHistoricalIndicatorData(
            mx,
            root=root / settings.provider_indicator_cache_root,
            ttl=timedelta(seconds=settings.provider_indicator_cache_ttl_seconds),
            persistent_instruments=None,
            disk_max_entries=2048,
            contract_namespace="mx-strict-fields.v4",
        ),
        store=store,
        max_workers=settings.local_worker_threads,
        max_pending=max_pending,
        indicator_routes=indicator_routes,
    )
    advisor = VibeVerifiedFactStrategyAdvisor(
        planner,
        review_transport=candidate,
        capability_matrix=matrix,
        provider_identity=planner.identity,
        model_semantic_review=True,
    )
    review = VibeBacktestReviewAdvisor(
        planner,
        review_transport=candidate,
        capability_matrix=matrix,
        provider_identity=planner.identity,
        model_semantic_review=True,
    )
    app = create_app(
        compiler=compiler,
        catalog=catalog,
        coverage_catalog=coverage,
        backtest_submission=service,
        run_store=store,
        draft_store=draft_store,
        live_market_data=mx,
        live_finance_data=mx,
        strategy_advisor=advisor,
        backtest_review_advisor=review,
        event_backtest_probe=lambda: False,
        readiness_probe=lambda: {
            "eastmoney_skill_configured": True,
            "strategy_model_configured": True,
            "analysis_model_configured": True,
            "web_research_configured": True,
        },
        cors_allowed_origins=settings.cors_origins,
        web_dist_root=None if settings.web_dist_root is None else root / settings.web_dist_root,
        include_portfolio_review=False,
    )
    app.state.runtime = service
    install_dialogue_progress(app, include_model_reasoning=include_model_reasoning)

    app.state.candidate_provider_identity = asdict(candidate.identity)
    app.state.candidate_provider_response_mode = settings.candidate_provider_response_mode
    fast = _language_transport_diagnostic(
        candidate,
        profile="extract_fast",
        thinking=settings.candidate_provider_thinking,
        reasoning_effort=settings.candidate_provider_reasoning_effort,
    )
    deep = _language_transport_diagnostic(
        planner,
        profile="plan_deep",
        thinking=settings.plan_deep_provider_thinking,
        reasoning_effort=settings.plan_deep_provider_reasoning_effort,
    )
    app.state.language_provider_diagnostics = {
        "candidate_translation": fast,
        "clarification_reply": fast,
        "idea_generation": deep,
        "strategy_advice": deep,
        "backtest_review": deep,
        "viewpoint_web_research": {
            "configured": True,
            "provider": (
                "tencent_wsa" if isinstance(primary_search, TencentWebSearchResearcher)
                else "duckduckgo_html"
            ),
            "model": "none",
        },
    }
    app.state.candidate_capability_projection = {
        "version": interpretation_matrix.schema_version,
        "hash": interpretation_matrix.content_hash,
    }
    app.router.add_event_handler("shutdown", service.shutdown)
    for model_transport in dict.fromkeys((candidate, planner)):
        app.router.add_event_handler("startup", model_transport.startup)
        # Worker shutdown above finishes before closing the ASGI-owned pool.
        app.router.add_event_handler("shutdown", model_transport.aclose)
    return app
