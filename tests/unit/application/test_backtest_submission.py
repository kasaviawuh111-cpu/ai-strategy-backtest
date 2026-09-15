import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from ashare_lab.adapters.market_data import LocalParquetMarketDataRepository
from ashare_lab.application.backtest_submission import (
    BacktestDataNotYetAvailableError,
    BacktestRunConfig,
    BacktestSubmissionService,
    EventDataUnavailableError,
    SubmissionVersions,
    _effective_warmup_calendar_days,
    latest_stable_a_share_data_date,
)
from ashare_lab.application.execute_backtest import (
    BacktestWorkItemError,
    _financial_facts_from_work_item,
)
from ashare_lab.domain.execution import CapacityMode
from ashare_lab.domain.financials import (
    FinancialFactRecord,
    FinancialMetricId,
    FinancialPeriodBasis,
    FinancialUnit,
    FinancialValueOrigin,
)
from ashare_lab.domain.provenance import SourceKind, SourceRef
from ashare_lab.domain.shared import DomainValidationError
from ashare_lab.domain.strategy import StrategySpec, canonical_hash
from ashare_lab.domain.time import PointInTimeAvailability
from ashare_lab.ports.backtest_runs import (
    BacktestJobState,
    BacktestResultIntegrityPolicy,
    BacktestRunRecord,
    CreateRunResult,
)
from ashare_lab.ports.financial_data import PinnedFinancialFacts

HASH = "sha256:" + "a" * 64


@pytest.fixture
def macd_strategy() -> StrategySpec:
    path = Path(__file__).parents[3] / "contracts/examples/strategy.macd-volume.daily.v1.json"
    return StrategySpec.model_validate_json(path.read_text(encoding="utf-8"))


@pytest.fixture
def event_strategy() -> StrategySpec:
    path = (
        Path(__file__).parents[3] / "contracts/examples/strategy.event-forecast-macd.daily.v1.json"
    )
    return StrategySpec.model_validate_json(path.read_text(encoding="utf-8"))


class MemoryStore:
    def __init__(self) -> None:
        self.by_fingerprint: dict[str, BacktestRunRecord] = {}

    def create_or_get(self, record: BacktestRunRecord) -> CreateRunResult:
        existing = self.by_fingerprint.get(record.fingerprint)
        if existing is not None:
            return CreateRunResult(existing, True)
        self.by_fingerprint[record.fingerprint] = record
        return CreateRunResult(record, False)

    def get(self, _run_id):
        raise NotImplementedError

    def transition(self, _run_id, **_kwargs):
        raise NotImplementedError

    def request_cancel(self, _run_id):
        raise NotImplementedError


class CapturingQueue:
    def __init__(self) -> None:
        self.run_ids: list[str] = []

    def enqueue(self, run_id) -> str:
        self.run_ids.append(str(run_id))
        return "job-1"


class FailOnceQueue(CapturingQueue):
    def enqueue(self, run_id) -> str:
        self.run_ids.append(str(run_id))
        if len(self.run_ids) == 1:
            raise RuntimeError("queue temporarily unavailable")
        return "job-2"


def write_daily(path: Path) -> None:
    pq.write_table(
        pa.table(
            {
                "stock_code": ["300059"],
                "date": [datetime(2025, 1, 2)],
                "open": [10.0],
                "high": [10.0],
                "low": [10.0],
                "close": [10.0],
                "volume": [1000.0],
                "amount": [10000.0],
            }
        ),
        path / "daily_ohlcv.parquet",
    )
    _write_empty_corporate_actions(path)


def _write_empty_corporate_actions(path: Path) -> None:
    schema = pa.schema(
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
    pq.write_table(pa.Table.from_pylist([], schema=schema), path / "corporate_actions.parquet")


def test_event_submission_fails_before_pinning_when_event_data_is_unavailable(
    tmp_path: Path,
    event_strategy: StrategySpec,
) -> None:
    write_daily(tmp_path)
    store = MemoryStore()
    queue = CapturingQueue()
    service = BacktestSubmissionService(
        market_data=LocalParquetMarketDataRepository(tmp_path),
        run_store=store,
        job_queue=queue,
        versions=SubmissionVersions(
            catalog_hash=HASH,
            engine_version="2.0.0a0",
            code_revision="git:test",
        ),
        event_data_available=lambda: False,
    )

    with pytest.raises(EventDataUnavailableError, match=r"events\.parquet"):
        service.submit(event_strategy, BacktestRunConfig())

    assert store.by_fingerprint == {}
    assert queue.run_ids == []


def test_submission_pins_data_and_reenqueues_a_still_queued_replay(
    tmp_path: Path,
    macd_strategy: StrategySpec,
) -> None:
    write_daily(tmp_path)
    store = MemoryStore()
    queue = CapturingQueue()
    service = BacktestSubmissionService(
        market_data=LocalParquetMarketDataRepository(tmp_path),
        run_store=store,
        job_queue=queue,
        versions=SubmissionVersions(
            catalog_hash=HASH,
            engine_version="2.0.0a0",
            code_revision="git:test",
        ),
        clock=lambda: datetime(2025, 1, 2, tzinfo=UTC),
    )

    first = service.submit(macd_strategy, BacktestRunConfig(allocation_ratio=Decimal("0.9")))
    replay = service.submit(macd_strategy, BacktestRunConfig(allocation_ratio=Decimal("0.9")))

    assert first.record.result_integrity_policy is BacktestResultIntegrityPolicy.BUNDLE_HASH_V1

    assert first.record.state is BacktestJobState.QUEUED
    assert replay.record.run_id == first.record.run_id
    assert replay.replayed is True
    assert queue.run_ids == [str(first.record.run_id), str(first.record.run_id)]
    assert '"snapshot_start"' in first.record.config_json


def test_live_submission_caps_settlement_tail_at_latest_stable_data_date(
    tmp_path: Path,
    macd_strategy: StrategySpec,
) -> None:
    write_daily(tmp_path)
    observed_at = datetime(2026, 9, 4, 5, tzinfo=UTC)
    created = BacktestSubmissionService(
        market_data=LocalParquetMarketDataRepository(tmp_path),
        run_store=MemoryStore(),
        job_queue=CapturingQueue(),
        versions=SubmissionVersions(
            catalog_hash=HASH,
            engine_version="2.0.0a0",
            code_revision="git:test",
        ),
        clock=lambda: observed_at,
        latest_stable_data_date=latest_stable_a_share_data_date,
    ).submit(macd_strategy, BacktestRunConfig())

    config = json.loads(created.record.config_json)
    assert config["requested_snapshot_end"] == "2026-09-10"
    assert config["available_data_end"] == "2026-09-03"
    assert config["snapshot_end"] == "2026-09-03"
    assert config["settlement_extension_days"] == 14


def test_live_historical_submission_keeps_the_full_settlement_tail(
    tmp_path: Path,
    macd_strategy: StrategySpec,
) -> None:
    write_daily(tmp_path)
    historical = macd_strategy.model_copy(
        update={
            "backtest": macd_strategy.backtest.model_copy(
                update={"end": date(2026, 8, 1)}
            )
        }
    )
    created = BacktestSubmissionService(
        market_data=LocalParquetMarketDataRepository(tmp_path),
        run_store=MemoryStore(),
        job_queue=CapturingQueue(),
        versions=SubmissionVersions(
            catalog_hash=HASH,
            engine_version="2.0.0a0",
            code_revision="git:test",
        ),
        clock=lambda: datetime(2026, 9, 4, 5, tzinfo=UTC),
        latest_stable_data_date=latest_stable_a_share_data_date,
    ).submit(historical, BacktestRunConfig())

    config = json.loads(created.record.config_json)
    assert config["requested_snapshot_end"] == "2026-08-15"
    assert config["snapshot_end"] == "2026-08-15"


def test_live_submission_rejects_end_after_stable_data_before_pinning(
    macd_strategy: StrategySpec,
) -> None:
    future = macd_strategy.model_copy(
        update={
            "backtest": macd_strategy.backtest.model_copy(
                update={"end": date(2026, 9, 4)}
            )
        }
    )

    class MustNotPin:
        def pin_snapshot(self, *_args, **_kwargs):
            raise AssertionError("market data must not be touched")

    store = MemoryStore()
    queue = CapturingQueue()
    service = BacktestSubmissionService(
        market_data=MustNotPin(),  # type: ignore[arg-type]
        run_store=store,
        job_queue=queue,
        versions=SubmissionVersions(
            catalog_hash=HASH,
            engine_version="2.0.0a0",
            code_revision="git:test",
        ),
        clock=lambda: datetime(2026, 9, 4, 5, tzinfo=UTC),
        latest_stable_data_date=latest_stable_a_share_data_date,
    )

    with pytest.raises(BacktestDataNotYetAvailableError, match="latest stable"):
        service.submit(future, BacktestRunConfig())

    assert store.by_fingerprint == {}
    assert queue.run_ids == []


def test_latest_stable_daily_date_uses_the_shanghai_1605_cutoff() -> None:
    assert latest_stable_a_share_data_date(
        datetime(2026, 9, 4, 7, 59, tzinfo=UTC)
    ) == date(2026, 9, 3)
    assert latest_stable_a_share_data_date(
        datetime(2026, 9, 4, 8, 5, tzinfo=UTC)
    ) == date(2026, 9, 4)


def test_financial_submission_pins_facts_and_worker_rejects_tampering(
    tmp_path: Path,
    macd_strategy: StrategySpec,
) -> None:
    write_daily(tmp_path)
    payload = macd_strategy.model_dump(mode="python")
    payload["entry"] = {
        "type": "financial_condition",
        "metric_id": "valuation.pe",
        "comparator": "lt",
        "value": 20,
        "unit": "TIMES",
        "period_basis": "point_in_time",
    }
    payload["execution"] = {
        "data_capability": "daily_ohlcv_financials",
        "evaluation_frequency": "financial_available_plus_1d_close",
    }
    strategy = StrategySpec.model_validate(payload)
    identity_basis: dict[str, object] = {"provider": "eastmoney", "series": [HASH]}
    checksum = canonical_hash(identity_basis)
    snapshot_id = "financial:" + checksum.removeprefix("sha256:")
    available_at = datetime(2025, 1, 2, 15, tzinfo=UTC)
    revision_id = "eastmoney:valuation.pe:2025-01-02"
    fact = FinancialFactRecord(
        instrument_id="300059.SZ",
        metric_id=FinancialMetricId.PE,
        value=Decimal("18"),
        value_origin=FinancialValueOrigin.PROVIDER_RAW,
        provider="eastmoney",
        source_dataset="RPT_CUSTOM_DMSK_TREND",
        source_field="INDICATOR_VALUE",
        report_period=None,
        report_type=None,
        period_basis=FinancialPeriodBasis.POINT_IN_TIME,
        statement_scope=None,
        unit=FinancialUnit.TIMES,
        availability=PointInTimeAvailability(
            observed_at=available_at,
            announced_at=None,
            first_available_at=available_at,
            signal_at=None,
            execution_at=None,
            retrieved_at=available_at,
            timezone="UTC",
            source="eastmoney",
            revision_id=revision_id,
        ),
        revision_id=revision_id,
        raw_response_sha256=HASH,
        snapshot_id=snapshot_id,
        source_refs=(
            SourceRef(
                provider="eastmoney",
                source_id="valuation.pe:2025-01-02",
                snapshot_id=snapshot_id,
                schema_version="test.v1",
                content_sha256=HASH,
                source_kind=SourceKind.PROVIDER_RECORD,
            ),
        ),
    )

    class Loader:
        def load(self, _strategy, _period, *, retrieved_at):
            assert retrieved_at == available_at
            return PinnedFinancialFacts(
                snapshot_id=snapshot_id,
                checksum=checksum,
                provider="eastmoney",
                schema_version="test.v1",
                coverage_start=date(2025, 1, 2),
                coverage_end=date(2025, 1, 2),
                identity_basis=identity_basis,
                facts=(fact,),
            )

    created = BacktestSubmissionService(
        market_data=LocalParquetMarketDataRepository(tmp_path),
        run_store=MemoryStore(),
        job_queue=CapturingQueue(),
        versions=SubmissionVersions(
            catalog_hash=HASH,
            engine_version="2.0.0a0",
            code_revision="git:test",
        ),
        clock=lambda: available_at,
        financial_fact_loader=Loader(),
    ).submit(strategy, BacktestRunConfig())
    config = json.loads(created.record.config_json)
    manifest = json.loads(created.record.manifest_json)

    restored = _financial_facts_from_work_item(strategy, manifest, config)
    assert restored == (fact,)
    assert manifest["financial_snapshot"] == config["financial_snapshot"]

    config["financial_snapshot"]["identity_basis"]["provider"] = "tampered"
    with pytest.raises(BacktestWorkItemError, match="manifest_mismatch"):
        _financial_facts_from_work_item(strategy, manifest, config)


def test_enqueue_failure_is_recovered_by_replaying_the_queued_submission(
    tmp_path: Path,
    macd_strategy: StrategySpec,
) -> None:
    write_daily(tmp_path)
    store = MemoryStore()
    queue = FailOnceQueue()
    service = BacktestSubmissionService(
        market_data=LocalParquetMarketDataRepository(tmp_path),
        run_store=store,
        job_queue=queue,
        versions=SubmissionVersions(
            catalog_hash=HASH,
            engine_version="2.0.0a0",
            code_revision="git:test",
        ),
        clock=lambda: datetime(2025, 1, 2, tzinfo=UTC),
    )

    with pytest.raises(RuntimeError, match="temporarily unavailable"):
        service.submit(macd_strategy, BacktestRunConfig())

    recovered = service.submit(macd_strategy, BacktestRunConfig())

    assert recovered.replayed is True
    assert recovered.record.state is BacktestJobState.QUEUED
    assert queue.run_ids == [str(recovered.record.run_id), str(recovered.record.run_id)]


def test_submission_fingerprint_changes_with_execution_assumption(
    tmp_path: Path,
    macd_strategy: StrategySpec,
) -> None:
    write_daily(tmp_path)
    store = MemoryStore()
    queue = CapturingQueue()
    service = BacktestSubmissionService(
        market_data=LocalParquetMarketDataRepository(tmp_path),
        run_store=store,
        job_queue=queue,
        versions=SubmissionVersions(
            catalog_hash=HASH,
            engine_version="2.0.0a0",
            code_revision="git:test",
        ),
        clock=lambda: datetime(2025, 1, 2, tzinfo=UTC),
    )

    first = service.submit(macd_strategy, BacktestRunConfig(slippage_bps=Decimal("5")))
    second = service.submit(macd_strategy, BacktestRunConfig(slippage_bps=Decimal("10")))

    assert first.record.fingerprint != second.record.fingerprint
    assert len(queue.run_ids) == 2


def test_capacity_mode_is_pinned_in_config_manifest_and_fingerprint(
    tmp_path: Path,
    macd_strategy: StrategySpec,
) -> None:
    write_daily(tmp_path)
    store = MemoryStore()
    queue = CapturingQueue()
    service = BacktestSubmissionService(
        market_data=LocalParquetMarketDataRepository(tmp_path),
        run_store=store,
        job_queue=queue,
        versions=SubmissionVersions(
            catalog_hash=HASH,
            engine_version="2.0.0a0",
            code_revision="git:test",
        ),
        clock=lambda: datetime(2025, 1, 2, tzinfo=UTC),
    )

    default = service.submit(macd_strategy, BacktestRunConfig())
    unlimited = service.submit(
        macd_strategy,
        BacktestRunConfig(capacity_mode=CapacityMode.UNLIMITED),
    )
    default_config = json.loads(default.record.config_json)
    default_manifest = json.loads(default.record.manifest_json)

    assert default_config["capacity_mode"] == "point_in_time_volume"
    assert default_manifest["assumptions"]["capacity_mode"] == "point_in_time_volume"
    assert default_manifest["assumptions"]["opening_auction_policy"] == (
        "cn.a_share.daily.published_open_proxy.latency_1s.cutoff_0915."
        "recorded_0930.not_exact.day_order.v2"
    )
    assert default_config["edge_entry_validity_sessions"] == 1
    assert default_config["event_entry_validity_sessions"] == 1
    assert default_config["state_entry_validity_sessions"] == 1
    assert default_manifest["assumptions"]["entry_signal_validity_policy"] == (
        "cn.a_share.daily.entry_signal_validity.edge_event_state.composite_fail_closed."
        "default_one_attempt.persistent_account_exits.v4"
    )
    assert default_manifest["assumptions"]["edge_entry_validity_sessions"] == "1"
    assert default_manifest["assumptions"]["event_entry_validity_sessions"] == "1"
    assert default_manifest["assumptions"]["state_entry_validity_sessions"] == "1"
    assert default.record.fingerprint != unlimited.record.fingerprint


def test_entry_signal_validity_is_a_pinned_fingerprint_input(
    tmp_path: Path,
    macd_strategy: StrategySpec,
) -> None:
    write_daily(tmp_path)
    service = BacktestSubmissionService(
        market_data=LocalParquetMarketDataRepository(tmp_path),
        run_store=MemoryStore(),
        job_queue=CapturingQueue(),
        versions=SubmissionVersions(
            catalog_hash=HASH,
            engine_version="2.0.0a0",
            code_revision="git:test",
        ),
        clock=lambda: datetime(2025, 1, 2, tzinfo=UTC),
    )

    default = service.submit(macd_strategy, BacktestRunConfig())
    changed = service.submit(
        macd_strategy,
        BacktestRunConfig(edge_entry_validity_sessions=4),
    )

    assert default.record.fingerprint != changed.record.fingerprint


@pytest.mark.parametrize("value", [0, 21, True])
def test_entry_signal_validity_rejects_invalid_session_counts(value: int) -> None:
    with pytest.raises(DomainValidationError, match="validity sessions"):
        BacktestRunConfig(edge_entry_validity_sessions=value)


@pytest.mark.parametrize("value", [2, 5, 20])
def test_event_signal_retry_fails_closed_without_revision_invalidation(value: int) -> None:
    with pytest.raises(DomainValidationError, match="revision invalidation"):
        BacktestRunConfig(event_entry_validity_sessions=value)


@pytest.mark.parametrize("value", [2, 5, 20])
def test_state_signal_retry_fails_closed_without_point_in_time_revalidation(
    value: int,
) -> None:
    with pytest.raises(DomainValidationError, match="state revalidation"):
        BacktestRunConfig(state_entry_validity_sessions=value)


def test_fingerprint_covers_portfolio_and_reporting_behavior(
    tmp_path: Path,
    macd_strategy: StrategySpec,
) -> None:
    write_daily(tmp_path)
    store = MemoryStore()
    queue = CapturingQueue()
    service = BacktestSubmissionService(
        market_data=LocalParquetMarketDataRepository(tmp_path),
        run_store=store,
        job_queue=queue,
        versions=SubmissionVersions(
            catalog_hash=HASH,
            engine_version="2.0.0a0",
            code_revision="git:test",
        ),
        clock=lambda: datetime(2025, 1, 2, tzinfo=UTC),
    )

    base = service.submit(
        macd_strategy,
        BacktestRunConfig(allocation_ratio=Decimal("0.8"), run_robustness=True),
    )
    changed_allocation = service.submit(
        macd_strategy,
        BacktestRunConfig(allocation_ratio=Decimal("0.9"), run_robustness=True),
    )
    changed_report = service.submit(
        macd_strategy,
        BacktestRunConfig(allocation_ratio=Decimal("0.8"), run_robustness=False),
    )

    assert (
        len(
            {
                base.record.fingerprint,
                changed_allocation.record.fingerprint,
                changed_report.record.fingerprint,
            }
        )
        == 3
    )


def test_long_indicator_dependency_expands_the_snapshot_warmup(
    tmp_path: Path,
    macd_strategy: StrategySpec,
) -> None:
    write_daily(tmp_path)
    payload = macd_strategy.model_dump(mode="python")
    payload["entry"]["children"][0]["params"] = {  # type: ignore[index]
        "fast": 12,
        "slow": 1_000,
        "signal": 500,
    }
    long_strategy = StrategySpec.model_validate(payload)
    service = BacktestSubmissionService(
        market_data=LocalParquetMarketDataRepository(tmp_path),
        run_store=MemoryStore(),
        job_queue=CapturingQueue(),
        versions=SubmissionVersions(
            catalog_hash=HASH,
            engine_version="2.0.0a0",
            code_revision="git:test",
        ),
        clock=lambda: datetime(2025, 1, 2, tzinfo=UTC),
    )

    created = service.submit(long_strategy, BacktestRunConfig())
    config = json.loads(created.record.config_json)

    assert config["effective_warmup_calendar_days"] == 2_414
    assert config["snapshot_start"] == "2014-05-24"


def test_snapshot_warmup_has_a_hard_upper_bound() -> None:
    with pytest.raises(ValueError, match="snapshot extensions are invalid"):
        BacktestRunConfig(warmup_calendar_days=3_651)


def test_parameterized_bbi_dependency_expands_warmup(
    macd_strategy: StrategySpec,
) -> None:
    payload = macd_strategy.model_dump(mode="python")
    payload["entry"] = {
        "type": "indicator_condition",
        "indicator_id": "technical.bbi",
        "definition_version": "1.0.0",
        "params": {
            "period_1": 3,
            "period_2": 6,
            "period_3": 12,
            "period_4": 1_000,
            "price_field": "close",
        },
        "timeframe": "1d",
        "evaluation_mode": "bar_close_confirmed",
        "trigger": "price_crosses_above",
        "value": None,
    }
    strategy = StrategySpec.model_validate(payload)

    assert (
        _effective_warmup_calendar_days(
            strategy,
            BacktestRunConfig(warmup_calendar_days=0),
        )
        == 1_616
    )


@pytest.mark.parametrize(
    ("indicator_id", "params", "trigger", "value", "expected_days"),
    (
        (
            "price.return_pct",
            {"period": 100, "price_field": "close"},
            "crosses_above",
            5,
            178,
        ),
        (
            "price.rolling_high",
            {"period": 100, "price_field": "close"},
            "new_high",
            None,
            176,
        ),
        ("price.consecutive_up", {"days": 100}, "at_least", None, 176),
        ("price.amplitude", {}, "crosses_above", 5, 19),
        ("market.amount", {}, "crosses_above", 100_000_000, 18),
        ("amount.average", {"period": 100}, "crosses_above", 100_000_000, 176),
        ("technical.rsi", {"period": 14}, "below", 30, 38),
        ("technical.rsi", {"period": 14}, "crosses_below", 30, 40),
    ),
)
def test_second_batch_indicator_dependencies_expand_warmup(
    macd_strategy: StrategySpec,
    indicator_id: str,
    params: dict[str, object],
    trigger: str,
    value: int | None,
    expected_days: int,
) -> None:
    payload = macd_strategy.model_dump(mode="python")
    condition = {
        "type": "indicator_condition",
        "indicator_id": indicator_id,
        "definition_version": "1.0.0",
        "params": params,
        "timeframe": "1d",
        "evaluation_mode": "bar_close_confirmed",
        "trigger": trigger,
        "value": value,
    }
    payload["entry"] = condition
    payload["exit"] = {"op": "first_of", "children": [condition]}
    strategy = StrategySpec.model_validate(payload)

    assert (
        _effective_warmup_calendar_days(
            strategy,
            BacktestRunConfig(warmup_calendar_days=0),
        )
        == expected_days
    )


@pytest.mark.parametrize(
    ("indicator_id", "params", "trigger", "value", "expected_days"),
    (
        ("price.true_range", {}, "crosses_above", 1, 19),
        ("technical.atr", {"period": 100}, "crosses_above", 1, 178),
        ("technical.natr", {"period": 100}, "crosses_above", 1, 178),
        ("technical.adx", {"period": 100}, "crosses_above", 25, 336),
        ("technical.dmi", {"period": 100}, "plus_crosses_above_minus", None, 336),
        (
            "technical.bias",
            {"period": 100, "price_field": "close"},
            "crosses_above",
            0,
            176,
        ),
        (
            "technical.roc",
            {"period": 100, "price_field": "close"},
            "crosses_above",
            0,
            178,
        ),
        (
            "technical.momentum",
            {"period": 100, "price_field": "close"},
            "crosses_above",
            0,
            178,
        ),
        (
            "technical.stochastic",
            {"k_period": 100, "d_period": 10},
            "k_crosses_above_d",
            None,
            190,
        ),
        ("technical.williams_r", {"period": 100}, "crosses_above", -50, 176),
        (
            "technical.donchian",
            {"period": 100},
            "price_crosses_above_upper",
            None,
            178,
        ),
        (
            "technical.return_stddev",
            {"period": 100, "price_field": "close"},
            "crosses_above",
            2,
            178,
        ),
        (
            "technical.historical_volatility",
            {"period": 100, "annualization_sessions": 252, "price_field": "close"},
            "crosses_above",
            20,
            178,
        ),
    ),
)
def test_p1_indicator_dependencies_use_parameterized_warmup(
    macd_strategy: StrategySpec,
    indicator_id: str,
    params: dict[str, object],
    trigger: str,
    value: int | None,
    expected_days: int,
) -> None:
    payload = macd_strategy.model_dump(mode="python")
    condition = {
        "type": "indicator_condition",
        "indicator_id": indicator_id,
        "definition_version": "1.0.0",
        "params": params,
        "timeframe": "1d",
        "evaluation_mode": "bar_close_confirmed",
        "trigger": trigger,
        "value": value,
    }
    payload["entry"] = condition
    payload["exit"] = {"op": "first_of", "children": [condition]}
    strategy = StrategySpec.model_validate(payload)

    assert (
        _effective_warmup_calendar_days(
            strategy,
            BacktestRunConfig(warmup_calendar_days=0),
        )
        == expected_days
    )


@pytest.mark.parametrize(
    ("indicator_id", "params", "trigger", "value", "expected_days"),
    (
        (
            "volume.relative",
            {"baseline_period": 100, "consecutive_days": 10},
            "consecutive_gte_multiple",
            1.2,
            190,
        ),
        (
            "volume.price_confirmation",
            {"baseline_period": 100, "volume_multiple": 1.5, "return_threshold_pct": 5},
            "surge_up",
            None,
            176,
        ),
        ("technical.obv", {}, "rising", None, 18),
        (
            "volume.price_divergence",
            {
                "left_bars": 3,
                "right_bars": 4,
                "min_separation": 5,
                "max_separation": 100,
                "price_threshold_pct": 2,
                "obv_threshold_adv": 1,
                "average_volume_period": 20,
            },
            "bearish",
            None,
            214,
        ),
        (
            "volume.price_divergence",
            {
                "left_bars": 3,
                "right_bars": 3,
                "min_separation": 500,
                "max_separation": 600,
                "price_threshold_pct": 2,
                "obv_threshold_adv": 1,
                "average_volume_period": 20,
            },
            "bearish",
            None,
            1_786,
        ),
        (
            "technical.trend_regime",
            {
                "short_period": 20,
                "long_period": 100,
                "slope_lookback": 10,
                "adx_period": 20,
                "adx_threshold": 25,
                "confirmation_days": 3,
                "stability_bars": 150,
            },
            "uptrend",
            None,
            258,
        ),
    ),
)
def test_volume_and_trend_dependencies_expand_warmup(
    macd_strategy: StrategySpec,
    indicator_id: str,
    params: dict[str, object],
    trigger: str,
    value: float | None,
    expected_days: int,
) -> None:
    payload = macd_strategy.model_dump(mode="python")
    condition = {
        "type": "indicator_condition",
        "indicator_id": indicator_id,
        "definition_version": "1.0.0",
        "params": params,
        "timeframe": "1d",
        "evaluation_mode": "bar_close_confirmed",
        "trigger": trigger,
        "value": value,
    }
    payload["entry"] = condition
    payload["exit"] = {"op": "first_of", "children": [condition]}
    strategy = StrategySpec.model_validate(payload)

    assert (
        _effective_warmup_calendar_days(
            strategy,
            BacktestRunConfig(warmup_calendar_days=0),
        )
        == expected_days
    )


@pytest.mark.parametrize(
    ("indicator_id", "params", "trigger"),
    (
        (
            "volume.price_divergence",
            {
                "left_bars": 100,
                "right_bars": 100,
                "min_separation": 1_000,
                "max_separation": 5_000,
                "price_threshold_pct": 2,
                "obv_threshold_adv": 1,
                "average_volume_period": 1_000,
            },
            "bearish",
        ),
        (
            "technical.trend_regime",
            {
                "short_period": 1_000,
                "long_period": 5_000,
                "slope_lookback": 1_000,
                "adx_period": 1_000,
                "adx_threshold": 25,
                "confirmation_days": 100,
                "stability_bars": 10_000,
            },
            "uptrend",
        ),
    ),
)
def test_indicator_warmup_above_snapshot_limit_is_rejected_instead_of_truncated(
    macd_strategy: StrategySpec,
    indicator_id: str,
    params: dict[str, object],
    trigger: str,
) -> None:
    payload = macd_strategy.model_dump(mode="python")
    condition = {
        "type": "indicator_condition",
        "indicator_id": indicator_id,
        "definition_version": "1.0.0",
        "params": params,
        "timeframe": "1d",
        "evaluation_mode": "bar_close_confirmed",
        "trigger": trigger,
        "value": None,
    }
    payload["entry"] = condition
    payload["exit"] = {"op": "first_of", "children": [condition]}
    strategy = StrategySpec.model_validate(payload)

    with pytest.raises(DomainValidationError, match="exceeding the 3650-day snapshot limit"):
        _effective_warmup_calendar_days(
            strategy,
            BacktestRunConfig(warmup_calendar_days=0),
        )
