"""Local strategy-only composition: real models plus Eastmoney Skills."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI

from ashare_lab.adapters.language.independent_web_research import (
    DuckDuckGoHtmlResearcher,
    QueryPlanningCurrentFactResearcher,
)
from ashare_lab.adapters.language.openai_compatible import OpenAICompatibleCandidateTransport
from ashare_lab.adapters.language.vibe_backtest_review import VibeBacktestReviewAdvisor
from ashare_lab.adapters.language.vibe_candidates import build_candidate_capability_matrix
from ashare_lab.adapters.language.vibe_strategy_advice import VibeVerifiedFactStrategyAdvisor
from ashare_lab.adapters.market_data.mx_daily_history import (
    PERSISTENT_SKILL_INSTRUMENTS,
    MxDailyHistoryClient,
)
from ashare_lab.adapters.market_data.mx_saas import MxSaasMarketDataClient
from ashare_lab.adapters.market_data.provider_indicator_cache import (
    FileCachedHistoricalIndicatorData,
)
from ashare_lab.adapters.persistence.backtest_runs import SQLAlchemyBacktestRunStore
from ashare_lab.application.backtest_submission import latest_stable_a_share_data_date
from ashare_lab.application.skill_backtest_service import SkillBacktestService
from ashare_lab.domain.catalog import load_catalog_directory, load_coverage_catalog_directory
from ashare_lab.settings import AppSettings

from .app import build_hybrid_candidate_compiler, create_app
from .dialogue_progress import install_dialogue_progress


def create_skill_app(settings: AppSettings) -> FastAPI:
    # Reuse model transport composition; do not build the legacy data runtime.
    from ashare_lab.bootstrap import (
        _build_candidate_transport,  # pyright: ignore[reportPrivateUsage]
        _build_plan_deep_transport,  # pyright: ignore[reportPrivateUsage]
        _language_transport_diagnostic,  # pyright: ignore[reportPrivateUsage]
        _prepare_sqlite_parent,  # pyright: ignore[reportPrivateUsage]
    )

    if settings.is_production or settings.queue_backend != "thread":
        raise ValueError("eastmoney_skill is a local acceptance profile; not a public deployment")
    api_key = settings.resolved_mx_saas_api_key()
    if api_key is None:
        raise ValueError("Eastmoney Skill credential is required")
    root = Path(__file__).resolve().parents[2]
    catalog_root = root / settings.catalog_root
    catalog = load_catalog_directory(catalog_root)
    coverage = load_coverage_catalog_directory(catalog_root / "coverage")
    matrix = build_candidate_capability_matrix(catalog, coverage)
    # Understand the user's requested indicator even when its Skill history is
    # unavailable; only propose/optimize within the executable subset.
    matrix = matrix.model_copy(
        update={
            "events": (),
            "exit_kinds": ("indicator", "holding_period", "position_return", "trailing_drawdown"),
        }
    )
    interpretation_matrix = matrix
    matrix = matrix.model_copy(update={
        "indicators": tuple(
            item for item in matrix.indicators
            if item.indicator_id in SkillBacktestService.available_indicator_ids
        ),
    })
    # DeepSeek streaming has no total generation deadline. This budget only
    # detects an upstream connection that sends nothing for five minutes.
    candidate = _build_candidate_transport(settings, timeout_seconds=300.0)
    planner = _build_plan_deep_transport(
        settings, extract_fast_transport=candidate, timeout_seconds=300.0,
    )
    if not isinstance(candidate, OpenAICompatibleCandidateTransport) or not isinstance(
        planner, OpenAICompatibleCandidateTransport
    ):
        raise ValueError("Local acceptance requires real models; rule fallback is disabled")
    if candidate.identity.provider != "deepseek" or planner.identity.provider != "deepseek":
        raise ValueError("Local acceptance requires DeepSeek for both model profiles")
    # Search is a separate, credential-free tool. DeepSeek reads its evidence;
    # no Ark credentials or model-native browsing are needed in this profile.
    researcher = QueryPlanningCurrentFactResearcher(
        inner=DuckDuckGoHtmlResearcher(), transport=candidate
    )
    mx = MxSaasMarketDataClient(
        api_key=api_key,
        timeout_seconds=settings.mx_saas_timeout_seconds,
        max_attempts=2,
        strict_indicator_contracts=True,
    )
    compiler = build_hybrid_candidate_compiler(
        catalog,
        candidate_transport=candidate,
        idea_transport=planner,
        capability_matrix=interpretation_matrix,
        idea_capability_matrix=matrix,
        backtest_anchor_date=latest_stable_a_share_data_date(datetime.now(UTC)),
        instrument_name_resolver=mx.resolve_instrument_name,
        researcher=researcher,
        idea_direct_dsl=True,
        candidate_repair_invalid_output=True,
    )
    _prepare_sqlite_parent(settings.database_url)
    store = SQLAlchemyBacktestRunStore(
        settings.database_url, initialize_schema=settings.initialize_schema
    )
    service = SkillBacktestService(
        history=MxDailyHistoryClient(
            client=mx, cache_root=root / settings.skill_history_cache_root
        ),
        indicators=FileCachedHistoricalIndicatorData(
            mx,
            root=root / settings.provider_indicator_cache_root,
            ttl=timedelta(seconds=settings.provider_indicator_cache_ttl_seconds),
            persistent_instruments=PERSISTENT_SKILL_INSTRUMENTS,
            contract_namespace="mx-strict-fields.v1",
        ),
        store=store,
        max_workers=settings.local_worker_threads,
    )
    advisor = VibeVerifiedFactStrategyAdvisor(
        planner,
        capability_matrix=matrix,
        provider_identity=planner.identity,
    )
    review = VibeBacktestReviewAdvisor(
        planner,
        capability_matrix=matrix,
        provider_identity=planner.identity,
    )
    app = create_app(
        compiler=compiler,
        catalog=catalog,
        coverage_catalog=coverage,
        backtest_submission=service,
        run_store=store,
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
    install_dialogue_progress(app, include_model_reasoning=True)

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
            "provider": "duckduckgo_html",
            "model": "none",
        },
    }
    app.state.candidate_capability_projection = {
        "version": interpretation_matrix.schema_version,
        "hash": interpretation_matrix.content_hash,
    }
    app.router.add_event_handler("shutdown", service.shutdown)
    return app
