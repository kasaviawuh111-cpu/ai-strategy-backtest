from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from ashare_lab.adapters.language.vibe_candidates import (
    CandidateProviderIdentityView,
    CandidateTransportError,
    CandidateTransportRequest,
    CandidateTransportResponse,
    VibeBoundedCandidateGenerator,
    build_candidate_capability_matrix,
)
from ashare_lab.adapters.persistence.strategy_v2_artifacts import (
    SQLAlchemyStrategyV2ArtifactStore,
)
from ashare_lab.api import create_app
from ashare_lab.application.strategy_v2_draft_issuer import GroundedStrategyV2Issuer
from ashare_lab.application.strategy_v2_http import (
    AssetRoutingStrategyV2ReceiptExecutor,
    PersistedStrategyV2HttpService,
    StrategyV2CompletedExecution,
    StrategyV2DraftCommand,
    StrategyV2HttpServiceError,
)
from ashare_lab.application.validation_receipts_v2 import ValidationReceiptServiceV2
from ashare_lab.domain.catalog import (
    load_catalog_directory,
    load_coverage_catalog_directory,
)
from ashare_lab.domain.instruments import (
    AssetType,
    Exchange,
    InstrumentResolver,
    SecurityMasterAssetType,
    SecurityMasterRecord,
    SecurityMasterSnapshot,
)
from ashare_lab.domain.market_data import DataSnapshotRef
from ashare_lab.domain.shared import StrongId
from ashare_lab.ports.market_data import DateRange
from ashare_lab.ports.trusted_snapshots import (
    TrustedInstrumentSelection,
    TrustedSecurityMasterSnapshot,
    TrustedSnapshotMetadata,
    TrustedSnapshotProviderUnavailableError,
    TrustedTechnicalSnapshot,
)
from tests.unit.application import test_execute_strategy_v2 as execution_fixtures

ROOT = Path(__file__).parents[3]
CATALOG = load_catalog_directory(ROOT / "catalogs")
CAPABILITIES = build_candidate_capability_matrix(
    CATALOG,
    load_coverage_catalog_directory(ROOT / "catalogs" / "coverage"),
)
NOW = datetime(2026, 8, 31, 8, tzinfo=UTC)
MASTER_ID = "security_master:" + "1" * 64
COMPOSITE_ID = "composite:" + "2" * 64
MARKET_ID = "choice:" + "3" * 64
CALENDAR_ID = "instrument_sessions:" + "4" * 64


class _Transport:
    def __init__(self, response: CandidateTransportResponse) -> None:
        self.response = response
        self.calls = 0
        self.requests: list[CandidateTransportRequest] = []

    async def generate_json(
        self,
        request: CandidateTransportRequest,
    ) -> CandidateTransportResponse:
        self.calls += 1
        self.requests.append(request)
        return self.response


class _UnavailableTransport(_Transport):
    def __init__(self) -> None:
        super().__init__({})

    async def generate_json(
        self,
        request: CandidateTransportRequest,
    ) -> CandidateTransportResponse:
        self.calls += 1
        self.requests.append(request)
        raise CandidateTransportError("provider unavailable")


class _SecurityLoader:
    def __init__(self) -> None:
        self.trusted = _trusted_master()

    def load(self, snapshot_id: object) -> TrustedSecurityMasterSnapshot:
        assert snapshot_id == MASTER_ID
        return self.trusted


class _MultiSecurityLoader:
    def __init__(self, *snapshots: TrustedSecurityMasterSnapshot) -> None:
        self._by_id = {item.metadata.snapshot_id: item for item in snapshots}

    def load(self, snapshot_id: object) -> TrustedSecurityMasterSnapshot:
        return self._by_id[str(snapshot_id)]


class _TechnicalLoader:
    def load_market_metadata(self, producer_snapshot_id: object) -> TrustedSnapshotMetadata:
        assert producer_snapshot_id == COMPOSITE_ID
        return _metadata(
            MARKET_ID,
            provider="Choice Quant API",
            schema_version="local-parquet.market-data.v3",
            period=DateRange(date(2021, 8, 31), date(2026, 8, 31)),
        )

    def load(self, **kwargs: object) -> TrustedTechnicalSnapshot:
        period = kwargs["period"]
        assert isinstance(period, DateRange)
        return _trusted_technical(period)


class _NeverExecutor:
    def execute(self, receipt_id: str) -> StrategyV2CompletedExecution:
        del receipt_id
        raise AssertionError("execution is outside this lifecycle contract")


def _metadata(
    snapshot_id: str,
    *,
    provider: str,
    schema_version: str,
    period: DateRange,
) -> TrustedSnapshotMetadata:
    return TrustedSnapshotMetadata(
        snapshot_id=snapshot_id,
        provider=provider,
        schema_version=schema_version,
        content_hash="sha256:" + snapshot_id.rsplit(":", maxsplit=1)[1],
        coverage=period,
        generated_at=NOW,
    )


def _trusted_master() -> TrustedSecurityMasterSnapshot:
    period = DateRange(date(2005, 1, 1), date(2026, 8, 31))
    snapshot = SecurityMasterSnapshot(
        snapshot_id=MASTER_ID,
        records=(
            SecurityMasterRecord(
                symbol="300059.SZ",
                name="东方财富",
                exchange=Exchange.SZ,
                asset_type=SecurityMasterAssetType.STOCK,
                listing_date=date(2010, 3, 19),
                tradable=True,
                data_source="server-master",
            ),
            SecurityMasterRecord(
                symbol="510300.SH",
                name="沪深300ETF",
                exchange=Exchange.SH,
                asset_type=SecurityMasterAssetType.ETF,
                listing_date=date(2012, 5, 28),
                tradable=True,
                data_source="server-master",
            ),
            SecurityMasterRecord(
                symbol="000300.SH",
                name="沪深300",
                exchange=Exchange.SH,
                asset_type=SecurityMasterAssetType.INDEX,
                listing_date=date(2005, 4, 8),
                tradable=False,
                data_source="server-master",
            ),
        ),
    )
    return TrustedSecurityMasterSnapshot(
        metadata=_metadata(
            MASTER_ID,
            provider="fixture-master",
            schema_version="security-master.v2",
            period=period,
        ),
        snapshot=snapshot,
        path=Path("/server/security-master"),
    )


def _trusted_technical(period: DateRange) -> TrustedTechnicalSnapshot:
    producer = _metadata(
        COMPOSITE_ID,
        provider="fixture-composite",
        schema_version="ashare-lab.composite-research-snapshot.v2",
        period=period,
    )
    market = _metadata(
        MARKET_ID,
        provider="Choice Quant API",
        schema_version="local-parquet.market-data.v3",
        period=period,
    )
    calendar = _metadata(
        CALENDAR_ID,
        provider="BaoStock Python API",
        schema_version="instrument-sessions.v1",
        period=period,
    )
    return TrustedTechnicalSnapshot(
        producer_snapshot_id=COMPOSITE_ID,
        snapshot_ref=DataSnapshotRef(
            snapshot_id=StrongId("snapshot:" + "5" * 64),
            checksum="sha256:" + "5" * 64,
            schema_version="local-parquet.market-data.v3",
            created_at=NOW,
            producer_schema_version=producer.schema_version,
            producer_snapshot_id=COMPOSITE_ID,
        ),
        market_data_metadata=market,
        calendar_metadata=calendar,
        execution_bars=(),
        signal_bars=(),
        sessions=(),
        corporate_actions=(),
        producer_metadata=producer,
    )


def _span(utterance: str, text: str) -> dict[str, object]:
    start = utterance.index(text)
    return {"start": start, "end": start + len(text), "text": text}


def _technical_batch(
    utterance: str,
    *,
    symbol: str | None = None,
) -> dict[str, object]:
    entry_text, exit_text = utterance.split("，", maxsplit=1)
    return {
        "candidates": [
            {
                "instrument_symbol": symbol,
                "entry": [
                    {
                        "kind": "indicator",
                        "indicator_id": "technical.macd",
                        "definition_version": "1.0.0",
                        "trigger": "golden_cross",
                        "params": {"fast": 12, "slow": 26, "signal": 9},
                    }
                ],
                "exit": [
                    {
                        "kind": "indicator",
                        "indicator_id": "technical.macd",
                        "definition_version": "1.0.0",
                        "trigger": "death_cross",
                        "params": {"fast": 12, "slow": 26, "signal": 9},
                    }
                ],
                "entry_spans": [_span(utterance, entry_text)],
                "exit_spans": [_span(utterance, exit_text)],
                "confidence": 0.96,
                "defaulted_fields": [
                    "/entry/0/params/fast",
                    "/entry/0/params/slow",
                    "/entry/0/params/signal",
                    "/exit/0/params/fast",
                    "/exit/0/params/slow",
                    "/exit/0/params/signal",
                ],
            }
        ]
    }


def _event_batch(utterance: str) -> dict[str, object]:
    entry_text, exit_text = utterance.split("，", maxsplit=1)
    return {
        "candidates": [
            {
                "entry": [
                    {
                        "kind": "event",
                        "event_code": "event.financial_results.annual_report",
                        "definition_version": "1.0.0",
                        "trigger": "published",
                        "attributes": {},
                    }
                ],
                "exit": [
                    {
                        "kind": "indicator",
                        "indicator_id": "technical.macd",
                        "definition_version": "1.0.0",
                        "trigger": "death_cross",
                        "params": {"fast": 12, "slow": 26, "signal": 9},
                    }
                ],
                "entry_spans": [_span(utterance, entry_text)],
                "exit_spans": [_span(utterance, exit_text)],
                "confidence": 0.96,
                "defaulted_fields": [
                    "/exit/0/params/fast",
                    "/exit/0/params/slow",
                    "/exit/0/params/signal",
                ],
            }
        ]
    }


def _ma_batch_with_explicit_period(utterance: str) -> dict[str, object]:
    period_text = "2025年1月2日至2025年1月8日"
    entry_text = "价格上穿2日均线（移动平均线）买入"
    exit_text = "价格下穿2日均线（移动平均线）卖出"
    return {
        "candidates": [
            {
                "instrument_symbol": None,
                "entry": [
                    {
                        "kind": "indicator",
                        "indicator_id": "technical.ma",
                        "definition_version": "1.0.0",
                        "trigger": "price_crosses_above",
                        "params": {"period": 2, "price_field": "close"},
                    }
                ],
                "exit": [
                    {
                        "kind": "indicator",
                        "indicator_id": "technical.ma",
                        "definition_version": "1.0.0",
                        "trigger": "price_crosses_below",
                        "params": {"period": 2, "price_field": "close"},
                    }
                ],
                "entry_spans": [_span(utterance, entry_text)],
                "exit_spans": [_span(utterance, exit_text)],
                "instrument_span": None,
                "backtest_span": _span(utterance, period_text),
                "backtest_start": "2025-01-02",
                "backtest_end": "2025-01-08",
                "backtest_lookback_years": None,
                "confidence": 0.98,
                "defaulted_fields": [
                    "/entry/0/params/price_field",
                    "/exit/0/params/price_field",
                ],
            }
        ]
    }


def _issuer(
    tmp_path: Path,
    transport: _Transport,
    *,
    now: datetime = NOW,
    receipt_ttl: timedelta = timedelta(minutes=15),
) -> tuple[GroundedStrategyV2Issuer, SQLAlchemyStrategyV2ArtifactStore]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    generator = VibeBoundedCandidateGenerator(
        transport,
        capability_matrix=CAPABILITIES,
        provider_identity=CandidateProviderIdentityView(
            provider="fixture-provider",
            model="fixture-model",
            prompt_version="fixture-prompt.v1",
            schema_version="fixture-schema.v1",
        ),
    )
    store = SQLAlchemyStrategyV2ArtifactStore(
        create_engine(f"sqlite+pysqlite:///{tmp_path / 'v2.db'}"),
        initialize_schema=True,
    )
    receipts = ValidationReceiptServiceV2(
        store=store,
        signing_key=b"r" * 32,
        receipt_ttl=receipt_ttl,
        clock=lambda: now,
    )
    return (
        GroundedStrategyV2Issuer(
            generator=generator,
            provider="fixture-provider",
            catalog=CATALOG,
            receipts=receipts,
            security_master_loader=_SecurityLoader(),
            security_master_snapshot_id=MASTER_ID,
            technical_snapshot_loader=_TechnicalLoader(),
            producer_snapshot_id=COMPOSITE_ID,
            backtest_anchor_date=date(2026, 8, 31),
            code_revision="a" * 40,
            draft_id_factory=lambda: "draft:p0d-http-test",
            clock=lambda: now,
        ),
        store,
    )


@pytest.mark.asyncio
async def test_ready_draft_and_validation_restore_without_recalling_provider(
    tmp_path: Path,
) -> None:
    utterance = "MACD金叉买入，MACD死叉卖出"
    transport = _Transport(_technical_batch(utterance))
    issuer, store = _issuer(tmp_path, transport)

    created = await issuer.create_draft(
        StrategyV2DraftCommand(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2099, 12, 31),
        )
    )

    assert created.status == "ready"
    assert created.strategy is not None
    assert created.strategy.instrument.symbol == "300059.SZ"
    assert created.strategy.backtest.start == date(2025, 8, 31)
    assert created.strategy.backtest.end == date(2026, 8, 31)
    assert created.strategy.backtest.initial_cash_cny == 1_000_000
    assert transport.calls == 1
    assert transport.requests[0].as_of_date == date(2026, 8, 31)
    persisted_draft = store.get_draft_revision(created.draft_id, created.revision)
    assert persisted_draft is not None
    assert persisted_draft.security_master_snapshot_id == MASTER_ID
    assert persisted_draft.producer_snapshot_id == COMPOSITE_ID

    restarted = GroundedStrategyV2Issuer(
        generator=issuer.generator,
        provider="fixture-provider",
        catalog=CATALOG,
        receipts=ValidationReceiptServiceV2(
            store=store,
            signing_key=b"r" * 32,
            receipt_ttl=timedelta(minutes=15),
            clock=lambda: NOW,
        ),
        security_master_loader=_SecurityLoader(),
        security_master_snapshot_id=MASTER_ID,
        technical_snapshot_loader=_TechnicalLoader(),
        producer_snapshot_id=COMPOSITE_ID,
        backtest_anchor_date=date(2026, 8, 31),
        code_revision="a" * 40,
        draft_id_factory=lambda: "draft:unused",
        clock=lambda: NOW,
    )
    recovered = restarted.get_draft(created.draft_id, created.revision)
    restarted.validate(created.draft_id, created.revision)

    assert recovered.strategy == created.strategy
    assert transport.calls == 1
    receipt = store.get_validation_receipt_for_draft_revision(created.draft_id, 1)
    assert receipt is not None
    assert receipt.claims.composite_snapshot is not None
    assert receipt.claims.composite_snapshot.snapshot_id == COMPOSITE_ID


@pytest.mark.asyncio
async def test_dynamic_etf_master_is_persisted_and_reopened_after_restart(
    tmp_path: Path,
) -> None:
    utterance = "MACD金叉买入，MACD死叉卖出"
    transport = _Transport(_technical_batch(utterance))
    base = _trusted_master()
    dynamic_id = "security_master:" + "6" * 64
    dynamic_snapshot = SecurityMasterSnapshot(
        snapshot_id=dynamic_id,
        records=(
            SecurityMasterRecord(
                symbol="510300.SH",
                name="沪深300ETF",
                exchange=Exchange.SH,
                asset_type=SecurityMasterAssetType.ETF,
                listing_date=date(2012, 5, 28),
                tradable=True,
                data_source="BaoStock query_stock_basic",
            ),
        ),
    )
    dynamic = TrustedSecurityMasterSnapshot(
        metadata=_metadata(
            dynamic_id,
            provider="BaoStock Python API",
            schema_version="security-master.v2",
            period=DateRange(date(2012, 5, 28), date(2026, 8, 31)),
        ),
        snapshot=dynamic_snapshot,
        path=tmp_path / "server-master",
    )
    loader = _MultiSecurityLoader(base, dynamic)
    generator = VibeBoundedCandidateGenerator(
        transport,
        capability_matrix=CAPABILITIES,
        provider_identity=CandidateProviderIdentityView(
            provider="fixture-provider",
            model="fixture-model",
            prompt_version="fixture-prompt.v1",
            schema_version="fixture-schema.v1",
        ),
    )
    store = SQLAlchemyStrategyV2ArtifactStore(
        create_engine(f"sqlite+pysqlite:///{tmp_path / 'dynamic.db'}"),
        initialize_schema=True,
    )
    receipts = ValidationReceiptServiceV2(
        store=store,
        signing_key=b"r" * 32,
        receipt_ttl=timedelta(minutes=15),
        clock=lambda: NOW,
    )

    class _DynamicResolver:
        def resolve_or_prepare(
            self,
            identifier: str,
            *,
            as_of: date,
        ) -> TrustedInstrumentSelection:
            instrument = InstrumentResolver(dynamic_snapshot).resolve(
                identifier,
                as_of=as_of,
            )
            instrument.require_tradable_on(as_of)
            assert identifier == "沪深300ETF"
            return TrustedInstrumentSelection(instrument=instrument, security_master=dynamic)

    issuer = GroundedStrategyV2Issuer(
        generator=generator,
        provider="fixture-provider",
        catalog=CATALOG,
        receipts=receipts,
        security_master_loader=loader,
        security_master_snapshot_id=MASTER_ID,
        technical_snapshot_loader=_TechnicalLoader(),
        producer_snapshot_id=COMPOSITE_ID,
        backtest_anchor_date=date(2026, 8, 31),
        instrument_resolver=_DynamicResolver(),
        code_revision="a" * 40,
        draft_id_factory=lambda: "draft:dynamic-etf",
        clock=lambda: NOW,
    )
    created = await issuer.create_draft(
        StrategyV2DraftCommand(
            utterance=utterance,
            instrument_context="沪深300ETF",
        )
    )
    persisted = store.get_draft_revision(created.draft_id, created.revision)
    assert persisted is not None
    assert persisted.security_master_snapshot_id == dynamic_id

    restarted = GroundedStrategyV2Issuer(
        generator=generator,
        provider="fixture-provider",
        catalog=CATALOG,
        receipts=receipts,
        security_master_loader=loader,
        security_master_snapshot_id=MASTER_ID,
        technical_snapshot_loader=_TechnicalLoader(),
        producer_snapshot_id=COMPOSITE_ID,
        backtest_anchor_date=date(2026, 8, 31),
        code_revision="a" * 40,
        clock=lambda: NOW,
    )
    restarted.validate(created.draft_id, created.revision)

    receipt = store.get_validation_receipt_for_draft_revision(created.draft_id, 1)
    assert receipt is not None
    assert receipt.claims.security_master.snapshot_id == dynamic_id
    assert transport.calls == 1


@pytest.mark.asyncio
async def test_expired_receipt_is_fully_revalidated_and_appended(
    tmp_path: Path,
) -> None:
    utterance = "MACD金叉买入，MACD死叉卖出"
    transport = _Transport(_technical_batch(utterance))
    issuer, store = _issuer(
        tmp_path,
        transport,
        receipt_ttl=timedelta(minutes=1),
    )
    draft = await issuer.create_draft(
        StrategyV2DraftCommand(
            utterance=utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2099, 1, 1),
        )
    )
    issuer.validate(draft.draft_id, draft.revision)
    first = store.get_validation_receipt_for_draft_revision(draft.draft_id, draft.revision)
    assert first is not None

    later = NOW + timedelta(minutes=2)
    restarted = GroundedStrategyV2Issuer(
        generator=issuer.generator,
        provider="fixture-provider",
        catalog=CATALOG,
        receipts=ValidationReceiptServiceV2(
            store=store,
            signing_key=b"r" * 32,
            receipt_ttl=timedelta(minutes=1),
            clock=lambda: later,
        ),
        security_master_loader=_SecurityLoader(),
        security_master_snapshot_id=MASTER_ID,
        technical_snapshot_loader=_TechnicalLoader(),
        producer_snapshot_id=COMPOSITE_ID,
        backtest_anchor_date=date(2026, 8, 31),
        code_revision="a" * 40,
        clock=lambda: later,
    )
    restarted.validate(draft.draft_id, draft.revision)
    second = store.get_validation_receipt_for_draft_revision(draft.draft_id, draft.revision)

    assert second is not None
    assert second.receipt_id != first.receipt_id
    assert second.issued_at == later
    assert transport.calls == 1


def test_http_provider_unavailable_fails_closed_without_dsl(tmp_path: Path) -> None:
    transport = _UnavailableTransport()
    issuer, store = _issuer(tmp_path, transport)
    service = PersistedStrategyV2HttpService(
        drafts=issuer,
        artifacts=store,
        executor=_NeverExecutor(),
    )
    with TestClient(create_app(strategy_v2_service=service)) as client:
        response = client.post(
            "/api/v2/strategy-drafts",
            json={
                "utterance": "跌下来一点就买，涨回去再卖",
                "instrument_context": "300059.SZ",
                "as_of_date": "2099-01-01",
            },
        )

    assert response.status_code == 201
    assert response.json()["status"] == "unsupported"
    assert response.json()["diagnostic_code"] == "candidate_provider_unavailable"
    assert response.json()["strategy"] is None
    assert transport.calls == 1


@pytest.mark.asyncio
async def test_security_discovery_provider_unavailable_is_explicit(tmp_path: Path) -> None:
    utterance = "MACD金叉买入，MACD死叉卖出"
    transport = _Transport(_technical_batch(utterance))
    issuer, store = _issuer(tmp_path, transport)

    class _UnavailableInstrumentResolver:
        def resolve_or_prepare(self, identifier: str, *, as_of: date) -> object:
            del identifier, as_of
            raise TrustedSnapshotProviderUnavailableError("provider offline")

    issuer = GroundedStrategyV2Issuer(
        generator=issuer.generator,
        provider="fixture-provider",
        catalog=CATALOG,
        receipts=ValidationReceiptServiceV2(
            store=store,
            signing_key=b"r" * 32,
            receipt_ttl=timedelta(minutes=15),
            clock=lambda: NOW,
        ),
        security_master_loader=_SecurityLoader(),
        security_master_snapshot_id=MASTER_ID,
        technical_snapshot_loader=_TechnicalLoader(),
        producer_snapshot_id=COMPOSITE_ID,
        backtest_anchor_date=date(2026, 8, 31),
        instrument_resolver=_UnavailableInstrumentResolver(),  # type: ignore[arg-type]
        code_revision="a" * 40,
        draft_id_factory=lambda: "draft:provider-unavailable",
        clock=lambda: NOW,
    )

    draft = await issuer.create_draft(
        StrategyV2DraftCommand(
            utterance=utterance,
            instrument_context="未知证券",
        )
    )

    assert draft.status == "invalid"
    assert draft.diagnostic_code == "provider_unavailable"
    assert draft.strategy is None


def test_sensitive_input_is_rejected_before_provider_or_storage(tmp_path: Path) -> None:
    utterance = "MACD金叉买入，MACD死叉卖出"
    transport = _Transport(_technical_batch(utterance))
    issuer, store = _issuer(tmp_path, transport)
    secret = "sk-1234567890abcdef1234567890abcdef"
    service = PersistedStrategyV2HttpService(
        drafts=issuer,
        artifacts=store,
        executor=_NeverExecutor(),
    )
    with TestClient(create_app(strategy_v2_service=service)) as client:
        response = client.post(
            "/api/v2/strategy-drafts",
            json={
                "utterance": f"{utterance} {secret}",
                "instrument_context": "300059.SZ",
            },
        )

    assert response.status_code == 422
    assert secret not in response.text
    assert transport.calls == 0
    assert store.get_draft_revision("draft:p0d-http-test", 1) is None


@pytest.mark.asyncio
async def test_index_and_event_candidates_fail_closed_without_etf_mapping(
    tmp_path: Path,
) -> None:
    utterance = "MACD金叉买入，MACD死叉卖出"
    index_transport = _Transport(_technical_batch(utterance))
    index_issuer, _ = _issuer(tmp_path / "index", index_transport)

    index = await index_issuer.create_draft(
        StrategyV2DraftCommand(
            utterance=utterance,
            instrument_context="000300.SH",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert index.status == "unsupported"
    assert index.diagnostic_code == "capability_unavailable"
    assert index.strategy is None

    event_utterance = "年度报告发布后买入，MACD死叉卖出"
    event_transport = _Transport(_event_batch(event_utterance))
    event_issuer, _ = _issuer(tmp_path / "event", event_transport)
    event = await event_issuer.create_draft(
        StrategyV2DraftCommand(
            utterance=event_utterance,
            instrument_context="300059.SZ",
            as_of_date=date(2026, 8, 30),
        )
    )

    assert event.status == "unsupported"
    assert event.diagnostic_code == "capability_unavailable"
    with pytest.raises(StrategyV2HttpServiceError, match="not ready"):
        event_issuer.validate(event.draft_id, event.revision)


def test_grounded_issuer_rejects_non_vibe_generator(tmp_path: Path) -> None:
    class _FakeGenerator:
        pass

    store = SQLAlchemyStrategyV2ArtifactStore(
        create_engine(f"sqlite+pysqlite:///{tmp_path / 'invalid.db'}"),
        initialize_schema=True,
    )
    with pytest.raises(TypeError, match="VibeBoundedCandidateGenerator"):
        GroundedStrategyV2Issuer(
            generator=_FakeGenerator(),  # type: ignore[arg-type]
            catalog=CATALOG,
            receipts=ValidationReceiptServiceV2(
                store=store,
                signing_key=b"r" * 32,
                receipt_ttl=timedelta(minutes=15),
            ),
            security_master_loader=_SecurityLoader(),
            security_master_snapshot_id=MASTER_ID,
            technical_snapshot_loader=_TechnicalLoader(),
            producer_snapshot_id=COMPOSITE_ID,
            backtest_anchor_date=date(2026, 8, 31),
            code_revision="a" * 40,
        )


def test_http_draft_and_validation_recover_after_service_recreation(tmp_path: Path) -> None:
    utterance = "MACD金叉买入，MACD死叉卖出"
    transport = _Transport(_technical_batch(utterance))
    first_issuer, store = _issuer(tmp_path, transport)
    first_service = PersistedStrategyV2HttpService(
        drafts=first_issuer,
        artifacts=store,
        executor=_NeverExecutor(),
    )
    with TestClient(create_app(strategy_v2_service=first_service)) as client:
        created = client.post(
            "/api/v2/strategy-drafts",
            json={
                "utterance": utterance,
                "instrument_context": "300059.SZ",
                "as_of_date": "2026-08-30",
            },
        )

    assert created.status_code == 201
    draft_id = created.json()["draft_id"]
    restarted = GroundedStrategyV2Issuer(
        generator=first_issuer.generator,
        provider="fixture-provider",
        catalog=CATALOG,
        receipts=ValidationReceiptServiceV2(
            store=store,
            signing_key=b"r" * 32,
            receipt_ttl=timedelta(minutes=15),
            clock=lambda: NOW,
        ),
        security_master_loader=_SecurityLoader(),
        security_master_snapshot_id=MASTER_ID,
        technical_snapshot_loader=_TechnicalLoader(),
        producer_snapshot_id=COMPOSITE_ID,
        backtest_anchor_date=date(2026, 8, 31),
        code_revision="a" * 40,
    )
    restarted_service = PersistedStrategyV2HttpService(
        drafts=restarted,
        artifacts=store,
        executor=_NeverExecutor(),
    )
    with TestClient(create_app(strategy_v2_service=restarted_service)) as client:
        recovered = client.get(f"/api/v2/strategy-drafts/{draft_id}/revisions/1")
        validated = client.post(
            "/api/v2/strategy-validations",
            json={"draft_id": draft_id, "revision": 1},
        )

    assert recovered.status_code == 200
    assert recovered.json()["strategy"] == created.json()["strategy"]
    assert validated.status_code == 200
    assert validated.json()["status"] == "validated"
    assert transport.calls == 1


def test_spoken_strategy_runs_through_bounded_dsl_receipt_and_http_execution(
    tmp_path: Path,
) -> None:
    utterance = (
        "2025年1月2日至2025年1月8日，价格上穿2日均线（移动平均线）买入，"
        "价格下穿2日均线（移动平均线）卖出"
    )
    transport = _Transport(_ma_batch_with_explicit_period(utterance))
    generator = VibeBoundedCandidateGenerator(
        transport,
        capability_matrix=CAPABILITIES,
        provider_identity=CandidateProviderIdentityView(
            provider="deepseek",
            model="bounded-model",
            prompt_version="fixture-prompt.v1",
            schema_version="fixture-schema.v1",
        ),
    )
    store = execution_fixtures._store(  # pyright: ignore[reportPrivateUsage]
        tmp_path / "spoken-http.db"
    )
    master = execution_fixtures._master(  # pyright: ignore[reportPrivateUsage]
        tmp_path
    )
    technical = execution_fixtures._technical()  # pyright: ignore[reportPrivateUsage]
    receipts = ValidationReceiptServiceV2(
        store=store,
        signing_key=execution_fixtures.SIGNING_KEY,
        receipt_ttl=timedelta(hours=1),
        clock=lambda: execution_fixtures.NOW,
    )
    issuer = GroundedStrategyV2Issuer(
        generator=generator,
        provider="deepseek",
        catalog=CATALOG,
        receipts=receipts,
        security_master_loader=execution_fixtures._MasterLoader(  # pyright: ignore[reportPrivateUsage]
            master,
            [],
        ),
        security_master_snapshot_id=execution_fixtures.SECURITY_MASTER_ID,
        technical_snapshot_loader=execution_fixtures._TechnicalLoader(  # pyright: ignore[reportPrivateUsage]
            technical,
            [],
        ),
        producer_snapshot_id=execution_fixtures.PRODUCER_ID,
        backtest_anchor_date=execution_fixtures.END,
        code_revision=execution_fixtures.GIT_SHA,
        initial_cash_cny=100_000,
        draft_id_factory=lambda: "draft:p0d-spoken-http",
        clock=lambda: execution_fixtures.NOW,
    )
    executor, _, _ = execution_fixtures._service(  # pyright: ignore[reportPrivateUsage]
        store=store,
        master=master,
        technical=technical,
        run_ids=("run:p0d-spoken-http",),
    )
    service = PersistedStrategyV2HttpService(
        drafts=issuer,
        artifacts=store,
        executor=AssetRoutingStrategyV2ReceiptExecutor(
            artifacts=store,
            executors={(AssetType.STOCK, Exchange.SH): executor},
        ),
    )

    with TestClient(create_app(strategy_v2_service=service)) as client:
        draft = client.post(
            "/api/v2/strategy-drafts",
            json={
                "utterance": utterance,
                "instrument_context": "600519.SH",
                "as_of_date": "2099-12-31",
            },
        )
        validated = client.post(
            "/api/v2/strategy-validations",
            json={"draft_id": "draft:p0d-spoken-http", "revision": 1},
        )
        executed = client.post(
            "/api/v2/backtest-runs",
            json={"draft_id": "draft:p0d-spoken-http", "revision": 1},
        )
        recovered = client.get("/api/v2/backtest-runs/run:p0d-spoken-http")

    assert draft.status_code == 201
    assert draft.json()["status"] == "ready"
    assert draft.json()["strategy"]["backtest"] == {
        "start": "2025-01-02",
        "end": "2025-01-08",
        "initial_cash_cny": 100_000,
    }
    assert validated.status_code == 200
    assert validated.json()["receipt_id"].startswith("receipt:")
    assert executed.status_code == 201
    assert executed.json()["run_id"] == "run:p0d-spoken-http"
    assert recovered.status_code == 200
    assert recovered.json()["manifest"]["original_input"] == utterance
    assert transport.calls == 1
    assert transport.requests[0].as_of_date == execution_fixtures.END
