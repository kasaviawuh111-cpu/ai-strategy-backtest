from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ashare_lab.adapters.market_data import (
    build_no_event_required_snapshot,
    compose_choice_event_snapshot,
)
from ashare_lab.adapters.market_data import choice_snapshot as choice_snapshot_module
from ashare_lab.adapters.market_data import trusted_snapshots as trusted_snapshots_module
from ashare_lab.adapters.market_data.local_parquet import LocalParquetMarketDataRepository
from ashare_lab.adapters.market_data.trusted_snapshots import (
    SecurityMasterInstrumentNormalizer,
    TrustedSecurityMasterSnapshotLoader,
    TrustedSnapshotCoverageError,
    TrustedSnapshotExpiredError,
    TrustedSnapshotIntegrityError,
    TrustedTechnicalSnapshotLoader,
    build_trusted_security_master_snapshot,
    build_trusted_v2_snapshot_contracts,
)
from ashare_lab.application.validation_receipts_v2 import snapshot_bindings_hash
from ashare_lab.domain.catalog import load_catalog_directory
from ashare_lab.domain.instruments import (
    Exchange,
    InstrumentResolver,
    SecurityMasterAssetType,
    SecurityMasterRecord,
    SecurityMasterSnapshot,
)
from ashare_lab.domain.market_data import CorporateAction, CorporateActionKind, TimeQuality
from ashare_lab.domain.shared import InstrumentId, StrongId
from ashare_lab.domain.strategy.models_v2 import (
    BacktestConfigV2,
    CatalogRefV2,
    ConditionGrounding,
    InterpretationCoverage,
    StrategySpecV2,
    TechnicalConditionV2,
)
from ashare_lab.domain.strategy.validation_v2 import (
    ConditionGroundingExpectation,
    StrategyCandidateV2,
    StrategyV2ValidationContext,
    condition_semantics_hash,
    validate_strategy_candidate_v2,
)
from ashare_lab.ports.market_data import DataRequirements, DateRange
from tests.unit.adapters import test_snapshot_registry as snapshot_registry_fixtures
from tests.unit.adapters.test_snapshot_registry import (  # pyright: ignore[reportPrivateUsage]
    _publish_choice,  # pyright: ignore[reportPrivateUsage]
    _publish_composite,  # pyright: ignore[reportPrivateUsage]
)


def _security_master(*, include_etf_and_index: bool = False) -> SecurityMasterSnapshot:
    records = [
        SecurityMasterRecord(
            symbol="300059.SZ",
            name="东方财富",
            exchange=Exchange.SZ,
            asset_type=SecurityMasterAssetType.STOCK,
            listing_date=date(2010, 3, 19),
            delisting_date=None,
            tradable=True,
            data_source="BaoStock query_stock_basic",
        )
    ]
    if include_etf_and_index:
        records.extend(
            (
                SecurityMasterRecord(
                    symbol="510300.SH",
                    name="沪深300ETF",
                    exchange=Exchange.SH,
                    asset_type=SecurityMasterAssetType.ETF,
                    listing_date=date(2012, 5, 28),
                    delisting_date=None,
                    tradable=True,
                    data_source="server security master",
                ),
                SecurityMasterRecord(
                    symbol="000300.SH",
                    name="沪深300",
                    exchange=Exchange.SH,
                    asset_type=SecurityMasterAssetType.INDEX,
                    listing_date=date(2005, 4, 8),
                    delisting_date=None,
                    tradable=False,
                    data_source="server security master",
                ),
            )
        )
    return SecurityMasterSnapshot(
        snapshot_id="security-master:logical-fixture",
        records=tuple(records),
    )


def _load_security_master(
    root: Path,
    *,
    include_etf_and_index: bool = False,
):
    published = build_trusted_security_master_snapshot(
        snapshot=_security_master(include_etf_and_index=include_etf_and_index),
        provider="BaoStock Python API",
        coverage=DateRange(date(2005, 4, 8), date(2026, 8, 30)),
        generated_at=datetime(2026, 8, 30, 8, tzinfo=UTC),
        output_root=root,
    )
    return TrustedSecurityMasterSnapshotLoader(
        root,
        max_age=timedelta(days=7),
        clock=lambda: datetime(2026, 8, 31, 8, tzinfo=UTC),
    ).load(published.metadata.snapshot_id)


def test_security_master_loads_only_from_content_addressed_server_root(tmp_path: Path) -> None:
    published = build_trusted_security_master_snapshot(
        snapshot=_security_master(),
        provider="BaoStock Python API",
        coverage=DateRange(date(2010, 3, 19), date(2026, 8, 30)),
        generated_at=datetime(2026, 8, 30, 8, tzinfo=UTC),
        output_root=tmp_path,
    )

    loaded = TrustedSecurityMasterSnapshotLoader(
        tmp_path,
        max_age=timedelta(days=7),
        clock=lambda: datetime(2026, 8, 31, 8, tzinfo=UTC),
    ).load(published.metadata.snapshot_id)

    assert loaded.snapshot.records[0].symbol == "300059.SZ"
    assert loaded.metadata.provider == "BaoStock Python API"
    assert loaded.metadata.schema_version == "ashare-lab.security-master-snapshot.v1"
    assert loaded.metadata.content_hash.startswith("sha256:")
    assert loaded.metadata.coverage == DateRange(date(2010, 3, 19), date(2026, 8, 30))
    with pytest.raises(TrustedSnapshotIntegrityError, match="snapshot id"):
        TrustedSecurityMasterSnapshotLoader(tmp_path).load(str(published.path))


def test_security_master_tamper_and_expiry_fail_closed(tmp_path: Path) -> None:
    published = build_trusted_security_master_snapshot(
        snapshot=_security_master(),
        provider="BaoStock Python API",
        coverage=DateRange(date(2010, 3, 19), date(2026, 8, 30)),
        generated_at=datetime(2026, 8, 30, 8, tzinfo=UTC),
        output_root=tmp_path,
    )
    loader = TrustedSecurityMasterSnapshotLoader(
        tmp_path,
        max_age=timedelta(hours=12),
        clock=lambda: datetime(2026, 8, 31, 8, tzinfo=UTC),
    )
    with pytest.raises(TrustedSnapshotExpiredError, match="expired"):
        loader.load(published.metadata.snapshot_id)

    payload_path = published.path / "security_master.json"
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    payload["records"][0]["name"] = "被篡改"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    fresh_loader = TrustedSecurityMasterSnapshotLoader(
        tmp_path,
        max_age=timedelta(days=7),
        clock=lambda: datetime(2026, 8, 31, 8, tzinfo=UTC),
    )
    with pytest.raises(TrustedSnapshotIntegrityError, match="hash mismatch"):
        fresh_loader.load(published.metadata.snapshot_id)


def test_composite_loader_reuses_registry_for_market_and_calendar(tmp_path: Path) -> None:
    registry_root = tmp_path / "registry"
    composite_path = _publish_composite(
        registry_root,
        tmp_path / "source",
        instrument=InstrumentId("300059.SZ"),
        start=date(2025, 1, 2),
        end=date(2025, 1, 10),
    )
    manifest = json.loads((composite_path / "snapshot_manifest.json").read_text())
    producer_snapshot_id = manifest["snapshotId"]
    security_master = _load_security_master(tmp_path / "security-master")

    loader = TrustedTechnicalSnapshotLoader(
        composite_root=registry_root,
        max_age=timedelta(days=7),
        clock=lambda: datetime(2025, 2, 2, 10, tzinfo=UTC),
    )
    metadata = loader.load_market_metadata(producer_snapshot_id)
    loaded = loader.load(
        producer_snapshot_id=producer_snapshot_id,
        instrument_id=InstrumentId("300059.SZ"),
        period=DateRange(date(2025, 1, 2), date(2025, 1, 10)),
        security_master=security_master,
    )

    assert metadata == loaded.market_data_metadata
    assert metadata.coverage.end == date(2025, 1, 10)
    assert loaded.market_data_metadata.provider == "Choice Quant API"
    assert loaded.calendar_metadata.provider == "BaoStock Python API"
    assert loaded.market_data_metadata.coverage == DateRange(date(2025, 1, 2), date(2025, 1, 10))
    assert loaded.snapshot_ref.producer_snapshot_id == producer_snapshot_id
    assert loaded.execution_bars
    assert loaded.signal_bars
    assert loaded.corporate_actions == ()
    assert {item.session_date for item in loaded.sessions} == {
        item.session_date for item in loaded.execution_bars
    }
    contracts = build_trusted_v2_snapshot_contracts(
        security_master=security_master,
        technical_snapshot=loaded,
        instrument_id=InstrumentId("300059.SZ"),
        period=DateRange(date(2025, 1, 2), date(2025, 1, 10)),
    )
    assert contracts.security_master.snapshot_id == security_master.metadata.snapshot_id
    assert contracts.trading_calendar.snapshot_id == loaded.calendar_metadata.snapshot_id
    assert contracts.market_data.snapshot_id == loaded.market_data_metadata.snapshot_id
    assert {item.snapshot_id for item in contracts.producer_children} == {
        item.snapshot_id for item in loaded.producer_children
    }
    assert {item.snapshot_id.split(":", maxsplit=1)[0] for item in contracts.producer_children} == {
        "choice",
        "events",
    }
    assert contracts.dataset_coverage.source_refs[0].provider == "Choice Quant API"
    assert (
        contracts.dataset_coverage.source_refs[0].content_sha256
        == loaded.market_data_metadata.content_hash
    )

    original_input = "MACD金叉买入，死叉卖出"
    instrument = InstrumentResolver(security_master.snapshot).resolve(
        "300059.SZ",
        as_of=date(2025, 1, 2),
    )
    entry = TechnicalConditionV2(
        indicator_id="technical.macd",
        definition_version="1.0.0",
        params={"fast": 12, "slow": 26, "signal": 9},
        trigger="golden_cross",
    )
    exit_condition = TechnicalConditionV2(
        indicator_id="technical.macd",
        definition_version="1.0.0",
        params={"fast": 12, "slow": 26, "signal": 9},
        trigger="death_cross",
    )
    backtest = BacktestConfigV2(
        start=date(2025, 1, 2),
        end=date(2025, 1, 10),
        initial_cash_cny=1_000_000,
    )
    strategy = StrategySpecV2(
        catalog=CatalogRefV2(
            catalog_id="cn_a.signals",
            release_version="2026.09.01",
        ),
        instrument=instrument,
        entry=entry,
        exit=exit_condition,
        interpretation_coverage=InterpretationCoverage(
            groundings=(
                ConditionGrounding(
                    dsl_path="$.entry",
                    source_start=0,
                    source_end=8,
                    source_text="MACD金叉买入",
                ),
                ConditionGrounding(
                    dsl_path="$.exit",
                    source_start=9,
                    source_end=13,
                    source_text="死叉卖出",
                ),
            )
        ),
        backtest=backtest,
    )
    candidate = StrategyCandidateV2(
        candidate_type="strategy_candidate.v2",
        original_input=original_input,
        draft_id="draft:trusted-snapshot-integration",
        revision=1,
        provider="rule_based",
        strategy=strategy,
    )
    context = StrategyV2ValidationContext(
        security_master=security_master.snapshot,
        original_input=original_input,
        draft_id="draft:trusted-snapshot-integration",
        revision=1,
        provider="rule_based",
        requested_instrument="300059.SZ",
        expected_backtest=backtest,
        catalog=load_catalog_directory(Path(__file__).resolve().parents[3] / "catalogs"),
        dataset_coverage=(contracts.dataset_coverage,),
        grounding_expectations=(
            ConditionGroundingExpectation(
                dsl_path="$.entry",
                source_start=0,
                source_end=8,
                source_text="MACD金叉买入",
                condition_hash=condition_semantics_hash(entry),
            ),
            ConditionGroundingExpectation(
                dsl_path="$.exit",
                source_start=9,
                source_end=13,
                source_text="死叉卖出",
                condition_hash=condition_semantics_hash(exit_condition),
            ),
        ),
        code_revision="1" * 40,
        trading_calendar_snapshot_id=contracts.trading_calendar.snapshot_id,
        composite_snapshot_id=contracts.composite_snapshot.snapshot_id,
        snapshot_bindings_hash=snapshot_bindings_hash(
            (
                contracts.security_master,
                contracts.trading_calendar,
                contracts.market_data,
                *contracts.producer_children,
                contracts.composite_snapshot,
            )
        ),
    )
    plan = validate_strategy_candidate_v2(candidate, context)
    assert plan.strategy.instrument.symbol == "300059.SZ"

    future_bar = replace(
        loaded.execution_bars[-1],
        available_at=loaded.market_data_metadata.generated_at + timedelta(seconds=1),
    )
    poisoned = replace(
        loaded,
        execution_bars=(*loaded.execution_bars[:-1], future_bar),
    )
    with pytest.raises(TrustedSnapshotIntegrityError, match="follows snapshot"):
        build_trusted_v2_snapshot_contracts(
            security_master=security_master,
            technical_snapshot=poisoned,
            instrument_id=InstrumentId("300059.SZ"),
            period=DateRange(date(2025, 1, 2), date(2025, 1, 10)),
        )


def test_fresh_composite_cannot_refresh_stale_market_or_calendar_source(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    stale_captured_at = datetime(2025, 1, 15, 8, tzinfo=UTC)
    choice_path = _publish_choice(
        source_root / "choice",
        instrument=InstrumentId("300059.SZ"),
        start=date(2025, 1, 2),
        end=date(2025, 1, 10),
        captured_at=stale_captured_at,
    )
    events = build_no_event_required_snapshot(
        instrument_id=InstrumentId("300059.SZ"),
        start=date(2025, 1, 2),
        end=date(2025, 1, 10),
        output_root=source_root / "events",
        captured_at=datetime(2025, 2, 1, 8, tzinfo=UTC),
    )
    composite = compose_choice_event_snapshot(
        choice_snapshot_path=choice_path,
        event_snapshot_path=events.path,
        output_root=tmp_path / "registry",
        composed_at=datetime(2025, 2, 1, 10, tzinfo=UTC),
    )
    producer_snapshot_id = json.loads(
        (composite.path / "snapshot_manifest.json").read_text(encoding="utf-8")
    )["snapshotId"]

    loader = TrustedTechnicalSnapshotLoader(
        composite_root=tmp_path / "registry",
        max_age=timedelta(days=1),
        # The wrapper is less than one day old, but its immutable Choice source
        # (including the calendar evidence) is more than two weeks old.
        clock=lambda: datetime(2025, 2, 2, 8, tzinfo=UTC),
    )
    with pytest.raises(TrustedSnapshotExpiredError, match="market_data"):
        loader.load(
            producer_snapshot_id=producer_snapshot_id,
            instrument_id=InstrumentId("300059.SZ"),
            period=DateRange(date(2025, 1, 2), date(2025, 1, 10)),
            security_master=_load_security_master(tmp_path / "security-master"),
        )


def test_calendar_freshness_prefers_its_own_acquisition_clock() -> None:
    source_manifest: dict[str, object] = {
        "schemaVersion": "choice.daily-research-snapshot.v1",
        "provider": "Choice Quant API",
        "requestedRange": ["2025-01-02", "2025-01-10"],
        "capturedAt": "2025-02-01T08:00:00+00:00",
        "sessionReference": {
            "provider": "BaoStock Python API",
            "queriedAt": "2025-01-15T08:00:00+00:00",
            "coverage": {"start": "2025-01-02", "end": "2025-01-10"},
        },
    }
    manifest = {
        "composedAt": "2025-02-02T08:00:00+00:00",
        "files": {
            "daily_ohlcv.parquet": {"sha256": "a" * 64},
            "signal_daily_ohlcv.parquet": {"sha256": "b" * 64},
            "corporate_actions.parquet": {"sha256": "c" * 64},
            "instrument_sessions.parquet": {"sha256": "d" * 64},
        },
    }

    market, calendar = trusted_snapshots_module._technical_metadata(  # pyright: ignore[reportPrivateUsage]
        producer_snapshot_id="composite:" + "e" * 64,
        manifest=manifest,
        source_manifest=source_manifest,
    )

    assert market.generated_at == datetime(2025, 2, 1, 8, tzinfo=UTC)
    assert calendar.generated_at == datetime(2025, 1, 15, 8, tzinfo=UTC)
    with pytest.raises(TrustedSnapshotExpiredError, match="trading_calendar"):
        trusted_snapshots_module._require_fresh(  # pyright: ignore[reportPrivateUsage]
            calendar,
            max_age=timedelta(days=1),
            clock=lambda: datetime(2025, 2, 2, 8, tzinfo=UTC),
        )


def test_composite_loader_rejects_path_stale_hash_and_insufficient_coverage(
    tmp_path: Path,
) -> None:
    registry_root = tmp_path / "registry"
    composite_path = _publish_composite(
        registry_root,
        tmp_path / "source",
        instrument=InstrumentId("300059.SZ"),
        start=date(2025, 1, 2),
        end=date(2025, 1, 10),
    )
    producer_snapshot_id = json.loads((composite_path / "snapshot_manifest.json").read_text())[
        "snapshotId"
    ]
    loader = TrustedTechnicalSnapshotLoader(
        composite_root=registry_root,
        max_age=timedelta(days=7),
        clock=lambda: datetime(2025, 2, 2, 10, tzinfo=UTC),
    )
    security_master = _load_security_master(tmp_path / "security-master")

    with pytest.raises(TrustedSnapshotIntegrityError, match="snapshot id"):
        loader.load(
            producer_snapshot_id=str(composite_path),
            instrument_id=InstrumentId("300059.SZ"),
            period=DateRange(date(2025, 1, 2), date(2025, 1, 10)),
            security_master=security_master,
        )
    with pytest.raises(TrustedSnapshotCoverageError, match="cover"):
        loader.load(
            producer_snapshot_id=producer_snapshot_id,
            instrument_id=InstrumentId("300059.SZ"),
            period=DateRange(date(2024, 12, 31), date(2025, 1, 10)),
            security_master=security_master,
        )

    stale_loader = TrustedTechnicalSnapshotLoader(
        composite_root=registry_root,
        max_age=timedelta(days=1),
        clock=lambda: datetime(2025, 2, 10, 10, tzinfo=UTC),
    )
    with pytest.raises(TrustedSnapshotExpiredError, match="expired"):
        stale_loader.load(
            producer_snapshot_id=producer_snapshot_id,
            instrument_id=InstrumentId("300059.SZ"),
            period=DateRange(date(2025, 1, 2), date(2025, 1, 10)),
            security_master=security_master,
        )

    action_path = composite_path / "corporate_actions.parquet"
    original_action_bytes = action_path.read_bytes()
    action_path.write_bytes(b"tampered")
    with pytest.raises(TrustedSnapshotIntegrityError, match="mismatch"):
        loader.load(
            producer_snapshot_id=producer_snapshot_id,
            instrument_id=InstrumentId("300059.SZ"),
            period=DateRange(date(2025, 1, 2), date(2025, 1, 10)),
            security_master=security_master,
        )
    action_path.write_bytes(original_action_bytes)

    (composite_path / "daily_ohlcv.parquet").write_bytes(b"tampered")
    with pytest.raises(TrustedSnapshotIntegrityError, match="mismatch"):
        loader.load(
            producer_snapshot_id=producer_snapshot_id,
            instrument_id=InstrumentId("300059.SZ"),
            period=DateRange(date(2025, 1, 2), date(2025, 1, 10)),
            security_master=security_master,
        )


def test_choice_loader_returns_actions_from_the_same_pinned_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shanghai = ZoneInfo("Asia/Shanghai")
    action = CorporateAction(
        action_id=StrongId("action:cash-dividend:2025"),
        source_action_id="source:cash-dividend:2025",
        instrument_id=InstrumentId("300059.SZ"),
        action_type=CorporateActionKind.CASH_DIVIDEND,
        record_date=date(2025, 1, 3),
        ex_date=date(2025, 1, 6),
        source_released_at=datetime(2025, 1, 3, 10, 0, tzinfo=shanghai),
        vendor_first_available_at=datetime(2025, 1, 3, 10, 1, tzinfo=shanghai),
        ingested_at=datetime(2025, 1, 3, 10, 3, tzinfo=shanghai),
        replay_available_at=datetime(2025, 1, 3, 10, 2, tzinfo=shanghai),
        revision_no=0,
        time_quality=TimeQuality.EXACT,
        provider="fixture-source",
        source_url="https://example.test/corporate-action",
        raw_response_sha256="c" * 64,
        validation_status="validated",
        gross_cash_per_share=Decimal("0.5"),
        cash_pay_date=date(2025, 1, 7),
    )
    monkeypatch.setattr(
        snapshot_registry_fixtures,
        "build_choice_snapshot",
        partial(snapshot_registry_fixtures.build_choice_snapshot, corporate_actions=(action,)),
    )
    choice_root = tmp_path / "choice"
    choice_path = _publish_choice(
        choice_root,
        instrument=InstrumentId("300059.SZ"),
        start=date(2025, 1, 2),
        end=date(2025, 1, 10),
    )
    producer_snapshot_id = json.loads((choice_path / "snapshot_manifest.json").read_text())[
        "snapshotId"
    ]
    composite_root = tmp_path / "composite"
    composite_root.mkdir()
    loaded = TrustedTechnicalSnapshotLoader(
        composite_root=composite_root,
        choice_root=choice_root,
        max_age=timedelta(days=7),
        clock=lambda: datetime(2025, 2, 2, 10, tzinfo=UTC),
    ).load(
        producer_snapshot_id=producer_snapshot_id,
        instrument_id=InstrumentId("300059.SZ"),
        period=DateRange(date(2025, 1, 2), date(2025, 1, 10)),
        security_master=_load_security_master(tmp_path / "security-master"),
    )

    assert loaded.corporate_actions == (action,)


def test_etf_requires_security_master_allowlist_and_index_stays_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    security_master = _load_security_master(
        tmp_path / "security-master",
        include_etf_and_index=True,
    )
    normalizer = SecurityMasterInstrumentNormalizer(security_master)
    monkeypatch.setattr(
        choice_snapshot_module,
        "normalize_instrument_id",
        normalizer,
    )
    choice_root = tmp_path / "choice"
    choice_path = _publish_choice(
        choice_root,
        instrument=InstrumentId("510300.SH"),
        start=date(2025, 1, 2),
        end=date(2025, 1, 10),
    )
    producer_snapshot_id = json.loads((choice_path / "snapshot_manifest.json").read_text())[
        "snapshotId"
    ]
    composite_root = tmp_path / "composite"
    composite_root.mkdir()

    # The low-level Parquet key parser now accepts the explicit SH/SZ ETF code
    # spaces.  That is not execution authority: the trusted loader below still
    # requires an exact, tradable ETF record in the server-owned master.
    low_level = LocalParquetMarketDataRepository(
        choice_path,
        profile="choice_snapshot",
    ).pin_snapshot(
        DataRequirements(
            instruments=(InstrumentId("510300.SH"),),
            datasets=("daily_ohlcv", "corporate_actions"),
        ),
        DateRange(date(2025, 1, 2), date(2025, 1, 10)),
    )
    assert str(low_level.snapshot_id).startswith("snapshot:")
    assert low_level.checksum.startswith("sha256:")

    loader = TrustedTechnicalSnapshotLoader(
        composite_root=composite_root,
        choice_root=choice_root,
        max_age=timedelta(days=7),
        clock=lambda: datetime(2025, 2, 2, 10, tzinfo=UTC),
    )
    with pytest.raises(TrustedSnapshotCoverageError, match="确认标的"):
        loader.load(
            producer_snapshot_id=producer_snapshot_id,
            instrument_id=InstrumentId("510300.SH"),
            period=DateRange(date(2025, 1, 2), date(2025, 1, 10)),
            security_master=_load_security_master(tmp_path / "stock-only-master"),
        )

    loaded = loader.load(
        producer_snapshot_id=producer_snapshot_id,
        instrument_id=InstrumentId("510300.SH"),
        period=DateRange(date(2025, 1, 2), date(2025, 1, 10)),
        security_master=security_master,
    )
    assert loaded.execution_bars[0].instrument_id == InstrumentId("510300.SH")
    assert loaded.sessions[0].instrument_id == InstrumentId("510300.SH")

    with pytest.raises(TrustedSnapshotCoverageError, match="指数"):
        loader.load(
            producer_snapshot_id=producer_snapshot_id,
            instrument_id=InstrumentId("000300.SH"),
            period=DateRange(date(2025, 1, 2), date(2025, 1, 10)),
            security_master=security_master,
        )
