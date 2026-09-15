"""FastAPI application factory for the versioned HTTP edge."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ashare_lab.adapters.language import RuleBasedCandidateGenerator
from ashare_lab.adapters.language.inspiration_stock_selection import InspirationStockSelector
from ashare_lab.adapters.language.openai_compatible import OpenAICompatibleCandidateTransport
from ashare_lab.adapters.language.vibe_candidates import (
    CandidateCapabilityMatrix,
    HybridCandidateGenerator,
    IdentifiedCandidateJsonTransport,
    VibeBoundedCandidateGenerator,
)
from ashare_lab.adapters.language.vibe_clarification import VibeClarificationDialogueRouter
from ashare_lab.adapters.language.vibe_ideas import VibeIdeaRouter
from ashare_lab.adapters.language.vibe_strategy_advice import VibeVerifiedFactStrategyAdvisor
from ashare_lab.adapters.language.vibe_strategy_editing import VibeStrategyEditor
from ashare_lab.application.compile_strategy import (
    DEFAULT_INITIAL_CASH_CNY,
    StrategyCompiler,
)
from ashare_lab.application.strategy_v2_http import StrategyV2HttpService
from ashare_lab.domain.catalog import (
    CatalogSnapshot,
    CoverageCatalogSnapshot,
    load_catalog_directory,
    load_coverage_catalog_directory,
)
from ashare_lab.domain.signals.runtime import validate_stable_indicator_evaluator_catalog
from ashare_lab.domain.signals.skill_numeric import SKILL_COMPARISON_IDS
from ashare_lab.domain.strategy import CatalogRef
from ashare_lab.ports.backtest_review import BacktestReviewAdvisor
from ashare_lab.ports.backtest_runs import BacktestRunStore
from ashare_lab.ports.current_fact_research import CurrentFactResearcher
from ashare_lab.ports.live_market_data import LiveFinanceData, LiveMarketData
from ashare_lab.ports.strategy_advice import VerifiedFactStrategyAdvisor
from ashare_lab.adapters.language.generation_preflight import GenerationMarketContextLoader

from .container import ApiContainer, BacktestSubmitter
from .errors import install_exception_handlers
from .middleware import RequestContextMiddleware
from .routes.backtest_runs import router as backtest_runs_router
from .routes.grid_backtests import router as grid_backtests_router
from .routes.instruments import router as instruments_router
from .routes.market_data import router as market_data_router
from .routes.portfolio_reviews import router as portfolio_reviews_router
from .routes.strategy_drafts import router as strategy_drafts_router
from .routes.strategy_v2 import router as strategy_v2_router
from .routes.system import router as system_router
from .store import DraftStore, InMemoryDraftStore
from .web_hosting import install_web_hosting

DEFAULT_MAX_BODY_BYTES = 16 * 1024
PORTFOLIO_REVIEW_BODY_LIMITS = {
    "/api/v1/portfolio-reviews/imports/parse": 2 * 1024 * 1024,
    "/api/v1/portfolio-reviews/analyze": 2 * 1024 * 1024,
    "/api/v1/portfolio-reviews/narrate-highlight": 1024 * 1024,
}


def _event_backtest_available_by_default() -> bool:
    return True


def _event_backtest_codes_unavailable_by_default() -> frozenset[str]:
    # A boolean probe cannot prove per-code acquisition coverage.  Test and
    # production composition roots must inject a snapshot-backed code probe.
    return frozenset()


def create_app(
    *,
    compiler: StrategyCompiler | None = None,
    catalog: CatalogSnapshot | None = None,
    coverage_catalog: CoverageCatalogSnapshot | None = None,
    draft_store: DraftStore | None = None,
    catalog_root: str | Path | None = None,
    service_version: str | None = None,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    backtest_submission: BacktestSubmitter | None = None,
    run_store: BacktestRunStore | None = None,
    strategy_v2_service: StrategyV2HttpService | None = None,
    readiness_probe: Callable[[], Mapping[str, bool]] | None = None,
    readiness_reasons_probe: Callable[[], Mapping[str, str]] | None = None,
    live_market_data: LiveMarketData | None = None,
    live_finance_data: LiveFinanceData | None = None,
    strategy_advisor: VerifiedFactStrategyAdvisor | None = None,
    backtest_review_advisor: BacktestReviewAdvisor | None = None,
    include_portfolio_review: bool = True,
    event_backtest_probe: Callable[[], bool] | None = None,
    event_backtest_codes_probe: Callable[[], frozenset[str]] | None = None,
    event_preparable_codes_probe: Callable[[], frozenset[str]] | None = None,
    event_document_text_backtest_codes_probe: Callable[[], frozenset[str]] | None = None,
    event_document_text_preparable_codes_probe: Callable[[], frozenset[str]] | None = None,
    cors_allowed_origins: tuple[str, ...] = (),
    web_dist_root: str | Path | None = None,
) -> FastAPI:
    """Build an isolated app instance suitable for tests and multiple workers."""

    if max_body_bytes <= 0:
        raise ValueError("max_body_bytes must be positive")
    if (backtest_submission is None) is not (run_store is None):
        raise ValueError("backtest_submission and run_store must be configured together")
    selected_catalog_root = (
        Path(catalog_root) if catalog_root is not None else _default_catalog_root()
    )
    selected_catalog = catalog or load_catalog_directory(selected_catalog_root)
    # This provider-backed operator has its own numeric evaluator, not an
    # invented OHLCV formula. Only the actually composed service can enable it.
    formula_catalog = selected_catalog
    if getattr(backtest_submission, "numeric_series_enabled", False):
        formula_catalog = selected_catalog.model_copy(update={
            "indicators": tuple(
                item for item in selected_catalog.indicators if item.id not in SKILL_COMPARISON_IDS
            ),
        })
    validate_stable_indicator_evaluator_catalog(formula_catalog)
    selected_coverage_catalog = coverage_catalog or load_coverage_catalog_directory(
        selected_catalog_root / "coverage"
    )
    selected_compiler = compiler or _default_compiler(selected_catalog)
    container = ApiContainer(
        compiler=selected_compiler,
        catalog=selected_catalog,
        coverage_catalog=selected_coverage_catalog,
        drafts=draft_store or InMemoryDraftStore(),
        service_version=service_version or _installed_version(),
        max_body_bytes=max_body_bytes,
        event_backtest_probe=event_backtest_probe or _event_backtest_available_by_default,
        event_backtest_codes_probe=(
            event_backtest_codes_probe or _event_backtest_codes_unavailable_by_default
        ),
        event_preparable_codes_probe=(
            event_preparable_codes_probe or _event_backtest_codes_unavailable_by_default
        ),
        event_document_text_backtest_codes_probe=(
            event_document_text_backtest_codes_probe or _event_backtest_codes_unavailable_by_default
        ),
        event_document_text_preparable_codes_probe=(
            event_document_text_preparable_codes_probe
            or _event_backtest_codes_unavailable_by_default
        ),
        backtest_submission=backtest_submission,
        run_store=run_store,
        strategy_v2_service=strategy_v2_service,
        readiness_probe=readiness_probe,
        readiness_reasons_probe=readiness_reasons_probe,
        live_market_data=live_market_data,
        live_finance_data=live_finance_data,
        strategy_advisor=strategy_advisor,
        backtest_review_advisor=backtest_review_advisor,
    )

    app = FastAPI(
        title="A-share Strategy Compiler API",
        version=container.service_version,
        openapi_url="/api/v1/openapi.json",
        docs_url="/api/v1/docs",
        redoc_url=None,
    )
    app.state.container = container
    install_exception_handlers(app)
    app.add_middleware(
        RequestContextMiddleware,
        max_body_bytes=max_body_bytes,
        path_max_body_bytes=PORTFOLIO_REVIEW_BODY_LIMITS,
    )
    if cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cors_allowed_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=[
                "Accept",
                "Content-Type",
                "Idempotency-Key",
                "X-Conversation-Parent-Draft-ID",
                "X-Dialogue-Progress-ID",
                "X-Request-ID",
            ],
            expose_headers=["Idempotency-Replayed", "X-Request-ID"],
            max_age=600,
        )
    app.include_router(system_router)
    app.include_router(strategy_drafts_router)
    app.include_router(backtest_runs_router)
    app.include_router(grid_backtests_router)
    app.include_router(strategy_v2_router)
    app.include_router(market_data_router)
    app.include_router(instruments_router)
    if include_portfolio_review:
        app.include_router(portfolio_reviews_router)
    install_web_hosting(app, web_dist_root)
    return app


def _default_compiler(catalog: CatalogSnapshot) -> StrategyCompiler:
    return _compiler_for_generator(catalog, RuleBasedCandidateGenerator())


def build_hybrid_candidate_compiler(
    catalog: CatalogSnapshot,
    *,
    candidate_transport: IdentifiedCandidateJsonTransport,
    idea_transport: IdentifiedCandidateJsonTransport | None = None,
    idea_repair_transport: IdentifiedCandidateJsonTransport | None = None,
    capability_matrix: CandidateCapabilityMatrix,
    idea_capability_matrix: CandidateCapabilityMatrix | None = None,
    backtest_anchor_date: date | Callable[[], date | None] | None = None,
    instrument_name_resolver: Callable[[str], str] | None = None,
    researcher: CurrentFactResearcher | None = None,
    idea_direct_dsl: bool = False,
    candidate_repair_invalid_output: bool = False,
    candidate_model_semantic_review: bool = False,
    inspiration_market_data: LiveMarketData | None = None,
    generation_market_context_loader: GenerationMarketContextLoader | None = None,
) -> StrategyCompiler:
    """Compose provider-first interpretation with deterministic offline compatibility."""

    selected_idea_transport = idea_transport or candidate_transport
    # A configured planner must not send review/repair to a disabled extractor.
    selected_review_transport = (
        candidate_transport if candidate_transport.identity.provider != "disabled"
        else selected_idea_transport
    )
    selected_idea_repair_transport = idea_repair_transport or selected_review_transport
    manifest = next(
        (item for item in catalog.manifests if item.catalog_id == "cn_a.signals"),
        catalog.manifests[0],
    )
    return _compiler_for_generator(
        catalog,
        HybridCandidateGenerator(
            deterministic=RuleBasedCandidateGenerator(),
            bounded_fallback=VibeBoundedCandidateGenerator(
                candidate_transport,
                capability_matrix=capability_matrix,
                provider_identity=candidate_transport.identity,
                repair_invalid_output=candidate_repair_invalid_output,
                model_semantic_review=candidate_model_semantic_review,
                instrument_name_resolver=instrument_name_resolver,
            ),
            instrument_name_resolver=instrument_name_resolver,
            model_first=candidate_transport.identity.provider != "disabled",
        ),
        backtest_anchor_date=backtest_anchor_date,
        idea_router=VibeIdeaRouter(
            selected_idea_transport,
            market_context_loader=generation_market_context_loader,
            review_transport=selected_review_transport,
            model_semantic_review=(
                candidate_model_semantic_review
                or isinstance(selected_idea_transport, OpenAICompatibleCandidateTransport)
            ),
            capability_matrix=idea_capability_matrix or capability_matrix,
            provider_identity=selected_idea_transport.identity,
            repair_transport=(
                selected_idea_repair_transport if candidate_repair_invalid_output else None
            ),
            repair_provider_identity=(
                selected_idea_repair_transport.identity if candidate_repair_invalid_output else None
            ),
            researcher=researcher,
            stock_selector=(InspirationStockSelector(
                transport=selected_review_transport,
                matrix=idea_capability_matrix or capability_matrix,
                provider=inspiration_market_data,
                advisor=VibeVerifiedFactStrategyAdvisor(
                    selected_review_transport,
                    capability_matrix=idea_capability_matrix or capability_matrix,
                    provider_identity=selected_review_transport.identity,
                    model_semantic_review=candidate_model_semantic_review,
                ),
            ) if inspiration_market_data is not None else None),
            strategy_catalog=(
                CatalogRef(
                    catalog_id=manifest.catalog_id,
                    release_version=manifest.release_version,
                )
                if idea_direct_dsl
                else None
            ),
            default_lookback_years=1,
            default_initial_cash_cny=DEFAULT_INITIAL_CASH_CNY,
        ),
        clarification_dialogue_router=VibeClarificationDialogueRouter(
            candidate_transport,
            capability_matrix=capability_matrix,
            model_semantic_review=True,
        ) if candidate_transport.identity.provider != "disabled" else None,
        instrument_name_resolver=instrument_name_resolver,
        current_fact_researcher=researcher,
        strategy_editor=VibeStrategyEditor(
            candidate_transport,
            resolve_pending_intent=True,
            catalog=catalog,
            capability_matrix=capability_matrix,
            provider_identity=candidate_transport.identity,
            model_semantic_review=True,
        ) if candidate_transport.identity.provider != "disabled" else None,
    )


def _compiler_for_generator(
    catalog: CatalogSnapshot,
    generator: RuleBasedCandidateGenerator | HybridCandidateGenerator,
    *,
    backtest_anchor_date: date | Callable[[], date | None] | None = None,
    idea_router: VibeIdeaRouter | None = None,
    clarification_dialogue_router: VibeClarificationDialogueRouter | None = None,
    instrument_name_resolver: Callable[[str], str] | None = None,
    strategy_editor: VibeStrategyEditor | None = None,
    current_fact_researcher: CurrentFactResearcher | None = None,
) -> StrategyCompiler:
    manifest = next(
        (item for item in catalog.manifests if item.catalog_id == "cn_a.signals"),
        catalog.manifests[0],
    )
    return StrategyCompiler(
        generator=generator,
        catalog=catalog,
        catalog_id=manifest.catalog_id,
        release_version=manifest.release_version,
        backtest_anchor_date=backtest_anchor_date,
        idea_router=idea_router,
        clarification_dialogue_router=clarification_dialogue_router,
        instrument_name_resolver=instrument_name_resolver,
        strategy_editor=strategy_editor,
        current_fact_researcher=current_fact_researcher,
    )


def _default_catalog_root() -> Path:
    return Path(__file__).resolve().parents[2] / "catalogs"


def _installed_version() -> str:
    try:
        return version("astock-backtest-engine")
    except PackageNotFoundError:
        return "2.0.0a0"
