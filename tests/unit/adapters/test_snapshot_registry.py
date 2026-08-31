from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import pytest

from ashare_lab.adapters.event_sources import (
    EventCollectionRequest,
    EventCollectionResult,
    EventSourceCollection,
    build_event_acquisition_coverage,
)
from ashare_lab.adapters.market_data import (
    LocalParquetMarketDataRepository,
    SnapshotIntegrityError,
    SnapshotRegistryAmbiguityError,
    SnapshotRegistryIntegrityError,
    SnapshotRegistryMarketDataRepository,
    SnapshotRegistryNoMatchError,
    compose_choice_event_snapshot,
)
from ashare_lab.adapters.market_data.choice_snapshot import (
    MIXED_CORPORATE_ACTION_COVERAGE_SCOPE,
    STRICT_CORPORATE_ACTION_CATEGORIES,
    STRICT_CORPORATE_ACTION_COVERAGE_SCOPE,
    TECHNICAL_SNAPSHOT_SCHEMA_VERSION,
    ChoiceSnapshotSpec,
    DailySnapshotSource,
    build_choice_snapshot,
)
from ashare_lab.adapters.market_data.event_snapshot import (
    build_event_snapshot,
    build_no_event_required_snapshot,
)
from ashare_lab.adapters.market_data.on_demand_snapshot import (
    OnDemandSnapshotMarketDataRepository,
    SnapshotPreparationResult,
)
from ashare_lab.domain.market_data import Board, DataSnapshotRef
from ashare_lab.domain.shared import DomainValidationError, InstrumentId, StrongId
from ashare_lab.ports.market_data import DataRequirements, DateRange
from tests.unit.adapters.event_sources.query_evidence import eastmoney_query_evidence
from tests.unit.adapters.session_reference_fixture import baostock_session_reference

SHANGHAI = ZoneInfo("Asia/Shanghai")
ANNUAL = "event.financial_results.annual_report"
SEMIANNUAL = "event.financial_results.semiannual_report"


def _publish_choice(
    registry_root: Path,
    *,
    instrument: InstrumentId,
    start: date,
    end: date,
    variant: int = 0,
    captured_at: datetime = datetime(2025, 2, 1, tzinfo=UTC),
    source: DailySnapshotSource | None = None,
    corporate_action_coverage: dict[str, object] | None = None,
) -> Path:
    trading_dates = _weekdays(start, end)
    execution_rows: list[dict[str, object]] = []
    signal_rows: list[dict[str, object]] = []
    for offset, trading_date in enumerate(trading_dates):
        close = Decimal("10.0") + Decimal(offset) / Decimal(10)
        execution_rows.append(
            {
                "date": trading_date.isoformat(),
                "open": close,
                "high": close + Decimal("0.2"),
                "low": close - Decimal("0.2"),
                "close": close + Decimal("0.1"),
                "preclose": close - Decimal("0.1"),
                "volume": 1_000 + offset,
                "amount": 10_000 + offset,
                "highlimit": "否",
                "lowlimit": "否",
                "tradestatus": "正常交易",
            }
        )
        signal_rows.append(
            {
                "date": trading_date.isoformat(),
                "open": close * 2,
                "high": (close + Decimal("0.2")) * 2,
                "low": (close - Decimal("0.2")) * 2,
                "close": (close + Decimal("0.1")) * 2,
            }
        )
    if source is not None and not source.limit_event_flags_available:
        execution_rows = [
            {key: value for key, value in row.items() if key not in {"highlimit", "lowlimit"}}
            for row in execution_rows
        ]
    board = Board.CHINEXT if instrument.value.startswith("3") else Board.MAIN
    session_rows, session_coverage = baostock_session_reference(
        symbol=instrument.value,
        start=start,
        end=end,
        execution_rows=execution_rows,
    )
    build_kwargs: dict[str, object] = {}
    if source is not None:
        build_kwargs["source"] = source
    return build_choice_snapshot(
        spec=ChoiceSnapshotSpec(
            symbol=instrument.value,
            start=start,
            end=end,
            listing_date=date(2000, 1, 1),
            board=board,
        ),
        execution_rows=execution_rows,
        signal_rows=signal_rows,
        market_calendar=trading_dates,
        raw_audit_payload={"responses": [f"fixture-{instrument}-{variant}"]},
        request_audit={"AdjustFlag": [1, 2]},
        prefix_stability={"status": "passed", "overlapRows": len(trading_dates)},
        output_root=registry_root,
        captured_at=captured_at,
        sdk_archive_sha256="a" * 64,
        session_reference_rows=session_rows,
        session_reference_coverage=session_coverage,
        corporate_action_coverage=corporate_action_coverage
        or {
            "status": "complete",
            "querySucceeded": True,
            "provider": "fixture-source",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "rawResponseSha256": "b" * 64,
            "coverageScope": STRICT_CORPORATE_ACTION_COVERAGE_SCOPE,
            "supportedCategories": list(STRICT_CORPORATE_ACTION_CATEGORIES),
            "unsupportedCategories": [],
        },
        **build_kwargs,
    ).path


def _publish_composite(
    registry_root: Path,
    source_root: Path,
    *,
    instrument: InstrumentId,
    start: date,
    end: date,
    event_codes: tuple[str, ...] = (ANNUAL,),
    no_event_required: bool = False,
    variant: int = 0,
    composed_minute: int | None = None,
    source: DailySnapshotSource | None = None,
    corporate_action_coverage: dict[str, object] | None = None,
) -> Path:
    choice_path = _publish_choice(
        source_root / "choice",
        instrument=instrument,
        start=start,
        end=end,
        variant=variant,
        source=source,
        corporate_action_coverage=corporate_action_coverage,
    )
    if no_event_required:
        events = build_no_event_required_snapshot(
            instrument_id=instrument,
            start=start,
            end=end,
            output_root=source_root / "events",
            captured_at=datetime(2025, 2, 1, 9, variant, tzinfo=SHANGHAI),
        )
    else:
        source = EventSourceCollection(
            provider="eastmoney",
            status="empty",
            observations=(),
            acquisition_evidence=eastmoney_query_evidence(
                instrument_id=instrument,
                start=start,
                end=end,
                event_codes=event_codes,
                total_hits=0,
            ),
        )
        request = EventCollectionRequest(
            instrument_id=instrument,
            start=start,
            end=end,
            retrieved_at=datetime(2025, 2, 1, 8, 0, tzinfo=SHANGHAI),
        )
        coverage = build_event_acquisition_coverage(
            request,
            EventCollectionResult(observations=(), sources=(source,)),
            requested_event_codes=event_codes,
        )
        events = build_event_snapshot(
            observations=(),
            acquisition_coverage=coverage,
            output_root=source_root / "events",
            captured_at=datetime(2025, 2, 1, 9, variant, tzinfo=SHANGHAI),
        )
    composite = compose_choice_event_snapshot(
        choice_snapshot_path=choice_path,
        event_snapshot_path=events.path,
        output_root=registry_root,
        composed_at=datetime(
            2025,
            2,
            1,
            10,
            variant if composed_minute is None else composed_minute,
            tzinfo=SHANGHAI,
        ),
    )
    return composite.path


def _push2_source() -> DailySnapshotSource:
    return DailySnapshotSource(
        schema_version=TECHNICAL_SNAPSHOT_SCHEMA_VERSION,
        snapshot_prefix="technical",
        provider="Eastmoney Push2His public endpoint",
        dataset="stock_kline_day",
        account_scope="public_undocumented_research_demo",
        adjustment_field="eastmoneyFqt",
        execution_adjustment=0,
        signal_adjustment=2,
        acquisition_implementation="AKShare-compatible Push2His thin adapter",
        limit_event_flags_available=False,
        status_cross_check="BaoStock supplies and validates historical trading status",
        previous_close_cross_check="Push2 implied preclose equals BaoStock per date",
        limitations=("public undocumented endpoint; not authorized for production use",),
    )


def _mixed_corporate_action_coverage(start: date, end: date) -> dict[str, object]:
    counts = {category: 0 for category in STRICT_CORPORATE_ACTION_CATEGORIES}
    positive_categories = ("cash_dividend", "rights_issue", "share_distribution")
    negative_categories = ("reverse_split", "stock_split")
    return {
        "status": "complete_mixed_mode",
        "querySucceeded": True,
        "provider": "Eastmoney public datasets",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "rowCount": 0,
        "zeroResult": True,
        "rawResponseSha256": "d" * 64,
        "coverageScope": MIXED_CORPORATE_ACTION_COVERAGE_SCOPE,
        "positiveCapableCategories": list(positive_categories),
        "negativeProofCategories": list(negative_categories),
        "unsupportedCategories": [],
        "strictEligibleUnderCurrentChoiceValidator": True,
        "categoryActionCounts": counts,
        "categoryCoverage": {
            **{
                category: {
                    "status": "complete_for_filtered_dataset",
                    "dataset": f"fixture-{category}",
                    "zeroResult": True,
                }
                for category in positive_categories
            },
            **{
                category: {
                    "status": "complete",
                    "categoryMode": "complete_negative_proof",
                    "dataset": "RPT_F10_EH_EQUITY",
                    "candidateCount": 0,
                    "zeroResult": True,
                }
                for category in negative_categories
            },
        },
        "negativeSplitProof": {
            "categoryMode": "complete_negative_proof",
            "queryScope": "full_instrument_history_filtered_locally_to_requested_interval",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "scannedRows": 1,
            "recognizedChangeReasons": ["高管股份变动"],
            "stockSplitCandidates": 0,
            "reverseSplitCandidates": 0,
            "sourceDataset": "RPT_F10_EH_EQUITY",
            "sourceDeclaredCount": 1,
            "sourceTotalPages": 1,
            "sourceRawResponseSha256": "e" * 64,
        },
        "timeQuality": "date_only_conservative",
        "dateAvailabilityPolicy": "implementation notice date @ 15:00:00 Asia/Shanghai",
        "hashSemantics": "fixture raw body aggregate",
    }


def _weekdays(start: date, end: date) -> list[date]:
    values: list[date] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            values.append(current)
        current += timedelta(days=1)
    if not values:
        raise AssertionError("fixture period must contain at least one weekday")
    return values


def _technical_requirements(instrument: InstrumentId) -> DataRequirements:
    return DataRequirements(
        instruments=(instrument,),
        datasets=("daily_ohlcv", "corporate_actions"),
    )


def _event_requirements(
    instrument: InstrumentId,
    *event_codes: str,
) -> DataRequirements:
    return DataRequirements(
        instruments=(instrument,),
        datasets=("daily_ohlcv", "corporate_actions", "events"),
        event_codes=tuple(event_codes),
    )


def _direct_checksum(
    snapshot_root: Path,
    requirements: DataRequirements,
    period: DateRange,
    *,
    profile: Literal[
        "choice_snapshot",
        "technical_snapshot",
        "composite_snapshot",
    ] = "composite_snapshot",
) -> str:
    repository = LocalParquetMarketDataRepository(
        snapshot_root,
        profile=profile,
    )
    return repository.pin_snapshot(requirements, period).checksum


def _expecting(
    requirements: DataRequirements,
    snapshot: DataSnapshotRef,
) -> DataRequirements:
    return replace(
        requirements,
        expected_snapshot_id=snapshot.snapshot_id,
        expected_snapshot_checksum=snapshot.checksum,
        expected_snapshot_schema_version=snapshot.schema_version,
        expected_producer_snapshot_schema_version=snapshot.producer_schema_version,
        expected_producer_snapshot_id=snapshot.producer_snapshot_id,
    )


class _RejectingPreparer:
    def __init__(self) -> None:
        self.calls = 0

    def prepare(
        self,
        requirements: DataRequirements,
        period: DateRange,
    ) -> SnapshotPreparationResult:
        del requirements, period
        self.calls += 1
        raise AssertionError("worker replay must not invoke the snapshot preparer")


def test_routes_multiple_instruments_and_reindexes_after_restart(tmp_path: Path) -> None:
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    first = InstrumentId("300059.SZ")
    second = InstrumentId("600519.SH")
    start = date(2025, 1, 2)
    end = date(2025, 1, 3)
    _publish_composite(
        registry_root,
        tmp_path / "source-first",
        instrument=first,
        start=start,
        end=end,
    )
    _publish_composite(
        registry_root,
        tmp_path / "source-second",
        instrument=second,
        start=start,
        end=end,
    )
    period = DateRange(start, end)

    before_restart = SnapshotRegistryMarketDataRepository(registry_root)
    old_ref = before_restart.pin_snapshot(_technical_requirements(second), period)
    before_bars = before_restart.load_daily_bars(old_ref, second, period)
    assert {bar.instrument_id for bar in before_bars} == {second}

    after_restart = SnapshotRegistryMarketDataRepository(registry_root)
    fresh_ref = after_restart.pin_snapshot(_technical_requirements(second), period)

    assert fresh_ref.checksum == old_ref.checksum
    assert fresh_ref.snapshot_id == old_ref.snapshot_id
    after_bars = after_restart.load_daily_bars(old_ref, second, period)
    assert {bar.instrument_id for bar in after_bars} == {second}


def test_registry_routes_composite_with_provider_neutral_technical_source(
    tmp_path: Path,
) -> None:
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    instrument = InstrumentId("300059.SZ")
    period = DateRange(date(2025, 1, 2), date(2025, 1, 3))
    _publish_composite(
        registry_root,
        tmp_path / "source-push2",
        instrument=instrument,
        start=period.start,
        end=period.end,
        no_event_required=True,
        source=_push2_source(),
    )

    repository = SnapshotRegistryMarketDataRepository(registry_root)
    selected = repository.pin_snapshot(_technical_requirements(instrument), period)

    assert selected.producer_schema_version == "ashare-lab.composite-research-snapshot.v2"
    assert repository.load_daily_bars(selected, instrument, period)


def test_chooses_unique_smallest_range_and_event_code_coverage(tmp_path: Path) -> None:
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    instrument = InstrumentId("300059.SZ")
    request_period = DateRange(date(2025, 1, 2), date(2025, 1, 3))
    wider = _publish_composite(
        registry_root,
        tmp_path / "source-wide",
        instrument=instrument,
        start=date(2024, 12, 30),
        end=date(2025, 1, 3),
        event_codes=(ANNUAL, SEMIANNUAL),
    )
    smallest = _publish_composite(
        registry_root,
        tmp_path / "source-small",
        instrument=instrument,
        start=request_period.start,
        end=request_period.end,
        event_codes=(ANNUAL,),
    )
    repository = SnapshotRegistryMarketDataRepository(registry_root)

    technical = _technical_requirements(instrument)
    annual = _event_requirements(instrument, ANNUAL)
    semiannual = _event_requirements(instrument, SEMIANNUAL)

    assert repository.pin_snapshot(technical, request_period).checksum == _direct_checksum(
        smallest, technical, request_period
    )
    assert repository.pin_snapshot(annual, request_period).checksum == _direct_checksum(
        smallest, annual, request_period
    )
    assert repository.pin_snapshot(semiannual, request_period).checksum == _direct_checksum(
        wider, semiannual, request_period
    )


def test_technical_request_does_not_require_event_codes(tmp_path: Path) -> None:
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    instrument = InstrumentId("600519.SH")
    start = date(2025, 1, 2)
    end = date(2025, 1, 3)
    _publish_composite(
        registry_root,
        tmp_path / "source",
        instrument=instrument,
        start=start,
        end=end,
        event_codes=(ANNUAL,),
    )
    repository = SnapshotRegistryMarketDataRepository(registry_root)
    period = DateRange(start, end)

    snapshot = repository.pin_snapshot(_technical_requirements(instrument), period)

    assert len(repository.load_daily_bars(snapshot, instrument, period)) == 2
    assert repository.load_corporate_actions(snapshot, instrument, period) == ()


def test_technical_only_composite_is_selectable_but_never_satisfies_event_request(
    tmp_path: Path,
) -> None:
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    instrument = InstrumentId("600519.SH")
    period = DateRange(date(2025, 1, 2), date(2025, 1, 3))
    snapshot_root = _publish_composite(
        registry_root,
        tmp_path / "source-no-event",
        instrument=instrument,
        start=period.start,
        end=period.end,
        no_event_required=True,
    )
    repository = SnapshotRegistryMarketDataRepository(registry_root)

    snapshot = repository.pin_snapshot(_technical_requirements(instrument), period)

    assert snapshot.producer_schema_version == "ashare-lab.composite-research-snapshot.v2"
    assert len(repository.load_daily_bars(snapshot, instrument, period)) == 2
    manifest = json.loads((snapshot_root / "snapshot_manifest.json").read_text(encoding="utf-8"))
    assert manifest["capabilities"]["events"] == "not_required"
    with pytest.raises(SnapshotRegistryNoMatchError, match="no registered snapshot"):
        repository.pin_snapshot(_event_requirements(instrument, ANNUAL), period)


def test_ten_year_technical_request_can_select_choice_only_snapshot(tmp_path: Path) -> None:
    composite_root = tmp_path / "composite"
    choice_root = tmp_path / "choice"
    composite_root.mkdir()
    choice_root.mkdir()
    instrument = InstrumentId("600519.SH")
    period = DateRange(date(2015, 1, 2), date(2025, 1, 3))
    choice_snapshot = _publish_choice(
        choice_root,
        instrument=instrument,
        start=period.start,
        end=period.end,
    )
    requirements = _technical_requirements(instrument)
    repository = SnapshotRegistryMarketDataRepository(
        composite_root,
        choice_root=choice_root,
    )

    selected = repository.pin_snapshot(requirements, period)
    bars = repository.load_daily_bars(selected, instrument, period)

    assert selected.checksum == _direct_checksum(
        choice_snapshot,
        requirements,
        period,
        profile="choice_snapshot",
    )
    assert len(bars) > 2_500


def test_event_request_never_falls_back_to_choice_only_snapshot(tmp_path: Path) -> None:
    composite_root = tmp_path / "composite"
    choice_root = tmp_path / "choice"
    composite_root.mkdir()
    choice_root.mkdir()
    instrument = InstrumentId("300059.SZ")
    period = DateRange(date(2025, 1, 2), date(2025, 1, 3))
    _publish_choice(
        choice_root,
        instrument=instrument,
        start=period.start,
        end=period.end,
    )
    repository = SnapshotRegistryMarketDataRepository(
        composite_root,
        choice_root=choice_root,
    )

    with pytest.raises(SnapshotRegistryNoMatchError, match="no registered snapshot covers"):
        repository.pin_snapshot(_event_requirements(instrument, ANNUAL), period)


def test_expected_choice_snapshot_replays_after_registry_restart(tmp_path: Path) -> None:
    composite_root = tmp_path / "composite"
    choice_root = tmp_path / "choice"
    composite_root.mkdir()
    choice_root.mkdir()
    instrument = InstrumentId("600519.SH")
    period = DateRange(date(2024, 12, 30), date(2025, 1, 3))
    _publish_choice(
        choice_root,
        instrument=instrument,
        start=period.start,
        end=period.end,
    )
    requirements = _technical_requirements(instrument)
    before_restart = SnapshotRegistryMarketDataRepository(
        composite_root,
        choice_root=choice_root,
    )
    old_ref = before_restart.pin_snapshot(requirements, period)

    after_restart = SnapshotRegistryMarketDataRepository(
        composite_root,
        choice_root=choice_root,
    )
    replay_ref = after_restart.pin_snapshot(_expecting(requirements, old_ref), period)

    assert replay_ref.snapshot_id == old_ref.snapshot_id
    assert replay_ref.checksum == old_ref.checksum
    assert len(after_restart.load_daily_bars(old_ref, instrument, period)) == 5
    assert len(after_restart.load_sessions(old_ref, instrument, period)) == 5


def test_on_demand_worker_restart_pins_exact_v2_beside_bad_legacy_and_rejects_bad_target(
    tmp_path: Path,
) -> None:
    composite_root = tmp_path / "composite"
    composite_root.mkdir()
    instrument = InstrumentId("600519.SH")
    period = DateRange(date(2024, 12, 30), date(2025, 1, 3))
    selected_root = _publish_composite(
        composite_root,
        tmp_path / "selected-sources",
        instrument=instrument,
        start=period.start,
        end=period.end,
        no_event_required=True,
    )
    requirements = _technical_requirements(instrument)
    submitted_ref = SnapshotRegistryMarketDataRepository(composite_root).pin_snapshot(
        requirements,
        period,
    )
    assert submitted_ref.producer_snapshot_id == f"composite:{selected_root.name}"

    legacy_root = composite_root / ("0" * 64)
    legacy_root.mkdir()
    (legacy_root / "snapshot_manifest.json").write_text(
        json.dumps({"schemaVersion": "ashare-lab.composite-research-snapshot.v1"}),
        encoding="utf-8",
    )
    preparer = _RejectingPreparer()
    restarted_registry = SnapshotRegistryMarketDataRepository(composite_root)
    restarted = OnDemandSnapshotMarketDataRepository(restarted_registry, preparer)

    replay_ref = restarted.pin_snapshot(_expecting(requirements, submitted_ref), period)

    assert replay_ref.snapshot_id == submitted_ref.snapshot_id
    assert replay_ref.checksum == submitted_ref.checksum
    assert replay_ref.schema_version == submitted_ref.schema_version
    assert replay_ref.producer_schema_version == submitted_ref.producer_schema_version
    assert replay_ref.producer_snapshot_id == submitted_ref.producer_snapshot_id
    assert len(restarted.load_daily_bars(replay_ref, instrument, period)) == 5
    assert preparer.calls == 0

    (selected_root / "daily_ohlcv.parquet").write_bytes(b"tampered")
    corrupt_target_worker = OnDemandSnapshotMarketDataRepository(
        SnapshotRegistryMarketDataRepository(composite_root),
        preparer,
    )
    with pytest.raises(SnapshotIntegrityError, match="file hash mismatch"):
        corrupt_target_worker.pin_snapshot(_expecting(requirements, submitted_ref), period)
    assert preparer.calls == 0


def test_restart_exact_pin_accepts_authoritative_mixed_corporate_action_coverage(
    tmp_path: Path,
) -> None:
    composite_root = tmp_path / "composite"
    composite_root.mkdir()
    instrument = InstrumentId("300059.SZ")
    period = DateRange(date(2025, 1, 2), date(2025, 1, 3))
    selected_root = _publish_composite(
        composite_root,
        tmp_path / "mixed-sources",
        instrument=instrument,
        start=period.start,
        end=period.end,
        corporate_action_coverage=_mixed_corporate_action_coverage(
            period.start,
            period.end,
        ),
    )
    requirements = _event_requirements(instrument, ANNUAL)
    submitted = SnapshotRegistryMarketDataRepository(composite_root).pin_snapshot(
        requirements,
        period,
    )
    assert submitted.producer_snapshot_id == f"composite:{selected_root.name}"
    producer_snapshot_id = submitted.producer_snapshot_id
    assert producer_snapshot_id is not None

    restarted = SnapshotRegistryMarketDataRepository(composite_root)
    replay = restarted.pin_selected_snapshot(
        producer_snapshot_id,
        _expecting(requirements, submitted),
        period,
    )

    assert replay.snapshot_id == submitted.snapshot_id
    assert replay.checksum == submitted.checksum
    assert replay.producer_schema_version == submitted.producer_schema_version
    assert replay.producer_snapshot_id == submitted.producer_snapshot_id
    assert restarted.load_corporate_actions(replay, instrument, period) == ()


def test_expected_push2_snapshot_replays_from_independent_registry_after_restart(
    tmp_path: Path,
) -> None:
    composite_root = tmp_path / "composite"
    choice_root = tmp_path / "choice"
    technical_root = tmp_path / "technical"
    composite_root.mkdir()
    choice_root.mkdir()
    technical_root.mkdir()
    instrument = InstrumentId("600519.SH")
    period = DateRange(date(2024, 12, 30), date(2025, 1, 3))
    technical_snapshot = _publish_choice(
        technical_root,
        instrument=instrument,
        start=period.start,
        end=period.end,
        source=_push2_source(),
    )
    requirements = _technical_requirements(instrument)
    before_restart = SnapshotRegistryMarketDataRepository(
        composite_root,
        choice_root=choice_root,
        technical_root=technical_root,
    )

    old_ref = before_restart.pin_snapshot(requirements, period)
    assert old_ref.producer_schema_version == TECHNICAL_SNAPSHOT_SCHEMA_VERSION
    assert old_ref.checksum == _direct_checksum(
        technical_snapshot,
        requirements,
        period,
        profile="technical_snapshot",
    )

    after_restart = SnapshotRegistryMarketDataRepository(
        composite_root,
        choice_root=choice_root,
        technical_root=technical_root,
    )
    replay_ref = after_restart.pin_snapshot(_expecting(requirements, old_ref), period)

    assert replay_ref.snapshot_id == old_ref.snapshot_id
    assert replay_ref.checksum == old_ref.checksum
    assert len(after_restart.load_daily_bars(old_ref, instrument, period)) == 5
    assert len(after_restart.load_sessions(old_ref, instrument, period)) == 5


@pytest.mark.parametrize("misplaced_profile", ["choice", "technical"])
def test_daily_provider_roots_reject_the_other_schema(
    tmp_path: Path,
    misplaced_profile: str,
) -> None:
    composite_root = tmp_path / "composite"
    choice_root = tmp_path / "choice"
    technical_root = tmp_path / "technical"
    composite_root.mkdir()
    choice_root.mkdir()
    technical_root.mkdir()
    destination = choice_root if misplaced_profile == "technical" else technical_root
    source = _push2_source() if misplaced_profile == "technical" else None
    _publish_choice(
        destination,
        instrument=InstrumentId("600519.SH"),
        start=date(2025, 1, 2),
        end=date(2025, 1, 3),
        source=source,
    )

    repository = SnapshotRegistryMarketDataRepository(
        composite_root,
        choice_root=choice_root,
        technical_root=technical_root,
    )
    with pytest.raises(SnapshotRegistryIntegrityError, match="unsupported schemaVersion"):
        repository.refresh()


@pytest.mark.parametrize(
    ("instrument", "period", "event_codes", "message"),
    [
        (
            InstrumentId("600519.SH"),
            DateRange(date(2025, 1, 2), date(2025, 1, 3)),
            (),
            "no registered snapshot covers",
        ),
        (
            InstrumentId("300059.SZ"),
            DateRange(date(2025, 1, 1), date(2025, 1, 3)),
            (),
            "no registered snapshot covers",
        ),
        (
            InstrumentId("300059.SZ"),
            DateRange(date(2025, 1, 2), date(2025, 1, 3)),
            (SEMIANNUAL,),
            "no registered snapshot covers",
        ),
    ],
)
def test_missing_instrument_range_or_event_code_fails_closed(
    tmp_path: Path,
    instrument: InstrumentId,
    period: DateRange,
    event_codes: tuple[str, ...],
    message: str,
) -> None:
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    available = InstrumentId("300059.SZ")
    _publish_composite(
        registry_root,
        tmp_path / "source",
        instrument=available,
        start=date(2025, 1, 2),
        end=date(2025, 1, 3),
    )
    repository = SnapshotRegistryMarketDataRepository(registry_root)
    requirements = (
        _event_requirements(instrument, *event_codes)
        if event_codes
        else _technical_requirements(instrument)
    )

    with pytest.raises(SnapshotRegistryNoMatchError, match=message):
        repository.pin_snapshot(requirements, period)


def test_duplicate_minimal_coverage_is_rejected_as_ambiguous(tmp_path: Path) -> None:
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    instrument = InstrumentId("300059.SZ")
    start = date(2025, 1, 2)
    end = date(2025, 1, 3)
    _publish_composite(
        registry_root,
        tmp_path / "source-one",
        instrument=instrument,
        start=start,
        end=end,
        variant=0,
        composed_minute=0,
    )
    _publish_composite(
        registry_root,
        tmp_path / "source-two",
        instrument=instrument,
        start=start,
        end=end,
        variant=1,
        composed_minute=0,
    )
    repository = SnapshotRegistryMarketDataRepository(registry_root)

    with pytest.raises(SnapshotRegistryAmbiguityError, match="latest publication time"):
        repository.pin_snapshot(
            _technical_requirements(instrument),
            DateRange(start, end),
        )


def test_fresh_submission_selects_latest_equally_minimal_snapshot(tmp_path: Path) -> None:
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    instrument = InstrumentId("300059.SZ")
    period = DateRange(date(2025, 1, 2), date(2025, 1, 3))
    older = _publish_composite(
        registry_root,
        tmp_path / "source-old",
        instrument=instrument,
        start=period.start,
        end=period.end,
        variant=0,
    )
    newer = _publish_composite(
        registry_root,
        tmp_path / "source-new",
        instrument=instrument,
        start=period.start,
        end=period.end,
        variant=1,
    )
    requirements = _technical_requirements(instrument)
    repository = SnapshotRegistryMarketDataRepository(registry_root)

    selected = repository.pin_snapshot(requirements, period)

    assert selected.checksum == _direct_checksum(newer, requirements, period)
    assert selected.checksum != _direct_checksum(older, requirements, period)


def test_replay_expected_identity_stays_on_old_snapshot_after_update(tmp_path: Path) -> None:
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    instrument = InstrumentId("300059.SZ")
    period = DateRange(date(2025, 1, 2), date(2025, 1, 3))
    _publish_composite(
        registry_root,
        tmp_path / "source-old",
        instrument=instrument,
        start=period.start,
        end=period.end,
        variant=0,
    )
    requirements = _technical_requirements(instrument)
    repository = SnapshotRegistryMarketDataRepository(registry_root)
    old_ref = repository.pin_snapshot(requirements, period)
    _publish_composite(
        registry_root,
        tmp_path / "source-new",
        instrument=instrument,
        start=period.start,
        end=period.end,
        variant=1,
    )

    fresh_ref = repository.pin_snapshot(requirements, period)
    replay_ref = repository.pin_snapshot(_expecting(requirements, old_ref), period)

    assert fresh_ref.checksum != old_ref.checksum
    assert replay_ref.snapshot_id == old_ref.snapshot_id
    assert replay_ref.checksum == old_ref.checksum
    assert replay_ref.schema_version == old_ref.schema_version


def test_expected_snapshot_identity_missing_from_registry_is_rejected(tmp_path: Path) -> None:
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    instrument = InstrumentId("300059.SZ")
    period = DateRange(date(2025, 1, 2), date(2025, 1, 3))
    _publish_composite(
        registry_root,
        tmp_path / "source",
        instrument=instrument,
        start=period.start,
        end=period.end,
    )
    requirements = replace(
        _technical_requirements(instrument),
        expected_snapshot_id=StrongId("snapshot:" + "0" * 64),
        expected_snapshot_checksum="sha256:" + "0" * 64,
        expected_snapshot_schema_version="local-parquet.market-data.v3",
    )
    repository = SnapshotRegistryMarketDataRepository(registry_root)

    with pytest.raises(SnapshotRegistryNoMatchError, match="expected immutable snapshot"):
        repository.pin_snapshot(requirements, period)


@pytest.mark.parametrize(
    ("expected_snapshot_id", "expected_snapshot_checksum", "expected_schema_version"),
    [
        (StrongId("snapshot:" + "0" * 64), None, None),
        (None, "sha256:" + "0" * 64, None),
        (None, None, "local-parquet.market-data.v3"),
        (StrongId("snapshot:" + "0" * 64), "sha256:" + "0" * 64, None),
    ],
)
def test_expected_snapshot_identity_fields_must_be_supplied_together(
    expected_snapshot_id: StrongId | None,
    expected_snapshot_checksum: str | None,
    expected_schema_version: str | None,
) -> None:
    with pytest.raises(DomainValidationError, match="must be supplied together"):
        DataRequirements(
            instruments=(InstrumentId("300059.SZ"),),
            datasets=("daily_ohlcv", "corporate_actions"),
            expected_snapshot_id=expected_snapshot_id,
            expected_snapshot_checksum=expected_snapshot_checksum,
            expected_snapshot_schema_version=expected_schema_version,
        )


def test_local_repository_hash_ignores_expected_replay_constraint(tmp_path: Path) -> None:
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    instrument = InstrumentId("300059.SZ")
    period = DateRange(date(2025, 1, 2), date(2025, 1, 3))
    snapshot_root = _publish_composite(
        registry_root,
        tmp_path / "source",
        instrument=instrument,
        start=period.start,
        end=period.end,
    )
    requirements = _technical_requirements(instrument)
    repository = LocalParquetMarketDataRepository(
        snapshot_root,
        profile="composite_snapshot",
    )
    unpinned_constraint = repository.pin_snapshot(requirements, period)

    expected_constraint = repository.pin_snapshot(
        _expecting(requirements, unpinned_constraint),
        period,
    )

    assert expected_constraint == unpinned_constraint


@pytest.mark.parametrize("damage", ["schema", "snapshot_id"])
def test_invalid_manifest_schema_or_identity_fails_during_index(
    tmp_path: Path,
    damage: str,
) -> None:
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    snapshot_root = _publish_composite(
        registry_root,
        tmp_path / "source",
        instrument=InstrumentId("300059.SZ"),
        start=date(2025, 1, 2),
        end=date(2025, 1, 3),
    )
    manifest_path = snapshot_root / "snapshot_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if damage == "schema":
        manifest["schemaVersion"] = "ashare-lab.composite-research-snapshot.v999"
        expected = "schemaVersion"
    else:
        manifest["snapshotId"] = "composite:" + "0" * 64
        expected = "snapshotId"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    repository = SnapshotRegistryMarketDataRepository(registry_root)
    with pytest.raises(SnapshotRegistryIntegrityError, match=expected):
        repository.refresh()


def test_explicit_content_id_ignores_unrelated_legacy_entry(tmp_path: Path) -> None:
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    instrument = InstrumentId("300059.SZ")
    period = DateRange(date(2025, 1, 2), date(2025, 1, 3))
    selected_root = _publish_composite(
        registry_root,
        tmp_path / "source",
        instrument=instrument,
        start=period.start,
        end=period.end,
    )
    legacy_root = registry_root / ("0" * 64)
    legacy_root.mkdir()
    (legacy_root / "snapshot_manifest.json").write_text(
        json.dumps({"schemaVersion": "ashare-lab.composite-research-snapshot.v1"}),
        encoding="utf-8",
    )

    broad_repository = SnapshotRegistryMarketDataRepository(registry_root)
    with pytest.raises(SnapshotRegistryIntegrityError, match="schemaVersion"):
        broad_repository.refresh()

    repository = SnapshotRegistryMarketDataRepository(
        registry_root,
        producer_snapshot_id=f"composite:{selected_root.name}",
    )
    snapshot = repository.pin_snapshot(_event_requirements(instrument, ANNUAL), period)

    assert repository.refresh() == 1
    assert snapshot.checksum == _direct_checksum(
        selected_root,
        _event_requirements(instrument, ANNUAL),
        period,
    )
    assert len(repository.load_daily_bars(snapshot, instrument, period)) == 2


def test_explicit_path_ignores_unrelated_damaged_entry(tmp_path: Path) -> None:
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    instrument = InstrumentId("300059.SZ")
    period = DateRange(date(2025, 1, 2), date(2025, 1, 3))
    selected_root = _publish_composite(
        registry_root,
        tmp_path / "source",
        instrument=instrument,
        start=period.start,
        end=period.end,
    )
    damaged_root = registry_root / ("0" * 64)
    damaged_root.mkdir()
    (damaged_root / "snapshot_manifest.json").write_text("not-json", encoding="utf-8")

    repository = SnapshotRegistryMarketDataRepository(
        registry_root,
        producer_snapshot_path=selected_root,
    )
    snapshot = repository.pin_snapshot(_technical_requirements(instrument), period)

    assert snapshot.checksum == _direct_checksum(
        selected_root,
        _technical_requirements(instrument),
        period,
    )


def test_newly_published_producer_is_pinned_by_exact_content_id(
    tmp_path: Path,
) -> None:
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    instrument = InstrumentId("300059.SZ")
    period = DateRange(date(2025, 1, 2), date(2025, 1, 3))
    requested = _publish_composite(
        registry_root,
        tmp_path / "source-requested",
        instrument=instrument,
        start=period.start,
        end=period.end,
        variant=1,
        composed_minute=1,
    )
    newer = _publish_composite(
        registry_root,
        tmp_path / "source-newer",
        instrument=instrument,
        start=period.start,
        end=period.end,
        variant=2,
        composed_minute=2,
    )
    requirements = _event_requirements(instrument, ANNUAL)
    repository = SnapshotRegistryMarketDataRepository(registry_root)

    generic = repository.pin_snapshot(requirements, period)
    exact = repository.pin_producer_snapshot(
        f"composite:{requested.name}",
        requested,
        requirements,
        period,
    )

    assert generic.checksum == _direct_checksum(newer, requirements, period)
    assert exact.checksum == _direct_checksum(requested, requirements, period)
    assert exact.checksum != generic.checksum


def test_newly_published_producer_path_must_match_content_id(tmp_path: Path) -> None:
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    instrument = InstrumentId("300059.SZ")
    period = DateRange(date(2025, 1, 2), date(2025, 1, 3))
    first = _publish_composite(
        registry_root,
        tmp_path / "source-first",
        instrument=instrument,
        start=period.start,
        end=period.end,
        variant=1,
    )
    second = _publish_composite(
        registry_root,
        tmp_path / "source-second",
        instrument=instrument,
        start=period.start,
        end=period.end,
        variant=2,
    )
    repository = SnapshotRegistryMarketDataRepository(registry_root)

    with pytest.raises(SnapshotRegistryIntegrityError, match="path does not match"):
        repository.pin_producer_snapshot(
            f"composite:{first.name}",
            second,
            _event_requirements(instrument, ANNUAL),
            period,
        )


def test_explicit_selection_target_still_fails_closed(tmp_path: Path) -> None:
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    legacy_root = registry_root / ("0" * 64)
    legacy_root.mkdir()
    (legacy_root / "snapshot_manifest.json").write_text(
        json.dumps({"schemaVersion": "ashare-lab.composite-research-snapshot.v1"}),
        encoding="utf-8",
    )

    repository = SnapshotRegistryMarketDataRepository(
        registry_root,
        producer_snapshot_id="composite:" + "0" * 64,
    )
    with pytest.raises(SnapshotRegistryIntegrityError, match="schemaVersion"):
        repository.refresh()


def test_explicit_snapshot_path_must_be_direct_registry_child(tmp_path: Path) -> None:
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    outside = tmp_path / ("0" * 64)
    outside.mkdir()

    with pytest.raises(SnapshotRegistryIntegrityError, match="direct child"):
        SnapshotRegistryMarketDataRepository(
            registry_root,
            producer_snapshot_path=outside,
        )


def test_manifest_composed_at_requires_an_explicit_timezone(tmp_path: Path) -> None:
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    snapshot_root = _publish_composite(
        registry_root,
        tmp_path / "source",
        instrument=InstrumentId("300059.SZ"),
        start=date(2025, 1, 2),
        end=date(2025, 1, 3),
    )
    manifest = json.loads((snapshot_root / "snapshot_manifest.json").read_text(encoding="utf-8"))
    manifest.pop("snapshotId")
    manifest["composedAt"] = "2025-02-01T10:00:00"
    payload = json.dumps(
        manifest,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    manifest["snapshotId"] = f"composite:{digest}"
    invalid_root = registry_root / digest
    shutil.copytree(snapshot_root, invalid_root)
    (invalid_root / "snapshot_manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )

    repository = SnapshotRegistryMarketDataRepository(registry_root)
    with pytest.raises(SnapshotRegistryIntegrityError, match="explicit timezone"):
        repository.refresh()


def test_choice_registered_file_tamper_fails_during_index(tmp_path: Path) -> None:
    composite_root = tmp_path / "composite"
    choice_root = tmp_path / "choice"
    composite_root.mkdir()
    choice_root.mkdir()
    snapshot_root = _publish_choice(
        choice_root,
        instrument=InstrumentId("600519.SH"),
        start=date(2025, 1, 2),
        end=date(2025, 1, 3),
    )
    (snapshot_root / "daily_ohlcv.parquet").write_bytes(b"tampered")

    repository = SnapshotRegistryMarketDataRepository(
        composite_root,
        choice_root=choice_root,
    )
    with pytest.raises(SnapshotRegistryIntegrityError, match="registered file hash mismatch"):
        repository.refresh()


def test_choice_captured_at_requires_an_explicit_timezone(tmp_path: Path) -> None:
    composite_root = tmp_path / "composite"
    choice_root = tmp_path / "choice"
    composite_root.mkdir()
    choice_root.mkdir()
    snapshot_root = _publish_choice(
        choice_root,
        instrument=InstrumentId("600519.SH"),
        start=date(2025, 1, 2),
        end=date(2025, 1, 3),
    )
    manifest = json.loads((snapshot_root / "snapshot_manifest.json").read_text(encoding="utf-8"))
    manifest.pop("snapshotId")
    manifest["capturedAt"] = "2025-02-01T02:00:00"
    payload = json.dumps(
        manifest,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    manifest["snapshotId"] = f"choice:{digest}"
    invalid_root = choice_root / digest
    shutil.copytree(snapshot_root, invalid_root)
    (invalid_root / "snapshot_manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )

    repository = SnapshotRegistryMarketDataRepository(
        composite_root,
        choice_root=choice_root,
    )
    with pytest.raises(SnapshotRegistryIntegrityError, match="explicit timezone"):
        repository.refresh()


def test_cross_profile_same_latest_time_is_ambiguous(tmp_path: Path) -> None:
    composite_root = tmp_path / "composite"
    choice_root = tmp_path / "choice"
    composite_root.mkdir()
    choice_root.mkdir()
    instrument = InstrumentId("300059.SZ")
    period = DateRange(date(2025, 1, 2), date(2025, 1, 3))
    _publish_choice(
        choice_root,
        instrument=instrument,
        start=period.start,
        end=period.end,
        variant=9,
        captured_at=datetime(2025, 2, 1, 2, 0, tzinfo=UTC),
    )
    _publish_composite(
        composite_root,
        tmp_path / "composite-source",
        instrument=instrument,
        start=period.start,
        end=period.end,
        composed_minute=0,
    )
    repository = SnapshotRegistryMarketDataRepository(
        composite_root,
        choice_root=choice_root,
    )

    with pytest.raises(SnapshotRegistryAmbiguityError, match="latest publication time"):
        repository.pin_snapshot(_technical_requirements(instrument), period)


def test_registered_file_tamper_is_rejected_before_pin_returns(tmp_path: Path) -> None:
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    snapshot_root = _publish_composite(
        registry_root,
        tmp_path / "source",
        instrument=InstrumentId("300059.SZ"),
        start=date(2025, 1, 2),
        end=date(2025, 1, 3),
    )
    (snapshot_root / "daily_ohlcv.parquet").write_bytes(b"tampered")
    repository = SnapshotRegistryMarketDataRepository(registry_root)

    with pytest.raises(SnapshotIntegrityError, match="hash mismatch"):
        repository.pin_snapshot(
            _technical_requirements(InstrumentId("300059.SZ")),
            DateRange(date(2025, 1, 2), date(2025, 1, 3)),
        )


def test_unrouted_or_mutated_reference_fails_closed(tmp_path: Path) -> None:
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    instrument = InstrumentId("300059.SZ")
    period = DateRange(date(2025, 1, 2), date(2025, 1, 3))
    _publish_composite(
        registry_root,
        tmp_path / "source",
        instrument=instrument,
        start=period.start,
        end=period.end,
    )
    repository = SnapshotRegistryMarketDataRepository(registry_root)
    snapshot = repository.pin_snapshot(_technical_requirements(instrument), period)
    mutated = DataSnapshotRef(
        snapshot_id=snapshot.snapshot_id,
        checksum=snapshot.checksum,
        schema_version=snapshot.schema_version,
        created_at=snapshot.created_at,
        producer_schema_version="wrong-producer-schema.v1",
    )

    with pytest.raises(SnapshotRegistryIntegrityError, match="checksum or schema"):
        repository.load_daily_bars(mutated, instrument, period)
