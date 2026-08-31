import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from ashare_lab.adapters.market_data import (
    LocalParquetMarketDataRepository,
    ResearchFallbackSessionProvider,
    SessionFactory,
)
from ashare_lab.adapters.persistence import InMemoryBacktestRunStore
from ashare_lab.application.backtest_submission import (
    BacktestRunConfig,
    BacktestSubmissionService,
    SubmissionVersions,
)
from ashare_lab.application.execute_backtest import (
    BacktestExecutionService,
    WorkerRuntimeIdentity,
)
from ashare_lab.domain.market_data import DataSnapshotRef, InstrumentSession
from ashare_lab.domain.shared import InstrumentId, RunId
from ashare_lab.domain.strategy import (
    BacktestConfig,
    CatalogRef,
    DailyExecutionPolicy,
    EventCondition,
    FirstOfExit,
    IndicatorCondition,
    Instrument,
    StrategySpec,
)
from ashare_lab.domain.strategy.models import JsonScalar
from ashare_lab.ports.backtest_runs import BacktestJobState
from ashare_lab.ports.market_data import DataRequirements, DateRange

HASH = "sha256:" + "a" * 64
REVISION = "a" * 40


def _versions(*, code_revision: str = REVISION) -> SubmissionVersions:
    return SubmissionVersions(
        catalog_hash=HASH,
        engine_version="2.0.0a0",
        code_revision=code_revision,
    )


def _runtime_identity(
    versions: SubmissionVersions | None = None,
    *,
    strict_replay: bool = False,
) -> WorkerRuntimeIdentity:
    return WorkerRuntimeIdentity.from_submission_versions(
        versions or _versions(),
        strict_replay=strict_replay,
    )


class CapturingQueue:
    def __init__(self) -> None:
        self.run_ids: list[str] = []

    def enqueue(self, run_id: RunId) -> str:
        self.run_ids.append(str(run_id))
        return "job:test"


class RejectingGlobalSessionProvider:
    version = ResearchFallbackSessionProvider.version

    def sessions_for(self, _bars):
        raise AssertionError("global session provider must not be used")


class CapturingLocalParquetRepository(LocalParquetMarketDataRepository):
    def __init__(
        self,
        data_root: Path,
        *,
        session_factory: SessionFactory | None = None,
    ) -> None:
        super().__init__(data_root, session_factory=session_factory)
        self.pin_requirements: list[DataRequirements] = []

    def pin_snapshot(
        self,
        requirements: DataRequirements,
        period: DateRange,
    ) -> DataSnapshotRef:
        self.pin_requirements.append(requirements)
        return super().pin_snapshot(requirements, period)


class ProducerSchemaSwitchingRepository(CapturingLocalParquetRepository):
    """Expose a changed producer identity between submission and worker pin."""

    def pin_snapshot(
        self,
        requirements: DataRequirements,
        period: DateRange,
    ) -> DataSnapshotRef:
        snapshot = super().pin_snapshot(requirements, period)
        producer_version = (
            "ashare-lab.composite-research-snapshot.v2"
            if len(self.pin_requirements) == 1
            else "ashare-lab.composite-research-snapshot.v999"
        )
        return replace(snapshot, producer_schema_version=producer_version)


class ProducerIdCapturingRepository(CapturingLocalParquetRepository):
    """Expose one stable producer content ID across submission and worker replay."""

    producer_snapshot_id = "composite:" + "b" * 64

    def pin_snapshot(
        self,
        requirements: DataRequirements,
        period: DateRange,
    ) -> DataSnapshotRef:
        snapshot = super().pin_snapshot(requirements, period)
        return replace(snapshot, producer_snapshot_id=self.producer_snapshot_id)

    @staticmethod
    def _local_ref(snapshot: DataSnapshotRef) -> DataSnapshotRef:
        return replace(snapshot, producer_snapshot_id=None)

    def load_daily_bars(self, snapshot, instrument_id, period):
        return super().load_daily_bars(self._local_ref(snapshot), instrument_id, period)

    def load_signal_bars(self, snapshot, instrument_id, period):
        return super().load_signal_bars(self._local_ref(snapshot), instrument_id, period)

    def load_sessions(self, snapshot, instrument_id, period):
        return super().load_sessions(self._local_ref(snapshot), instrument_id, period)

    def load_corporate_actions(self, snapshot, instrument_id, period):
        return super().load_corporate_actions(self._local_ref(snapshot), instrument_id, period)


class ClaimBarrierStore(InMemoryBacktestRunStore):
    def __init__(self) -> None:
        super().__init__()
        self._claim_barrier = threading.Barrier(2)

    def transition(self, run_id: RunId, **kwargs):
        if kwargs.get("target") is BacktestJobState.RUNNING_DATA:
            self._claim_barrier.wait(timeout=5)
        return super().transition(run_id, **kwargs)


def _strategy() -> StrategySpec:
    params: dict[str, JsonScalar] = {"period": 2, "price_field": "close"}
    return StrategySpec(
        catalog=CatalogRef(catalog_id="cn_a.signals", release_version="test"),
        instrument=Instrument(symbol="300059.SZ"),
        entry=IndicatorCondition(
            indicator_id="technical.ma",
            definition_version="1.0.0",
            params=params,
            trigger="price_crosses_above",
        ),
        exit=FirstOfExit(
            children=(
                IndicatorCondition(
                    indicator_id="technical.ma",
                    definition_version="1.0.0",
                    params=params,
                    trigger="price_crosses_below",
                ),
            )
        ),
        backtest=BacktestConfig(
            start=date(2025, 1, 2),
            end=date(2025, 1, 10),
            initial_cash_cny=100_000,
        ),
    )


def _event_strategy() -> StrategySpec:
    params: dict[str, JsonScalar] = {"period": 2, "price_field": "close"}
    return StrategySpec(
        catalog=CatalogRef(catalog_id="cn_a.signals", release_version="test"),
        instrument=Instrument(symbol="300059.SZ"),
        entry=EventCondition(
            event_code="event.financial_results.earnings_forecast_published",
            definition_version="1.0.0",
        ),
        exit=FirstOfExit(
            children=(
                IndicatorCondition(
                    indicator_id="technical.ma",
                    definition_version="1.0.0",
                    params=params,
                    trigger="price_crosses_below",
                ),
            )
        ),
        execution=DailyExecutionPolicy(
            data_capability="daily_ohlcv_events",
            evaluation_frequency="event_available_plus_1d_close",
        ),
        backtest=BacktestConfig(
            start=date(2025, 1, 2),
            end=date(2025, 1, 10),
            initial_cash_cny=100_000,
        ),
    )


def _write_bars(root: Path) -> None:
    start = date(2024, 12, 30)
    closes = [10, 9, 11, 12, 8, 7, 7, 8, 9, 6, 5, 5]
    dates = [start + timedelta(days=index) for index in range(len(closes))]
    table: Any = pa.table(  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
        {
            "stock_code": ["300059"] * len(closes),
            "date": dates,
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1_000_000] * len(closes),
            "amount": [value * 1_000_000 for value in closes],
        }
    )
    pq.write_table(  # pyright: ignore[reportUnknownMemberType]
        table,
        root / "daily_ohlcv.parquet",
    )
    _write_empty_corporate_actions(root)


def _write_empty_corporate_actions(root: Path) -> None:
    schema: Any = pa.schema(  # pyright: ignore[reportUnknownMemberType]
        [
            ("stock_code", pa.string()),
            ("action_id", pa.string()),
            ("source_action_id", pa.string()),
            ("action_type", pa.string()),
            ("record_date", pa.date32()),
            ("ex_date", pa.date32()),
            ("source_released_at", pa.string()),
            ("vendor_first_available_at", pa.string()),
            ("ingested_at", pa.string()),
            ("replay_available_at", pa.string()),
            ("revision_no", pa.int32()),
            ("time_quality", pa.string()),
            ("provider", pa.string()),
            ("source_url", pa.string()),
            ("raw_response_sha256", pa.string()),
            ("validation_status", pa.string()),
            ("currency", pa.string()),
            ("gross_cash_per_share", pa.decimal128(20, 8)),
            ("cash_pay_date", pa.date32()),
            ("share_multiplier", pa.decimal128(20, 8)),
            ("share_credit_date", pa.date32()),
            ("share_sellable_date", pa.date32()),
            ("rights_ratio", pa.decimal128(20, 8)),
            ("rights_subscription_price", pa.decimal128(20, 8)),
            ("rights_payment_deadline", pa.date32()),
            ("rights_listing_date", pa.date32()),
        ]
    )
    empty: Any = pa.Table.from_pylist(  # pyright: ignore[reportUnknownMemberType]
        [],
        schema=schema,
    )
    pq.write_table(  # pyright: ignore[reportUnknownMemberType]
        empty,
        root / "corporate_actions.parquet",
    )


def _write_events(root: Path) -> None:
    table: Any = pa.table(  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
        {
            "stock_code": ["300059"],
            "event_id": ["event-forecast-2025-01-02"],
            "event_code": ["event.financial_results.earnings_forecast_published"],
            "occurred_at": [None],
            "source_released_at": ["2025-01-02T08:00:00+08:00"],
            "vendor_first_available_at": ["2025-01-02T08:01:00+08:00"],
            "ingested_at": ["2025-01-02T08:02:00+08:00"],
            "revision_no": [0],
            "time_quality": ["exact"],
            "attributes_json": ['{"source":"fixture"}'],
        }
    )
    pq.write_table(  # pyright: ignore[reportUnknownMemberType]
        table,
        root / "events.parquet",
    )


def test_submitted_work_item_runs_to_a_stable_result(tmp_path: Path) -> None:
    _write_bars(tmp_path)
    repository = CapturingLocalParquetRepository(tmp_path)
    store = InMemoryBacktestRunStore()
    queue = CapturingQueue()
    submission = BacktestSubmissionService(
        market_data=repository,
        run_store=store,
        job_queue=queue,
        versions=_versions(),
        clock=lambda: datetime(2025, 1, 1, tzinfo=UTC),
    )
    created = submission.submit(
        _strategy(),
        BacktestRunConfig(
            participation_rate=Decimal("1"),
            slippage_bps=Decimal("0"),
            allocation_ratio=Decimal("0.9"),
        ),
    )

    executor = BacktestExecutionService(
        market_data=repository,
        session_reference=ResearchFallbackSessionProvider(),
        run_store=store,
        runtime_identity=_runtime_identity(),
    )
    completed = executor.execute(created.record.run_id)
    replay = executor.execute(created.record.run_id)

    assert completed.state is BacktestJobState.SUCCEEDED
    assert replay == completed
    assert queue.run_ids == [created.record.run_id.value]
    result = json.loads(completed.result_json or "{}")
    assert result["summary"]["runId"] == created.record.run_id.value
    assert result["summary"]["tradeCount"] == 1
    assert {item["kind"] for item in result["activities"]} >= {
        "signal",
        "order",
        "fill",
    }


def test_worker_can_read_market_rules_from_the_same_pinned_snapshot(tmp_path: Path) -> None:
    _write_bars(tmp_path)
    fallback = ResearchFallbackSessionProvider()
    repository: ProducerIdCapturingRepository

    def session_factory(
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> tuple[InstrumentSession, ...]:
        bars = repository.load_daily_bars(snapshot, instrument_id, period)
        return tuple(fallback.sessions_for(bars))

    repository = ProducerIdCapturingRepository(
        tmp_path,
        session_factory=session_factory,
    )
    store = InMemoryBacktestRunStore()
    submission = BacktestSubmissionService(
        market_data=repository,
        run_store=store,
        job_queue=CapturingQueue(),
        versions=_versions(),
        clock=lambda: datetime(2025, 1, 1, tzinfo=UTC),
    )
    created = submission.submit(
        _strategy(),
        BacktestRunConfig(
            participation_rate=Decimal("1"),
            slippage_bps=Decimal("0"),
            allocation_ratio=Decimal("0.9"),
        ),
    )

    completed = BacktestExecutionService(
        market_data=repository,
        session_reference=RejectingGlobalSessionProvider(),
        run_store=store,
        runtime_identity=_runtime_identity(),
        sessions_from_snapshot=True,
    ).execute(created.record.run_id)

    assert completed.state is BacktestJobState.SUCCEEDED
    assert len(repository.pin_requirements) == 2
    submission_requirements, worker_requirements = repository.pin_requirements
    assert submission_requirements.expected_snapshot_id is None
    assert submission_requirements.expected_snapshot_checksum is None
    assert submission_requirements.expected_snapshot_schema_version is None
    assert submission_requirements.expected_producer_snapshot_schema_version is None
    assert submission_requirements.expected_producer_snapshot_id is None
    expected_snapshot = json.loads(created.record.manifest_json)["data_snapshot"]
    assert str(worker_requirements.expected_snapshot_id) == expected_snapshot["snapshot_id"]
    assert worker_requirements.expected_snapshot_checksum == expected_snapshot["checksum"]
    assert (
        worker_requirements.expected_snapshot_schema_version == expected_snapshot["schema_version"]
    )
    assert (
        worker_requirements.expected_producer_snapshot_schema_version
        == expected_snapshot["producer_schema_version"]
    )
    assert expected_snapshot["producer_snapshot_id"] == repository.producer_snapshot_id
    assert worker_requirements.expected_producer_snapshot_id == repository.producer_snapshot_id


def test_worker_rejects_changed_producer_snapshot_schema_before_loading_data(
    tmp_path: Path,
) -> None:
    _write_bars(tmp_path)
    repository = ProducerSchemaSwitchingRepository(tmp_path)
    store = InMemoryBacktestRunStore()
    created = BacktestSubmissionService(
        market_data=repository,
        run_store=store,
        job_queue=CapturingQueue(),
        versions=_versions(),
        clock=lambda: datetime(2025, 1, 1, tzinfo=UTC),
    ).submit(_strategy(), BacktestRunConfig(run_robustness=False))

    completed = BacktestExecutionService(
        market_data=repository,
        session_reference=ResearchFallbackSessionProvider(),
        run_store=store,
        runtime_identity=_runtime_identity(),
    ).execute(created.record.run_id)

    assert completed.state is BacktestJobState.FAILED
    assert completed.error_code == "producer_snapshot_schema_version_mismatch"


def test_cancelled_work_item_does_not_enter_the_engine(tmp_path: Path) -> None:
    _write_bars(tmp_path)
    repository = LocalParquetMarketDataRepository(tmp_path)
    store = InMemoryBacktestRunStore()
    submission = BacktestSubmissionService(
        market_data=repository,
        run_store=store,
        job_queue=CapturingQueue(),
        versions=_versions(),
        clock=lambda: datetime(2025, 1, 1, tzinfo=UTC),
    )
    created = submission.submit(_strategy(), BacktestRunConfig())
    store.request_cancel(created.record.run_id)

    completed = BacktestExecutionService(
        market_data=repository,
        session_reference=ResearchFallbackSessionProvider(),
        run_store=store,
        runtime_identity=_runtime_identity(),
    ).execute(created.record.run_id)

    assert completed.state is BacktestJobState.CANCELLED
    assert completed.result_json is None


def test_event_work_item_pins_events_and_buys_at_same_open_when_known_pre_open(
    tmp_path: Path,
) -> None:
    _write_bars(tmp_path)
    _write_events(tmp_path)
    repository = LocalParquetMarketDataRepository(tmp_path)
    store = InMemoryBacktestRunStore()
    queue = CapturingQueue()
    submission = BacktestSubmissionService(
        market_data=repository,
        run_store=store,
        job_queue=queue,
        versions=_versions(),
        clock=lambda: datetime(2025, 1, 1, tzinfo=UTC),
    )
    created = submission.submit(
        _event_strategy(),
        BacktestRunConfig(
            participation_rate=Decimal("1"),
            slippage_bps=Decimal("0"),
            allocation_ratio=Decimal("0.9"),
            run_robustness=False,
        ),
    )

    completed = BacktestExecutionService(
        market_data=repository,
        session_reference=ResearchFallbackSessionProvider(),
        run_store=store,
        runtime_identity=_runtime_identity(),
    ).execute(created.record.run_id)

    assert completed.state is BacktestJobState.SUCCEEDED
    result = json.loads(completed.result_json or "{}")
    buy_fills = [
        item for item in result["activities"] if item["kind"] == "fill" and item["side"] == "buy"
    ]
    assert buy_fills[0]["occurredAt"].startswith("2025-01-02T09:30:00")
    assert buy_fills[0]["timeQuality"] == "daily_bar_open_proxy"
    assert "不代表" in buy_fills[0]["timeSemantics"]


def test_duplicate_workers_only_one_claims_a_queued_run(tmp_path: Path) -> None:
    _write_bars(tmp_path)
    repository = LocalParquetMarketDataRepository(tmp_path)
    store = ClaimBarrierStore()
    created = BacktestSubmissionService(
        market_data=repository,
        run_store=store,
        job_queue=CapturingQueue(),
        versions=_versions(),
        clock=lambda: datetime(2025, 1, 1, tzinfo=UTC),
    ).submit(
        _strategy(),
        BacktestRunConfig(
            participation_rate=Decimal("1"),
            slippage_bps=Decimal("0"),
            allocation_ratio=Decimal("0.9"),
            run_robustness=False,
        ),
    )
    executor = BacktestExecutionService(
        market_data=repository,
        session_reference=ResearchFallbackSessionProvider(),
        run_store=store,
        runtime_identity=_runtime_identity(),
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(lambda _: executor.execute(created.record.run_id), range(2)))

    final = store.get(created.record.run_id)
    assert final is not None
    assert final.state is BacktestJobState.SUCCEEDED
    assert all(outcome.state is not BacktestJobState.FAILED for outcome in outcomes)


def test_worker_rejects_config_that_no_longer_matches_manifest(tmp_path: Path) -> None:
    _write_bars(tmp_path)
    repository = LocalParquetMarketDataRepository(tmp_path)
    store = InMemoryBacktestRunStore()
    created = BacktestSubmissionService(
        market_data=repository,
        run_store=store,
        job_queue=CapturingQueue(),
        versions=_versions(),
        clock=lambda: datetime(2025, 1, 1, tzinfo=UTC),
    ).submit(_strategy(), BacktestRunConfig(run_robustness=False))
    changed_config = json.loads(created.record.config_json)
    changed_config["slippage_bps"] = "999"
    store._by_run_id[created.record.run_id] = replace(  # pyright: ignore[reportPrivateUsage]
        created.record,
        config_json=json.dumps(changed_config),
    )

    completed = BacktestExecutionService(
        market_data=repository,
        session_reference=ResearchFallbackSessionProvider(),
        run_store=store,
        runtime_identity=_runtime_identity(),
    ).execute(created.record.run_id)

    assert completed.state is BacktestJobState.FAILED
    assert completed.error_code == "config_hash_mismatch"


@pytest.mark.parametrize(
    ("runtime_change", "expected_error"),
    [
        ({"code_revision": "b" * 40}, "code_revision_mismatch"),
        ({"engine_version": "2.0.0a1"}, "engine_version_mismatch"),
        ({"catalog_hash": "sha256:" + "b" * 64}, "catalog_hash_mismatch"),
        ({"fee_schedule_version": "fees.v2"}, "fee_schedule_version_mismatch"),
        ({"market_rule_version": "market-rules.v2"}, "market_rule_version_mismatch"),
        ({"opening_auction_policy": "auction.v2"}, "opening_auction_policy_mismatch"),
        (
            {"entry_signal_validity_policy": "entry-validity.v2"},
            "entry_signal_validity_policy_mismatch",
        ),
        ({"benchmark_policy": "benchmark.v2"}, "benchmark_policy_mismatch"),
    ],
)
def test_worker_rejects_work_created_by_a_different_runtime_identity(
    tmp_path: Path,
    runtime_change: dict[str, str],
    expected_error: str,
) -> None:
    _write_bars(tmp_path)
    repository = LocalParquetMarketDataRepository(tmp_path)
    store = InMemoryBacktestRunStore()
    versions = _versions()
    created = BacktestSubmissionService(
        market_data=repository,
        run_store=store,
        job_queue=CapturingQueue(),
        versions=versions,
        clock=lambda: datetime(2025, 1, 1, tzinfo=UTC),
    ).submit(_strategy(), BacktestRunConfig(run_robustness=False))
    current_identity = replace(
        _runtime_identity(versions, strict_replay=True),
        **runtime_change,
    )

    completed = BacktestExecutionService(
        market_data=repository,
        session_reference=ResearchFallbackSessionProvider(),
        run_store=store,
        runtime_identity=current_identity,
    ).execute(created.record.run_id)

    assert completed.state is BacktestJobState.FAILED
    assert completed.error_code == expected_error


def test_strict_worker_rejects_a_dirty_current_revision_before_loading_data(
    tmp_path: Path,
) -> None:
    _write_bars(tmp_path)
    repository = LocalParquetMarketDataRepository(tmp_path)
    store = InMemoryBacktestRunStore()
    versions = _versions(code_revision=REVISION + "+dirty")
    created = BacktestSubmissionService(
        market_data=repository,
        run_store=store,
        job_queue=CapturingQueue(),
        versions=versions,
        clock=lambda: datetime(2025, 1, 1, tzinfo=UTC),
    ).submit(_strategy(), BacktestRunConfig(run_robustness=False))

    completed = BacktestExecutionService(
        market_data=repository,
        session_reference=ResearchFallbackSessionProvider(),
        run_store=store,
        runtime_identity=_runtime_identity(versions, strict_replay=True),
    ).execute(created.record.run_id)

    assert completed.state is BacktestJobState.FAILED
    assert completed.error_code == "runtime_code_revision_not_clean"
