"""Application-facing interfaces for immutable market-data snapshots."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
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
from ashare_lab.domain.shared import DomainValidationError, InstrumentId, StrongId

_SNAPSHOT_CHECKSUM = re.compile(r"^sha256:[0-9a-f]{64}$")
_PRODUCER_SNAPSHOT_ID = re.compile(r"^[a-z][a-z0-9_-]*:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class DateRange:
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise DomainValidationError("date range start must not exceed end")


@dataclass(frozen=True, slots=True)
class DataRequirements:
    instruments: tuple[InstrumentId, ...]
    datasets: tuple[str, ...]
    event_codes: tuple[str, ...] = ()
    needs_event_document_text: bool = False
    needs_minute: bool = False
    needs_tick: bool = False
    needs_l2_queue: bool = False
    expected_snapshot_id: StrongId | None = None
    expected_snapshot_checksum: str | None = None
    expected_snapshot_schema_version: str | None = None
    expected_producer_snapshot_schema_version: str | None = None
    expected_producer_snapshot_id: str | None = None

    def __post_init__(self) -> None:
        if not self.instruments:
            raise DomainValidationError("at least one instrument is required")
        if not self.datasets:
            raise DomainValidationError("at least one dataset is required")
        if self.event_codes and "events" not in self.datasets:
            raise DomainValidationError("event codes require the events dataset")
        if self.needs_event_document_text and "events" not in self.datasets:
            raise DomainValidationError("event document text requires the events dataset")
        if self.needs_event_document_text and not self.event_codes:
            raise DomainValidationError("event document text requires explicit event codes")
        if len(set(self.event_codes)) != len(self.event_codes) or any(
            not item.startswith("event.") for item in self.event_codes
        ):
            raise DomainValidationError("event codes must be unique canonical event.* codes")
        if self.needs_l2_queue and not self.needs_tick:
            raise DomainValidationError("L2 queue data requires tick capability")
        expected_fields = (
            self.expected_snapshot_id,
            self.expected_snapshot_checksum,
            self.expected_snapshot_schema_version,
        )
        if any(item is not None for item in expected_fields) and not all(
            item is not None for item in expected_fields
        ):
            raise DomainValidationError(
                "expected snapshot id, checksum, and schema version must be supplied together"
            )
        raw_expected_snapshot_id: object = self.expected_snapshot_id
        if self.expected_snapshot_id is not None and not isinstance(
            raw_expected_snapshot_id, StrongId
        ):
            raise DomainValidationError("expected snapshot id must be a StrongId")
        if self.expected_snapshot_checksum is not None and (
            _SNAPSHOT_CHECKSUM.fullmatch(self.expected_snapshot_checksum) is None
        ):
            raise DomainValidationError(
                "expected snapshot checksum must be sha256:<64 lowercase hex digits>"
            )
        if (
            self.expected_snapshot_schema_version is not None
            and not self.expected_snapshot_schema_version
        ):
            raise DomainValidationError("expected snapshot schema version cannot be empty")
        if self.expected_producer_snapshot_schema_version is not None:
            if self.expected_snapshot_id is None:
                raise DomainValidationError(
                    "expected producer snapshot schema requires an expected snapshot identity"
                )
            if not self.expected_producer_snapshot_schema_version:
                raise DomainValidationError(
                    "expected producer snapshot schema version cannot be empty"
                )
        if self.expected_producer_snapshot_id is not None:
            if self.expected_snapshot_id is None:
                raise DomainValidationError(
                    "expected producer snapshot id requires an expected snapshot identity"
                )
            if _PRODUCER_SNAPSHOT_ID.fullmatch(self.expected_producer_snapshot_id) is None:
                raise DomainValidationError(
                    "expected producer snapshot id must be <provider>:<64 lowercase hex digits>"
                )


class MarketDataRepository(Protocol):
    """Pin first, then read exclusively through that immutable snapshot."""

    def pin_snapshot(
        self, requirements: DataRequirements, period: DateRange
    ) -> DataSnapshotRef: ...

    def load_daily_bars(
        self,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> Sequence[DailyBar]: ...

    def load_signal_bars(
        self,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> Sequence[DailyBar]: ...

    def load_minute_bars(
        self,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> Sequence[MinuteBar]: ...

    def load_signal_minute_closes(
        self,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> Sequence[MinuteClose]: ...

    def load_sessions(
        self,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> Sequence[InstrumentSession]: ...

    def load_events(
        self,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> Sequence[EventEnvelope]: ...

    def load_corporate_actions(
        self,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> Sequence[CorporateAction]: ...
