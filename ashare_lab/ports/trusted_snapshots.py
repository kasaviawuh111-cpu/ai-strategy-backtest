"""Application-facing contracts for server-owned Strategy v2 snapshots.

Concrete adapters decide how immutable snapshots are located and verified on
disk.  The application receives only these already-verified value objects and
loader protocols, so it never imports infrastructure or accepts a client path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal, Protocol

from ashare_lab.domain.instruments import (
    InstrumentRef,
    InstrumentResolutionError,
    InstrumentResolver,
    SecurityMasterSnapshot,
)
from ashare_lab.domain.market_data import (
    CorporateAction,
    DailyBar,
    DataSnapshotRef,
    InstrumentSession,
)
from ashare_lab.domain.provenance import SourceRef
from ashare_lab.domain.runs import SnapshotBindingV2
from ashare_lab.domain.shared import InstrumentId, require_aware
from ashare_lab.domain.strategy import DatasetCoverageV2

from .market_data import DateRange

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class TrustedSnapshotError(RuntimeError):
    """Base error for the Strategy v2 trusted-snapshot boundary."""


class TrustedSnapshotIntegrityError(TrustedSnapshotError):
    """A selected content object is unsafe, malformed or has changed."""


class TrustedSnapshotCoverageError(TrustedSnapshotError):
    """A valid snapshot does not prove the exact requested range or symbol."""


class TrustedSnapshotExpiredError(TrustedSnapshotCoverageError):
    """A valid snapshot is too old for a new Strategy v2 submission."""


class TrustedSnapshotProviderUnavailableError(TrustedSnapshotCoverageError):
    """A required server-owned acquisition/search provider is unavailable."""


@dataclass(frozen=True, slots=True)
class TrustedSnapshotMetadata:
    """Stable identity and provenance shared by all Strategy v2 snapshots."""

    snapshot_id: str
    provider: str
    schema_version: str
    content_hash: str
    coverage: DateRange
    generated_at: datetime
    producer_snapshot_id: str | None = None

    def __post_init__(self) -> None:
        if not self.snapshot_id or ":" not in self.snapshot_id:
            raise TrustedSnapshotIntegrityError("snapshot id is invalid")
        if not self.provider.strip():
            raise TrustedSnapshotIntegrityError("snapshot provider is missing")
        if not self.schema_version.strip():
            raise TrustedSnapshotIntegrityError("snapshot schema version is missing")
        if (
            not self.content_hash.startswith("sha256:")
            or _SHA256.fullmatch(self.content_hash.removeprefix("sha256:")) is None
        ):
            raise TrustedSnapshotIntegrityError("snapshot content hash is invalid")
        require_aware(self.generated_at, "generated_at")


@dataclass(frozen=True, slots=True)
class TrustedSecurityMasterSnapshot:
    metadata: TrustedSnapshotMetadata
    snapshot: SecurityMasterSnapshot
    path: Path

    def __post_init__(self) -> None:
        if type(self.snapshot) is not SecurityMasterSnapshot:
            raise TrustedSnapshotIntegrityError("security-master domain snapshot is invalid")
        if self.metadata.snapshot_id != self.snapshot.snapshot_id:
            raise TrustedSnapshotIntegrityError(
                "security-master metadata and domain snapshot identities differ"
            )


@dataclass(frozen=True, slots=True)
class TrustedTechnicalSnapshot:
    """A fully-read daily price/session selection pinned by a trusted adapter."""

    producer_snapshot_id: str
    snapshot_ref: DataSnapshotRef
    market_data_metadata: TrustedSnapshotMetadata
    calendar_metadata: TrustedSnapshotMetadata
    execution_bars: tuple[DailyBar, ...]
    signal_bars: tuple[DailyBar, ...]
    sessions: tuple[InstrumentSession, ...]
    corporate_actions: tuple[CorporateAction, ...]
    producer_metadata: TrustedSnapshotMetadata | None = None
    producer_children: tuple[TrustedSnapshotMetadata, ...] = ()


@dataclass(frozen=True, slots=True)
class TrustedV2SnapshotContracts:
    """Validator inputs derived only from already-loaded trusted artifacts."""

    security_master: SnapshotBindingV2
    trading_calendar: SnapshotBindingV2
    market_data: SnapshotBindingV2
    composite_snapshot: SnapshotBindingV2
    producer_children: tuple[SnapshotBindingV2, ...]
    dataset_coverage: DatasetCoverageV2


@dataclass(frozen=True, slots=True)
class TrustedStrategyV2SnapshotSelection:
    """Server-selected immutable producer prepared before receipt issuance."""

    producer_snapshot_id: str
    coverage_end: date

    def __post_init__(self) -> None:
        if re.fullmatch(r"composite:[0-9a-f]{64}", self.producer_snapshot_id) is None:
            raise TrustedSnapshotIntegrityError(
                "Strategy v2 selection must be an outer Composite content id"
            )
        if type(self.coverage_end) is not date:
            raise TrustedSnapshotIntegrityError("Strategy v2 coverage_end must be a date")


class TrustedStrategyV2SnapshotResolver(Protocol):
    """Resolve or prepare data using only server-owned providers and roots.

    This boundary is called while issuing a draft.  Execution never calls it;
    it reopens the exact persisted ``producer_snapshot_id`` offline.
    """

    def resolve_or_prepare(
        self,
        *,
        instrument_id: InstrumentId,
        requested_period: DateRange,
        security_master: TrustedSecurityMasterSnapshot,
    ) -> TrustedStrategyV2SnapshotSelection: ...


@dataclass(frozen=True, slots=True)
class TrustedInstrumentSelection:
    """One authoritative identity plus the master snapshot that proved it."""

    instrument: InstrumentRef
    security_master: TrustedSecurityMasterSnapshot


class TrustedInstrumentResolver(Protocol):
    """Resolve exact code/name using server-owned reference providers only."""

    def resolve_or_prepare(self, identifier: str, *, as_of: date) -> TrustedInstrumentSelection: ...


class TrustedSecurityMasterSnapshotLoader(Protocol):
    def load(self, snapshot_id: object) -> TrustedSecurityMasterSnapshot: ...


class TrustedTechnicalSnapshotLoader(Protocol):
    def load(
        self,
        *,
        producer_snapshot_id: object,
        instrument_id: InstrumentId,
        period: DateRange,
        security_master: TrustedSecurityMasterSnapshot,
    ) -> TrustedTechnicalSnapshot: ...


def build_trusted_v2_snapshot_contracts(
    *,
    security_master: TrustedSecurityMasterSnapshot,
    technical_snapshot: TrustedTechnicalSnapshot,
    instrument_id: InstrumentId,
    period: DateRange,
) -> TrustedV2SnapshotContracts:
    """Project adapter-verified evidence into validator/runtime contracts."""

    try:
        resolver = InstrumentResolver(security_master.snapshot)
        start_identity = resolver.resolve(str(instrument_id), as_of=period.start)
        end_identity = resolver.resolve(str(instrument_id), as_of=period.end)
    except InstrumentResolutionError as error:
        raise TrustedSnapshotCoverageError(str(error)) from error
    if start_identity != end_identity:
        raise TrustedSnapshotIntegrityError(
            "security-master identity changes inside the requested period"
        )
    canonical_instrument = InstrumentId(start_identity.symbol)
    for metadata in (
        security_master.metadata,
        technical_snapshot.calendar_metadata,
        technical_snapshot.market_data_metadata,
    ):
        if period.start < metadata.coverage.start or period.end > metadata.coverage.end:
            raise TrustedSnapshotCoverageError(
                f"trusted snapshot does not cover requested period: {metadata.snapshot_id}"
            )
    if any(
        item.instrument_id != canonical_instrument for item in technical_snapshot.execution_bars
    ):
        raise TrustedSnapshotIntegrityError(
            "technical snapshot execution bars do not match the requested instrument"
        )
    if any(item.instrument_id != canonical_instrument for item in technical_snapshot.signal_bars):
        raise TrustedSnapshotIntegrityError(
            "technical snapshot signal bars do not match the requested instrument"
        )
    if any(item.instrument_id != canonical_instrument for item in technical_snapshot.sessions):
        raise TrustedSnapshotIntegrityError(
            "technical snapshot sessions do not match the requested instrument"
        )

    market = technical_snapshot.market_data_metadata
    producer = technical_snapshot.producer_metadata
    if producer is None or not producer.snapshot_id.startswith("composite:"):
        raise TrustedSnapshotIntegrityError(
            "Strategy v2 requires an outer content-addressed composite snapshot"
        )
    validate_trusted_market_availability(
        execution_bars=technical_snapshot.execution_bars,
        signal_bars=technical_snapshot.signal_bars,
        corporate_actions=technical_snapshot.corporate_actions,
        market_metadata=market,
    )
    source_ref = SourceRef(
        provider=market.provider,
        source_id="technical.daily.bundle",
        snapshot_id=market.snapshot_id,
        schema_version=market.schema_version,
        content_sha256=market.content_hash,
    )
    return TrustedV2SnapshotContracts(
        security_master=_snapshot_binding("security_master", security_master.metadata),
        trading_calendar=_snapshot_binding(
            "trading_calendar",
            technical_snapshot.calendar_metadata,
        ),
        market_data=_snapshot_binding("market_data", market),
        composite_snapshot=_snapshot_binding("composite_snapshot", producer),
        producer_children=tuple(
            _snapshot_binding("producer_child", item)
            for item in sorted(
                technical_snapshot.producer_children,
                key=lambda metadata: metadata.snapshot_id,
            )
        ),
        dataset_coverage=DatasetCoverageV2(
            dataset_id="daily_ohlcv",
            instrument_symbol=str(canonical_instrument),
            start=period.start,
            end=period.end,
            timezone="Asia/Shanghai",
            availability_field="first_available_at",
            retrieved_at_role="audit_only",
            missing_value_policy="null_or_no_signal",
            source_refs=(source_ref,),
        ),
    )


def validate_trusted_market_availability(
    *,
    execution_bars: tuple[DailyBar, ...],
    signal_bars: tuple[DailyBar, ...],
    corporate_actions: tuple[CorporateAction, ...],
    market_metadata: TrustedSnapshotMetadata,
) -> None:
    if any(
        bar.available_at > market_metadata.generated_at for bar in (*execution_bars, *signal_bars)
    ):
        raise TrustedSnapshotIntegrityError(
            "market bar first availability follows snapshot generated_at"
        )
    if any(
        action.replay_available_at > market_metadata.generated_at
        or action.ingested_at > market_metadata.generated_at
        for action in corporate_actions
    ):
        raise TrustedSnapshotIntegrityError(
            "corporate-action availability follows snapshot generated_at"
        )


def _snapshot_binding(
    kind: Literal[
        "security_master",
        "trading_calendar",
        "market_data",
        "composite_snapshot",
        "producer_child",
    ],
    metadata: TrustedSnapshotMetadata,
) -> SnapshotBindingV2:
    return SnapshotBindingV2(
        kind=kind,
        snapshot_id=metadata.snapshot_id,
        provider=metadata.provider,
        schema_version=metadata.schema_version,
        content_hash=metadata.content_hash,
        coverage_start=metadata.coverage.start,
        coverage_end=metadata.coverage.end,
        generated_at=metadata.generated_at,
    )


__all__ = [
    "TrustedInstrumentResolver",
    "TrustedInstrumentSelection",
    "TrustedSecurityMasterSnapshot",
    "TrustedSecurityMasterSnapshotLoader",
    "TrustedSnapshotCoverageError",
    "TrustedSnapshotError",
    "TrustedSnapshotExpiredError",
    "TrustedSnapshotIntegrityError",
    "TrustedSnapshotMetadata",
    "TrustedSnapshotProviderUnavailableError",
    "TrustedStrategyV2SnapshotResolver",
    "TrustedStrategyV2SnapshotSelection",
    "TrustedTechnicalSnapshot",
    "TrustedTechnicalSnapshotLoader",
    "TrustedV2SnapshotContracts",
    "build_trusted_v2_snapshot_contracts",
    "validate_trusted_market_availability",
]
