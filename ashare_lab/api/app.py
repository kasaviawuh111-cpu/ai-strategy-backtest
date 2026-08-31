"""FastAPI application factory for the versioned HTTP edge."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ashare_lab.adapters.language import RuleBasedCandidateGenerator
from ashare_lab.adapters.language.vibe_candidates import (
    CandidateCapabilityMatrix,
    HybridCandidateGenerator,
    IdentifiedCandidateJsonTransport,
    VibeBoundedCandidateGenerator,
)
from ashare_lab.application.compile_strategy import StrategyCompiler
from ashare_lab.domain.catalog import (
    CatalogSnapshot,
    CoverageCatalogSnapshot,
    load_catalog_directory,
    load_coverage_catalog_directory,
)
from ashare_lab.domain.signals.runtime import validate_stable_indicator_evaluator_catalog
from ashare_lab.ports.backtest_runs import BacktestRunStore

from .container import ApiContainer, BacktestSubmitter
from .errors import install_exception_handlers
from .middleware import RequestContextMiddleware
from .routes.backtest_runs import router as backtest_runs_router
from .routes.strategy_drafts import router as strategy_drafts_router
from .routes.system import router as system_router
from .store import InMemoryDraftStore

DEFAULT_MAX_BODY_BYTES = 16 * 1024


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
    draft_store: InMemoryDraftStore | None = None,
    catalog_root: str | Path | None = None,
    service_version: str | None = None,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    backtest_submission: BacktestSubmitter | None = None,
    run_store: BacktestRunStore | None = None,
    readiness_probe: Callable[[], Mapping[str, bool]] | None = None,
    event_backtest_probe: Callable[[], bool] | None = None,
    event_backtest_codes_probe: Callable[[], frozenset[str]] | None = None,
    event_preparable_codes_probe: Callable[[], frozenset[str]] | None = None,
    event_document_text_backtest_codes_probe: Callable[[], frozenset[str]] | None = None,
    event_document_text_preparable_codes_probe: Callable[[], frozenset[str]] | None = None,
    cors_allowed_origins: tuple[str, ...] = (),
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
    validate_stable_indicator_evaluator_catalog(selected_catalog)
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
        readiness_probe=readiness_probe,
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
    app.add_middleware(RequestContextMiddleware, max_body_bytes=max_body_bytes)
    if cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cors_allowed_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type", "Idempotency-Key", "X-Request-ID"],
            expose_headers=["Idempotency-Replayed", "X-Request-ID"],
            max_age=600,
        )
    app.include_router(system_router)
    app.include_router(strategy_drafts_router)
    app.include_router(backtest_runs_router)
    return app


def _default_compiler(catalog: CatalogSnapshot) -> StrategyCompiler:
    return _compiler_for_generator(catalog, RuleBasedCandidateGenerator())


def build_hybrid_candidate_compiler(
    catalog: CatalogSnapshot,
    *,
    candidate_transport: IdentifiedCandidateJsonTransport,
    capability_matrix: CandidateCapabilityMatrix,
    backtest_anchor_date: date | None = None,
) -> StrategyCompiler:
    """Compose rule-first interpretation with one Catalog-bounded fallback."""

    return _compiler_for_generator(
        catalog,
        HybridCandidateGenerator(
            deterministic=RuleBasedCandidateGenerator(),
            bounded_fallback=VibeBoundedCandidateGenerator(
                candidate_transport,
                capability_matrix=capability_matrix,
                provider_identity=candidate_transport.identity,
            ),
        ),
        backtest_anchor_date=backtest_anchor_date,
    )


def _compiler_for_generator(
    catalog: CatalogSnapshot,
    generator: RuleBasedCandidateGenerator | HybridCandidateGenerator,
    *,
    backtest_anchor_date: date | None = None,
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
    )


def _default_catalog_root() -> Path:
    return Path(__file__).resolve().parents[2] / "catalogs"


def _installed_version() -> str:
    try:
        return version("astock-backtest-engine")
    except PackageNotFoundError:
        return "2.0.0a0"
