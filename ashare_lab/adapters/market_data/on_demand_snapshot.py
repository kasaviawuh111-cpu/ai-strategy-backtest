"""Prepare an immutable snapshot at submission time, then route reads locally.

This adapter is deliberately a submission-side wrapper.  A fresh request may
invoke the injected preparer after the local registry proves that coverage is
missing.  A worker replay carrying an expected snapshot identity never invokes
the preparer: it must find and pin that exact immutable selection or fail
closed.  Internal Demo deployments may refresh every fresh submission; the
published result remains an immutable audit artifact rather than mutable
runtime cache.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ashare_lab.domain.market_data import (
    CorporateAction,
    DailyBar,
    DataSnapshotRef,
    EventEnvelope,
    InstrumentSession,
    MinuteBar,
    MinuteClose,
)
from ashare_lab.domain.shared import DomainValidationError, InstrumentId
from ashare_lab.ports.market_data import DataRequirements, DateRange

from .local_parquet import MarketDataCapabilityError, normalize_instrument_id
from .snapshot_registry import (
    SnapshotRegistryMarketDataRepository,
    SnapshotRegistryNoMatchError,
)

_PRODUCER_SNAPSHOT_ID = re.compile(r"^[a-z][a-z0-9_-]*:[0-9a-f]{64}$")


class SnapshotPreparationError(MarketDataCapabilityError):
    """Stable failure exposed when on-demand acquisition cannot be completed."""


class SnapshotPreparationUnavailableError(SnapshotPreparationError):
    """A supported request cannot be prepared because a dependency is unavailable."""


class SnapshotPreparationFailedError(SnapshotPreparationUnavailableError):
    """The injected preparer failed before publishing a usable snapshot."""


class SnapshotPreparationIncompleteError(SnapshotPreparationUnavailableError):
    """Preparation returned, but the registry still cannot prove coverage."""


class SnapshotPreparationDocumentTextIncompleteError(SnapshotPreparationIncompleteError):
    """A document source cannot prove complete, page-covering text."""


class SnapshotPreparationUnsupportedError(SnapshotPreparationError):
    """The requested scope cannot be supplied by the configured Demo sources."""


@dataclass(frozen=True, slots=True)
class SnapshotPreparationResult:
    """Auditable identity and path published by one preparation attempt."""

    producer_snapshot_id: str
    path: Path

    def __post_init__(self) -> None:
        if _PRODUCER_SNAPSHOT_ID.fullmatch(self.producer_snapshot_id) is None:
            raise DomainValidationError(
                "producer_snapshot_id must be <provider>:<64 lowercase hex digits>"
            )
        object.__setattr__(self, "path", self.path.expanduser().resolve())


class SnapshotPreparer(Protocol):
    """Publish one immutable producer snapshot for a missing request."""

    def prepare(
        self,
        requirements: DataRequirements,
        period: DateRange,
    ) -> SnapshotPreparationResult: ...


@dataclass(frozen=True, slots=True)
class _PreparationKey:
    instruments: tuple[str, ...]
    datasets: tuple[str, ...]
    event_codes: tuple[str, ...]
    needs_event_document_text: bool
    start: str
    end: str
    needs_minute: bool
    needs_tick: bool
    needs_l2_queue: bool

    @classmethod
    def from_request(
        cls,
        requirements: DataRequirements,
        period: DateRange,
    ) -> _PreparationKey:
        return cls(
            instruments=tuple(
                sorted(
                    {
                        str(normalize_instrument_id(instrument))
                        for instrument in requirements.instruments
                    }
                )
            ),
            datasets=tuple(sorted(set(requirements.datasets))),
            event_codes=tuple(sorted(set(requirements.event_codes))),
            needs_event_document_text=requirements.needs_event_document_text,
            start=period.start.isoformat(),
            end=period.end.isoformat(),
            needs_minute=requirements.needs_minute,
            needs_tick=requirements.needs_tick,
            needs_l2_queue=requirements.needs_l2_queue,
        )

    def label(self) -> str:
        codes = ",".join(self.event_codes) or "none"
        return (
            f"instrument={','.join(self.instruments)}, period={self.start}..{self.end}, "
            f"event_codes={codes}, document_text={self.needs_event_document_text}"
        )


@dataclass(slots=True)
class _PreparationState:
    lock: threading.Lock
    completed_attempts: int = 0
    last_result: SnapshotPreparationResult | None = None
    last_failure: Exception | None = None


class OnDemandSnapshotMarketDataRepository:
    """Add process-local, single-flight preparation to a snapshot registry.

    The first registry lookup is always local.  Only a fresh request with no
    expected snapshot identity may invoke ``preparer.prepare``.  Calls that
    observed the same preparation generation share its result or failure, so
    concurrent identical misses do not fan out into duplicate provider calls.
    """

    def __init__(
        self,
        registry: SnapshotRegistryMarketDataRepository,
        preparer: SnapshotPreparer,
        *,
        refresh_each_submission: bool = False,
    ) -> None:
        self._registry = registry
        self._preparer = preparer
        self._refresh_each_submission = refresh_each_submission
        self._states_lock = threading.RLock()
        self._states: dict[_PreparationKey, _PreparationState] = {}
        self._preparation_results: list[SnapshotPreparationResult] = []

    @property
    def preparation_results(self) -> tuple[SnapshotPreparationResult, ...]:
        """Return successful preparer publications recorded in this process."""

        with self._states_lock:
            return tuple(self._preparation_results)

    def validate_registry(self) -> int:
        """Re-index immutable publications without invoking any provider."""

        return self._registry.refresh()

    def pin_snapshot(
        self,
        requirements: DataRequirements,
        period: DateRange,
    ) -> DataSnapshotRef:
        if _has_expected_snapshot(requirements):
            # Worker replay already carries the exact producer content ID.
            # Bypass the broad availability scan so unrelated legacy or
            # damaged registry children cannot poison this immutable target.
            if requirements.expected_producer_snapshot_id is None:
                raise SnapshotRegistryNoMatchError(
                    "on-demand worker replay requires an expected producer content ID"
                )
            return self._registry.pin_selected_snapshot(
                requirements.expected_producer_snapshot_id,
                requirements,
                period,
            )
        key = _PreparationKey.from_request(requirements, period)
        state, observed_attempts = self._observe_state(key)
        try:
            existing = self._registry.pin_snapshot(requirements, period)
        except SnapshotRegistryNoMatchError:
            if _has_expected_snapshot(requirements):
                raise
        else:
            if _has_expected_snapshot(requirements) or not self._refresh_each_submission:
                return existing

        with state.lock:
            if state.completed_attempts != observed_attempts:
                return self._consume_completed_attempt(
                    key=key,
                    state=state,
                    requirements=requirements,
                    period=period,
                )

            if not self._refresh_each_submission:
                # Another key or an external publisher may have filled the
                # registry between the first miss and this critical section.
                try:
                    return self._registry.pin_snapshot(requirements, period)
                except SnapshotRegistryNoMatchError:
                    pass

            try:
                result = self._preparer.prepare(requirements, period)
                if not result.path.is_dir():
                    raise FileNotFoundError(
                        f"prepared snapshot path is not a directory: {result.path}"
                    )
            except SnapshotPreparationError as exc:
                self._complete_attempt(state, failure=exc)
                raise
            except Exception as exc:
                failure = SnapshotPreparationFailedError(
                    "on-demand snapshot preparation failed for " + key.label()
                )
                self._complete_attempt(state, failure=failure)
                raise failure from exc

            with self._states_lock:
                self._preparation_results.append(result)

            try:
                snapshot = self._registry.pin_producer_snapshot(
                    result.producer_snapshot_id,
                    result.path,
                    requirements,
                    period,
                )
            except SnapshotRegistryNoMatchError as exc:
                failure = SnapshotPreparationIncompleteError(
                    "prepared producer snapshot still does not cover the request: "
                    f"{key.label()}, producer_snapshot_id={result.producer_snapshot_id}, "
                    f"path={result.path}"
                )
                self._complete_attempt(state, result=result, failure=failure)
                raise failure from exc
            except Exception as exc:
                self._complete_attempt(state, result=result, failure=exc)
                raise

            self._complete_attempt(state, result=result)
            return snapshot

    def _observe_state(
        self,
        key: _PreparationKey,
    ) -> tuple[_PreparationState, int]:
        with self._states_lock:
            state = self._states.get(key)
            if state is None:
                state = _PreparationState(lock=threading.Lock())
                self._states[key] = state
            return state, state.completed_attempts

    def _complete_attempt(
        self,
        state: _PreparationState,
        *,
        result: SnapshotPreparationResult | None = None,
        failure: Exception | None = None,
    ) -> None:
        with self._states_lock:
            state.completed_attempts += 1
            state.last_result = result
            state.last_failure = failure

    def _consume_completed_attempt(
        self,
        *,
        key: _PreparationKey,
        state: _PreparationState,
        requirements: DataRequirements,
        period: DateRange,
    ) -> DataSnapshotRef:
        if state.last_failure is not None:
            raise state.last_failure
        result = state.last_result
        if result is None:
            raise SnapshotPreparationIncompleteError(
                "completed snapshot preparation did not publish an auditable producer for "
                + key.label()
            )
        try:
            return self._registry.pin_producer_snapshot(
                result.producer_snapshot_id,
                result.path,
                requirements,
                period,
            )
        except SnapshotRegistryNoMatchError as exc:
            raise SnapshotPreparationIncompleteError(
                "completed snapshot preparation is no longer selectable for "
                f"{key.label()}; producer={result.producer_snapshot_id} at {result.path}"
            ) from exc

    def load_daily_bars(
        self,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> Sequence[DailyBar]:
        return self._registry.load_daily_bars(snapshot, instrument_id, period)

    def load_signal_bars(
        self,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> Sequence[DailyBar]:
        return self._registry.load_signal_bars(snapshot, instrument_id, period)

    def load_minute_bars(
        self,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> Sequence[MinuteBar]:
        return self._registry.load_minute_bars(snapshot, instrument_id, period)

    def load_signal_minute_closes(
        self,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> Sequence[MinuteClose]:
        return self._registry.load_signal_minute_closes(
            snapshot,
            instrument_id,
            period,
        )

    def load_sessions(
        self,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> Sequence[InstrumentSession]:
        return self._registry.load_sessions(snapshot, instrument_id, period)

    def load_events(
        self,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> Sequence[EventEnvelope]:
        return self._registry.load_events(snapshot, instrument_id, period)

    def load_corporate_actions(
        self,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> Sequence[CorporateAction]:
        return self._registry.load_corporate_actions(snapshot, instrument_id, period)


def _has_expected_snapshot(requirements: DataRequirements) -> bool:
    return any(
        value is not None
        for value in (
            requirements.expected_snapshot_id,
            requirements.expected_snapshot_checksum,
            requirements.expected_snapshot_schema_version,
            requirements.expected_producer_snapshot_schema_version,
            requirements.expected_producer_snapshot_id,
        )
    )


__all__ = [
    "OnDemandSnapshotMarketDataRepository",
    "SnapshotPreparationDocumentTextIncompleteError",
    "SnapshotPreparationError",
    "SnapshotPreparationFailedError",
    "SnapshotPreparationIncompleteError",
    "SnapshotPreparationResult",
    "SnapshotPreparationUnavailableError",
    "SnapshotPreparationUnsupportedError",
    "SnapshotPreparer",
]
