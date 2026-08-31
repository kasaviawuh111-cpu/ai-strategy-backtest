from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ashare_lab.adapters.persistence import create_backtest_run_engine
from ashare_lab.adapters.persistence.strategy_v2_artifacts import (
    SQLAlchemyStrategyV2ArtifactStore,
    create_strategy_v2_artifact_schema,
)
from ashare_lab.application.daily_backtest import DailyBacktestConfig, DailyBacktestInput
from ashare_lab.application.execute_strategy_v2 import (
    ExecuteStrategyV2Error,
    ExecuteStrategyV2Service,
    derive_technical_engine_run_key,
)
from ashare_lab.application.technical_v2 import (
    bind_stock_fee_provider,
)
from ashare_lab.application.validation_receipts_v2 import ValidationReceiptServiceV2
from ashare_lab.domain.catalog import load_catalog_directory
from ashare_lab.domain.execution import AshareExchange, FeeCalculator, FeePolicy, TradingCalendar
from ashare_lab.domain.instruments import (
    AssetType,
    Exchange,
    InstrumentRef,
    SecurityMasterAssetType,
    SecurityMasterRecord,
    SecurityMasterSnapshot,
)
from ashare_lab.domain.market_data import (
    Board,
    DailyBar,
    DataSnapshotRef,
    InstrumentSession,
    PriceBasis,
    TradingStatus,
)
from ashare_lab.domain.runs import DraftRevisionV2
from ashare_lab.domain.shared import InstrumentId, Money, Price, Quantity, StrongId
from ashare_lab.domain.strategy.models_v2 import (
    BacktestConfigV2,
    CatalogRefV2,
    ConditionGrounding,
    InterpretationCoverage,
    StrategySpecV2,
    TechnicalConditionV2,
    iter_condition_leaf_paths,
)
from ashare_lab.domain.strategy.validation_v2 import (
    ConditionGroundingExpectation,
    StrategyCandidateV2,
    StrategyV2ValidationContext,
    adapt_technical_strategy_v2_to_v1,
    condition_semantics_hash,
    validate_strategy_candidate_v2,
)
from ashare_lab.ports.market_data import DateRange
from ashare_lab.ports.trusted_snapshots import (
    TrustedSecurityMasterSnapshot,
    TrustedSnapshotMetadata,
    TrustedTechnicalSnapshot,
    build_trusted_v2_snapshot_contracts,
)

ROOT = Path(__file__).resolve().parents[3]
TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2025, 1, 10, 12, tzinfo=UTC)
START = date(2025, 1, 2)
END = date(2025, 1, 8)
ORIGINAL_INPUT = "收盘上穿2日均线买入，下穿2日均线卖出"
GIT_SHA = "1" * 40
SIGNING_KEY = b"p0c-integration-signing-key-at-least-32-bytes"
SECURITY_MASTER_ID = "security_master:" + "c" * 64
MARKET_ID = "market_data:" + "e" * 64
CALENDAR_ID = "trading_calendar:" + "d" * 64
PRODUCER_ID = "composite:" + "f" * 64


def _instrument() -> InstrumentRef:
    return InstrumentRef(
        symbol="600519.SH",
        name="贵州茅台",
        exchange=Exchange.SH,
        asset_type=AssetType.STOCK,
        currency="CNY",
        listing_date=date(2001, 8, 27),
        tradable=True,
        data_source="choice.security_master",
    )


def _master(tmp_path: Path) -> TrustedSecurityMasterSnapshot:
    snapshot = SecurityMasterSnapshot(
        snapshot_id=SECURITY_MASTER_ID,
        records=(
            SecurityMasterRecord(
                symbol="600519.SH",
                name="贵州茅台",
                exchange=Exchange.SH,
                asset_type=SecurityMasterAssetType.STOCK,
                currency="CNY",
                listing_date=date(2001, 8, 27),
                tradable=True,
                data_source="choice.security_master",
            ),
        ),
    )
    return TrustedSecurityMasterSnapshot(
        metadata=TrustedSnapshotMetadata(
            snapshot_id=SECURITY_MASTER_ID,
            provider="choice.security_master",
            schema_version="security-master-snapshot.v1",
            content_hash="sha256:" + "c" * 64,
            coverage=DateRange(START, END),
            generated_at=NOW,
        ),
        snapshot=snapshot,
        path=tmp_path / "trusted-master",
    )


def _bars() -> tuple[DailyBar, ...]:
    closes = ("10", "9", "11", "12", "8", "7", "9")
    instrument = InstrumentId("600519.SH")
    return tuple(
        DailyBar(
            instrument_id=instrument,
            session_date=START + timedelta(days=index),
            open=Price(Decimal(close)),
            high=Price(Decimal(close)),
            low=Price(Decimal(close)),
            close=Price(Decimal(close)),
            volume=Quantity(1_000_000),
            turnover=Decimal(close) * Decimal("1000000"),
            available_at=datetime.combine(
                START + timedelta(days=index),
                datetime.min.time().replace(hour=15),
                tzinfo=TZ,
            ),
        )
        for index, close in enumerate(closes)
    )


def _technical(*, content_suffix: str = "e") -> TrustedTechnicalSnapshot:
    bars = _bars()
    sessions = tuple(
        InstrumentSession(
            instrument_id=bar.instrument_id,
            session_date=bar.session_date,
            board=Board.MAIN,
            status=TradingStatus.TRADING,
            previous_close=Price(Decimal("10")),
            upper_limit=Price(Decimal("50")),
            lower_limit=Price(Decimal("1")),
            minimum_buy_quantity=100,
            buy_quantity_increment=100,
        )
        for bar in bars
    )
    signal_bars = tuple(replace(bar, price_basis=PriceBasis.BACK_ADJUSTED) for bar in bars)
    return TrustedTechnicalSnapshot(
        producer_snapshot_id=PRODUCER_ID,
        snapshot_ref=DataSnapshotRef(
            snapshot_id=StrongId("snapshot:" + "a" * 64),
            checksum="sha256:" + "a" * 64,
            schema_version="local-parquet.market-data.v3",
            created_at=NOW,
            producer_snapshot_id=PRODUCER_ID,
        ),
        market_data_metadata=TrustedSnapshotMetadata(
            snapshot_id=MARKET_ID,
            producer_snapshot_id=PRODUCER_ID,
            provider="Choice Quant API",
            schema_version="choice.daily-research-snapshot.v1",
            content_hash="sha256:" + content_suffix * 64,
            coverage=DateRange(START, END),
            generated_at=NOW,
        ),
        calendar_metadata=TrustedSnapshotMetadata(
            snapshot_id=CALENDAR_ID,
            producer_snapshot_id=PRODUCER_ID,
            provider="BaoStock Python API",
            schema_version="cn-a-share-trading-calendar.v1",
            content_hash="sha256:" + "d" * 64,
            coverage=DateRange(START, END),
            generated_at=NOW,
        ),
        execution_bars=bars,
        signal_bars=signal_bars,
        sessions=sessions,
        corporate_actions=(),
    )


def _strategy() -> StrategySpecV2:
    entry = TechnicalConditionV2(
        indicator_id="technical.ma",
        definition_version="1.0.0",
        params={"period": 2, "price_field": "close"},
        trigger="price_crosses_above",
    )
    exit_condition = entry.model_copy(update={"trigger": "price_crosses_below"})
    return StrategySpecV2(
        catalog=CatalogRefV2(catalog_id="cn_a.signals", release_version="2026.08.30"),
        instrument=_instrument(),
        entry=entry,
        exit=exit_condition,
        interpretation_coverage=InterpretationCoverage(
            groundings=(
                ConditionGrounding(
                    dsl_path="$.entry",
                    source_start=0,
                    source_end=11,
                    source_text=ORIGINAL_INPUT[0:11],
                ),
                ConditionGrounding(
                    dsl_path="$.exit",
                    source_start=12,
                    source_end=len(ORIGINAL_INPUT),
                    source_text=ORIGINAL_INPUT[12:],
                ),
            )
        ),
        backtest=BacktestConfigV2(
            start=START,
            end=END,
            initial_cash_cny=100_000,
        ),
    )


def _expectations(strategy: StrategySpecV2) -> tuple[ConditionGroundingExpectation, ...]:
    leaves = dict(iter_condition_leaf_paths(strategy))
    return tuple(
        ConditionGroundingExpectation(
            dsl_path=grounding.dsl_path,
            source_start=grounding.source_start,
            source_end=grounding.source_end,
            source_text=grounding.source_text,
            condition_hash=condition_semantics_hash(leaves[grounding.dsl_path]),
        )
        for grounding in strategy.interpretation_coverage.groundings
    )


def _store(path: Path) -> SQLAlchemyStrategyV2ArtifactStore:
    engine = create_backtest_run_engine(f"sqlite+pysqlite:///{path}")
    create_strategy_v2_artifact_schema(engine)
    return SQLAlchemyStrategyV2ArtifactStore(engine)


@dataclass
class _MasterLoader:
    snapshot: TrustedSecurityMasterSnapshot
    calls: list[object]

    def load(self, snapshot_id: object) -> TrustedSecurityMasterSnapshot:
        self.calls.append(snapshot_id)
        return self.snapshot


@dataclass
class _TechnicalLoader:
    snapshot: TrustedTechnicalSnapshot
    calls: list[tuple[object, InstrumentId, DateRange, str]]

    def load(
        self,
        *,
        producer_snapshot_id: object,
        instrument_id: InstrumentId,
        period: DateRange,
        security_master: TrustedSecurityMasterSnapshot,
    ) -> TrustedTechnicalSnapshot:
        self.calls.append(
            (producer_snapshot_id, instrument_id, period, security_master.metadata.snapshot_id)
        )
        return self.snapshot


@dataclass(frozen=True)
class _Issued:
    receipt_id: str
    plan_id: str


def _issue(
    *,
    store: SQLAlchemyStrategyV2ArtifactStore,
    master: TrustedSecurityMasterSnapshot,
    technical: TrustedTechnicalSnapshot,
    draft_id: str = "draft:p0c-integrated",
) -> _Issued:
    strategy = _strategy()
    contracts = build_trusted_v2_snapshot_contracts(
        security_master=master,
        technical_snapshot=technical,
        instrument_id=InstrumentId(strategy.instrument.symbol),
        period=DateRange(START, END),
    )
    catalog = load_catalog_directory(ROOT / "catalogs")
    context = StrategyV2ValidationContext(
        security_master=master.snapshot,
        original_input=ORIGINAL_INPUT,
        draft_id=draft_id,
        revision=1,
        provider="deepseek",
        requested_instrument=strategy.instrument.symbol,
        expected_backtest=strategy.backtest,
        catalog=catalog,
        dataset_coverage=(contracts.dataset_coverage,),
        grounding_expectations=_expectations(strategy),
        code_revision=GIT_SHA,
    )
    plan = validate_strategy_candidate_v2(
        StrategyCandidateV2(
            original_input=ORIGINAL_INPUT,
            draft_id=context.draft_id,
            revision=context.revision,
            provider=context.provider,
            strategy=strategy,
        ),
        context,
    )
    receipts = ValidationReceiptServiceV2(
        store=store,
        signing_key=SIGNING_KEY,
        receipt_ttl=timedelta(hours=1),
        clock=lambda: NOW,
    )
    receipts.save_draft(
        DraftRevisionV2(
            draft_id=context.draft_id,
            revision=context.revision,
            original_input=ORIGINAL_INPUT,
            provider=context.provider,
            created_at=NOW,
        )
    )
    receipt = receipts.persist_validated_plan(
        plan,
        snapshot_bindings=(
            contracts.security_master,
            contracts.trading_calendar,
            contracts.market_data,
        ),
    )
    return _Issued(receipt_id=receipt.receipt_id, plan_id=plan.plan_id)


def _fees(*, commission_rate: Decimal = Decimal("0.0003")):
    return bind_stock_fee_provider(
        FeeCalculator(
            FeePolicy(
                exchange=AshareExchange.SHANGHAI,
                commission_rate=commission_rate,
                minimum_commission=Money(Decimal("5")),
            )
        )
    )


def _config() -> DailyBacktestConfig:
    return DailyBacktestConfig(
        participation_rate=Decimal("1"),
        slippage_bps=Decimal("0"),
        allocation_ratio=Decimal("0.9"),
    )


def _calendar(snapshot: TrustedTechnicalSnapshot) -> TradingCalendar:
    return TradingCalendar(
        version=snapshot.calendar_metadata.snapshot_id,
        sessions=tuple(item.session_date for item in snapshot.sessions),
    )


def _service(
    *,
    store: SQLAlchemyStrategyV2ArtifactStore,
    master: TrustedSecurityMasterSnapshot,
    technical: TrustedTechnicalSnapshot,
    run_ids: tuple[str, ...] = ("run:p0c-integrated",),
) -> tuple[ExecuteStrategyV2Service, _MasterLoader, _TechnicalLoader]:
    master_loader = _MasterLoader(master, [])
    technical_loader = _TechnicalLoader(technical, [])
    pending_run_ids = iter(run_ids)
    service = ExecuteStrategyV2Service(
        receipt_service=ValidationReceiptServiceV2(
            store=store,
            signing_key=SIGNING_KEY,
            receipt_ttl=timedelta(hours=1),
            clock=lambda: NOW + timedelta(minutes=5),
        ),
        security_master_loader=master_loader,
        technical_snapshot_loader=technical_loader,
        security_master_snapshot_id=SECURITY_MASTER_ID,
        producer_snapshot_id=PRODUCER_ID,
        catalog=load_catalog_directory(ROOT / "catalogs"),
        code_revision=GIT_SHA,
        calendar_factory=_calendar,
        fee_provider=_fees(),
        backtest_config=_config(),
        run_id_factory=lambda: next(pending_run_ids),
        clock=lambda: NOW + timedelta(minutes=5),
    )
    return service, master_loader, technical_loader


def test_receipt_only_service_recovers_after_restart_and_persists_trace(
    tmp_path: Path,
) -> None:
    database = tmp_path / "integrated.db"
    master = _master(tmp_path)
    technical = _technical()
    first_store = _store(database)
    issued = _issue(store=first_store, master=master, technical=technical)
    first_store.engine.dispose()

    restarted_store = _store(database)
    service, master_loader, technical_loader = _service(
        store=restarted_store,
        master=master,
        technical=technical,
    )
    completed = service.execute(issued.receipt_id)
    persisted = restarted_store.get_run_manifest(completed.run_id)
    stored_plan = restarted_store.get_plan(issued.plan_id)

    assert persisted == completed.manifest
    assert persisted is not None
    assert stored_plan is not None
    assert persisted.original_input == ORIGINAL_INPUT
    assert persisted.final_strategy_json == stored_plan.strategy_json
    assert persisted.engine_run_key.startswith("v2_")
    assert persisted.backtest_config_json
    assert persisted.backtest_config_hash.startswith("sha256:")
    assert persisted.fee_policy_version == _fees().policy_version
    assert persisted.fee_policy_hash == _fees().parameters_hash
    assert persisted.engine_result_hash == completed.result.content_hash
    assert persisted.signal_records == completed.manifest.signal_records
    assert persisted.signal_records
    assert master_loader.calls == [SECURITY_MASTER_ID]
    assert technical_loader.calls == [
        (PRODUCER_ID, InstrumentId("600519.SH"), DateRange(START, END), SECURITY_MASTER_ID)
    ]
    with pytest.raises(TypeError):
        service.execute(issued.receipt_id, executable=True)  # type: ignore[call-arg]


def test_integrated_result_is_deterministic_and_identical_to_direct_v1(tmp_path: Path) -> None:
    store = _store(tmp_path / "parity.db")
    master = _master(tmp_path)
    technical = _technical()
    issued = _issue(store=store, master=master, technical=technical)
    service, _, _ = _service(
        store=store,
        master=master,
        technical=technical,
        run_ids=("run:p0c-first", "run:p0c-second"),
    )

    first = service.execute(issued.receipt_id)
    second = service.execute(issued.receipt_id)
    plan_record = store.get_plan(issued.plan_id)
    assert plan_record is not None
    contracts = build_trusted_v2_snapshot_contracts(
        security_master=master,
        technical_snapshot=technical,
        instrument_id=InstrumentId("600519.SH"),
        period=DateRange(START, END),
    )
    engine_run_key = derive_technical_engine_run_key(
        strategy_hash=plan_record.strategy_hash,
        snapshot_bindings=(
            contracts.security_master,
            contracts.trading_calendar,
            contracts.market_data,
        ),
        code_revision=plan_record.code_revision,
        backtest_config=_config(),
        fee_provider=_fees(),
    )
    direct = __import__(
        "ashare_lab.application.daily_backtest",
        fromlist=["run_daily_backtest"],
    ).run_daily_backtest(
        DailyBacktestInput(
            run_key=engine_run_key,
            strategy=adapt_technical_strategy_v2_to_v1(plan_record.strategy),
            bars=technical.execution_bars,
            signal_bars=technical.signal_bars,
            sessions=technical.sessions,
            calendar=_calendar(technical),
            fee_calculator=_fees(),
            corporate_actions=technical.corporate_actions,
            config=_config(),
        )
    )

    assert first.run_id != second.run_id
    assert first.manifest.engine_run_key == second.manifest.engine_run_key
    assert first.manifest.fee_policy_hash == second.manifest.fee_policy_hash
    assert first.manifest.backtest_config_hash == second.manifest.backtest_config_hash
    assert store.get_run_manifest(first.run_id) == first.manifest
    assert store.get_run_manifest(second.run_id) == second.manifest
    assert first.result.content_hash == second.result.content_hash == direct.content_hash
    assert first.result.orders == direct.orders
    assert first.result.fills == direct.fills
    assert tuple(item.fingerprint for item in first.manifest.signal_records) == tuple(
        item.fingerprint for item in second.manifest.signal_records
    )
    assert (
        derive_technical_engine_run_key(
            strategy_hash=plan_record.strategy_hash,
            snapshot_bindings=(
                contracts.security_master,
                contracts.trading_calendar,
                contracts.market_data,
            ),
            code_revision=plan_record.code_revision,
            backtest_config=replace(_config(), slippage_bps=Decimal("1")),
            fee_provider=_fees(),
        )
        != engine_run_key
    )
    assert (
        derive_technical_engine_run_key(
            strategy_hash=plan_record.strategy_hash,
            snapshot_bindings=(
                contracts.security_master,
                contracts.trading_calendar,
                contracts.market_data,
            ),
            code_revision=plan_record.code_revision,
            backtest_config=_config(),
            fee_provider=_fees(commission_rate=Decimal("0.0002")),
        )
        != engine_run_key
    )


def test_same_strategy_from_two_drafts_has_one_semantic_replay_identity(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path / "two-drafts.db")
    master = _master(tmp_path)
    technical = _technical()
    first_issued = _issue(
        store=store,
        master=master,
        technical=technical,
        draft_id="draft:p0c-semantic-first",
    )
    second_issued = _issue(
        store=store,
        master=master,
        technical=technical,
        draft_id="draft:p0c-semantic-second",
    )
    assert first_issued.plan_id != second_issued.plan_id

    service, _, _ = _service(
        store=store,
        master=master,
        technical=technical,
        run_ids=("run:p0c-two-drafts-first", "run:p0c-two-drafts-second"),
    )
    first = service.execute(first_issued.receipt_id)
    second = service.execute(second_issued.receipt_id)

    assert first.manifest.plan_id != second.manifest.plan_id
    assert first.manifest.strategy_hash == second.manifest.strategy_hash
    assert first.manifest.engine_run_key == second.manifest.engine_run_key
    assert first.result.content_hash == second.result.content_hash
    assert first.result.orders == second.result.orders
    assert first.result.fills == second.result.fills
    assert tuple(record.fingerprint for record in first.manifest.signal_records) != tuple(
        record.fingerprint for record in second.manifest.signal_records
    )
    assert sorted(
        record.semantic_fingerprint for record in first.manifest.signal_records
    ) == sorted(record.semantic_fingerprint for record in second.manifest.signal_records)


def test_unknown_receipt_and_current_snapshot_drift_fail_closed(tmp_path: Path) -> None:
    store = _store(tmp_path / "fail-closed.db")
    master = _master(tmp_path)
    trusted = _technical()
    issued = _issue(store=store, master=master, technical=trusted)
    service, _, _ = _service(store=store, master=master, technical=trusted)

    with pytest.raises(ExecuteStrategyV2Error, match="receipt") as missing:
        service.execute("receipt:" + "0" * 64)
    assert missing.value.code == "receipt_untrusted"

    drifted_service, _, _ = _service(
        store=store,
        master=master,
        technical=replace(
            trusted,
            market_data_metadata=replace(
                trusted.market_data_metadata,
                content_hash="sha256:" + "9" * 64,
            ),
        ),
    )
    with pytest.raises(ExecuteStrategyV2Error, match="snapshot") as drifted:
        drifted_service.execute(issued.receipt_id)
    assert drifted.value.code == "receipt_untrusted"
