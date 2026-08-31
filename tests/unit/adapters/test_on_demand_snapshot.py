from __future__ import annotations

import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

import pytest

from ashare_lab.adapters.market_data.on_demand_snapshot import (
    OnDemandSnapshotMarketDataRepository,
    SnapshotPreparationFailedError,
    SnapshotPreparationIncompleteError,
    SnapshotPreparationResult,
)
from ashare_lab.adapters.market_data.snapshot_registry import (
    SnapshotRegistryIntegrityError,
    SnapshotRegistryMarketDataRepository,
    SnapshotRegistryNoMatchError,
)
from ashare_lab.domain.market_data import (
    CorporateAction,
    DailyBar,
    DataSnapshotRef,
    EventEnvelope,
    InstrumentSession,
    MinuteBar,
    MinuteClose,
)
from ashare_lab.domain.shared import InstrumentId, StrongId
from ashare_lab.ports.market_data import DataRequirements, DateRange

INSTRUMENT = InstrumentId("300059.SZ")
PERIOD = DateRange(date(2021, 1, 4), date(2025, 12, 31))
REF = DataSnapshotRef(
    snapshot_id=StrongId("snapshot:" + "1" * 64),
    checksum="sha256:" + "1" * 64,
    schema_version="local-parquet.market-data.v3",
    created_at=datetime(2026, 8, 30, tzinfo=UTC),
    producer_snapshot_id="composite:" + "2" * 64,
)
FUTURE_REF = DataSnapshotRef(
    snapshot_id=StrongId("snapshot:" + "9" * 64),
    checksum="sha256:" + "9" * 64,
    schema_version="local-parquet.market-data.v3",
    created_at=datetime(2099, 1, 1, tzinfo=UTC),
    producer_snapshot_id="composite:" + "9" * 64,
)


def _requirements(*, expected: bool = False) -> DataRequirements:
    values: dict[str, object] = {}
    if expected:
        values = {
            "expected_snapshot_id": REF.snapshot_id,
            "expected_snapshot_checksum": REF.checksum,
            "expected_snapshot_schema_version": REF.schema_version,
            "expected_producer_snapshot_id": REF.producer_snapshot_id,
        }
    return DataRequirements(
        instruments=(INSTRUMENT,),
        datasets=("daily_ohlcv", "corporate_actions", "events"),
        event_codes=("event.financial_results.annual_report",),
        **values,
    )


class FakeRegistry:
    def __init__(
        self,
        *,
        available: bool,
        generic_ref: DataSnapshotRef = REF,
        producer_ref: DataSnapshotRef = REF,
    ) -> None:
        self.available = available
        self.generic_ref = generic_ref
        self.producer_ref = producer_ref
        self.refresh_calls = 0
        self.pin_calls = 0
        self.selected_pin_calls: list[tuple[str, DataRequirements, DateRange]] = []
        self.producer_pin_calls: list[tuple[str, Path, DataRequirements, DateRange]] = []
        self.load_calls: list[tuple[str, object, InstrumentId, DateRange]] = []
        self._condition = threading.Condition()
        self.load_results: dict[str, object] = {
            "daily": [object()],
            "signal": [object()],
            "minute": [object()],
            "signal_minute": [object()],
            "sessions": [object()],
            "events": [object()],
            "actions": [object()],
        }

    def pin_snapshot(
        self,
        requirements: DataRequirements,
        period: DateRange,
    ) -> DataSnapshotRef:
        del requirements, period
        with self._condition:
            self.pin_calls += 1
            self._condition.notify_all()
        if not self.available:
            raise SnapshotRegistryNoMatchError("fixture registry miss")
        return self.generic_ref

    def pin_producer_snapshot(
        self,
        producer_snapshot_id: str,
        producer_snapshot_path: str | Path,
        requirements: DataRequirements,
        period: DateRange,
    ) -> DataSnapshotRef:
        path = Path(producer_snapshot_path).resolve()
        self.producer_pin_calls.append((producer_snapshot_id, path, requirements, period))
        if not self.available:
            raise SnapshotRegistryNoMatchError("fixture exact producer miss")
        return self.producer_ref

    def pin_selected_snapshot(
        self,
        producer_snapshot_id: str,
        requirements: DataRequirements,
        period: DateRange,
    ) -> DataSnapshotRef:
        self.selected_pin_calls.append((producer_snapshot_id, requirements, period))
        if not self.available:
            raise SnapshotRegistryNoMatchError("fixture registry miss")
        return self.generic_ref

    def wait_for_pin_calls(self, count: int) -> None:
        with self._condition:
            assert self._condition.wait_for(lambda: self.pin_calls >= count, timeout=5)

    def refresh(self) -> int:
        self.refresh_calls += 1
        return 1 if self.available else 0

    def _load(
        self,
        name: str,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> object:
        self.load_calls.append((name, snapshot, instrument_id, period))
        return self.load_results[name]

    def load_daily_bars(
        self,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> Sequence[DailyBar]:
        return cast(Sequence[DailyBar], self._load("daily", snapshot, instrument_id, period))

    def load_signal_bars(
        self,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> Sequence[DailyBar]:
        return cast(Sequence[DailyBar], self._load("signal", snapshot, instrument_id, period))

    def load_minute_bars(
        self,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> Sequence[MinuteBar]:
        return cast(Sequence[MinuteBar], self._load("minute", snapshot, instrument_id, period))

    def load_signal_minute_closes(
        self,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> Sequence[MinuteClose]:
        return cast(
            Sequence[MinuteClose],
            self._load("signal_minute", snapshot, instrument_id, period),
        )

    def load_sessions(
        self,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> Sequence[InstrumentSession]:
        return cast(
            Sequence[InstrumentSession],
            self._load("sessions", snapshot, instrument_id, period),
        )

    def load_events(
        self,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> Sequence[EventEnvelope]:
        return cast(Sequence[EventEnvelope], self._load("events", snapshot, instrument_id, period))

    def load_corporate_actions(
        self,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> Sequence[CorporateAction]:
        return cast(
            Sequence[CorporateAction],
            self._load("actions", snapshot, instrument_id, period),
        )


@dataclass
class FakePreparer:
    registry: FakeRegistry
    result: SnapshotPreparationResult
    publish: bool = True
    failure: Exception | None = None
    calls: int = 0

    def prepare(
        self,
        requirements: DataRequirements,
        period: DateRange,
    ) -> SnapshotPreparationResult:
        del requirements, period
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        if self.publish:
            self.registry.available = True
        return self.result


class BlockingPreparer(FakePreparer):
    def __init__(self, registry: FakeRegistry, result: SnapshotPreparationResult) -> None:
        super().__init__(registry, result)
        self.started = threading.Event()
        self.release = threading.Event()
        self._calls_lock = threading.Lock()

    def prepare(
        self,
        requirements: DataRequirements,
        period: DateRange,
    ) -> SnapshotPreparationResult:
        del requirements, period
        with self._calls_lock:
            self.calls += 1
        self.started.set()
        assert self.release.wait(timeout=5)
        self.registry.available = True
        return self.result


def _result(tmp_path: Path) -> SnapshotPreparationResult:
    output = tmp_path / ("2" * 64)
    output.mkdir()
    return SnapshotPreparationResult(
        producer_snapshot_id="composite:" + "2" * 64,
        path=output,
    )


def _wrapper(
    registry: FakeRegistry,
    preparer: FakePreparer,
    *,
    refresh_each_submission: bool = False,
) -> OnDemandSnapshotMarketDataRepository:
    return OnDemandSnapshotMarketDataRepository(
        cast(SnapshotRegistryMarketDataRepository, registry),
        preparer,
        refresh_each_submission=refresh_each_submission,
    )


def test_registry_hit_never_prepares(tmp_path: Path) -> None:
    registry = FakeRegistry(available=True)
    preparer = FakePreparer(registry, _result(tmp_path))
    repository = _wrapper(registry, preparer)

    snapshot = repository.pin_snapshot(_requirements(), PERIOD)

    assert snapshot is REF
    assert preparer.calls == 0
    assert registry.refresh_calls == 0
    assert repository.preparation_results == ()


def test_refresh_policy_reacquires_for_each_sequential_fresh_submission(
    tmp_path: Path,
) -> None:
    registry = FakeRegistry(available=True)
    result = _result(tmp_path)
    preparer = FakePreparer(registry, result)
    repository = _wrapper(
        registry,
        preparer,
        refresh_each_submission=True,
    )

    assert repository.pin_snapshot(_requirements(), PERIOD) is REF
    assert repository.pin_snapshot(_requirements(), PERIOD) is REF

    assert preparer.calls == 2
    assert registry.refresh_calls == 0
    assert [item[0] for item in registry.producer_pin_calls] == [
        result.producer_snapshot_id,
        result.producer_snapshot_id,
    ]
    assert repository.preparation_results == (result, result)


def test_refresh_policy_never_returns_old_snapshot_when_provider_fails(
    tmp_path: Path,
) -> None:
    registry = FakeRegistry(available=True)
    source_error = SnapshotPreparationFailedError("stable provider failure")
    preparer = FakePreparer(
        registry,
        _result(tmp_path),
        failure=source_error,
    )
    repository = _wrapper(
        registry,
        preparer,
        refresh_each_submission=True,
    )

    with pytest.raises(SnapshotPreparationFailedError) as caught:
        repository.pin_snapshot(_requirements(), PERIOD)

    assert caught.value is source_error
    assert registry.pin_calls == 1
    assert preparer.calls == 1
    assert registry.producer_pin_calls == []
    assert repository.preparation_results == ()


def test_refresh_policy_never_returns_old_snapshot_when_new_publication_is_incomplete(
    tmp_path: Path,
) -> None:
    registry = FakeRegistry(available=True)
    result = _result(tmp_path)
    preparer = FakePreparer(registry, result)
    repository = _wrapper(
        registry,
        preparer,
        refresh_each_submission=True,
    )

    def reject_new_publication(
        producer_snapshot_id: str,
        producer_snapshot_path: str | Path,
        requirements: DataRequirements,
        period: DateRange,
    ) -> DataSnapshotRef:
        registry.producer_pin_calls.append(
            (
                producer_snapshot_id,
                Path(producer_snapshot_path).resolve(),
                requirements,
                period,
            )
        )
        raise SnapshotRegistryNoMatchError("fixture exact producer is incomplete")

    registry.pin_producer_snapshot = reject_new_publication  # type: ignore[method-assign]
    with pytest.raises(
        SnapshotPreparationIncompleteError,
        match="prepared producer snapshot still does not cover the request",
    ) as caught:
        repository.pin_snapshot(_requirements(), PERIOD)

    assert result.producer_snapshot_id in str(caught.value)
    assert registry.pin_calls == 1
    assert preparer.calls == 1
    assert registry.producer_pin_calls == [
        (result.producer_snapshot_id, result.path, _requirements(), PERIOD)
    ]
    assert repository.preparation_results == (result,)


def test_registry_miss_prepares_refreshes_and_retries(tmp_path: Path) -> None:
    registry = FakeRegistry(available=False)
    result = _result(tmp_path)
    preparer = FakePreparer(registry, result)
    repository = _wrapper(registry, preparer)

    snapshot = repository.pin_snapshot(_requirements(), PERIOD)

    assert snapshot is REF
    assert preparer.calls == 1
    assert registry.refresh_calls == 0
    assert registry.producer_pin_calls == [
        (result.producer_snapshot_id, result.path, _requirements(), PERIOD)
    ]
    assert repository.preparation_results == (result,)


def test_expected_snapshot_miss_never_prepares_or_refreshes(tmp_path: Path) -> None:
    registry = FakeRegistry(available=False)
    preparer = FakePreparer(registry, _result(tmp_path))
    repository = _wrapper(registry, preparer)

    with pytest.raises(SnapshotRegistryNoMatchError, match="fixture registry miss"):
        repository.pin_snapshot(_requirements(expected=True), PERIOD)

    assert preparer.calls == 0
    assert registry.refresh_calls == 0
    assert registry.pin_calls == 0
    assert registry.selected_pin_calls == [
        (REF.producer_snapshot_id, _requirements(expected=True), PERIOD)
    ]


def test_expected_replay_without_producer_content_id_fails_without_broad_scan(
    tmp_path: Path,
) -> None:
    registry = FakeRegistry(available=True)
    preparer = FakePreparer(registry, _result(tmp_path))
    repository = _wrapper(registry, preparer)
    legacy_requirements = replace(
        _requirements(expected=True),
        expected_producer_snapshot_id=None,
    )

    with pytest.raises(SnapshotRegistryNoMatchError, match="requires an expected producer"):
        repository.pin_snapshot(legacy_requirements, PERIOD)

    assert registry.pin_calls == 0
    assert registry.selected_pin_calls == []
    assert preparer.calls == 0


def test_expected_snapshot_repin_never_prepares_under_refresh_policy(tmp_path: Path) -> None:
    registry = FakeRegistry(available=True)
    preparer = FakePreparer(registry, _result(tmp_path))
    repository = _wrapper(registry, preparer, refresh_each_submission=True)

    assert repository.pin_snapshot(_requirements(expected=True), PERIOD) is REF

    assert preparer.calls == 0
    assert registry.producer_pin_calls == []
    assert registry.pin_calls == 0
    assert registry.selected_pin_calls == [
        (REF.producer_snapshot_id, _requirements(expected=True), PERIOD)
    ]


def test_registry_integrity_failure_never_triggers_preparation(tmp_path: Path) -> None:
    registry = FakeRegistry(available=False)
    preparer = FakePreparer(registry, _result(tmp_path))
    repository = _wrapper(registry, preparer)

    def reject_integrity(
        requirements: DataRequirements,
        period: DateRange,
    ) -> DataSnapshotRef:
        del requirements, period
        raise SnapshotRegistryIntegrityError("fixture integrity failure")

    registry.pin_snapshot = reject_integrity  # type: ignore[method-assign]
    with pytest.raises(SnapshotRegistryIntegrityError, match="fixture integrity failure"):
        repository.pin_snapshot(_requirements(), PERIOD)

    assert preparer.calls == 0
    assert registry.refresh_calls == 0


def test_concurrent_identical_misses_prepare_once(tmp_path: Path) -> None:
    registry = FakeRegistry(available=False)
    preparer = BlockingPreparer(registry, _result(tmp_path))
    repository = _wrapper(registry, preparer)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(repository.pin_snapshot, _requirements(), PERIOD)
        assert preparer.started.wait(timeout=5)
        second = pool.submit(repository.pin_snapshot, _requirements(), PERIOD)
        registry.wait_for_pin_calls(3)
        preparer.release.set()
        assert first.result(timeout=5) is REF
        assert second.result(timeout=5) is REF

    assert preparer.calls == 1
    assert registry.refresh_calls == 0
    assert len(registry.producer_pin_calls) == 2
    assert {item[:2] for item in registry.producer_pin_calls} == {
        (preparer.result.producer_snapshot_id, preparer.result.path)
    }


def test_concurrent_refresh_pins_the_prepared_old_snapshot_not_a_future_registry_entry(
    tmp_path: Path,
) -> None:
    registry = FakeRegistry(
        available=True,
        generic_ref=FUTURE_REF,
        producer_ref=REF,
    )
    result = _result(tmp_path)
    preparer = BlockingPreparer(registry, result)
    repository = _wrapper(registry, preparer, refresh_each_submission=True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(repository.pin_snapshot, _requirements(), PERIOD)
        assert preparer.started.wait(timeout=5)
        second = pool.submit(repository.pin_snapshot, _requirements(), PERIOD)
        registry.wait_for_pin_calls(2)
        preparer.release.set()

        assert first.result(timeout=5) is REF
        assert second.result(timeout=5) is REF

    assert preparer.calls == 1
    assert [item[:2] for item in registry.producer_pin_calls] == [
        (result.producer_snapshot_id, result.path),
        (result.producer_snapshot_id, result.path),
    ]


def test_concurrent_identical_failures_share_one_attempt(tmp_path: Path) -> None:
    registry = FakeRegistry(available=False)
    source_error = SnapshotPreparationFailedError("stable provider failure")
    preparer = BlockingPreparer(registry, _result(tmp_path))
    repository = _wrapper(registry, preparer)

    def fail_after_release(
        requirements: DataRequirements,
        period: DateRange,
    ) -> SnapshotPreparationResult:
        del requirements, period
        with preparer._calls_lock:
            preparer.calls += 1
        preparer.started.set()
        assert preparer.release.wait(timeout=5)
        raise source_error

    preparer.prepare = fail_after_release  # type: ignore[method-assign]
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(repository.pin_snapshot, _requirements(), PERIOD)
        assert preparer.started.wait(timeout=5)
        second = pool.submit(repository.pin_snapshot, _requirements(), PERIOD)
        registry.wait_for_pin_calls(3)
        preparer.release.set()
        with pytest.raises(SnapshotPreparationFailedError) as first_error:
            first.result(timeout=5)
        with pytest.raises(SnapshotPreparationFailedError) as second_error:
            second.result(timeout=5)

    assert first_error.value is source_error
    assert second_error.value is source_error
    assert preparer.calls == 1
    assert registry.refresh_calls == 0
    assert registry.producer_pin_calls == []


def test_preparer_return_without_registry_coverage_fails_closed(tmp_path: Path) -> None:
    registry = FakeRegistry(available=False)
    result = _result(tmp_path)
    preparer = FakePreparer(registry, result, publish=False)
    repository = _wrapper(registry, preparer)

    with pytest.raises(
        SnapshotPreparationIncompleteError,
        match="still does not cover the request",
    ) as caught:
        repository.pin_snapshot(_requirements(), PERIOD)

    assert result.producer_snapshot_id in str(caught.value)
    assert str(result.path) in str(caught.value)
    assert preparer.calls == 1
    assert registry.refresh_calls == 0
    assert len(registry.producer_pin_calls) == 1
    assert repository.preparation_results == (result,)


def test_unknown_preparer_exception_is_wrapped_with_stable_failure(tmp_path: Path) -> None:
    registry = FakeRegistry(available=False)
    source_error = RuntimeError("provider secret diagnostic")
    preparer = FakePreparer(registry, _result(tmp_path), failure=source_error)
    repository = _wrapper(registry, preparer)

    with pytest.raises(
        SnapshotPreparationFailedError,
        match=r"on-demand snapshot preparation failed for instrument=300059\.SZ",
    ) as caught:
        repository.pin_snapshot(_requirements(), PERIOD)

    assert caught.value.__cause__ is source_error
    assert "provider secret diagnostic" not in str(caught.value)


def test_stable_preparation_exception_is_propagated(tmp_path: Path) -> None:
    registry = FakeRegistry(available=False)
    source_error = SnapshotPreparationFailedError("stable provider failure")
    preparer = FakePreparer(registry, _result(tmp_path), failure=source_error)
    repository = _wrapper(registry, preparer)

    with pytest.raises(SnapshotPreparationFailedError) as caught:
        repository.pin_snapshot(_requirements(), PERIOD)

    assert caught.value is source_error


def test_all_load_methods_delegate_arguments_and_return_values(tmp_path: Path) -> None:
    registry = FakeRegistry(available=True)
    repository = _wrapper(registry, FakePreparer(registry, _result(tmp_path)))

    results = (
        repository.load_daily_bars(REF, INSTRUMENT, PERIOD),
        repository.load_signal_bars(REF, INSTRUMENT, PERIOD),
        repository.load_minute_bars(REF, INSTRUMENT, PERIOD),
        repository.load_signal_minute_closes(REF, INSTRUMENT, PERIOD),
        repository.load_sessions(REF, INSTRUMENT, PERIOD),
        repository.load_events(REF, INSTRUMENT, PERIOD),
        repository.load_corporate_actions(REF, INSTRUMENT, PERIOD),
    )

    names = ("daily", "signal", "minute", "signal_minute", "sessions", "events", "actions")
    assert results == tuple(registry.load_results[name] for name in names)
    assert registry.load_calls == [(name, REF, INSTRUMENT, PERIOD) for name in names]
