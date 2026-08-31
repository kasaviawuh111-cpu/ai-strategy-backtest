"""Select one immutable composite snapshot from a content-addressed registry."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Literal, cast

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

from .choice_snapshot import (
    SNAPSHOT_SCHEMA_VERSION as CHOICE_SNAPSHOT_SCHEMA_VERSION,
)
from .choice_snapshot import TECHNICAL_SNAPSHOT_SCHEMA_VERSION
from .composite_snapshot import COMPOSITE_SNAPSHOT_SCHEMA_VERSION
from .event_snapshot import EVENT_SNAPSHOT_SCHEMA_VERSION, NO_EVENT_REQUIRED_MODE
from .local_parquet import (
    LocalParquetMarketDataRepository,
    MarketDataAdapterError,
    MarketDataCapabilityError,
    SessionFactory,
    SnapshotIntegrityError,
    normalize_instrument_id,
)

_MANIFEST_FILENAME = "snapshot_manifest.json"
_CHOICE_SOURCE_MANIFEST = "source/choice_snapshot_manifest.json"
_TECHNICAL_SOURCE_MANIFEST = "source/technical_snapshot_manifest.json"
_EVENT_SOURCE_MANIFEST = "source/event_snapshot_manifest.json"
_CONTENT_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_PRODUCER_SNAPSHOT_ID = re.compile(
    r"^(?P<profile>composite|choice|technical):(?P<digest>[0-9a-f]{64})$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HASH_CHUNK_BYTES = 1024 * 1024
_CHOICE_REQUIRED_FILES = frozenset(
    {
        "corporate_actions.parquet",
        "daily_ohlcv.parquet",
        "instrument_sessions.parquet",
        "raw/provider_response.json",
        "raw/requests.json",
        "signal_daily_ohlcv.parquet",
    }
)

type _RepositoryProfile = Literal[
    "choice_snapshot",
    "technical_snapshot",
    "composite_snapshot",
]


class SnapshotRegistryError(MarketDataAdapterError):
    """Base error for the content-addressed composite registry."""


class SnapshotRegistryIntegrityError(SnapshotIntegrityError, SnapshotRegistryError):
    """A registry entry is malformed, mutable, or has an invalid identity."""


class SnapshotRegistryNoMatchError(MarketDataCapabilityError, SnapshotRegistryError):
    """No immutable composite entry proves all requested coverage."""


class SnapshotRegistryAmbiguityError(SnapshotRegistryIntegrityError):
    """More than one equally minimal snapshot can satisfy the request."""


class SnapshotRegistryRouteError(SnapshotRegistryIntegrityError):
    """A read reference cannot be routed to a delegate pinned in this process."""


@dataclass(frozen=True, slots=True)
class _RegistryEntry:
    producer_snapshot_id: str
    path: Path
    instrument_id: InstrumentId
    technical_period: DateRange
    event_codes: frozenset[str]
    profile: _RepositoryProfile
    supports_events: bool
    published_at: datetime


@dataclass(frozen=True, slots=True)
class _PinnedRoute:
    entry: _RegistryEntry
    delegate: LocalParquetMarketDataRepository
    local_ref: DataSnapshotRef


@dataclass(frozen=True, slots=True)
class _ExplicitSelection:
    profile: _RepositoryProfile
    path: Path
    producer_snapshot_id: str | None = None


class SnapshotRegistryMarketDataRepository:
    """Route a request to the smallest proven Choice, technical, or composite snapshot.

    The registry directory contains one content-addressed child directory per
    producer snapshot.  By default, all producer manifests are re-indexed
    before every pin and any malformed entry fails the registry closed.  A
    caller may instead select one immutable producer by content ID or direct
    child path; that mode validates only the named target, so an unrelated old
    schema cannot poison an explicit replay.  Reads still require a selection
    to have been pinned in this process; after a restart, the worker re-pins its
    manifest request and can then route the equivalent persisted reference.
    """

    def __init__(
        self,
        composite_root: str | Path,
        *,
        choice_root: str | Path | None = None,
        technical_root: str | Path | None = None,
        producer_snapshot_id: str | None = None,
        producer_snapshot_path: str | Path | None = None,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._composite_root = Path(composite_root).expanduser().resolve()
        self._choice_root = (
            Path(choice_root).expanduser().resolve() if choice_root is not None else None
        )
        self._technical_root = (
            Path(technical_root).expanduser().resolve() if technical_root is not None else None
        )
        self._session_factory = session_factory
        self._explicit_selection = _resolve_explicit_selection(
            composite_root=self._composite_root,
            choice_root=self._choice_root,
            technical_root=self._technical_root,
            producer_snapshot_id=producer_snapshot_id,
            producer_snapshot_path=producer_snapshot_path,
        )
        self._lock = threading.RLock()
        # A worker learns the exact producer content ID from its durable work
        # item only after this process-level registry has been constructed.
        # Defer broad indexing until an unselected pin or explicit refresh so
        # an unrelated legacy entry cannot prevent that exact replay path.
        self._entries: tuple[_RegistryEntry, ...] = ()
        self._delegates: dict[Path, LocalParquetMarketDataRepository] = {}
        self._routes: dict[str, _PinnedRoute] = {}

    def refresh(self) -> int:
        """Re-index all published children and return the valid entry count."""

        entries = self._scan_entries()
        with self._lock:
            self._entries = entries
        return len(entries)

    def pin_snapshot(
        self,
        requirements: DataRequirements,
        period: DateRange,
    ) -> DataSnapshotRef:
        """Select, fully validate, and pin one unambiguous producer snapshot."""

        if requirements.expected_producer_snapshot_id is not None:
            return self.pin_selected_snapshot(
                requirements.expected_producer_snapshot_id,
                requirements,
                period,
            )
        entries = self._scan_entries()
        instruments = tuple(
            sorted(
                {normalize_instrument_id(item) for item in requirements.instruments},
                key=str,
            )
        )
        if len(instruments) != 1:
            raise SnapshotRegistryNoMatchError(
                "snapshot registry requires exactly one canonical instrument"
            )
        instrument = instruments[0]
        needs_events = "events" in requirements.datasets
        requested_codes = frozenset(requirements.event_codes)
        candidates = tuple(
            entry
            for entry in entries
            if entry.instrument_id == instrument
            and _contains(entry.technical_period, period)
            and (
                not needs_events
                or (entry.supports_events and requested_codes.issubset(entry.event_codes))
            )
        )
        if not candidates:
            code_text = ", ".join(sorted(requested_codes)) or "none"
            raise SnapshotRegistryNoMatchError(
                "no registered snapshot covers "
                f"instrument={instrument}, period={period.start.isoformat()}.."
                f"{period.end.isoformat()}, event_codes={code_text}"
            )

        with self._lock:
            self._entries = entries
            if requirements.expected_snapshot_id is not None:
                matching_pins: list[
                    tuple[
                        _RegistryEntry,
                        LocalParquetMarketDataRepository,
                        DataSnapshotRef,
                    ]
                ] = []
                for candidate in candidates:
                    delegate = self._delegate_for(candidate)
                    try:
                        local_ref = delegate.pin_snapshot(requirements, period)
                    except MarketDataCapabilityError:
                        continue
                    if _matches_expected(requirements, local_ref):
                        matching_pins.append((candidate, delegate, local_ref))
                if not matching_pins:
                    raise SnapshotRegistryNoMatchError(
                        "expected immutable snapshot selection is not available in the registry: "
                        + str(requirements.expected_snapshot_id)
                    )
                if len(matching_pins) != 1:
                    identities = ", ".join(
                        sorted(item[0].producer_snapshot_id for item in matching_pins)
                    )
                    raise SnapshotRegistryAmbiguityError(
                        "expected snapshot selection is produced by multiple registry entries: "
                        + identities
                    )
                selected, delegate, local_ref = matching_pins[0]
            else:
                if requirements.needs_event_document_text:
                    validated: list[
                        tuple[
                            _RegistryEntry,
                            LocalParquetMarketDataRepository,
                            DataSnapshotRef,
                        ]
                    ] = []
                    for candidate in candidates:
                        candidate_delegate = self._delegate_for(candidate)
                        try:
                            candidate_ref = candidate_delegate.pin_snapshot(
                                requirements,
                                period,
                            )
                        except MarketDataCapabilityError:
                            continue
                        validated.append((candidate, candidate_delegate, candidate_ref))
                    if not validated:
                        raise SnapshotRegistryNoMatchError(
                            "no registered snapshot proves complete event document text for "
                            f"instrument={instrument}, period={period.start.isoformat()}.."
                            f"{period.end.isoformat()}"
                        )
                    selected = _select_freshest_minimal(
                        tuple(item[0] for item in validated),
                        needs_events=needs_events,
                    )
                    selected, delegate, local_ref = next(
                        item for item in validated if item[0] == selected
                    )
                else:
                    selected = _select_freshest_minimal(
                        candidates,
                        needs_events=needs_events,
                    )
                    delegate = self._delegate_for(selected)
                    local_ref = delegate.pin_snapshot(requirements, period)
            return self._register_route(selected, delegate, local_ref)

    def pin_producer_snapshot(
        self,
        producer_snapshot_id: str,
        producer_snapshot_path: str | Path,
        requirements: DataRequirements,
        period: DateRange,
    ) -> DataSnapshotRef:
        """Pin exactly one newly published producer and register its read route.

        Submission-time acquisition must not publish one immutable object and
        then silently select a different registry entry by freshness.  Resolve
        the content ID against the configured roots, require the preparer path
        to identify that same direct child, and validate only that producer.
        """

        return self.pin_selected_snapshot(
            producer_snapshot_id,
            requirements,
            period,
            producer_snapshot_path=producer_snapshot_path,
        )

    def pin_selected_snapshot(
        self,
        producer_snapshot_id: str,
        requirements: DataRequirements,
        period: DateRange,
        *,
        producer_snapshot_path: str | Path | None = None,
    ) -> DataSnapshotRef:
        """Validate and pin only one explicitly named producer snapshot.

        A durable worker replay supplies the content ID and lets the configured
        registry root determine the direct-child path.  A just-finished
        preparer also supplies its returned path, which must resolve to that
        same child.  Neither form indexes unrelated registry entries.
        """

        selection = _resolve_explicit_selection(
            composite_root=self._composite_root,
            choice_root=self._choice_root,
            technical_root=self._technical_root,
            producer_snapshot_id=producer_snapshot_id,
            producer_snapshot_path=None,
        )
        if selection is None:  # pragma: no cover - producer ID always selects
            raise SnapshotRegistryNoMatchError("producer snapshot selection is missing")
        if self._explicit_selection is not None and selection.path != self._explicit_selection.path:
            raise SnapshotRegistryIntegrityError(
                "requested producer snapshot differs from the registry's explicit selection"
            )
        if producer_snapshot_path is not None:
            published_path = Path(producer_snapshot_path).expanduser().resolve()
            if published_path != selection.path:
                raise SnapshotRegistryIntegrityError(
                    "prepared producer snapshot path does not match its content ID"
                )
        loader = _entry_loader(selection.profile)
        entry = loader(selection.path)
        if entry.producer_snapshot_id != producer_snapshot_id:
            raise SnapshotRegistryIntegrityError(
                "prepared producer snapshot identity does not match its manifest"
            )
        instruments = tuple(
            sorted(
                {normalize_instrument_id(item) for item in requirements.instruments},
                key=str,
            )
        )
        if len(instruments) != 1:
            raise SnapshotRegistryNoMatchError(
                "snapshot registry requires exactly one canonical instrument"
            )
        needs_events = "events" in requirements.datasets
        requested_codes = frozenset(requirements.event_codes)
        if (
            entry.instrument_id != instruments[0]
            or not _contains(entry.technical_period, period)
            or (
                needs_events
                and (not entry.supports_events or not requested_codes.issubset(entry.event_codes))
            )
        ):
            raise SnapshotRegistryNoMatchError(
                "prepared producer snapshot does not cover the exact submitted request: "
                + producer_snapshot_id
            )

        with self._lock:
            delegate = self._delegate_for(entry)
            local_ref = delegate.pin_snapshot(requirements, period)
            routed_ref = replace(
                local_ref,
                producer_snapshot_id=entry.producer_snapshot_id,
            )
            if requirements.expected_snapshot_id is not None and not _matches_expected(
                requirements, routed_ref
            ):
                raise SnapshotRegistryNoMatchError(
                    "selected producer does not match the expected immutable snapshot: "
                    + producer_snapshot_id
                )
            self._entries = (
                *(
                    item
                    for item in self._entries
                    if item.producer_snapshot_id != producer_snapshot_id
                ),
                entry,
            )
            return self._register_route(entry, delegate, local_ref)

    def _delegate_for(
        self,
        entry: _RegistryEntry,
    ) -> LocalParquetMarketDataRepository:
        delegate = self._delegates.get(entry.path)
        if delegate is None:
            delegate = LocalParquetMarketDataRepository(
                entry.path,
                session_factory=self._session_factory,
                profile=entry.profile,
            )
            self._delegates[entry.path] = delegate
        return delegate

    def _register_route(
        self,
        entry: _RegistryEntry,
        delegate: LocalParquetMarketDataRepository,
        local_ref: DataSnapshotRef,
    ) -> DataSnapshotRef:
        routed_ref = replace(
            local_ref,
            producer_snapshot_id=entry.producer_snapshot_id,
        )
        route_key = str(routed_ref.snapshot_id)
        existing = self._routes.get(route_key)
        if existing is not None and (
            existing.entry.path != entry.path
            or existing.local_ref.checksum != local_ref.checksum
            or existing.local_ref.schema_version != local_ref.schema_version
            or existing.local_ref.producer_schema_version != local_ref.producer_schema_version
        ):
            raise SnapshotRegistryAmbiguityError(
                f"snapshot selection identity is routed by multiple producers: {route_key}"
            )
        self._routes[route_key] = _PinnedRoute(
            entry=entry,
            delegate=delegate,
            local_ref=local_ref,
        )
        return routed_ref

    def load_daily_bars(
        self,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> Sequence[DailyBar]:
        route = self._route(snapshot)
        return route.delegate.load_daily_bars(route.local_ref, instrument_id, period)

    def load_signal_bars(
        self,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> Sequence[DailyBar]:
        route = self._route(snapshot)
        return route.delegate.load_signal_bars(route.local_ref, instrument_id, period)

    def load_minute_bars(
        self,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> Sequence[MinuteBar]:
        route = self._route(snapshot)
        return route.delegate.load_minute_bars(route.local_ref, instrument_id, period)

    def load_signal_minute_closes(
        self,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> Sequence[MinuteClose]:
        route = self._route(snapshot)
        return route.delegate.load_signal_minute_closes(
            route.local_ref,
            instrument_id,
            period,
        )

    def load_sessions(
        self,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> Sequence[InstrumentSession]:
        route = self._route(snapshot)
        return route.delegate.load_sessions(route.local_ref, instrument_id, period)

    def load_events(
        self,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> Sequence[EventEnvelope]:
        route = self._route(snapshot)
        return route.delegate.load_events(route.local_ref, instrument_id, period)

    def load_corporate_actions(
        self,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> Sequence[CorporateAction]:
        route = self._route(snapshot)
        return route.delegate.load_corporate_actions(route.local_ref, instrument_id, period)

    def _route(self, snapshot: DataSnapshotRef) -> _PinnedRoute:
        route_key = str(snapshot.snapshot_id)
        with self._lock:
            route = self._routes.get(route_key)
        if route is None:
            raise SnapshotRegistryRouteError(
                "snapshot reference is not routed; pin the immutable request after process restart"
            )
        if (
            snapshot.checksum != route.local_ref.checksum
            or snapshot.schema_version != route.local_ref.schema_version
            or snapshot.producer_schema_version != route.local_ref.producer_schema_version
        ):
            raise SnapshotRegistryRouteError(
                "snapshot reference checksum or schema does not match its routed pin"
            )
        if snapshot.producer_snapshot_id != route.entry.producer_snapshot_id:
            raise SnapshotRegistryRouteError(
                "snapshot reference producer identity does not match its routed pin"
            )
        return route

    def _scan_entries(self) -> tuple[_RegistryEntry, ...]:
        if self._explicit_selection is not None:
            selection = self._explicit_selection
            loader = _entry_loader(selection.profile)
            entry = loader(selection.path)
            if (
                selection.producer_snapshot_id is not None
                and entry.producer_snapshot_id != selection.producer_snapshot_id
            ):
                raise SnapshotRegistryIntegrityError(
                    "selected producer snapshot identity does not match its manifest"
                )
            return (entry,)

        entries = list(
            _scan_content_root(
                self._composite_root,
                label="composite",
                loader=_load_composite_registry_entry,
            )
        )
        if self._choice_root is not None:
            entries.extend(
                _scan_content_root(
                    self._choice_root,
                    label="Choice",
                    loader=_load_choice_registry_entry,
                )
            )
        if self._technical_root is not None:
            entries.extend(
                _scan_content_root(
                    self._technical_root,
                    label="technical",
                    loader=_load_technical_registry_entry,
                )
            )
        identities: set[str] = set()
        for entry in entries:
            if entry.producer_snapshot_id in identities:
                raise SnapshotRegistryAmbiguityError(
                    "duplicate producer snapshot identity in registry: "
                    + entry.producer_snapshot_id
                )
            identities.add(entry.producer_snapshot_id)
        return tuple(entries)


def _resolve_explicit_selection(
    *,
    composite_root: Path,
    choice_root: Path | None,
    technical_root: Path | None,
    producer_snapshot_id: str | None,
    producer_snapshot_path: str | Path | None,
) -> _ExplicitSelection | None:
    if producer_snapshot_id is not None and producer_snapshot_path is not None:
        raise SnapshotRegistryIntegrityError(
            "select a producer snapshot by either content ID or path, not both"
        )
    _validate_distinct_registry_roots(composite_root, choice_root, technical_root)
    if producer_snapshot_id is not None:
        match = _PRODUCER_SNAPSHOT_ID.fullmatch(producer_snapshot_id)
        if match is None:
            raise SnapshotRegistryIntegrityError(
                "producer snapshot ID must be composite:<sha256>, choice:<sha256>, "
                "or technical:<sha256>"
            )
        prefix = match.group("profile")
        profile: _RepositoryProfile
        root: Path
        if prefix == "composite":
            profile = "composite_snapshot"
            root = composite_root
        elif prefix == "choice":
            profile = "choice_snapshot"
            if choice_root is None:
                raise SnapshotRegistryNoMatchError(
                    "selected Choice producer snapshot requires a Choice registry root"
                )
            root = choice_root
        else:
            profile = "technical_snapshot"
            if technical_root is None:
                raise SnapshotRegistryNoMatchError(
                    "selected technical producer snapshot requires a technical registry root"
                )
            root = technical_root
        path = root / match.group("digest")
        _validate_explicit_target(path, root=root)
        return _ExplicitSelection(
            profile=profile,
            path=path,
            producer_snapshot_id=producer_snapshot_id,
        )
    if producer_snapshot_path is None:
        return None

    declared = Path(producer_snapshot_path).expanduser()
    resolved = declared.resolve()
    if resolved.parent == composite_root:
        profile = "composite_snapshot"
        root = composite_root
    elif choice_root is not None and resolved.parent == choice_root:
        profile = "choice_snapshot"
        root = choice_root
    elif technical_root is not None and resolved.parent == technical_root:
        profile = "technical_snapshot"
        root = technical_root
    else:
        raise SnapshotRegistryIntegrityError(
            "selected producer snapshot path must be a direct child of a configured registry root"
        )
    _validate_explicit_target(declared, root=root)
    return _ExplicitSelection(profile=profile, path=resolved)


def _validate_distinct_registry_roots(
    composite_root: Path,
    choice_root: Path | None,
    technical_root: Path | None,
) -> None:
    named = tuple(
        (name, root)
        for name, root in (
            ("composite", composite_root),
            ("Choice", choice_root),
            ("technical", technical_root),
        )
        if root is not None
    )
    if len({root for _, root in named}) != len(named):
        raise SnapshotRegistryIntegrityError(
            "composite, Choice, and technical snapshot registry roots must be distinct directories"
        )


def _entry_loader(
    profile: _RepositoryProfile,
) -> Callable[[Path], _RegistryEntry]:
    return {
        "composite_snapshot": _load_composite_registry_entry,
        "choice_snapshot": _load_choice_registry_entry,
        "technical_snapshot": _load_technical_registry_entry,
    }[profile]


def _validate_explicit_target(path: Path, *, root: Path) -> None:
    if _CONTENT_DIGEST.fullmatch(path.name) is None:
        raise SnapshotRegistryIntegrityError(
            "selected producer snapshot directory must be named by its 64-character digest"
        )
    if path.is_symlink() or path.resolve().parent != root:
        raise SnapshotRegistryIntegrityError(
            "selected producer snapshot path is not a safe direct registry child"
        )
    if not path.is_dir():
        raise SnapshotRegistryNoMatchError(f"selected producer snapshot is not published: {path}")


def _scan_content_root(
    root: Path,
    *,
    label: str,
    loader: Callable[[Path], _RegistryEntry],
) -> tuple[_RegistryEntry, ...]:
    if not root.is_dir():
        raise SnapshotRegistryIntegrityError(
            f"{label} snapshot registry root is not a directory: {root}"
        )
    entries: list[_RegistryEntry] = []
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        if _CONTENT_DIGEST.fullmatch(child.name) is None:
            continue
        if child.is_symlink() or not child.is_dir():
            raise SnapshotRegistryIntegrityError(
                f"content-addressed registry entry is not a safe directory: {child}"
            )
        entries.append(loader(child))
    return tuple(entries)


def _load_composite_registry_entry(root: Path) -> _RegistryEntry:
    manifest = _load_json(root / _MANIFEST_FILENAME, "composite snapshot manifest")
    if manifest.get("schemaVersion") != COMPOSITE_SNAPSHOT_SCHEMA_VERSION:
        raise SnapshotRegistryIntegrityError(
            f"composite registry entry has an unsupported schemaVersion: {root}"
        )
    producer_snapshot_id = _validate_snapshot_identity(
        manifest,
        prefix="composite",
        expected_digest=root.name,
        label="composite snapshot",
    )
    published_at = _manifest_time(
        manifest,
        key="composedAt",
        label="composite snapshot",
    )
    capabilities = _mapping(manifest, "capabilities", "composite snapshot")
    for name in ("technicalDaily", "corporateActions"):
        if capabilities.get(name) != "validated_for_demo":
            raise SnapshotRegistryIntegrityError(
                f"composite snapshot capability is not validated_for_demo: {name}"
            )
    event_capability = capabilities.get("events")
    if event_capability not in {"validated_for_demo", "not_required"}:
        raise SnapshotRegistryIntegrityError(
            "composite snapshot event capability is neither validated nor explicitly not required"
        )

    registered_files = _mapping(manifest, "files", "composite snapshot")
    has_choice = _CHOICE_SOURCE_MANIFEST in registered_files
    has_technical = _TECHNICAL_SOURCE_MANIFEST in registered_files
    if has_choice is has_technical:
        raise SnapshotRegistryIntegrityError(
            "composite snapshot must register exactly one allowlisted technical source"
        )
    if has_technical:
        technical_relative = _TECHNICAL_SOURCE_MANIFEST
        technical_base_key = "technical"
        technical_schema = TECHNICAL_SNAPSHOT_SCHEMA_VERSION
        technical_prefix = "technical"
    else:
        technical_relative = _CHOICE_SOURCE_MANIFEST
        technical_base_key = "choice"
        technical_schema = CHOICE_SNAPSHOT_SCHEMA_VERSION
        technical_prefix = "choice"
    choice_manifest = _load_registered_source_manifest(
        root,
        manifest,
        relative=technical_relative,
        base_key=technical_base_key,
        expected_schema=technical_schema,
        snapshot_prefix=technical_prefix,
    )
    event_manifest = _load_registered_source_manifest(
        root,
        manifest,
        relative=_EVENT_SOURCE_MANIFEST,
        base_key="events",
        expected_schema=EVENT_SNAPSHOT_SCHEMA_VERSION,
        snapshot_prefix="events",
    )

    raw_symbol = choice_manifest.get("symbol")
    if not isinstance(raw_symbol, str):
        raise SnapshotRegistryIntegrityError("technical source manifest symbol is invalid")
    try:
        instrument = normalize_instrument_id(raw_symbol)
    except MarketDataAdapterError as exc:
        raise SnapshotRegistryIntegrityError("technical source manifest symbol is invalid") from exc
    technical_period = _choice_period(choice_manifest)

    corporate_coverage = _complete_coverage(
        manifest,
        key="corporateActionCoverage",
        label="corporate-action",
    )
    source_corporate_coverage = _mapping(
        choice_manifest,
        "corporateActionCoverage",
        "technical source manifest",
    )
    if corporate_coverage != source_corporate_coverage:
        raise SnapshotRegistryIntegrityError(
            "composite corporate-action coverage differs from its technical source manifest"
        )
    corporate_period = _coverage_period(corporate_coverage, "corporate-action")
    if not _contains(corporate_period, technical_period):
        raise SnapshotRegistryIntegrityError(
            "corporate-action coverage does not span technical price coverage"
        )

    event_coverage = _mapping(
        manifest,
        "eventAcquisitionCoverage",
        "composite snapshot",
    )
    source_event_coverage = _mapping(
        event_manifest,
        "acquisitionCoverage",
        "event source manifest",
    )
    if event_coverage != source_event_coverage:
        raise SnapshotRegistryIntegrityError(
            "composite event coverage differs from its event source manifest"
        )
    event_period = _coverage_period(event_coverage, "event acquisition")
    raw_event_instrument = event_coverage.get("instrumentId")
    if not isinstance(raw_event_instrument, str):
        raise SnapshotRegistryIntegrityError("event acquisition instrumentId is invalid")
    try:
        event_instrument = normalize_instrument_id(raw_event_instrument)
    except MarketDataAdapterError as exc:
        raise SnapshotRegistryIntegrityError("event acquisition instrumentId is invalid") from exc
    if event_instrument != instrument:
        raise SnapshotRegistryIntegrityError(
            "event acquisition instrument does not match technical price coverage"
        )
    if not _contains(event_period, technical_period):
        raise SnapshotRegistryIntegrityError(
            "event acquisition coverage does not span technical price coverage"
        )
    supports_events = event_capability == "validated_for_demo"
    if supports_events:
        if (
            event_coverage.get("status") != "complete"
            or event_coverage.get("querySucceeded") is not True
        ):
            raise SnapshotRegistryIntegrityError("event acquisition coverage is incomplete")
        event_codes = _event_codes(event_coverage)
    else:
        _validate_no_event_required_registry_contract(
            composite_manifest=manifest,
            event_manifest=event_manifest,
            event_coverage=event_coverage,
        )
        event_codes = frozenset[str]()
    return _RegistryEntry(
        producer_snapshot_id=producer_snapshot_id,
        path=root,
        instrument_id=instrument,
        technical_period=technical_period,
        event_codes=event_codes,
        profile="composite_snapshot",
        supports_events=supports_events,
        published_at=published_at,
    )


def _load_choice_registry_entry(root: Path) -> _RegistryEntry:
    return _load_daily_registry_entry(
        root,
        expected_schema=CHOICE_SNAPSHOT_SCHEMA_VERSION,
        prefix="choice",
        label="Choice",
        profile="choice_snapshot",
    )


def _load_technical_registry_entry(root: Path) -> _RegistryEntry:
    return _load_daily_registry_entry(
        root,
        expected_schema=TECHNICAL_SNAPSHOT_SCHEMA_VERSION,
        prefix="technical",
        label="technical",
        profile="technical_snapshot",
    )


def _load_daily_registry_entry(
    root: Path,
    *,
    expected_schema: str,
    prefix: Literal["choice", "technical"],
    label: str,
    profile: Literal["choice_snapshot", "technical_snapshot"],
) -> _RegistryEntry:
    manifest = _load_json(root / _MANIFEST_FILENAME, f"{label} snapshot manifest")
    if manifest.get("schemaVersion") != expected_schema:
        raise SnapshotRegistryIntegrityError(
            f"{label} registry entry has an unsupported schemaVersion: {root}"
        )
    producer_snapshot_id = _validate_snapshot_identity(
        manifest,
        prefix=prefix,
        expected_digest=root.name,
        label=f"{label} snapshot",
    )
    published_at = _manifest_time(
        manifest,
        key="capturedAt",
        label=f"{label} snapshot",
    )
    capabilities = _mapping(manifest, "capabilities", f"{label} snapshot")
    for name in ("technicalDaily", "corporateActions"):
        if capabilities.get(name) != "validated_for_demo":
            raise SnapshotRegistryIntegrityError(
                f"{label} snapshot capability is not validated_for_demo: {name}"
            )
    raw_symbol = manifest.get("symbol")
    if not isinstance(raw_symbol, str):
        raise SnapshotRegistryIntegrityError(f"{label} snapshot symbol is invalid")
    try:
        instrument = normalize_instrument_id(raw_symbol)
    except MarketDataAdapterError as exc:
        raise SnapshotRegistryIntegrityError(f"{label} snapshot symbol is invalid") from exc
    technical_period = _choice_period(manifest)
    corporate_coverage = _complete_coverage(
        manifest,
        key="corporateActionCoverage",
        label="corporate-action",
    )
    corporate_period = _coverage_period(corporate_coverage, "corporate-action")
    if not _contains(corporate_period, technical_period):
        raise SnapshotRegistryIntegrityError(
            f"{label} corporate-action coverage does not span technical price coverage"
        )
    _validate_registered_files(
        root,
        manifest,
        required=_CHOICE_REQUIRED_FILES,
        label=f"{label} snapshot",
    )
    return _RegistryEntry(
        producer_snapshot_id=producer_snapshot_id,
        path=root,
        instrument_id=instrument,
        technical_period=technical_period,
        event_codes=frozenset(),
        profile=profile,
        supports_events=False,
        published_at=published_at,
    )


def _validate_registered_files(
    root: Path,
    manifest: Mapping[str, object],
    *,
    required: frozenset[str],
    label: str,
) -> None:
    files = _mapping(manifest, "files", label)
    names: set[str] = set()
    for raw_name in files:
        if not isinstance(raw_name, str) or not raw_name:
            raise SnapshotRegistryIntegrityError(
                f"{label} registered file names must be non-empty strings"
            )
        names.add(raw_name)
    missing = sorted(required - names)
    if missing:
        raise SnapshotRegistryIntegrityError(
            f"{label} is missing required files: " + ", ".join(missing)
        )
    for name, raw_metadata in files.items():
        if not isinstance(name, str) or not isinstance(raw_metadata, Mapping):
            raise SnapshotRegistryIntegrityError(f"{label} registered file metadata is invalid")
        metadata = cast(Mapping[object, object], raw_metadata)
        expected_bytes = metadata.get("bytes")
        expected_sha256 = metadata.get("sha256")
        if (
            not isinstance(expected_bytes, int)
            or isinstance(expected_bytes, bool)
            or expected_bytes < 0
            or not isinstance(expected_sha256, str)
            or _SHA256.fullmatch(expected_sha256) is None
        ):
            raise SnapshotRegistryIntegrityError(
                f"{label} registered file metadata is invalid: {name}"
            )
        path = _safe_registered_path(root, name)
        try:
            actual_bytes = path.stat().st_size
            actual_sha256 = _sha256_file(path)
        except OSError as exc:
            raise SnapshotRegistryIntegrityError(
                f"cannot read {label} registered file: {name}"
            ) from exc
        if actual_bytes != expected_bytes or actual_sha256 != expected_sha256:
            raise SnapshotRegistryIntegrityError(f"{label} registered file hash mismatch: {name}")


def _load_registered_source_manifest(
    root: Path,
    composite_manifest: Mapping[str, object],
    *,
    relative: str,
    base_key: str,
    expected_schema: str,
    snapshot_prefix: str,
) -> dict[str, object]:
    files = _mapping(composite_manifest, "files", "composite snapshot")
    raw_metadata = files.get(relative)
    if not isinstance(raw_metadata, Mapping):
        raise SnapshotRegistryIntegrityError(
            f"composite snapshot does not register source manifest: {relative}"
        )
    metadata = cast(Mapping[object, object], raw_metadata)
    expected_bytes = metadata.get("bytes")
    expected_sha256 = metadata.get("sha256")
    if (
        not isinstance(expected_bytes, int)
        or isinstance(expected_bytes, bool)
        or expected_bytes < 0
        or not isinstance(expected_sha256, str)
        or _SHA256.fullmatch(expected_sha256) is None
    ):
        raise SnapshotRegistryIntegrityError(
            f"composite source manifest metadata is invalid: {relative}"
        )
    path = _safe_registered_path(root, relative)
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise SnapshotRegistryIntegrityError(
            f"cannot read registered source manifest: {relative}"
        ) from exc
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if len(payload) != expected_bytes or actual_sha256 != expected_sha256:
        raise SnapshotRegistryIntegrityError(
            f"registered source manifest hash mismatch: {relative}"
        )
    try:
        decoded = cast(object, json.loads(payload))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotRegistryIntegrityError(
            f"registered source manifest is not valid JSON: {relative}"
        ) from exc
    if not isinstance(decoded, dict):
        raise SnapshotRegistryIntegrityError(
            f"registered source manifest must be an object: {relative}"
        )
    manifest = cast(dict[str, object], decoded)
    if manifest.get("schemaVersion") != expected_schema:
        raise SnapshotRegistryIntegrityError(
            f"registered source manifest has an unsupported schemaVersion: {relative}"
        )
    source_id = _validate_snapshot_identity(
        manifest,
        prefix=snapshot_prefix,
        expected_digest=None,
        label=relative,
    )
    bases = _mapping(composite_manifest, "baseSnapshots", "composite snapshot")
    raw_base = bases.get(base_key)
    if not isinstance(raw_base, Mapping):
        raise SnapshotRegistryIntegrityError(
            f"composite base snapshot metadata is missing: {base_key}"
        )
    base = cast(Mapping[object, object], raw_base)
    if (
        base.get("snapshotId") != source_id
        or base.get("schemaVersion") != expected_schema
        or base.get("manifestSha256") != actual_sha256
    ):
        raise SnapshotRegistryIntegrityError(
            f"composite base snapshot metadata does not match: {base_key}"
        )
    return manifest


def _load_json(path: Path, label: str) -> dict[str, object]:
    try:
        decoded = cast(object, json.loads(path.read_bytes()))
    except OSError as exc:
        raise SnapshotRegistryIntegrityError(f"cannot read {label}: {path}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotRegistryIntegrityError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(decoded, dict):
        raise SnapshotRegistryIntegrityError(f"{label} must be a JSON object: {path}")
    return cast(dict[str, object], decoded)


def _validate_snapshot_identity(
    manifest: Mapping[str, object],
    *,
    prefix: str,
    expected_digest: str | None,
    label: str,
) -> str:
    body = dict(manifest)
    snapshot_id = body.pop("snapshotId", None)
    digest = hashlib.sha256(_canonical_json_bytes(body)).hexdigest()
    if snapshot_id != f"{prefix}:{digest}":
        raise SnapshotRegistryIntegrityError(f"{label} snapshotId does not match its manifest")
    if expected_digest is not None and digest != expected_digest:
        raise SnapshotRegistryIntegrityError(f"{label} directory does not match its content digest")
    return cast(str, snapshot_id)


def _choice_period(manifest: Mapping[str, object]) -> DateRange:
    raw = manifest.get("requestedRange")
    if not isinstance(raw, list):
        raise SnapshotRegistryIntegrityError("Choice requestedRange is invalid")
    values = cast(list[object], raw)
    if len(values) != 2 or any(not isinstance(item, str) for item in values):
        raise SnapshotRegistryIntegrityError("Choice requestedRange is invalid")
    return _parse_period(cast(str, values[0]), cast(str, values[1]), "Choice requestedRange")


def _complete_coverage(
    manifest: Mapping[str, object],
    *,
    key: str,
    label: str,
) -> Mapping[object, object]:
    raw = manifest.get(key)
    if not isinstance(raw, Mapping):
        raise SnapshotRegistryIntegrityError(f"{label} coverage is missing")
    coverage = cast(Mapping[object, object], raw)
    if coverage.get("status") != "complete" or coverage.get("querySucceeded") is not True:
        raise SnapshotRegistryIntegrityError(f"{label} coverage is incomplete")
    return coverage


def _coverage_period(coverage: Mapping[object, object], label: str) -> DateRange:
    raw_start = coverage.get("start")
    raw_end = coverage.get("end")
    if not isinstance(raw_start, str) or not isinstance(raw_end, str):
        raise SnapshotRegistryIntegrityError(f"{label} coverage dates are invalid")
    return _parse_period(raw_start, raw_end, f"{label} coverage")


def _parse_period(raw_start: str, raw_end: str, label: str) -> DateRange:
    try:
        return DateRange(date.fromisoformat(raw_start), date.fromisoformat(raw_end))
    except (ValueError, DomainValidationError) as exc:
        raise SnapshotRegistryIntegrityError(f"{label} dates are invalid") from exc


def _event_codes(coverage: Mapping[object, object]) -> frozenset[str]:
    raw_codes = coverage.get("requestedEventCodes")
    if not isinstance(raw_codes, list):
        raise SnapshotRegistryIntegrityError("event requestedEventCodes is invalid")
    values = cast(list[object], raw_codes)
    if (
        not values
        or any(not isinstance(item, str) or not item.startswith("event.") for item in values)
        or len(set(cast(list[str], values))) != len(values)
    ):
        raise SnapshotRegistryIntegrityError("event requestedEventCodes is invalid")
    codes = frozenset(cast(list[str], values))
    raw_lanes = coverage.get("coverageByEventCode")
    if not isinstance(raw_lanes, Mapping):
        raise SnapshotRegistryIntegrityError("event code-level coverage is missing")
    lanes = cast(Mapping[object, object], raw_lanes)
    lane_codes: set[str] = set()
    for raw_code in lanes:
        if not isinstance(raw_code, str):
            raise SnapshotRegistryIntegrityError("event code-level coverage is inconsistent")
        lane_codes.add(raw_code)
    if frozenset(lane_codes) != codes:
        raise SnapshotRegistryIntegrityError("event code-level coverage is inconsistent")
    for code, raw_lane in lanes.items():
        if not isinstance(raw_lane, Mapping):
            raise SnapshotRegistryIntegrityError(f"event lane coverage is invalid: {code}")
        lane = cast(Mapping[object, object], raw_lane)
        if lane.get("status") != "complete" or lane.get("querySucceeded") is not True:
            raise SnapshotRegistryIntegrityError(f"event lane coverage is incomplete: {code}")
    return codes


def _validate_no_event_required_registry_contract(
    *,
    composite_manifest: Mapping[str, object],
    event_manifest: Mapping[str, object],
    event_coverage: Mapping[object, object],
) -> None:
    composite_requirement = _mapping(
        composite_manifest,
        "eventRequirement",
        "composite snapshot",
    )
    source_requirement = _mapping(
        event_manifest,
        "eventRequirement",
        "event source manifest",
    )
    if composite_requirement != source_requirement or (
        composite_requirement.get("mode") != NO_EVENT_REQUIRED_MODE
        or composite_requirement.get("eventDataAvailable") is not False
        or composite_requirement.get("acquisitionPerformed") is not False
    ):
        raise SnapshotRegistryIntegrityError(
            "technical-only composite event requirement is inconsistent"
        )
    if (
        event_coverage.get("mode") != NO_EVENT_REQUIRED_MODE
        or event_coverage.get("status") != "not_required"
        or event_coverage.get("acquisitionPerformed") is not False
        or event_coverage.get("queriedAt") is not None
        or event_coverage.get("querySucceeded") is not False
        or event_coverage.get("rowCount") != 0
        or event_coverage.get("zeroResult") is not False
        or event_coverage.get("emptyByConstruction") is not True
        or event_coverage.get("requestedEventCodes") != []
        or event_coverage.get("requiredProviders") != []
        or event_coverage.get("sourceStatuses") != []
        or event_coverage.get("providerQueryEvidence") != {}
        or event_coverage.get("coverageByEventCode") != {}
    ):
        raise SnapshotRegistryIntegrityError(
            "technical-only composite contains an invalid no-event-required declaration"
        )
    supplemental = event_coverage.get("supplementalEvidence")
    if supplemental != {"rowCount": 0, "observationsByProvider": {}}:
        raise SnapshotRegistryIntegrityError(
            "technical-only composite supplemental event evidence is not empty"
        )
    instrument_id = event_coverage.get("instrumentId")
    raw_start = event_coverage.get("start")
    raw_end = event_coverage.get("end")
    declared_at = event_coverage.get("declaredAt")
    if not all(isinstance(item, str) and item for item in (instrument_id, raw_start, raw_end)):
        raise SnapshotRegistryIntegrityError("technical-only composite no-event scope is invalid")
    if not isinstance(declared_at, str):
        raise SnapshotRegistryIntegrityError(
            "technical-only composite no-event declaration time is invalid"
        )
    try:
        parsed_declared_at = datetime.fromisoformat(declared_at)
    except ValueError as exc:
        raise SnapshotRegistryIntegrityError(
            "technical-only composite no-event declaration time is invalid"
        ) from exc
    if parsed_declared_at.tzinfo is None or parsed_declared_at.utcoffset() is None:
        raise SnapshotRegistryIntegrityError(
            "technical-only composite no-event declaration time needs a timezone"
        )
    declaration: dict[str, object] = {
        "mode": NO_EVENT_REQUIRED_MODE,
        "instrumentId": cast(str, instrument_id),
        "start": cast(str, raw_start),
        "end": cast(str, raw_end),
        "declaredAt": declared_at,
        "requestedEventCodes": [],
    }
    empty_evidence: dict[str, object] = {
        "sourceStatuses": [],
        "providerQueryEvidence": {},
        "supplementalEvidence": {
            "rowCount": 0,
            "observationsByProvider": {},
        },
        "coverageByEventCode": {},
    }
    audit = event_coverage.get("auditSummary")
    if not isinstance(audit, Mapping):
        raise SnapshotRegistryIntegrityError("technical-only composite no-event audit is missing")
    typed_audit = cast(Mapping[object, object], audit)
    if (
        typed_audit.get("requestSha256")
        != hashlib.sha256(_canonical_json_bytes(declaration)).hexdigest()
        or typed_audit.get("sourceStatusSha256")
        != hashlib.sha256(_canonical_json_bytes(empty_evidence)).hexdigest()
        or typed_audit.get("requiredProvidersSatisfied") != []
        or typed_audit.get("optionalUnavailableProviders") != []
        or typed_audit.get("successfulProviders") != []
    ):
        raise SnapshotRegistryIntegrityError(
            "technical-only composite no-event audit is inconsistent"
        )
    source_rows = _mapping(event_manifest, "rowCounts", "event source manifest")
    composite_rows = _mapping(composite_manifest, "rowCounts", "composite snapshot")
    if (
        source_rows.get("canonicalEvents") != 0
        or source_rows.get("observations") != 0
        or source_rows.get("quarantinedEvents") != 0
        or composite_rows.get("canonicalEvents") != 0
        or composite_rows.get("eventObservations") != 0
    ):
        raise SnapshotRegistryIntegrityError("technical-only composite contains event rows")


def _mapping(
    manifest: Mapping[str, object],
    key: str,
    label: str,
) -> Mapping[object, object]:
    raw = manifest.get(key)
    if not isinstance(raw, Mapping):
        raise SnapshotRegistryIntegrityError(f"{label} field {key!r} must be an object")
    return cast(Mapping[object, object], raw)


def _safe_registered_path(root: Path, raw_relative: str) -> Path:
    relative = PurePosixPath(raw_relative)
    if (
        relative.is_absolute()
        or relative.as_posix() != raw_relative
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise SnapshotRegistryIntegrityError(
            f"snapshot manifest contains an unsafe registered path: {raw_relative}"
        )
    declared = root.joinpath(*relative.parts)
    path = declared.resolve()
    if declared.is_symlink() or not path.is_relative_to(root) or not path.is_file():
        raise SnapshotRegistryIntegrityError(
            f"registered source manifest does not exist: {raw_relative}"
        )
    return path


def _canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_time(
    manifest: Mapping[str, object],
    *,
    key: str,
    label: str,
) -> datetime:
    raw = manifest.get(key)
    if not isinstance(raw, str):
        raise SnapshotRegistryIntegrityError(f"{label} {key} is invalid")
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise SnapshotRegistryIntegrityError(f"{label} {key} is invalid") from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise SnapshotRegistryIntegrityError(f"{label} {key} must include an explicit timezone")
    return value.astimezone(UTC)


def _contains(outer: DateRange, inner: DateRange) -> bool:
    return outer.start <= inner.start and outer.end >= inner.end


def _matches_expected(requirements: DataRequirements, snapshot: DataSnapshotRef) -> bool:
    expected_id = requirements.expected_snapshot_id
    expected_checksum = requirements.expected_snapshot_checksum
    expected_schema = requirements.expected_snapshot_schema_version
    if expected_id is None or expected_checksum is None or expected_schema is None:
        return False
    return (
        snapshot.snapshot_id == expected_id
        and snapshot.checksum == expected_checksum
        and snapshot.schema_version == expected_schema
        and (
            requirements.expected_producer_snapshot_schema_version is None
            or snapshot.producer_schema_version
            == requirements.expected_producer_snapshot_schema_version
        )
        and (
            requirements.expected_producer_snapshot_id is None
            or snapshot.producer_snapshot_id == requirements.expected_producer_snapshot_id
        )
    )


def _select_freshest_minimal(
    candidates: tuple[_RegistryEntry, ...],
    *,
    needs_events: bool,
) -> _RegistryEntry:
    minimal = tuple(
        candidate
        for candidate in candidates
        if not any(
            other is not candidate
            and _strictly_smaller(other, candidate, needs_events=needs_events)
            for other in candidates
        )
    )
    if len(minimal) == 1:
        return minimal[0]
    footprints: set[tuple[date, date, frozenset[str]]] = {
        (
            item.technical_period.start,
            item.technical_period.end,
            item.event_codes if needs_events else frozenset[str](),
        )
        for item in minimal
    }
    identities = ", ".join(sorted(item.producer_snapshot_id for item in minimal))
    if len(footprints) != 1:
        raise SnapshotRegistryAmbiguityError(
            "multiple incomparable minimal composite snapshots satisfy the request: " + identities
        )
    latest_time = max(item.published_at for item in minimal)
    latest = tuple(item for item in minimal if item.published_at == latest_time)
    if len(latest) != 1:
        raise SnapshotRegistryAmbiguityError(
            "multiple equally minimal snapshots share the latest publication time: " + identities
        )
    return latest[0]


def _strictly_smaller(
    candidate: _RegistryEntry,
    other: _RegistryEntry,
    *,
    needs_events: bool,
) -> bool:
    interval_subset = (
        candidate.technical_period.start >= other.technical_period.start
        and candidate.technical_period.end <= other.technical_period.end
    )
    codes_subset = not needs_events or candidate.event_codes.issubset(other.event_codes)
    strictly_less = candidate.technical_period != other.technical_period or (
        needs_events and candidate.event_codes != other.event_codes
    )
    return interval_subset and codes_subset and strictly_less


__all__ = [
    "SnapshotRegistryAmbiguityError",
    "SnapshotRegistryError",
    "SnapshotRegistryIntegrityError",
    "SnapshotRegistryMarketDataRepository",
    "SnapshotRegistryNoMatchError",
    "SnapshotRegistryRouteError",
]
