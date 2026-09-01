"""Composition roots: the only module that wires concrete infrastructure."""

from __future__ import annotations

import importlib
import importlib.util
import json
import re
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast

from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.engine import make_url

from ashare_lab.adapters.event_sources.eastmoney import eastmoney_preparable_event_codes
from ashare_lab.adapters.jobs import RQBacktestJobQueue, ThreadBacktestJobQueue
from ashare_lab.adapters.language.openai_compatible import (
    DisabledCandidateJsonTransport,
    OpenAICompatibleCandidateTransport,
)
from ashare_lab.adapters.language.vibe_candidates import (
    CandidateCapabilityMatrix,
    CandidateProviderIdentityView,
    VibeBoundedCandidateGenerator,
    build_candidate_capability_matrix,
)
from ashare_lab.adapters.market_data import (
    BaoStockReferenceAdapter,
    BaoStockSecurityCandidateSearch,
    BaoStockSecuritySearchClient,
    BaoStockSecuritySearchUnavailableError,
    ChoiceBaoStockEastmoneyCandidateSearch,
    ChoiceSecurityCandidateSearch,
    ChoiceSecuritySearchClient,
    ChoiceSecuritySearchUnavailableError,
    EastmoneySecurityCandidateSearch,
    InternalDemoSnapshotPreparer,
    LocalParquetMarketDataRepository,
    MarketDataAdapterError,
    OnDemandSnapshotMarketDataRepository,
    ParquetInstrumentSessionProvider,
    ResearchFallbackSessionProvider,
    SecurityMasterInstrumentNormalizer,
    ServerOwnedTrustedInstrumentResolver,
    ServerOwnedTrustedSnapshotResolver,
    SnapshotRegistryMarketDataRepository,
)
from ashare_lab.adapters.market_data.baostock_reference import BaoStockClient
from ashare_lab.adapters.market_data.trusted_snapshots import (
    TrustedSecurityMasterSnapshotLoader,
    TrustedTechnicalSnapshotLoader,
)
from ashare_lab.adapters.persistence import (
    SQLAlchemyBacktestRunStore,
    SQLAlchemyStrategyV2ArtifactStore,
)
from ashare_lab.api import create_app as create_http_app
from ashare_lab.api.app import build_hybrid_candidate_compiler
from ashare_lab.application.backtest_submission import (
    BacktestRunConfig,
    BacktestSubmissionService,
    SubmissionVersions,
)
from ashare_lab.application.daily_backtest import DailyBacktestConfig, FeeQuoteProvider
from ashare_lab.application.execute_backtest import (
    BacktestExecutionService,
    WorkerRuntimeIdentity,
)
from ashare_lab.application.execute_strategy_v2 import (
    ExecuteStrategyV2Error,
    ExecuteStrategyV2Service,
)
from ashare_lab.application.strategy_v2_draft_issuer import (
    GroundedStrategyV2Issuer,
    StrategyV2ProducerPin,
)
from ashare_lab.application.strategy_v2_http import (
    AssetRoutingStrategyV2ReceiptExecutor,
    PersistedStrategyV2HttpService,
    StrategyV2HttpService,
)
from ashare_lab.application.technical_v2 import (
    bind_stock_fee_provider,
    create_etf_all_in_fee_provider,
)
from ashare_lab.application.validation_receipts_v2 import ValidationReceiptServiceV2
from ashare_lab.domain.catalog import (
    CatalogSnapshot,
    load_catalog_directory,
    load_coverage_catalog_directory,
)
from ashare_lab.domain.events.catalog import DOCUMENT_TEXT_EVENT_CODES
from ashare_lab.domain.execution import (
    AshareExchange,
    FeeCalculator,
    FeePolicy,
    HistoricalAshareRuleBook,
    TradingCalendar,
)
from ashare_lab.domain.instruments import (
    AssetType,
    Exchange,
    InstrumentRef,
    SecurityMasterRecord,
)
from ashare_lab.domain.market_data import DailyBar, DataSnapshotRef, InstrumentSession
from ashare_lab.domain.shared import DomainValidationError, InstrumentId, Money
from ashare_lab.domain.signals.runtime import validate_stable_indicator_evaluator_catalog
from ashare_lab.ports.backtest_runs import BacktestJobQueue
from ashare_lab.ports.market_data import DataRequirements, DateRange, MarketDataRepository
from ashare_lab.ports.session_reference import SessionReferenceProvider
from ashare_lab.ports.trusted_snapshots import (
    TrustedSecurityMasterSnapshot,
    TrustedTechnicalSnapshot,
)
from ashare_lab.settings import AppSettings


@dataclass(frozen=True, slots=True)
class ExecutionRuntime:
    settings: AppSettings
    catalog: CatalogSnapshot
    versions: SubmissionVersions
    market_data: MarketDataRepository
    run_store: SQLAlchemyBacktestRunStore
    executor: BacktestExecutionService
    strict_snapshot: DataSnapshotRef | None
    backtest_anchor_date: date | None


@dataclass(frozen=True, slots=True)
class ApiRuntime:
    execution: ExecutionRuntime
    catalog: CatalogSnapshot
    queue: BacktestJobQueue
    submission: BacktestSubmissionService
    event_backtest_probe: Callable[[], bool]
    event_backtest_codes_probe: Callable[[], frozenset[str]]
    event_preparable_codes_probe: Callable[[], frozenset[str]]
    event_document_text_backtest_codes_probe: Callable[[], frozenset[str]]
    event_document_text_preparable_codes_probe: Callable[[], frozenset[str]]
    strategy_v2_service: StrategyV2HttpService | None = None
    strategy_v2_unavailable_reason: str | None = None


class _BaoStockLoginResult(Protocol):
    error_code: object
    error_msg: object


class _BaoStockRuntimeClient(BaoStockClient, Protocol):
    def login(self) -> _BaoStockLoginResult: ...

    def logout(self) -> _BaoStockLoginResult: ...


class _PinnedSnapshotSessionReference:
    """Policy identity only; session facts are loaded from the pinned snapshot."""

    version = HistoricalAshareRuleBook.version

    def sessions_for(self, bars: Sequence[DailyBar]) -> Sequence[InstrumentSession]:
        del bars
        raise RuntimeError("pinned snapshot sessions must be loaded through market_data")


def build_execution_runtime(settings: AppSettings | None = None) -> ExecutionRuntime:
    selected = settings or AppSettings()
    market_data, sessions_from_snapshot = _build_market_data_repository(selected)
    # A strict replay must never create a run manifest from a dirty/pseudo
    # revision or from a downgraded data profile.  `/ready` remains useful for
    # dependency health, but it is not the authorization boundary.
    strict_snapshot = _validate_strict_replay_configuration(selected, market_data)
    # The pinned acquisition range includes the default post-backtest settlement
    # tail.  Relative natural-language periods must end at the last date that can
    # still reserve that tail, otherwise every otherwise-valid run is rejected
    # for asking beyond the immutable snapshot.
    backtest_anchor_date = (
        market_data.strict_composite_period().end
        - timedelta(days=BacktestRunConfig().settlement_extension_days)
        if strict_snapshot is not None and isinstance(market_data, LocalParquetMarketDataRepository)
        else None
    )
    # Validate market-rule provenance before opening databases or starting workers.
    session_reference = _build_session_reference(selected)
    catalog = load_catalog_directory(selected.catalog_root)
    validate_stable_indicator_evaluator_catalog(catalog)
    versions = SubmissionVersions(
        catalog_hash=catalog.content_hash,
        engine_version=selected.engine_version,
        code_revision=selected.code_revision,
        market_rule_version=session_reference.version,
    )
    _prepare_sqlite_parent(selected.database_url)
    store = SQLAlchemyBacktestRunStore(
        selected.database_url,
        initialize_schema=selected.initialize_schema,
    )
    executor = BacktestExecutionService(
        market_data=market_data,
        session_reference=session_reference,
        run_store=store,
        runtime_identity=WorkerRuntimeIdentity.from_submission_versions(
            versions,
            strict_replay=(
                selected.market_data_profile == "composite_snapshot" or selected.is_production
            ),
        ),
        sessions_from_snapshot=sessions_from_snapshot,
    )
    return ExecutionRuntime(
        settings=selected,
        catalog=catalog,
        versions=versions,
        market_data=market_data,
        run_store=store,
        executor=executor,
        strict_snapshot=strict_snapshot,
        backtest_anchor_date=backtest_anchor_date,
    )


def _build_market_data_repository(
    settings: AppSettings,
) -> tuple[MarketDataRepository, bool]:
    if settings.market_data_profile != "on_demand_snapshot":
        return (
            LocalParquetMarketDataRepository(
                settings.data_root,
                profile=settings.market_data_profile,
            ),
            False,
        )

    repository_root = Path(__file__).resolve().parents[1]
    choice_root = _repository_path(repository_root, settings.choice_snapshot_root)
    technical_root = _repository_path(repository_root, settings.technical_snapshot_root)
    event_root = _repository_path(repository_root, settings.event_snapshot_root)
    composite_root = _repository_path(repository_root, settings.composite_snapshot_root)
    preparation_root = _repository_path(repository_root, settings.snapshot_preparation_root)
    for root in (choice_root, technical_root, event_root, composite_root, preparation_root):
        root.mkdir(parents=True, exist_ok=True)
    # The runnable on-demand registry exposes only finished Composite v2
    # publications.  Choice and provider-neutral technical snapshots are
    # source artifacts for the preparer; selecting either one directly would
    # bypass the event/corporate-action/provenance contract carried by the
    # Composite manifest.
    registry = SnapshotRegistryMarketDataRepository(composite_root)
    preparer = InternalDemoSnapshotPreparer(
        repository_root,
        python_executable=sys.executable,
        choice_output_root=choice_root,
        technical_output_root=technical_root,
        event_output_root=event_root,
        composite_output_root=composite_root,
        temporary_root=preparation_root,
    )
    return (
        OnDemandSnapshotMarketDataRepository(
            registry,
            preparer,
            refresh_each_submission=settings.on_demand_refresh_each_submission,
        ),
        True,
    )


def _repository_path(repository_root: Path, path: Path) -> Path:
    expanded = path.expanduser()
    return expanded.resolve() if expanded.is_absolute() else (repository_root / expanded).resolve()


def _build_strategy_v2_snapshot_repository(
    settings: AppSettings,
    *,
    trusted_master: TrustedSecurityMasterSnapshot,
) -> OnDemandSnapshotMarketDataRepository:
    """Build the server-only Choice-first acquisition boundary for v2 drafts."""

    repository_root = Path(__file__).resolve().parents[1]
    choice_setting = settings.strategy_v2_choice_root or settings.choice_snapshot_root
    technical_setting = settings.strategy_v2_technical_root or settings.technical_snapshot_root
    choice_root = _repository_path(repository_root, choice_setting)
    technical_root = _repository_path(repository_root, technical_setting)
    event_root = _repository_path(repository_root, settings.event_snapshot_root)
    composite_root = _repository_path(repository_root, settings.strategy_v2_composite_root)
    preparation_root = _repository_path(repository_root, settings.snapshot_preparation_root)
    for root in (choice_root, technical_root, event_root, composite_root, preparation_root):
        root.mkdir(parents=True, exist_ok=True)
    registry = SnapshotRegistryMarketDataRepository(
        composite_root,
        instrument_normalizer=SecurityMasterInstrumentNormalizer(trusted_master),
    )
    preparer = InternalDemoSnapshotPreparer(
        repository_root,
        python_executable=sys.executable,
        choice_output_root=choice_root,
        technical_output_root=technical_root,
        event_output_root=event_root,
        composite_output_root=composite_root,
        temporary_root=preparation_root,
    )
    return OnDemandSnapshotMarketDataRepository(
        registry,
        preparer,
        refresh_each_submission=settings.on_demand_refresh_each_submission,
    )


def _resolve_strategy_v2_baostock_record(symbol: str) -> SecurityMasterRecord:
    """Query one exact server-selected symbol without acquiring price history."""

    if re.fullmatch(r"[0-9]{6}\.(?:SH|SZ|BJ)", symbol) is None:
        raise ValueError("Strategy v2 security-master preparation requires a canonical symbol")
    try:
        client = cast(_BaoStockRuntimeClient, importlib.import_module("baostock"))
    except ImportError as error:
        raise RuntimeError("BaoStock security-master runtime is unavailable") from error
    login = client.login()
    if str(login.error_code) != "0":
        raise RuntimeError("BaoStock security-master login failed")
    try:
        return BaoStockReferenceAdapter(client).resolve_security_master(symbol=symbol).record
    finally:
        client.logout()


def _search_strategy_v2_choice_candidates(identifier: str) -> tuple[str, ...]:
    """Discover candidates through Choice; authoritative identity is separate."""

    try:
        module = importlib.import_module("EmQuantAPI")
        client = cast(ChoiceSecuritySearchClient, module.c)
    except (ImportError, AttributeError) as error:
        raise ChoiceSecuritySearchUnavailableError(
            "Choice security candidate search is unavailable"
        ) from error
    return ChoiceSecurityCandidateSearch(client)(identifier)


def _search_strategy_v2_baostock_candidates(identifier: str) -> tuple[str, ...]:
    """Discover bounded names/codes; exact identity is re-queried separately."""

    try:
        client = cast(_BaoStockRuntimeClient, importlib.import_module("baostock"))
    except ImportError as error:
        raise BaoStockSecuritySearchUnavailableError(
            "BaoStock security candidate search is unavailable"
        ) from error
    login = client.login()
    if str(login.error_code) != "0":
        raise BaoStockSecuritySearchUnavailableError(
            "BaoStock security candidate search login failed"
        )
    try:
        return BaoStockSecurityCandidateSearch(cast(BaoStockSecuritySearchClient, client))(
            identifier
        )
    finally:
        client.logout()


def build_api_runtime(
    settings: AppSettings | None = None,
    *,
    strategy_v2_service: StrategyV2HttpService | None = None,
) -> ApiRuntime:
    execution = build_execution_runtime(settings)
    selected = execution.settings
    catalog = execution.catalog
    event_backtest_probe = _event_backtest_probe(selected, execution.market_data)
    event_backtest_codes_probe = _event_backtest_codes_probe(
        selected,
        execution.market_data,
    )
    event_preparable_codes_probe = _event_preparable_codes_probe(
        selected,
        execution.market_data,
    )
    event_document_text_backtest_codes_probe = _event_document_text_backtest_codes_probe(
        selected,
        execution.market_data,
    )
    event_document_text_preparable_codes_probe = _event_document_text_preparable_codes_probe(
        selected,
        execution.market_data,
    )
    queue: BacktestJobQueue
    if selected.queue_backend == "rq":
        queue = RQBacktestJobQueue(
            queue_name=selected.queue_name,
            redis_url=selected.redis_url,
        )
    else:
        queue = ThreadBacktestJobQueue(
            execution.executor.execute,
            max_workers=selected.local_worker_threads,
        )
    submission = BacktestSubmissionService(
        market_data=execution.market_data,
        run_store=execution.run_store,
        job_queue=queue,
        versions=execution.versions,
        event_data_available=event_backtest_probe,
    )
    return ApiRuntime(
        execution=execution,
        catalog=catalog,
        queue=queue,
        submission=submission,
        event_backtest_probe=event_backtest_probe,
        event_backtest_codes_probe=event_backtest_codes_probe,
        event_preparable_codes_probe=event_preparable_codes_probe,
        event_document_text_backtest_codes_probe=event_document_text_backtest_codes_probe,
        event_document_text_preparable_codes_probe=(event_document_text_preparable_codes_probe),
        strategy_v2_service=strategy_v2_service,
    )


def create_configured_app(
    settings: AppSettings | None = None,
    *,
    strategy_v2_service: StrategyV2HttpService | None = None,
) -> FastAPI:
    runtime = build_api_runtime(settings, strategy_v2_service=strategy_v2_service)
    selected = runtime.execution.settings
    coverage_catalog = load_coverage_catalog_directory(selected.catalog_root / "coverage")
    capability_matrix = build_candidate_capability_matrix(runtime.catalog, coverage_catalog)
    candidate_transport = _build_candidate_transport(selected)
    compiler = build_hybrid_candidate_compiler(
        runtime.catalog,
        candidate_transport=candidate_transport,
        capability_matrix=capability_matrix,
        backtest_anchor_date=runtime.execution.backtest_anchor_date,
    )
    if strategy_v2_service is None:
        configured_v2, unavailable_reason = _build_strategy_v2_http_service(
            runtime,
            candidate_transport=candidate_transport,
            capability_matrix=capability_matrix,
        )
        runtime = replace(
            runtime,
            strategy_v2_service=configured_v2,
            strategy_v2_unavailable_reason=unavailable_reason,
        )
    app = create_http_app(
        compiler=compiler,
        catalog=runtime.catalog,
        coverage_catalog=coverage_catalog,
        catalog_root=selected.catalog_root,
        service_version=selected.engine_version,
        max_body_bytes=selected.max_body_bytes,
        backtest_submission=runtime.submission,
        run_store=runtime.execution.run_store,
        strategy_v2_service=runtime.strategy_v2_service,
        readiness_probe=_readiness_probe(runtime),
        readiness_reasons_probe=_readiness_reasons_probe(runtime),
        event_backtest_probe=runtime.event_backtest_probe,
        event_backtest_codes_probe=runtime.event_backtest_codes_probe,
        event_preparable_codes_probe=runtime.event_preparable_codes_probe,
        event_document_text_backtest_codes_probe=(runtime.event_document_text_backtest_codes_probe),
        event_document_text_preparable_codes_probe=(
            runtime.event_document_text_preparable_codes_probe
        ),
        cors_allowed_origins=selected.cors_origins,
    )
    app.state.runtime = runtime
    app.state.candidate_provider_identity = asdict(candidate_transport.identity)
    app.state.candidate_provider_response_mode = (
        selected.candidate_provider_response_mode
        if isinstance(candidate_transport, OpenAICompatibleCandidateTransport)
        else "disabled"
    )
    app.state.candidate_capability_projection = {
        "version": capability_matrix.schema_version,
        "hash": capability_matrix.content_hash,
    }
    if isinstance(runtime.queue, ThreadBacktestJobQueue):
        app.router.add_event_handler("shutdown", runtime.queue.shutdown)
    return app


def _build_candidate_transport(
    settings: AppSettings,
) -> DisabledCandidateJsonTransport | OpenAICompatibleCandidateTransport:
    if settings.candidate_provider_mode == "disabled":
        return DisabledCandidateJsonTransport(
            prompt_version=settings.candidate_prompt_version,
            schema_version=settings.candidate_schema_version,
        )
    endpoint = settings.candidate_provider_endpoint
    model = settings.candidate_provider_model
    if endpoint is None or model is None:
        # AppSettings validates this first; keep the composition root fail-closed
        # if a custom settings object ever bypasses normal validation.
        raise RuntimeError("candidate provider endpoint and model are required")
    return OpenAICompatibleCandidateTransport(
        endpoint=str(endpoint),
        provider=settings.candidate_provider_name,
        model=model,
        prompt_version=settings.candidate_prompt_version,
        schema_version=settings.candidate_schema_version,
        api_key=settings.candidate_provider_api_key,
        timeout_seconds=settings.candidate_provider_timeout_seconds,
        response_mode=settings.candidate_provider_response_mode,
        max_request_bytes=settings.candidate_provider_max_request_bytes,
        max_response_bytes=settings.candidate_provider_max_response_bytes,
    )


def _build_strategy_v2_http_service(
    runtime: ApiRuntime,
    *,
    candidate_transport: (DisabledCandidateJsonTransport | OpenAICompatibleCandidateTransport),
    capability_matrix: CandidateCapabilityMatrix,
) -> tuple[StrategyV2HttpService | None, str | None]:
    """Build the complete v2 runtime only from explicit server configuration."""

    settings = runtime.execution.settings
    if not settings.strategy_v2_enabled:
        return None, None
    if not isinstance(candidate_transport, OpenAICompatibleCandidateTransport):
        return None, "bounded candidate provider is disabled"
    master_id = settings.strategy_v2_security_master_snapshot_id
    default_producer_id = settings.strategy_v2_producer_snapshot_id
    producer_ids_by_symbol = dict(settings.strategy_v2_producer_snapshot_ids)
    signing_secret = settings.strategy_v2_receipt_signing_key
    if master_id is None:
        return None, "STRATEGY_V2_SECURITY_MASTER_SNAPSHOT_ID is required"
    if signing_secret is None:
        return None, "STRATEGY_V2_RECEIPT_SIGNING_KEY is required"
    signing_key = signing_secret.get_secret_value().encode("utf-8")
    if len(signing_key) < 32:
        return None, "STRATEGY_V2_RECEIPT_SIGNING_KEY must contain at least 32 bytes"
    if re.fullmatch(r"[0-9a-f]{40}", settings.code_revision) is None:
        return None, "Strategy v2 requires a clean 40-character CODE_REVISION"
    if importlib.util.find_spec("baostock") is None:
        return None, "BaoStock security-master runtime is unavailable"
    all_producer_ids = set(producer_ids_by_symbol.values())
    if default_producer_id is not None:
        all_producer_ids.add(default_producer_id)
    for producer_id in all_producer_ids:
        producer_manifest = (
            settings.strategy_v2_composite_root.expanduser().resolve()
            / producer_id.removeprefix("composite:")
            / "snapshot_manifest.json"
        )
        if not producer_manifest.is_file():
            return None, "configured Strategy v2 composite snapshot is missing"

    max_age = timedelta(days=settings.strategy_v2_snapshot_max_age_days)
    master_loader = TrustedSecurityMasterSnapshotLoader(
        settings.strategy_v2_security_master_root,
        max_age=max_age,
    )
    technical_loader = TrustedTechnicalSnapshotLoader(
        composite_root=settings.strategy_v2_composite_root,
        choice_root=settings.strategy_v2_choice_root,
        technical_root=settings.strategy_v2_technical_root,
        max_age=max_age,
    )
    try:
        # Fail before announcing readiness if the configured security master is
        # stale, missing or tampered. Per-instrument producer coverage remains
        # an execution-time fail-closed gate.
        trusted_master = master_loader.load(master_id)
        metadata_by_id = {
            producer_id: technical_loader.load_market_metadata(producer_id)
            for producer_id in all_producer_ids
        }
        snapshot_resolver = ServerOwnedTrustedSnapshotResolver(
            repository_factory=lambda selected_master: _build_strategy_v2_snapshot_repository(
                settings,
                trusted_master=selected_master,
            ),
            technical_loader=technical_loader,
            exact_pins=producer_ids_by_symbol,
            default_pin=default_producer_id,
        )
        instrument_resolver = ServerOwnedTrustedInstrumentResolver(
            baseline_loader=master_loader,
            baseline_snapshot_id=master_id,
            output_root=settings.strategy_v2_security_master_root,
            record_provider=_resolve_strategy_v2_baostock_record,
            candidate_search=ChoiceBaoStockEastmoneyCandidateSearch(
                choice=_search_strategy_v2_choice_candidates,
                baostock=_search_strategy_v2_baostock_candidates,
                eastmoney=EastmoneySecurityCandidateSearch(),
            ),
        )
        artifact_store = SQLAlchemyStrategyV2ArtifactStore(
            runtime.execution.run_store.engine,
            initialize_schema=settings.initialize_schema,
        )
    except Exception as error:
        return None, f"Strategy v2 trusted bootstrap failed: {type(error).__name__}"

    receipts = ValidationReceiptServiceV2(
        store=artifact_store,
        signing_key=signing_key,
        receipt_ttl=timedelta(seconds=settings.strategy_v2_receipt_ttl_seconds),
    )
    identity = candidate_transport.identity
    generator = VibeBoundedCandidateGenerator(
        candidate_transport,
        capability_matrix=capability_matrix,
        provider_identity=CandidateProviderIdentityView(
            provider=identity.provider,
            model=identity.model,
            prompt_version=identity.prompt_version,
            schema_version=identity.schema_version,
        ),
    )
    issuer = GroundedStrategyV2Issuer(
        generator=generator,
        provider=identity.provider,
        catalog=runtime.catalog,
        receipts=receipts,
        security_master_loader=master_loader,
        security_master_snapshot_id=master_id,
        technical_snapshot_loader=technical_loader,
        producer_snapshot_id=default_producer_id,
        backtest_anchor_date=(
            metadata_by_id[default_producer_id].coverage.end
            if default_producer_id is not None
            else (
                min(item.coverage.end for item in metadata_by_id.values())
                if metadata_by_id
                else trusted_master.metadata.coverage.end
            )
        ),
        producer_pins={
            symbol: StrategyV2ProducerPin(
                snapshot_id=producer_id,
                coverage_end=metadata_by_id[producer_id].coverage.end,
            )
            for symbol, producer_id in producer_ids_by_symbol.items()
        },
        instrument_resolver=instrument_resolver,
        snapshot_resolver=snapshot_resolver,
        code_revision=settings.code_revision,
    )

    def build_executor(
        fee_provider: FeeQuoteProvider,
        producer_id: str,
        selected_master_id: str,
    ) -> ExecuteStrategyV2Service:
        return ExecuteStrategyV2Service(
            receipt_service=receipts,
            security_master_loader=master_loader,
            technical_snapshot_loader=technical_loader,
            security_master_snapshot_id=selected_master_id,
            producer_snapshot_id=producer_id,
            catalog=runtime.catalog,
            code_revision=settings.code_revision,
            calendar_factory=_strategy_v2_calendar,
            fee_provider=fee_provider,
            backtest_config=DailyBacktestConfig(),
            run_id_factory=lambda: f"run:v2_{uuid.uuid4().hex}",
            clock=lambda: datetime.now(UTC),
        )

    fee_providers: dict[tuple[AssetType, Exchange], FeeQuoteProvider] = {}
    exchange_pairs = (
        (Exchange.SH, AshareExchange.SHANGHAI),
        (Exchange.SZ, AshareExchange.SHENZHEN),
        (Exchange.BJ, AshareExchange.BEIJING),
    )
    for exchange, cash_equity_exchange in exchange_pairs:
        stock_fees = bind_stock_fee_provider(
            FeeCalculator(
                FeePolicy(
                    exchange=cash_equity_exchange,
                    commission_rate=settings.strategy_v2_commission_rate,
                    minimum_commission=Money(
                        settings.strategy_v2_minimum_commission_cny,
                    ),
                )
            )
        )
        etf_fees = create_etf_all_in_fee_provider(
            exchange=exchange,
            commission_rate=settings.strategy_v2_commission_rate,
            minimum_commission=Money(settings.strategy_v2_minimum_commission_cny),
        )
        fee_providers[(AssetType.STOCK, exchange)] = stock_fees
        fee_providers[(AssetType.ETF, exchange)] = etf_fees

    def executor_for_plan(
        instrument: InstrumentRef,
        producer_id: str,
        selected_master_id: str,
    ) -> ExecuteStrategyV2Service:
        fee_provider = fee_providers.get((instrument.asset_type, instrument.exchange))
        if fee_provider is None:
            raise ExecuteStrategyV2Error(
                "capability_unavailable",
                "server has no attested execution policy for this asset and exchange",
            )
        return build_executor(fee_provider, producer_id, selected_master_id)

    routed_executor = AssetRoutingStrategyV2ReceiptExecutor(
        artifacts=artifact_store,
        executors={},
        executor_factory=executor_for_plan,
    )
    return (
        PersistedStrategyV2HttpService(
            drafts=issuer,
            artifacts=artifact_store,
            executor=routed_executor,
        ),
        None,
    )


def _strategy_v2_calendar(snapshot: TrustedTechnicalSnapshot) -> TradingCalendar:
    return TradingCalendar(
        version=snapshot.calendar_metadata.snapshot_id,
        sessions=tuple(item.session_date for item in snapshot.sessions),
    )


def _prepare_sqlite_parent(database_url: str) -> None:
    parsed = make_url(database_url)
    if parsed.get_backend_name() != "sqlite":
        return
    database = parsed.database
    if database is None or database in {"", ":memory:"}:
        return
    Path(database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def _build_session_reference(settings: AppSettings) -> SessionReferenceProvider:
    if settings.market_data_profile == "on_demand_snapshot":
        return _PinnedSnapshotSessionReference()
    if settings.is_production and settings.session_reference_mode != "parquet":
        raise RuntimeError(
            "production requires SESSION_REFERENCE_MODE=parquet; "
            "research_300059 is a labelled local demo fallback"
        )
    if settings.session_reference_mode == "parquet":
        return ParquetInstrumentSessionProvider(settings.session_reference_path)
    return ResearchFallbackSessionProvider()


def _validate_strict_replay_configuration(
    settings: AppSettings,
    market_data: MarketDataRepository,
) -> DataSnapshotRef | None:
    """Fail before opening the run store when formal replay evidence is invalid."""

    strict_runtime = settings.market_data_profile == "composite_snapshot" or settings.is_production
    if not strict_runtime:
        return None

    failures: list[str] = []
    if settings.market_data_profile != "composite_snapshot":
        failures.append("production requires MARKET_DATA_PROFILE=composite_snapshot")
    if settings.session_reference_mode != "parquet":
        failures.append("strict replay requires SESSION_REFERENCE_MODE=parquet")
    if not settings.event_data_required:
        failures.append("strict replay requires EVENT_DATA_REQUIRED=true")
    if re.fullmatch(r"[0-9a-f]{40}", settings.code_revision) is None:
        failures.append("strict replay requires a clean 40-character Git SHA")
    elif not settings.code_revision_matches_workspace:
        failures.append("CODE_REVISION does not match the clean local Git workspace")

    required_files = (
        "daily_ohlcv.parquet",
        "signal_daily_ohlcv.parquet",
        "corporate_actions.parquet",
        "events.parquet",
        "event_observations.parquet",
        "snapshot_manifest.json",
    )
    missing = [name for name in required_files if not (settings.data_root / name).is_file()]
    if missing:
        failures.append("strict composite is missing files: " + ", ".join(missing))
    if not settings.session_reference_path.is_file():
        failures.append("strict replay session reference file is missing")
    strict_snapshot: DataSnapshotRef | None = None
    if isinstance(market_data, LocalParquetMarketDataRepository):
        try:
            strict_snapshot = market_data.pin_strict_composite_snapshot()
        except MarketDataAdapterError as exc:
            failures.append(f"strict composite snapshot pin failed: {exc}")
    else:
        failures.append("strict replay requires one explicitly selected composite snapshot")

    if failures:
        raise RuntimeError("strict replay runtime rejected: " + "; ".join(failures))
    return strict_snapshot


def _readiness_probe(runtime: ApiRuntime) -> Callable[[], dict[str, bool]]:
    settings = runtime.execution.settings

    def probe() -> dict[str, bool]:
        if settings.market_data_profile == "on_demand_snapshot":
            dependencies_ready = _on_demand_dependencies_available(
                settings,
                runtime.execution.market_data,
            )
            checks = {
                "on_demand_snapshot_registry": dependencies_ready,
                "on_demand_provider_runtime": _on_demand_provider_runtime_available(),
            }
        else:
            checks = {
                "daily_data": (settings.data_root / "daily_ohlcv.parquet").is_file(),
            }
        if settings.market_data_profile == "composite_snapshot" or settings.is_production:
            checks["code_revision"] = (
                re.fullmatch(r"[0-9a-f]{40}", settings.code_revision) is not None
                and settings.code_revision_matches_workspace
            )
        if settings.market_data_profile in {"choice_snapshot", "composite_snapshot"}:
            checks["signal_data"] = (settings.data_root / "signal_daily_ohlcv.parquet").is_file()
            checks["corporate_actions"] = (
                settings.data_root / "corporate_actions.parquet"
            ).is_file()
            checks["snapshot_manifest"] = (settings.data_root / "snapshot_manifest.json").is_file()
            checks["corporate_action_manifest"] = _validated_corporate_action_manifest(
                settings,
                runtime.execution.market_data,
            )
        if settings.market_data_profile == "composite_snapshot":
            checks["event_observations"] = (
                settings.data_root / "event_observations.parquet"
            ).is_file()
            checks["strict_snapshot_pin"] = isinstance(
                runtime.execution.market_data,
                LocalParquetMarketDataRepository,
            ) and _strict_composite_snapshot_available(
                runtime.execution.market_data,
            )
        if settings.event_backtest_enabled:
            if settings.market_data_profile == "on_demand_snapshot":
                checks["event_profile"] = True
                checks["event_acquisition_path"] = _on_demand_dependencies_available(
                    settings,
                    runtime.execution.market_data,
                )
            else:
                checks["event_profile"] = settings.market_data_profile == "composite_snapshot"
                checks["event_data"] = (
                    settings.market_data_profile == "composite_snapshot"
                    and (settings.data_root / "events.parquet").is_file()
                )
        try:
            with runtime.execution.run_store.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            checks["run_store"] = True
        except Exception:
            checks["run_store"] = False
        if settings.market_data_profile == "on_demand_snapshot":
            checks["session_reference"] = True
        elif settings.session_reference_mode == "parquet":
            checks["session_reference"] = settings.session_reference_path.is_file()
        else:
            checks["session_reference"] = True
        if settings.queue_backend == "rq":
            try:
                from redis import Redis

                checks["job_queue"] = bool(
                    Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
                        settings.redis_url,
                        socket_connect_timeout=1,
                        socket_timeout=1,
                    ).ping()  # pyright: ignore[reportUnknownMemberType]
                )
            except Exception:
                checks["job_queue"] = False
        else:
            checks["job_queue"] = True
        if settings.strategy_v2_enabled:
            checks["strategy_v2_runtime"] = runtime.strategy_v2_service is not None
        return checks

    return probe


def _readiness_reasons_probe(runtime: ApiRuntime) -> Callable[[], dict[str, str]]:
    def probe() -> dict[str, str]:
        if runtime.strategy_v2_unavailable_reason is None:
            return {}
        return {"strategy_v2_runtime": runtime.strategy_v2_unavailable_reason}

    return probe


def _event_backtest_probe(
    settings: AppSettings,
    market_data: MarketDataRepository,
) -> Callable[[], bool]:
    event_path = settings.data_root / "events.parquet"
    observations_path = settings.data_root / "event_observations.parquet"
    manifest_path = settings.data_root / "snapshot_manifest.json"

    def probe() -> bool:
        if settings.market_data_profile == "on_demand_snapshot":
            return settings.event_backtest_enabled and _on_demand_dependencies_available(
                settings,
                market_data,
            )
        if (
            not settings.event_backtest_enabled
            or settings.market_data_profile != "composite_snapshot"
            or not event_path.is_file()
        ):
            return False
        return (
            observations_path.is_file()
            and manifest_path.is_file()
            and isinstance(market_data, LocalParquetMarketDataRepository)
            and (_strict_composite_snapshot_available(market_data))
        )

    return probe


def _event_backtest_codes_probe(
    settings: AppSettings,
    market_data: MarketDataRepository,
) -> Callable[[], frozenset[str]]:
    """Resolve runnable event codes from validated per-lane snapshot coverage."""

    def probe() -> frozenset[str]:
        if settings.market_data_profile == "on_demand_snapshot":
            # Provider readiness is not snapshot-backed availability.  A
            # concrete instrument+period request must first be prepared and
            # pinned before any code can be advertised as backtest_available.
            return frozenset()
        if (
            not settings.event_backtest_enabled
            or settings.market_data_profile != "composite_snapshot"
        ):
            return frozenset()
        try:
            if not isinstance(market_data, LocalParquetMarketDataRepository):
                return frozenset()
            return market_data.strict_composite_event_codes()
        except MarketDataAdapterError:
            return frozenset()

    return probe


def _event_preparable_codes_probe(
    settings: AppSettings,
    market_data: MarketDataRepository,
) -> Callable[[], frozenset[str]]:
    """Resolve codes that can be prepared for a concrete on-demand request."""

    def probe() -> frozenset[str]:
        if (
            settings.market_data_profile == "on_demand_snapshot"
            and settings.event_backtest_enabled
            and _on_demand_dependencies_available(settings, market_data)
        ):
            return eastmoney_preparable_event_codes()
        return frozenset()

    return probe


def _event_document_text_backtest_codes_probe(
    settings: AppSettings,
    market_data: MarketDataRepository,
) -> Callable[[], frozenset[str]]:
    """Resolve only pinned lanes with complete, validated document artifacts."""

    def probe() -> frozenset[str]:
        if (
            not settings.event_backtest_enabled
            or settings.market_data_profile != "composite_snapshot"
            or not isinstance(market_data, LocalParquetMarketDataRepository)
        ):
            return frozenset()
        try:
            return market_data.strict_composite_event_document_text_codes()
        except MarketDataAdapterError:
            return frozenset()

    return probe


def _event_document_text_preparable_codes_probe(
    settings: AppSettings,
    market_data: MarketDataRepository,
) -> Callable[[], frozenset[str]]:
    """Resolve opt-in document lanes only when their extractor runtime exists."""

    def probe() -> frozenset[str]:
        if (
            settings.market_data_profile == "on_demand_snapshot"
            and settings.event_backtest_enabled
            and _on_demand_dependencies_available(settings, market_data)
            and _on_demand_document_text_runtime_available()
        ):
            return eastmoney_preparable_event_codes() & DOCUMENT_TEXT_EVENT_CODES
        return frozenset()

    return probe


def _strict_composite_snapshot_available(
    market_data: LocalParquetMarketDataRepository,
) -> bool:
    """Use the real v2 loader/pin path as the readiness authorization check."""

    try:
        market_data.pin_strict_composite_snapshot()
    except MarketDataAdapterError:
        return False
    return True


def _on_demand_dependencies_available(
    settings: AppSettings,
    market_data: MarketDataRepository,
) -> bool:
    if not isinstance(market_data, OnDemandSnapshotMarketDataRepository):
        return False
    repository_root = Path(__file__).resolve().parents[1]
    roots = (
        _repository_path(repository_root, settings.choice_snapshot_root),
        _repository_path(repository_root, settings.technical_snapshot_root),
        _repository_path(repository_root, settings.event_snapshot_root),
        _repository_path(repository_root, settings.composite_snapshot_root),
        _repository_path(repository_root, settings.snapshot_preparation_root),
    )
    commands = (
        repository_root / "scripts/prepare_baostock_reference.py",
        repository_root / "scripts/prepare_eastmoney_corporate_actions.py",
        repository_root / "scripts/prepare_choice_snapshot.py",
        repository_root / "scripts/prepare_eastmoney_snapshot.py",
        repository_root / "scripts/prepare_event_snapshot.py",
    )
    if not all(path.is_dir() for path in roots) or not all(path.is_file() for path in commands):
        return False
    if not _on_demand_provider_runtime_available():
        return False
    try:
        market_data.validate_registry()
    except MarketDataAdapterError:
        return False
    return True


def _on_demand_provider_runtime_available() -> bool:
    session_runtime = importlib.util.find_spec("baostock") is not None
    choice_runtime = importlib.util.find_spec("EmQuantAPI") is not None
    push2_runtime = importlib.util.find_spec("httpx") is not None
    return session_runtime and (choice_runtime or push2_runtime)


def _on_demand_document_text_runtime_available() -> bool:
    return importlib.util.find_spec("pypdfium2") is not None


def _validated_corporate_action_manifest(
    settings: AppSettings,
    market_data: MarketDataRepository,
) -> bool:
    """Run the same producer/coverage pin used by a real backtest submission."""

    if not isinstance(market_data, LocalParquetMarketDataRepository):
        return False
    if settings.market_data_profile == "composite_snapshot":
        return _strict_composite_snapshot_available(market_data)
    try:
        decoded: object = json.loads(
            (settings.data_root / "snapshot_manifest.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    payload = _string_mapping(decoded)
    if payload is None:
        return False
    raw_symbol = payload.get("symbol")
    raw_range = payload.get("requestedRange")
    if not isinstance(raw_symbol, str) or not isinstance(raw_range, list):
        return False
    typed_range = cast(list[object], raw_range)
    if len(typed_range) != 2 or any(not isinstance(item, str) for item in typed_range):
        return False
    try:
        period = DateRange(
            date.fromisoformat(cast(str, typed_range[0])),
            date.fromisoformat(cast(str, typed_range[1])),
        )
        market_data.pin_snapshot(
            DataRequirements(
                instruments=(InstrumentId(raw_symbol),),
                datasets=("daily_ohlcv", "corporate_actions"),
            ),
            period,
        )
    except (ValueError, DomainValidationError, MarketDataAdapterError):
        return False
    return True


def _string_mapping(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    raw = cast(Mapping[object, object], value)
    if any(not isinstance(key, str) for key in raw):
        return None
    return cast(Mapping[str, object], raw)
