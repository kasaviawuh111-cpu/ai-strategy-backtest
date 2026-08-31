"""Point-in-time-safe access to the local A-share daily Parquet dataset.

The adapter deliberately exposes only capabilities that the local files can
prove.  In particular, historical sessions (ST state, listing-day rules and
price limits) must be supplied by an explicit ``SessionFactory`` rather than
being guessed from a stock-code prefix.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol, cast
from zoneinfo import ZoneInfo

import duckdb

from ashare_lab.adapters.market_data.event_snapshot import (
    NO_EVENT_REQUIRED_MODE,
    EventSnapshotError,
    validate_persisted_acquisition_query_evidence,
)
from ashare_lab.domain.events.catalog import DOCUMENT_TEXT_EVENT_CODES
from ashare_lab.domain.events.document_metrics import (
    DocumentMetricError,
    validate_document_text_artifact,
)
from ashare_lab.domain.market_data import (
    AshareInstrumentCodeError,
    CorporateAction,
    CorporateActionKind,
    DailyBar,
    DataSnapshotRef,
    EventEnvelope,
    InstrumentSession,
    MarketEvent,
    MinuteBar,
    MinuteClose,
    PriceBasis,
    TimeQuality,
    normalize_a_share_instrument,
)
from ashare_lab.domain.shared import (
    DomainValidationError,
    InstrumentId,
    Price,
    Quantity,
    StrongId,
)
from ashare_lab.ports.market_data import DataRequirements, DateRange

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_SCHEMA_VERSION = "local-parquet.market-data.v3"
_DAILY_DATASET = "daily_ohlcv"
_DAILY_FILENAME = "daily_ohlcv.parquet"
_SIGNAL_DAILY_DATASET = "signal_daily_ohlcv"
_SIGNAL_DAILY_FILENAME = "signal_daily_ohlcv.parquet"
_MINUTE_DATASET = "minute_ohlcv"
_MINUTE_FILENAME = "minute_ohlcv.parquet"
_SIGNAL_MINUTE_DATASET = "signal_minute_close"
_SIGNAL_MINUTE_FILENAME = "signal_minute_close.parquet"
_PRODUCER_MANIFEST_DATASET = "producer_manifest"
_PRODUCER_MANIFEST_FILENAME = "snapshot_manifest.json"
_CHOICE_SNAPSHOT_SCHEMA_VERSION = "choice.daily-research-snapshot.v1"
_TECHNICAL_SNAPSHOT_SCHEMA_VERSION = "ashare-lab.daily-research-snapshot.v1"
_CHOICE_MINUTE_SNAPSHOT_SCHEMA_VERSION = "choice.minute-research-snapshot.v1"
_COMPOSITE_SNAPSHOT_SCHEMA_VERSION = "ashare-lab.composite-research-snapshot.v2"
_CHOICE_REQUIRED_FILES = frozenset(
    {
        "corporate_actions.parquet",
        _DAILY_FILENAME,
        _SIGNAL_DAILY_FILENAME,
        "instrument_sessions.parquet",
        "raw/provider_response.json",
        "raw/requests.json",
    }
)
_CHOICE_MINUTE_REQUIRED_FILES = frozenset(
    {
        _MINUTE_FILENAME,
        _SIGNAL_MINUTE_FILENAME,
        "raw/requests.json",
    }
)
_EVENT_DATASET = "events"
_EVENT_FILENAME = "events.parquet"
_EVENT_OBSERVATIONS_FILENAME = "event_observations.parquet"
_CORPORATE_ACTION_DATASET = "corporate_actions"
_CORPORATE_ACTION_FILENAME = "corporate_actions.parquet"
_HASH_CHUNK_BYTES = 1024 * 1024
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PRODUCER_SNAPSHOT_ID = re.compile(r"^[a-z][a-z0-9_-]*:[0-9a-f]{64}$")

type MarketDataProfile = Literal[
    "generic_parquet",
    "choice_snapshot",
    "technical_snapshot",
    "choice_minute_snapshot",
    "composite_snapshot",
]


def _producer_schema_version(profile: MarketDataProfile) -> str | None:
    return {
        "generic_parquet": None,
        "choice_snapshot": _CHOICE_SNAPSHOT_SCHEMA_VERSION,
        "technical_snapshot": _TECHNICAL_SNAPSHOT_SCHEMA_VERSION,
        "choice_minute_snapshot": _CHOICE_MINUTE_SNAPSHOT_SCHEMA_VERSION,
        "composite_snapshot": _COMPOSITE_SNAPSHOT_SCHEMA_VERSION,
    }[profile]


class MarketDataAdapterError(RuntimeError):
    """Base error for local market-data access."""


class MarketDataCapabilityError(MarketDataAdapterError):
    """Raised when the requested historical fact is not available locally."""


class SnapshotIntegrityError(MarketDataAdapterError):
    """Raised when a pinned file no longer matches its immutable snapshot."""


class SnapshotScopeError(MarketDataAdapterError):
    """Raised when a read attempts to exceed the instruments or dates pinned."""


class MarketDataSchemaError(MarketDataAdapterError):
    """Raised when a Parquet row cannot be converted to a canonical record."""


class SessionFactory(Protocol):
    """Build historically accurate sessions from an explicitly supplied source.

    Implementations are responsible for point-in-time board, ST, suspension,
    listing-day and price-limit facts.  The local OHLCV adapter does not infer
    any of these facts.
    """

    def __call__(
        self,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> Sequence[InstrumentSession]: ...


@dataclass(frozen=True, slots=True)
class _FileFingerprint:
    dataset: str
    path: Path
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class _PinnedSnapshot:
    ref: DataSnapshotRef
    datasets: tuple[str, ...]
    instruments: tuple[InstrumentId, ...]
    period: DateRange
    files: tuple[_FileFingerprint, ...]


def normalize_instrument_id(value: InstrumentId | str) -> InstrumentId:
    """Normalize a six-digit mainland stock code to ``<code>.<exchange>``.

    The mapping covers the A-share code spaces used by Shanghai, Shenzhen and
    Beijing.  A supplied suffix must agree with the code-space mapping, which
    catches silent cross-exchange data errors early.
    """

    try:
        return normalize_a_share_instrument(value)
    except AshareInstrumentCodeError as exc:
        raise MarketDataSchemaError(str(exc)) from exc


type InstrumentNormalizer = Callable[[InstrumentId | str], InstrumentId]


def _validate_event_coverage_scope(
    coverage: Mapping[object, object],
    *,
    instruments: tuple[InstrumentId, ...],
    period: DateRange,
    requested_event_codes: tuple[str, ...],
    instrument_normalizer: InstrumentNormalizer,
) -> None:
    instrument_id = coverage.get("instrumentId")
    if not isinstance(instrument_id, str):
        raise SnapshotIntegrityError("composite snapshot event acquisition instrumentId is invalid")
    try:
        coverage_instrument = instrument_normalizer(instrument_id)
    except MarketDataSchemaError as exc:
        raise SnapshotIntegrityError(
            "composite snapshot event acquisition instrumentId is invalid"
        ) from exc
    if any(item != coverage_instrument for item in instruments):
        raise MarketDataCapabilityError(
            "requested instrument is outside event acquisition coverage"
        )

    raw_start = coverage.get("start")
    raw_end = coverage.get("end")
    if not isinstance(raw_start, str) or not isinstance(raw_end, str):
        raise SnapshotIntegrityError("composite snapshot event acquisition dates are invalid")
    try:
        coverage_start = date.fromisoformat(raw_start)
        coverage_end = date.fromisoformat(raw_end)
    except ValueError as exc:
        raise SnapshotIntegrityError(
            "composite snapshot event acquisition dates are invalid"
        ) from exc
    if coverage_start > coverage_end:
        raise SnapshotIntegrityError("composite snapshot event acquisition range is inverted")
    if period.start < coverage_start or period.end > coverage_end:
        raise MarketDataCapabilityError("requested period is outside event acquisition coverage")

    raw_codes = coverage.get("requestedEventCodes")
    if not isinstance(raw_codes, list):
        raise SnapshotIntegrityError("composite snapshot requested event-code coverage is invalid")
    typed_codes = cast(list[object], raw_codes)
    if not typed_codes or any(
        not isinstance(item, str) or not item.startswith("event.") for item in typed_codes
    ):
        raise SnapshotIntegrityError("composite snapshot requested event-code coverage is invalid")
    covered_codes = set(cast(list[str], typed_codes))
    raw_code_coverage = coverage.get("coverageByEventCode")
    if not isinstance(raw_code_coverage, Mapping):
        raise SnapshotIntegrityError("composite snapshot event code-level coverage is missing")
    typed_code_coverage = cast(Mapping[object, object], raw_code_coverage)
    if set(typed_code_coverage) != covered_codes:
        raise SnapshotIntegrityError("composite snapshot event code-level coverage is inconsistent")
    for code, raw_lane in typed_code_coverage.items():
        if not isinstance(raw_lane, Mapping):
            raise SnapshotIntegrityError(
                f"composite snapshot event lane coverage is invalid: {code}"
            )
        lane = cast(Mapping[object, object], raw_lane)
        if lane.get("status") != "complete" or lane.get("querySucceeded") is not True:
            raise SnapshotIntegrityError(
                f"composite snapshot event lane coverage is incomplete: {code}"
            )
    missing_codes = sorted(set(requested_event_codes) - covered_codes)
    if missing_codes:
        raise MarketDataCapabilityError(
            "requested event codes are outside acquisition coverage: " + ", ".join(missing_codes)
        )

    row_count = coverage.get("rowCount")
    if (
        not isinstance(row_count, int)
        or isinstance(row_count, bool)
        or row_count < 0
        or coverage.get("zeroResult") is not (row_count == 0)
    ):
        raise SnapshotIntegrityError(
            "composite snapshot event acquisition row counts are inconsistent"
        )
    required_providers = coverage.get("requiredProviders")
    if not isinstance(required_providers, list):
        raise SnapshotIntegrityError(
            "composite snapshot did not require the Eastmoney event source"
        )
    typed_required_providers = cast(list[object], required_providers)
    if "eastmoney" not in typed_required_providers or any(
        not isinstance(item, str) for item in typed_required_providers
    ):
        raise SnapshotIntegrityError(
            "composite snapshot did not require the Eastmoney event source"
        )
    source_statuses = coverage.get("sourceStatuses")
    if not isinstance(source_statuses, list) or not any(
        _is_successful_eastmoney_status(item) for item in cast(list[object], source_statuses)
    ):
        raise SnapshotIntegrityError("composite snapshot lacks a successful Eastmoney event query")
    if any(not isinstance(key, str) for key in coverage):
        raise SnapshotIntegrityError("composite snapshot event acquisition keys are invalid")
    try:
        validate_persisted_acquisition_query_evidence(cast(Mapping[str, object], coverage))
    except EventSnapshotError as exc:
        raise SnapshotIntegrityError(
            f"composite snapshot event query evidence is invalid: {exc}"
        ) from exc


def _validate_no_event_required_composite_contract(
    *,
    manifest: Mapping[str, object],
    coverage: Mapping[object, object],
    row_counts: Mapping[object, object],
    instruments: tuple[InstrumentId, ...],
    period: DateRange,
    instrument_normalizer: InstrumentNormalizer,
) -> None:
    requirement = manifest.get("eventRequirement")
    expected_requirement = {
        "mode": NO_EVENT_REQUIRED_MODE,
        "eventDataAvailable": False,
        "acquisitionPerformed": False,
    }
    if requirement != expected_requirement:
        raise SnapshotIntegrityError("technical-only composite event requirement is inconsistent")
    if (
        coverage.get("mode") != NO_EVENT_REQUIRED_MODE
        or coverage.get("status") != "not_required"
        or coverage.get("acquisitionPerformed") is not False
        or coverage.get("queriedAt") is not None
        or coverage.get("querySucceeded") is not False
        or coverage.get("rowCount") != 0
        or coverage.get("zeroResult") is not False
        or coverage.get("emptyByConstruction") is not True
        or coverage.get("requestedEventCodes") != []
        or coverage.get("requiredProviders") != []
        or coverage.get("sourceStatuses") != []
        or coverage.get("providerQueryEvidence") != {}
        or coverage.get("coverageByEventCode") != {}
        or row_counts.get("canonicalEvents") != 0
        or row_counts.get("eventObservations") != 0
    ):
        raise SnapshotIntegrityError(
            "technical-only composite no-event-required declaration is invalid"
        )
    raw_instrument = coverage.get("instrumentId")
    if not isinstance(raw_instrument, str):
        raise SnapshotIntegrityError("technical-only composite instrument coverage is invalid")
    try:
        coverage_instrument = instrument_normalizer(raw_instrument)
    except MarketDataSchemaError as exc:
        raise SnapshotIntegrityError(
            "technical-only composite instrument coverage is invalid"
        ) from exc
    if instruments != (coverage_instrument,):
        raise MarketDataCapabilityError(
            "requested instrument is outside technical-only composite coverage"
        )
    coverage_start = _coverage_iso_date(coverage, "start")
    coverage_end = _coverage_iso_date(coverage, "end")
    if coverage_start > coverage_end:
        raise SnapshotIntegrityError("technical-only composite coverage range is inverted")
    if period.start < coverage_start or period.end > coverage_end:
        raise MarketDataCapabilityError(
            "requested period is outside technical-only composite coverage"
        )


def _validate_no_event_required_source_manifest(
    *,
    composite_manifest: Mapping[str, object],
    source_path: Path,
) -> None:
    try:
        decoded: object = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SnapshotIntegrityError(
            "technical-only composite event source manifest is unreadable"
        ) from exc
    if not isinstance(decoded, dict):
        raise SnapshotIntegrityError("technical-only composite event source manifest is invalid")
    source = cast(dict[str, object], decoded)
    if source.get("schemaVersion") != "ashare-lab.event-snapshot.v2":
        raise SnapshotIntegrityError("technical-only composite event source schema is invalid")
    body = dict(source)
    snapshot_id = body.pop("snapshotId", None)
    digest = hashlib.sha256(
        json.dumps(
            body,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    if snapshot_id != f"events:{digest}":
        raise SnapshotIntegrityError("technical-only composite event source identity is invalid")
    if source.get("eventRequirement") != composite_manifest.get("eventRequirement") or source.get(
        "acquisitionCoverage"
    ) != composite_manifest.get("eventAcquisitionCoverage"):
        raise SnapshotIntegrityError(
            "technical-only composite event source evidence is inconsistent"
        )
    source_rows = source.get("rowCounts")
    if not isinstance(source_rows, Mapping):
        raise SnapshotIntegrityError("technical-only composite event source row counts are invalid")
    typed_rows = cast(Mapping[object, object], source_rows)
    if (
        typed_rows.get("canonicalEvents") != 0
        or typed_rows.get("observations") != 0
        or typed_rows.get("quarantinedEvents") != 0
    ):
        raise SnapshotIntegrityError(
            "technical-only composite event source must contain no event rows"
        )
    try:
        with duckdb.connect(database=":memory:") as connection:
            event_count = connection.execute(
                "SELECT COUNT(*) FROM read_parquet(?)",
                [str(source_path.parents[1] / _EVENT_FILENAME)],
            ).fetchone()
            observation_count = connection.execute(
                "SELECT COUNT(*) FROM read_parquet(?)",
                [str(source_path.parents[1] / _EVENT_OBSERVATIONS_FILENAME)],
            ).fetchone()
    except duckdb.Error as exc:
        raise SnapshotIntegrityError("technical-only composite event files are unreadable") from exc
    if event_count != (0,) or observation_count != (0,):
        raise SnapshotIntegrityError("technical-only composite event files must be empty")


def _is_successful_eastmoney_status(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    status = cast(Mapping[object, object], value)
    return (
        status.get("provider") == "eastmoney"
        and status.get("required") is True
        and status.get("querySucceeded") is True
        and status.get("status") in {"available", "empty"}
    )


class LocalParquetMarketDataRepository:
    """Read projected daily bars through immutable, content-addressed snapshots."""

    def __init__(
        self,
        data_root: str | Path,
        *,
        session_factory: SessionFactory | None = None,
        profile: MarketDataProfile = "generic_parquet",
        instrument_normalizer: InstrumentNormalizer = normalize_instrument_id,
    ) -> None:
        self._data_root = Path(data_root).expanduser().resolve()
        self._session_factory = session_factory
        self._profile = profile
        self._instrument_normalizer = instrument_normalizer
        self._snapshots: dict[str, _PinnedSnapshot] = {}
        self._snapshot_lock = threading.RLock()

    def pin_snapshot(
        self,
        requirements: DataRequirements,
        period: DateRange,
    ) -> DataSnapshotRef:
        """Hash and lock every file needed by a reproducible read slice."""

        self._validate_capabilities(requirements)
        requested_datasets = set(requirements.datasets)
        instruments = tuple(
            sorted(
                {self._instrument_normalizer(item) for item in requirements.instruments},
                key=str,
            )
        )
        manifest_files: tuple[_FileFingerprint, ...] = ()
        if self._profile in {"choice_snapshot", "technical_snapshot"}:
            requested_datasets.update(
                {
                    _SIGNAL_DAILY_DATASET,
                    _PRODUCER_MANIFEST_DATASET,
                    _CORPORATE_ACTION_DATASET,
                }
            )
            self._require_choice_snapshot_files()
            manifest_files = self._validate_choice_snapshot_manifest(
                instruments=instruments,
                period=period,
            )
        elif self._profile == "choice_minute_snapshot":
            requested_datasets.update(
                {
                    _MINUTE_DATASET,
                    _SIGNAL_MINUTE_DATASET,
                    _PRODUCER_MANIFEST_DATASET,
                }
            )
            self._require_choice_minute_snapshot_files()
            manifest_files = self._validate_choice_minute_snapshot_manifest(
                instruments=instruments,
                period=period,
            )
        elif self._profile == "composite_snapshot":
            requires_events = _EVENT_DATASET in requested_datasets
            requested_datasets.update(
                {
                    _SIGNAL_DAILY_DATASET,
                    _PRODUCER_MANIFEST_DATASET,
                    _CORPORATE_ACTION_DATASET,
                    # A strict Composite is one immutable replay universe.
                    # Technical and event strategies over the same stock and
                    # period must pin the same slice identity rather than
                    # inventing different selections from the same producer.
                    _EVENT_DATASET,
                }
            )
            self._require_composite_snapshot_files()
            manifest_files = self._validate_composite_snapshot_manifest(
                instruments=instruments,
                period=period,
                requested_event_codes=requirements.event_codes,
                requires_events=requires_events,
            )
            if requirements.needs_event_document_text:
                self._validate_event_document_text_coverage(
                    instruments=instruments,
                    requested_event_codes=requirements.event_codes,
                )
        else:
            if (
                _DAILY_DATASET in requested_datasets
                and (self._data_root / _SIGNAL_DAILY_FILENAME).is_file()
            ):
                requested_datasets.add(_SIGNAL_DAILY_DATASET)
            if (
                _MINUTE_DATASET in requested_datasets
                and (self._data_root / _SIGNAL_MINUTE_FILENAME).is_file()
            ):
                requested_datasets.add(_SIGNAL_MINUTE_DATASET)
            if (self._data_root / _PRODUCER_MANIFEST_FILENAME).is_file():
                requested_datasets.add(_PRODUCER_MANIFEST_DATASET)
        datasets = tuple(sorted(requested_datasets))
        manifest_by_dataset = {item.dataset: item for item in manifest_files}
        files = tuple(
            [manifest_by_dataset.get(dataset) or self._fingerprint(dataset) for dataset in datasets]
            + [item for item in manifest_files if item.dataset not in requested_datasets]
        )
        manifest = {
            "schema_version": _SCHEMA_VERSION,
            "datasets": list(datasets),
            "instruments": [str(item) for item in instruments],
            "period": {"start": period.start.isoformat(), "end": period.end.isoformat()},
            "files": [
                {
                    "dataset": item.dataset,
                    "filename": item.path.name,
                    "size": item.size,
                    "sha256": item.sha256,
                }
                for item in files
            ],
        }
        manifest_bytes = json.dumps(
            manifest,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        digest = hashlib.sha256(manifest_bytes).hexdigest()
        snapshot_key = f"snapshot:{digest}"

        with self._snapshot_lock:
            existing = self._snapshots.get(snapshot_key)
            if existing is not None:
                return existing.ref
            ref = DataSnapshotRef(
                snapshot_id=StrongId(snapshot_key),
                checksum=f"sha256:{digest}",
                schema_version=_SCHEMA_VERSION,
                created_at=datetime.now(UTC),
                producer_schema_version=_producer_schema_version(
                    cast(MarketDataProfile, self._profile)
                ),
                producer_snapshot_id=self._producer_snapshot_id(
                    manifest_by_dataset.get(_PRODUCER_MANIFEST_DATASET)
                ),
            )
            self._snapshots[snapshot_key] = _PinnedSnapshot(
                ref=ref,
                datasets=datasets,
                instruments=instruments,
                period=period,
                files=files,
            )
            return ref

    def _producer_snapshot_id(
        self,
        fingerprint: _FileFingerprint | None,
    ) -> str | None:
        """Read the immutable producer identity already covered by the pin."""

        if self._profile == "generic_parquet":
            return None
        if fingerprint is None:
            raise SnapshotIntegrityError("producer manifest was not included in the pin")
        manifest_path = self._data_root / _PRODUCER_MANIFEST_FILENAME
        try:
            manifest_bytes = manifest_path.read_bytes()
            decoded: object = json.loads(manifest_bytes)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SnapshotIntegrityError("producer snapshot manifest is unreadable") from exc
        if hashlib.sha256(manifest_bytes).hexdigest() != fingerprint.sha256:
            raise SnapshotIntegrityError("producer snapshot manifest changed while being pinned")
        if not isinstance(decoded, Mapping):
            raise SnapshotIntegrityError("producer snapshot manifest must be an object")
        raw_snapshot_id = cast(Mapping[object, object], decoded).get("snapshotId")
        if (
            not isinstance(raw_snapshot_id, str)
            or _PRODUCER_SNAPSHOT_ID.fullmatch(raw_snapshot_id) is None
        ):
            raise SnapshotIntegrityError("producer snapshot identity is invalid")
        return raw_snapshot_id

    def pin_strict_composite_snapshot(self) -> DataSnapshotRef:
        """Validate and pin the complete declared v2 composite coverage.

        Composition roots use this exact loader path for startup and readiness
        checks.  Deriving the request from the persisted acquisition coverage
        prevents a shallow file-exists probe from claiming readiness while the
        real run loader would reject the schema, scope, lanes, or file hashes.
        """

        if self._profile != "composite_snapshot":
            raise MarketDataCapabilityError(
                "strict composite validation requires the composite_snapshot profile"
            )
        _manifest_bytes, manifest = self._load_composite_snapshot_manifest()
        coverage = manifest.get("eventAcquisitionCoverage")
        if not isinstance(coverage, Mapping):
            raise SnapshotIntegrityError(
                "composite snapshot event acquisition coverage is incomplete"
            )
        typed_coverage = cast(Mapping[object, object], coverage)
        raw_instrument = typed_coverage.get("instrumentId")
        raw_start = typed_coverage.get("start")
        raw_end = typed_coverage.get("end")
        raw_codes = typed_coverage.get("requestedEventCodes")
        if (
            not isinstance(raw_instrument, str)
            or not isinstance(raw_start, str)
            or not isinstance(raw_end, str)
            or not isinstance(raw_codes, list)
            or not raw_codes
            or any(not isinstance(item, str) for item in cast(list[object], raw_codes))
        ):
            raise SnapshotIntegrityError("composite snapshot event acquisition scope is invalid")
        try:
            period = DateRange(date.fromisoformat(raw_start), date.fromisoformat(raw_end))
            instrument = self._instrument_normalizer(raw_instrument)
        except (ValueError, DomainValidationError, MarketDataSchemaError) as exc:
            raise SnapshotIntegrityError(
                "composite snapshot event acquisition scope is invalid"
            ) from exc
        return self.pin_snapshot(
            DataRequirements(
                instruments=(instrument,),
                datasets=("daily_ohlcv", "corporate_actions", "events"),
                event_codes=tuple(cast(list[str], raw_codes)),
            ),
            period,
        )

    def strict_composite_period(self) -> DateRange:
        """Return the validated replay interval of the selected Composite."""

        snapshot = self.pin_strict_composite_snapshot()
        with self._snapshot_lock:
            pinned = self._snapshots.get(str(snapshot.snapshot_id))
        if pinned is None:
            raise SnapshotIntegrityError("strict composite snapshot period is not pinned")
        return pinned.period

    def strict_composite_event_codes(self) -> frozenset[str]:
        """Return only event lanes proven complete by the strict v2 loader.

        Calling the real pin path first is intentional: a manifest key alone
        is not runtime authorization.  The pin validates schema, source
        provenance, lane completeness, scope, row counts and every registered
        file hash before any code is advertised through the HTTP capability.
        """

        self.pin_strict_composite_snapshot()
        _manifest_bytes, manifest = self._load_composite_snapshot_manifest()
        coverage = manifest.get("eventAcquisitionCoverage")
        if not isinstance(coverage, Mapping):
            raise SnapshotIntegrityError(
                "composite snapshot event acquisition coverage is incomplete"
            )
        raw_codes = cast(Mapping[object, object], coverage).get("requestedEventCodes")
        if not isinstance(raw_codes, list):
            raise SnapshotIntegrityError(
                "composite snapshot requested event-code coverage is invalid"
            )
        # The strict pin has already checked exact equality with
        # coverageByEventCode and complete/querySucceeded for every lane.
        return frozenset(cast(list[str], raw_codes))

    def strict_composite_event_document_text_codes(self) -> frozenset[str]:
        """Return pinned lanes whose complete text is executable right now.

        Generic event coverage is intentionally insufficient.  Every matching
        canonical row for a document-text lane must carry one valid artifact
        tied to its frozen source document.  Complete zero-result lanes remain
        executable because there is no missing matching report.
        """

        snapshot = self.pin_strict_composite_snapshot()
        with self._snapshot_lock:
            pinned = self._snapshots.get(str(snapshot.snapshot_id))
        if pinned is None or len(pinned.instruments) != 1:
            raise SnapshotIntegrityError("strict composite document-text scope is not pinned")
        candidates = self.strict_composite_event_codes() & DOCUMENT_TEXT_EVENT_CODES
        available: set[str] = set()
        for event_code in sorted(candidates):
            try:
                self._validate_event_document_text_coverage(
                    instruments=pinned.instruments,
                    requested_event_codes=(event_code,),
                )
            except MarketDataCapabilityError:
                continue
            available.add(event_code)
        return frozenset(available)

    def load_daily_bars(
        self,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> Sequence[DailyBar]:
        """Load unadjusted bars for matching, accounting and portfolio valuation."""

        return self._load_bar_dataset(
            snapshot,
            instrument_id,
            period,
            dataset=_DAILY_DATASET,
            filename=_DAILY_FILENAME,
            price_basis=PriceBasis.UNADJUSTED,
        )

    def load_signal_bars(
        self,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> Sequence[DailyBar]:
        """Load adjusted signal bars when pinned, otherwise use legacy raw bars."""

        pinned, _ = self._prepare_read(snapshot, instrument_id, period)
        if _SIGNAL_DAILY_DATASET not in pinned.datasets:
            if self._profile in {
                "choice_snapshot",
                "technical_snapshot",
                "composite_snapshot",
            }:
                raise MarketDataCapabilityError(
                    f"{self._profile} profile requires pinned back-adjusted signal bars"
                )
            return self.load_daily_bars(snapshot, instrument_id, period)
        return self._load_bar_dataset(
            snapshot,
            instrument_id,
            period,
            dataset=_SIGNAL_DAILY_DATASET,
            filename=_SIGNAL_DAILY_FILENAME,
            price_basis=PriceBasis.BACK_ADJUSTED,
        )

    def load_minute_bars(
        self,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> Sequence[MinuteBar]:
        """Load completed unadjusted one-minute bars for matching and capacity."""

        pinned, canonical_instrument = self._prepare_read(snapshot, instrument_id, period)
        if _MINUTE_DATASET not in pinned.datasets:
            raise MarketDataCapabilityError(
                "minute_ohlcv bars were not included in this pinned snapshot"
            )
        minute_file = next(item for item in pinned.files if item.dataset == _MINUTE_DATASET)
        query = """
            SELECT
                stock_code,
                epoch_us(bar_start_at),
                epoch_us(bar_end_at),
                epoch_us(available_at),
                "open",
                high,
                low,
                "close",
                volume,
                amount,
                price_basis,
                interval
            FROM read_parquet(?)
            WHERE stock_code = ?
              AND epoch_us(bar_start_at) >= ?
              AND epoch_us(bar_start_at) < ?
            ORDER BY bar_start_at ASC
        """
        start_at = datetime.combine(period.start, time.min, tzinfo=_SHANGHAI)
        end_at = datetime.combine(_exclusive_day_after(period.end), time.min, tzinfo=_SHANGHAI)
        try:
            with duckdb.connect(database=":memory:") as connection:
                rows = connection.execute(
                    query,
                    [
                        str(minute_file.path),
                        _provider_code(canonical_instrument),
                        _datetime_epoch_us(start_at),
                        _datetime_epoch_us(end_at),
                    ],
                ).fetchall()
        except duckdb.Error as exc:
            raise MarketDataSchemaError(
                f"cannot read {_MINUTE_FILENAME} with the required v1 schema: {exc}"
            ) from exc
        bars = tuple(self._row_to_minute_bar(row, canonical_instrument, period) for row in rows)
        starts = [item.bar_start_at for item in bars]
        if len(starts) != len(set(starts)):
            raise MarketDataSchemaError(f"duplicate minute bars found for {canonical_instrument}")
        self._verify_files_unchanged(pinned)
        return bars

    def load_signal_minute_closes(
        self,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> Sequence[MinuteClose]:
        """Load completed back-adjusted minute closes for indicator evaluation."""

        pinned, canonical_instrument = self._prepare_read(snapshot, instrument_id, period)
        if _SIGNAL_MINUTE_DATASET not in pinned.datasets:
            raise MarketDataCapabilityError(
                "signal_minute_close was not included in this pinned snapshot"
            )
        signal_file = next(item for item in pinned.files if item.dataset == _SIGNAL_MINUTE_DATASET)
        query = """
            SELECT
                stock_code,
                epoch_us(bar_start_at),
                epoch_us(bar_end_at),
                epoch_us(available_at),
                "close",
                price_basis,
                interval
            FROM read_parquet(?)
            WHERE stock_code = ?
              AND epoch_us(bar_start_at) >= ?
              AND epoch_us(bar_start_at) < ?
            ORDER BY bar_start_at ASC
        """
        start_at = datetime.combine(period.start, time.min, tzinfo=_SHANGHAI)
        end_at = datetime.combine(_exclusive_day_after(period.end), time.min, tzinfo=_SHANGHAI)
        try:
            with duckdb.connect(database=":memory:") as connection:
                rows = connection.execute(
                    query,
                    [
                        str(signal_file.path),
                        _provider_code(canonical_instrument),
                        _datetime_epoch_us(start_at),
                        _datetime_epoch_us(end_at),
                    ],
                ).fetchall()
        except duckdb.Error as exc:
            raise MarketDataSchemaError(
                f"cannot read {_SIGNAL_MINUTE_FILENAME} with the required v1 schema: {exc}"
            ) from exc
        closes = tuple(self._row_to_minute_close(row, canonical_instrument, period) for row in rows)
        starts = [item.bar_start_at for item in closes]
        if len(starts) != len(set(starts)):
            raise MarketDataSchemaError(
                f"duplicate minute signal closes found for {canonical_instrument}"
            )
        self._verify_files_unchanged(pinned)
        return closes

    def _load_bar_dataset(
        self,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
        *,
        dataset: str,
        filename: str,
        price_basis: PriceBasis,
    ) -> Sequence[DailyBar]:
        """Load one explicitly identified and already pinned daily-bar series."""

        pinned, canonical_instrument = self._prepare_read(snapshot, instrument_id, period)
        if dataset not in pinned.datasets:
            raise MarketDataCapabilityError(
                f"{dataset} bars were not included in this pinned snapshot"
            )
        daily_file = next(item for item in pinned.files if item.dataset == dataset)

        # The static projection and parameterized predicates let DuckDB push
        # column and row-group pruning into the Parquet scan.
        query = """
            SELECT stock_code, "date", "open", high, low, "close", volume, amount
            FROM read_parquet(?)
            WHERE stock_code = ?
              AND "date" >= ?
              AND "date" < ?
            ORDER BY "date" ASC
        """
        end_exclusive = _exclusive_day_after(period.end)
        try:
            with duckdb.connect(database=":memory:") as connection:
                rows = connection.execute(
                    query,
                    [
                        str(daily_file.path),
                        _provider_code(canonical_instrument),
                        period.start,
                        end_exclusive,
                    ],
                ).fetchall()
        except duckdb.Error as exc:
            raise MarketDataSchemaError(
                f"cannot read {filename} with the required v1 schema: {exc}"
            ) from exc

        bars = tuple(
            self._row_to_daily_bar(
                row,
                canonical_instrument,
                period,
                price_basis=price_basis,
            )
            for row in rows
        )
        dates = [bar.session_date for bar in bars]
        if len(dates) != len(set(dates)):
            raise MarketDataSchemaError(f"duplicate daily bars found for {canonical_instrument}")

        # A second verification catches a file replaced while DuckDB was
        # scanning it; returning mixed-version rows would violate the snapshot.
        self._verify_files_unchanged(pinned)
        return bars

    def load_sessions(
        self,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> Sequence[InstrumentSession]:
        pinned, canonical_instrument = self._prepare_read(
            snapshot,
            instrument_id,
            period,
        )
        if self._session_factory is not None:
            sessions = tuple(
                sorted(
                    self._session_factory(snapshot, canonical_instrument, period),
                    key=lambda item: item.session_date,
                )
            )
        elif self._profile in {
            "choice_snapshot",
            "technical_snapshot",
            "composite_snapshot",
        }:
            # Producer snapshots register and hash this file in the same pin as
            # OHLCV, events and corporate actions.  Read it through the
            # content-addressed root instead of a process-global side path so a
            # multi-snapshot worker cannot mix market-rule facts across runs.
            from .parquet_sessions import ParquetInstrumentSessionProvider

            session_path = self._data_root / "instrument_sessions.parquet"
            if not any(item.path == session_path for item in pinned.files):
                raise SnapshotIntegrityError(
                    "instrument-session reference was not included in the pinned snapshot"
                )
            provider = ParquetInstrumentSessionProvider(
                session_path,
                instrument_normalizer=self._instrument_normalizer,
            )
            sessions = tuple(
                provider.sessions_for_period(
                    canonical_instrument,
                    start=period.start,
                    end=period.end,
                )
            )
        else:
            raise MarketDataCapabilityError(
                "historical sessions require an injected SessionFactory with "
                "point-in-time ST, suspension, board and price-limit facts"
            )
        seen_dates: set[date] = set()
        for session in sessions:
            if session.instrument_id != canonical_instrument:
                raise MarketDataSchemaError(
                    "SessionFactory returned a session for a different instrument"
                )
            if not period.start <= session.session_date <= period.end:
                raise MarketDataSchemaError(
                    "SessionFactory returned a session outside the requested range"
                )
            if session.session_date in seen_dates:
                raise MarketDataSchemaError(
                    "SessionFactory returned duplicate instrument-session dates"
                )
            seen_dates.add(session.session_date)
        self._verify_files_unchanged(pinned)
        return sessions

    def load_events(
        self,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> Sequence[EventEnvelope]:
        pinned, canonical_instrument = self._prepare_read(snapshot, instrument_id, period)
        if _EVENT_DATASET not in pinned.datasets:
            raise MarketDataCapabilityError("events were not included in this pinned snapshot")
        event_file = next(item for item in pinned.files if item.dataset == _EVENT_DATASET)
        try:
            with duckdb.connect(database=":memory:") as connection:
                described = connection.execute(
                    "DESCRIBE SELECT * FROM read_parquet(?)",
                    [str(event_file.path)],
                ).fetchall()
                columns = {str(row[0]) for row in described}
                v2_columns = {
                    "source_event_id",
                    "replay_available_at",
                    "provider",
                    "source_url",
                    "raw_response_sha256",
                    "validation_status",
                }
                if v2_columns <= columns:
                    query = """
                        SELECT
                            stock_code,
                            event_id,
                            source_event_id,
                            event_code,
                            CAST(occurred_at AS VARCHAR) AS occurred_at,
                            CAST(source_released_at AS VARCHAR) AS source_released_at,
                            CAST(vendor_first_available_at AS VARCHAR)
                                AS vendor_first_available_at,
                            CAST(ingested_at AS VARCHAR) AS ingested_at,
                            CAST(replay_available_at AS VARCHAR) AS replay_available_at,
                            revision_no,
                            provider,
                            source_url,
                            raw_response_sha256,
                            time_quality,
                            validation_status,
                            attributes_json
                        FROM read_parquet(?)
                        WHERE stock_code = ?
                        ORDER BY event_id ASC, revision_no ASC
                    """
                else:
                    query = """
                        SELECT
                            stock_code,
                            event_id,
                            event_code,
                            CAST(occurred_at AS VARCHAR) AS occurred_at,
                            CAST(source_released_at AS VARCHAR) AS source_released_at,
                            CAST(vendor_first_available_at AS VARCHAR)
                                AS vendor_first_available_at,
                            CAST(ingested_at AS VARCHAR) AS ingested_at,
                            revision_no,
                            time_quality,
                            attributes_json
                        FROM read_parquet(?)
                        WHERE stock_code = ?
                        ORDER BY event_id ASC, revision_no ASC
                    """
                rows = connection.execute(
                    query,
                    [str(event_file.path), _provider_code(canonical_instrument)],
                ).fetchall()
        except duckdb.Error as exc:
            raise MarketDataSchemaError(
                f"cannot read {_EVENT_FILENAME} with the required v1 schema: {exc}"
            ) from exc

        events = tuple(self._row_to_event(row, canonical_instrument) for row in rows)
        if self._profile == "composite_snapshot":
            invalid = tuple(
                item
                for item in events
                if item.validation_status != "validated"
                or item.replay_available_at is None
                or item.time_quality not in {TimeQuality.EXACT, TimeQuality.VENDOR_OBSERVED}
                or item.provider is None
                or item.source_event_id is None
                or item.raw_response_sha256 is None
            )
            if invalid:
                raise MarketDataSchemaError(
                    "composite event rows must be validated, second-level, replayable, "
                    "and carry complete source provenance"
                )
        revision_keys = [(item.event.event_id.value, item.revision_no) for item in events]
        if len(revision_keys) != len(set(revision_keys)):
            raise MarketDataSchemaError("duplicate event_id and revision_no rows found")
        selected = tuple(
            sorted(
                (item for item in events if _event_overlaps_period(item, period)),
                key=lambda item: (
                    item.available_at or item.ingested_at,
                    item.event.event_id.value,
                    item.revision_no,
                ),
            )
        )
        self._verify_files_unchanged(pinned)
        return selected

    def load_corporate_actions(
        self,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> Sequence[CorporateAction]:
        """Load validated terms without inferring actions from adjusted prices."""

        pinned, canonical_instrument = self._prepare_read(snapshot, instrument_id, period)
        if _CORPORATE_ACTION_DATASET not in pinned.datasets:
            raise MarketDataCapabilityError(
                "corporate actions were not included in this pinned snapshot"
            )
        action_file = next(
            item for item in pinned.files if item.dataset == _CORPORATE_ACTION_DATASET
        )
        query = """
            SELECT
                stock_code,
                action_id,
                source_action_id,
                action_type,
                record_date,
                ex_date,
                CAST(source_released_at AS VARCHAR) AS source_released_at,
                CAST(vendor_first_available_at AS VARCHAR) AS vendor_first_available_at,
                CAST(ingested_at AS VARCHAR) AS ingested_at,
                CAST(replay_available_at AS VARCHAR) AS replay_available_at,
                revision_no,
                time_quality,
                provider,
                source_url,
                raw_response_sha256,
                validation_status,
                currency,
                gross_cash_per_share,
                cash_pay_date,
                share_multiplier,
                share_credit_date,
                share_sellable_date,
                rights_ratio,
                rights_subscription_price,
                rights_payment_deadline,
                rights_listing_date
            FROM read_parquet(?)
            WHERE stock_code = ?
              AND ex_date >= ?
              AND ex_date < ?
            ORDER BY ex_date ASC, action_id ASC, revision_no ASC
        """
        try:
            with duckdb.connect(database=":memory:") as connection:
                rows = connection.execute(
                    query,
                    [
                        str(action_file.path),
                        _provider_code(canonical_instrument),
                        period.start,
                        _exclusive_day_after(period.end),
                    ],
                ).fetchall()
        except duckdb.Error as exc:
            raise MarketDataSchemaError(
                f"cannot read {_CORPORATE_ACTION_FILENAME} with the required v1 schema: {exc}"
            ) from exc

        actions = tuple(self._row_to_corporate_action(row, canonical_instrument) for row in rows)
        revision_keys = [(item.action_id.value, item.revision_no) for item in actions]
        if len(revision_keys) != len(set(revision_keys)):
            raise MarketDataSchemaError(
                "duplicate corporate-action action_id and revision_no rows found"
            )
        selected = _select_corporate_action_revisions(actions)
        source_leg_keys = [(item.source_action_id, item.action_type) for item in selected]
        if len(source_leg_keys) != len(set(source_leg_keys)):
            raise MarketDataSchemaError(
                "multiple selected revisions exist for one corporate-action source leg"
            )
        share_actions = {
            CorporateActionKind.SHARE_DISTRIBUTION,
            CorporateActionKind.STOCK_SPLIT,
            CorporateActionKind.REVERSE_SPLIT,
        }
        share_dates = [item.ex_date for item in selected if item.action_type in share_actions]
        if len(share_dates) != len(set(share_dates)):
            raise MarketDataSchemaError(
                "multiple share-changing actions on one ex_date must be normalized "
                "into one multiplier"
            )
        self._verify_files_unchanged(pinned)
        return selected

    def _prepare_read(
        self,
        snapshot: DataSnapshotRef,
        instrument_id: InstrumentId,
        period: DateRange,
    ) -> tuple[_PinnedSnapshot, InstrumentId]:
        snapshot_key = str(snapshot.snapshot_id)
        with self._snapshot_lock:
            pinned = self._snapshots.get(snapshot_key)
        if pinned is None:
            raise SnapshotIntegrityError("snapshot was not pinned by this repository instance")
        if snapshot != pinned.ref:
            raise SnapshotIntegrityError("snapshot reference does not match its pin")

        canonical_instrument = self._instrument_normalizer(instrument_id)
        if canonical_instrument not in pinned.instruments:
            raise SnapshotScopeError(
                f"instrument {canonical_instrument} was not included in the snapshot"
            )
        if period.start < pinned.period.start or period.end > pinned.period.end:
            raise SnapshotScopeError("requested date range exceeds the pinned snapshot range")
        self._verify_files_unchanged(pinned)
        return pinned, canonical_instrument

    def _validate_capabilities(self, requirements: DataRequirements) -> None:
        requested = set(requirements.datasets)
        if requirements.needs_minute and _MINUTE_DATASET not in requested:
            raise MarketDataCapabilityError(
                "minute requests must explicitly include the minute_ohlcv dataset"
            )
        if requirements.needs_tick:
            raise MarketDataCapabilityError("the local repository does not provide tick trades")
        if requirements.needs_l2_queue:
            raise MarketDataCapabilityError("the local repository does not provide L2 order queues")
        unsupported = sorted(
            set(requirements.datasets)
            - {
                _DAILY_DATASET,
                _SIGNAL_DAILY_DATASET,
                _MINUTE_DATASET,
                _SIGNAL_MINUTE_DATASET,
                _PRODUCER_MANIFEST_DATASET,
                _EVENT_DATASET,
                _CORPORATE_ACTION_DATASET,
            }
        )
        if unsupported:
            raise MarketDataCapabilityError(
                "unsupported local market-data datasets: " + ", ".join(unsupported)
            )

    def _require_choice_snapshot_files(self) -> None:
        required = (
            _DAILY_FILENAME,
            _SIGNAL_DAILY_FILENAME,
            _CORPORATE_ACTION_FILENAME,
            _PRODUCER_MANIFEST_FILENAME,
        )
        missing = tuple(name for name in required if not (self._data_root / name).is_file())
        if missing:
            raise MarketDataCapabilityError(
                f"{self._profile} profile requires files: " + ", ".join(missing)
            )

    def _require_choice_minute_snapshot_files(self) -> None:
        required = (
            _MINUTE_FILENAME,
            _SIGNAL_MINUTE_FILENAME,
            _PRODUCER_MANIFEST_FILENAME,
        )
        missing = tuple(name for name in required if not (self._data_root / name).is_file())
        if missing:
            raise MarketDataCapabilityError(
                "choice_minute_snapshot profile requires files: " + ", ".join(missing)
            )

    def _require_composite_snapshot_files(self) -> None:
        required = (
            _DAILY_FILENAME,
            _SIGNAL_DAILY_FILENAME,
            _EVENT_FILENAME,
            _EVENT_OBSERVATIONS_FILENAME,
            _CORPORATE_ACTION_FILENAME,
            "instrument_sessions.parquet",
            _PRODUCER_MANIFEST_FILENAME,
        )
        missing = tuple(name for name in required if not (self._data_root / name).is_file())
        if missing:
            raise MarketDataCapabilityError(
                "composite_snapshot profile requires files: " + ", ".join(missing)
            )

    def _validate_composite_snapshot_manifest(
        self,
        *,
        instruments: tuple[InstrumentId, ...],
        period: DateRange,
        requested_event_codes: tuple[str, ...],
        requires_events: bool,
    ) -> tuple[_FileFingerprint, ...]:
        """Validate a Choice + event snapshot and pin every registered input."""

        manifest_path = self._data_root / _PRODUCER_MANIFEST_FILENAME
        manifest_bytes, manifest = self._load_composite_snapshot_manifest()

        snapshot_id = manifest.get("snapshotId")
        body = dict(manifest)
        body.pop("snapshotId", None)
        digest = hashlib.sha256(
            json.dumps(
                body,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        if snapshot_id != f"composite:{digest}":
            raise SnapshotIntegrityError(
                "composite snapshotId does not match the canonical manifest body"
            )
        if self._data_root.name != digest:
            raise SnapshotIntegrityError(
                "composite snapshot directory does not match the manifest digest"
            )
        if len(instruments) != 1:
            raise MarketDataCapabilityError(
                "composite snapshot requires exactly one covered instrument"
            )

        event_policy = manifest.get("eventPolicy")
        typed_event_policy = (
            cast(Mapping[object, object], event_policy)
            if isinstance(event_policy, Mapping)
            else None
        )
        if typed_event_policy is None or not typed_event_policy.get("strictDemoSecondsOnly"):
            raise SnapshotIntegrityError(
                "composite snapshot must declare strict second-level event validation"
            )
        capabilities = manifest.get("capabilities")
        typed_capabilities = (
            cast(Mapping[object, object], capabilities)
            if isinstance(capabilities, Mapping)
            else None
        )
        if typed_capabilities is None:
            raise SnapshotIntegrityError("composite snapshot capabilities are missing")
        event_capability = typed_capabilities.get("events")
        no_event_required = event_capability == "not_required"
        if event_capability not in {"validated_for_demo", "not_required"}:
            raise SnapshotIntegrityError("composite snapshot event capability is invalid")
        if typed_capabilities.get("corporateActions") != "validated_for_demo":
            raise SnapshotIntegrityError(
                "composite snapshot does not contain validated corporate-action coverage"
            )
        coverage = manifest.get("corporateActionCoverage")
        typed_coverage = (
            cast(Mapping[object, object], coverage) if isinstance(coverage, Mapping) else None
        )
        if typed_coverage is None:
            raise SnapshotIntegrityError(
                "composite snapshot corporate-action coverage is incomplete"
            )
        event_coverage = manifest.get("eventAcquisitionCoverage")
        typed_event_coverage = (
            cast(Mapping[object, object], event_coverage)
            if isinstance(event_coverage, Mapping)
            else None
        )
        if typed_event_coverage is None:
            raise SnapshotIntegrityError(
                "composite snapshot event acquisition coverage is incomplete"
            )
        row_counts = manifest.get("rowCounts")
        typed_row_counts = (
            cast(Mapping[object, object], row_counts) if isinstance(row_counts, Mapping) else None
        )
        if typed_row_counts is None:
            raise SnapshotIntegrityError(
                "composite snapshot event acquisition row count is inconsistent"
            )
        if no_event_required:
            if requires_events or requested_event_codes:
                raise MarketDataCapabilityError(
                    "technical-only composite cannot satisfy an event data request"
                )
            _validate_no_event_required_composite_contract(
                manifest=manifest,
                coverage=typed_event_coverage,
                row_counts=typed_row_counts,
                instruments=instruments,
                period=period,
                instrument_normalizer=self._instrument_normalizer,
            )
        else:
            if (
                typed_event_coverage.get("status") != "complete"
                or typed_event_coverage.get("querySucceeded") is not True
                or typed_event_coverage.get("rowCount") != typed_row_counts.get("eventObservations")
            ):
                raise SnapshotIntegrityError(
                    "composite snapshot event acquisition coverage is incomplete"
                )
            _validate_event_coverage_scope(
                typed_event_coverage,
                instruments=instruments,
                period=period,
                requested_event_codes=requested_event_codes,
                instrument_normalizer=self._instrument_normalizer,
            )

        raw_files = manifest.get("files")
        if not isinstance(raw_files, Mapping):
            raise SnapshotIntegrityError("composite snapshot manifest files must be an object")
        files = cast(Mapping[object, object], raw_files)
        required_files = {
            _DAILY_FILENAME,
            _SIGNAL_DAILY_FILENAME,
            _EVENT_FILENAME,
            _EVENT_OBSERVATIONS_FILENAME,
            _CORPORATE_ACTION_FILENAME,
            "instrument_sessions.parquet",
            "source/event_snapshot_manifest.json",
        }
        names = {name for name in files if isinstance(name, str)}
        technical_manifests = {
            "source/choice_snapshot_manifest.json",
            "source/technical_snapshot_manifest.json",
        }
        present_technical_manifests = technical_manifests & names
        if len(present_technical_manifests) != 1:
            raise SnapshotIntegrityError(
                "composite snapshot must register exactly one allowlisted technical manifest"
            )
        missing = sorted(required_files - names)
        if missing:
            raise SnapshotIntegrityError(
                "composite snapshot manifest is missing required files: " + ", ".join(missing)
            )

        known_datasets = {
            _DAILY_FILENAME: _DAILY_DATASET,
            _SIGNAL_DAILY_FILENAME: _SIGNAL_DAILY_DATASET,
            _EVENT_FILENAME: _EVENT_DATASET,
            _CORPORATE_ACTION_FILENAME: _CORPORATE_ACTION_DATASET,
        }
        fingerprints: list[_FileFingerprint] = [
            _FileFingerprint(
                dataset=_PRODUCER_MANIFEST_DATASET,
                path=manifest_path,
                size=len(manifest_bytes),
                sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            )
        ]
        for raw_relative, raw_metadata in sorted(files.items(), key=lambda item: str(item[0])):
            if not isinstance(raw_relative, str) or not raw_relative:
                raise SnapshotIntegrityError(
                    "composite snapshot manifest file names must be non-empty strings"
                )
            relative = PurePosixPath(raw_relative)
            if (
                relative.is_absolute()
                or relative.as_posix() != raw_relative
                or any(part in {"", ".", ".."} for part in relative.parts)
            ):
                raise SnapshotIntegrityError(
                    f"composite snapshot manifest contains an unsafe file path: {raw_relative}"
                )
            if raw_relative == _PRODUCER_MANIFEST_FILENAME:
                raise SnapshotIntegrityError(
                    "composite snapshot manifest cannot include its own file hash"
                )
            if not isinstance(raw_metadata, Mapping):
                raise SnapshotIntegrityError(
                    f"composite snapshot metadata must be an object: {raw_relative}"
                )
            metadata = cast(Mapping[object, object], raw_metadata)
            expected_size = metadata.get("bytes")
            expected_hash = metadata.get("sha256")
            if (
                not isinstance(expected_size, int)
                or isinstance(expected_size, bool)
                or expected_size < 0
            ):
                raise SnapshotIntegrityError(
                    f"composite snapshot file size is invalid: {raw_relative}"
                )
            if not isinstance(expected_hash, str) or _SHA256.fullmatch(expected_hash) is None:
                raise SnapshotIntegrityError(
                    f"composite snapshot file sha256 is invalid: {raw_relative}"
                )
            path = self._data_root.joinpath(*relative.parts).resolve()
            if not path.is_relative_to(self._data_root) or not path.is_file():
                raise SnapshotIntegrityError(
                    f"composite snapshot manifest file does not exist: {raw_relative}"
                )
            actual_size = path.stat().st_size
            actual_hash = _sha256_file(path)
            if actual_size != expected_size or actual_hash != expected_hash:
                raise SnapshotIntegrityError(
                    f"composite snapshot file hash mismatch: {raw_relative}"
                )
            fingerprints.append(
                _FileFingerprint(
                    dataset=known_datasets.get(
                        raw_relative,
                        f"composite_manifest_file:{raw_relative}",
                    ),
                    path=path,
                    size=actual_size,
                    sha256=actual_hash,
                )
            )
        _validate_persisted_corporate_action_coverage(
            manifest=manifest,
            coverage=typed_coverage,
            start=_coverage_iso_date(typed_event_coverage, "start"),
            end=_coverage_iso_date(typed_event_coverage, "end"),
            path=self._data_root / _CORPORATE_ACTION_FILENAME,
            instrument=instruments[0],
        )
        if no_event_required:
            _validate_no_event_required_source_manifest(
                composite_manifest=manifest,
                source_path=self._data_root / "source/event_snapshot_manifest.json",
            )
        declared_event_rows = (
            typed_row_counts.get("canonicalEvents"),
            typed_row_counts.get("eventObservations"),
        )
        if any(type(value) is int and value > 0 for value in declared_event_rows):
            self._validate_present_event_document_text_artifacts(instruments=instruments)
        try:
            manifest_unchanged = manifest_path.read_bytes() == manifest_bytes
        except OSError as exc:
            raise SnapshotIntegrityError(
                "composite snapshot manifest changed while it was being pinned"
            ) from exc
        if not manifest_unchanged:
            raise SnapshotIntegrityError(
                "composite snapshot manifest changed while it was being pinned"
            )
        return tuple(fingerprints)

    def _validate_event_document_text_coverage(
        self,
        *,
        instruments: tuple[InstrumentId, ...],
        requested_event_codes: tuple[str, ...],
    ) -> None:
        """Require every matching canonical event to carry a complete text artifact.

        A valid zero-row acquisition remains executable and simply produces no
        signal.  When a report exists, however, submission must not queue a
        run that will discover only at execution time that its document text
        was never frozen.
        """

        if len(instruments) != 1 or not requested_event_codes:
            raise MarketDataCapabilityError(
                "event document text requires one instrument and explicit event codes"
            )
        self._validate_event_document_text_artifacts(
            instruments=instruments,
            requested_event_codes=frozenset(requested_event_codes),
            require_every_matching=True,
        )

    def _validate_present_event_document_text_artifacts(
        self,
        *,
        instruments: tuple[InstrumentId, ...],
    ) -> None:
        """Validate every advertised artifact during strict Composite preflight."""

        if len(instruments) != 1:
            raise MarketDataCapabilityError(
                "event document text preflight requires one covered instrument"
            )
        self._validate_event_document_text_artifacts(
            instruments=instruments,
            requested_event_codes=frozenset(),
            require_every_matching=False,
        )

    def _validate_event_document_text_artifacts(
        self,
        *,
        instruments: tuple[InstrumentId, ...],
        requested_event_codes: frozenset[str],
        require_every_matching: bool,
    ) -> None:
        event_path = self._data_root / _EVENT_FILENAME
        observation_path = self._data_root / _EVENT_OBSERVATIONS_FILENAME
        try:
            with duckdb.connect(database=":memory:") as connection:
                rows = connection.execute(
                    """
                    SELECT
                        event_code,
                        stock_code,
                        provider,
                        source_event_id,
                        revision_no,
                        attributes_json
                    FROM read_parquet(?) AS events
                    WHERE stock_code = ?
                    ORDER BY event_code, event_id, revision_no
                    """,
                    [
                        str(event_path),
                        _provider_code(instruments[0]),
                    ],
                ).fetchall()
        except duckdb.Error as exc:
            raise MarketDataSchemaError(
                "cannot validate frozen event document text coverage"
            ) from exc

        artifacts: list[
            tuple[
                str,
                tuple[object, object, object, object],
                Mapping[str, str | int | Decimal | bool | None],
            ]
        ] = []
        for (
            raw_event_code,
            raw_stock_code,
            raw_provider,
            raw_source_event_id,
            raw_revision_no,
            raw_attributes,
        ) in rows:
            event_code = _required_text(raw_event_code, "event_code")
            if requested_event_codes and event_code not in requested_event_codes:
                continue
            attributes = _event_attributes(raw_attributes)
            # Scope/version metadata alone does not make generic report events
            # unusable.  A document-text strategy still takes the strict path
            # below and requires every matching event to contain the complete
            # frozen payload.
            has_document_artifact = "document_text" in attributes
            if not has_document_artifact:
                if require_every_matching:
                    raise MarketDataCapabilityError(
                        "event document text is not frozen for every matching report: " + event_code
                    )
                continue
            artifacts.append(
                (
                    event_code,
                    (
                        raw_stock_code,
                        raw_provider,
                        raw_source_event_id,
                        raw_revision_no,
                    ),
                    attributes,
                )
            )
        if not artifacts:
            return

        try:
            with duckdb.connect(database=":memory:") as connection:
                observation_rows = connection.execute(
                    """
                    SELECT
                        stock_code,
                        provider,
                        source_event_id,
                        revision_no,
                        document_sha256
                    FROM read_parquet(?)
                    WHERE stock_code = ?
                    ORDER BY provider, source_event_id, revision_no
                    """,
                    [
                        str(observation_path),
                        _provider_code(instruments[0]),
                    ],
                ).fetchall()
        except duckdb.Error as exc:
            raise MarketDataSchemaError(
                "cannot validate frozen event document source evidence"
            ) from exc

        source_hashes: dict[tuple[object, object, object, object], object] = {}
        for (
            raw_stock_code,
            raw_provider,
            raw_source_event_id,
            raw_revision_no,
            raw_hash,
        ) in observation_rows:
            key = (
                raw_stock_code,
                raw_provider,
                raw_source_event_id,
                raw_revision_no,
            )
            if key in source_hashes:
                raise MarketDataSchemaError(
                    "event document source evidence contains duplicate observations"
                )
            source_hashes[key] = raw_hash

        for event_code, source_key, attributes in artifacts:
            try:
                validate_document_text_artifact(
                    attributes,
                    expected_source_sha256=_required_text(
                        source_hashes.get(source_key),
                        "document_sha256",
                    ),
                )
            except (DocumentMetricError, MarketDataSchemaError) as exc:
                raise MarketDataCapabilityError(
                    f"event document text is not frozen or invalid for {event_code}: {exc}"
                ) from exc

    def _load_composite_snapshot_manifest(self) -> tuple[bytes, dict[str, object]]:
        """Decode the one authoritative composite manifest and require v2."""

        manifest_path = self._data_root / _PRODUCER_MANIFEST_FILENAME
        try:
            manifest_bytes = manifest_path.read_bytes()
        except OSError as exc:
            raise SnapshotIntegrityError(
                f"cannot read composite snapshot manifest: {manifest_path}"
            ) from exc
        try:
            decoded = cast(object, json.loads(manifest_bytes))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SnapshotIntegrityError("composite snapshot manifest is not valid JSON") from exc
        if not isinstance(decoded, dict):
            raise SnapshotIntegrityError("composite snapshot manifest must be a JSON object")
        manifest = cast(dict[str, object], decoded)
        if manifest.get("schemaVersion") != _COMPOSITE_SNAPSHOT_SCHEMA_VERSION:
            raise SnapshotIntegrityError(
                "composite snapshot manifest has an unsupported schemaVersion"
            )
        return manifest_bytes, manifest

    def _validate_choice_minute_snapshot_manifest(
        self,
        *,
        instruments: tuple[InstrumentId, ...],
        period: DateRange,
    ) -> tuple[_FileFingerprint, ...]:
        """Validate a Choice minute producer manifest and every registered shard."""

        manifest_path = self._data_root / _PRODUCER_MANIFEST_FILENAME
        try:
            manifest_bytes = manifest_path.read_bytes()
        except OSError as exc:
            raise SnapshotIntegrityError(
                f"cannot read Choice minute snapshot manifest: {manifest_path}"
            ) from exc
        try:
            decoded = cast(object, json.loads(manifest_bytes))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SnapshotIntegrityError(
                "Choice minute snapshot manifest is not valid JSON"
            ) from exc
        if not isinstance(decoded, dict):
            raise SnapshotIntegrityError("Choice minute snapshot manifest must be an object")
        manifest = cast(dict[str, object], decoded)
        if manifest.get("schemaVersion") != _CHOICE_MINUTE_SNAPSHOT_SCHEMA_VERSION:
            raise SnapshotIntegrityError(
                "Choice minute snapshot manifest has an unsupported schemaVersion"
            )

        snapshot_id = manifest.get("snapshotId")
        body = dict(manifest)
        body.pop("snapshotId", None)
        digest = hashlib.sha256(
            json.dumps(
                body,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        if snapshot_id != f"choice-minute:{digest}":
            raise SnapshotIntegrityError(
                "Choice minute snapshotId does not match the canonical manifest body"
            )
        if self._data_root.name != digest:
            raise SnapshotIntegrityError(
                "Choice minute snapshot directory does not match the manifest digest"
            )

        symbol = manifest.get("symbol")
        if not isinstance(symbol, str):
            raise SnapshotIntegrityError("Choice minute snapshot symbol is invalid")
        try:
            snapshot_instrument = self._instrument_normalizer(symbol)
        except MarketDataSchemaError as exc:
            raise SnapshotIntegrityError("Choice minute snapshot symbol is invalid") from exc
        if any(item != snapshot_instrument for item in instruments):
            raise MarketDataCapabilityError(
                "requested instrument is outside Choice minute snapshot coverage"
            )

        requested_range = manifest.get("requestedRange")
        if not isinstance(requested_range, list):
            raise SnapshotIntegrityError("Choice minute requestedRange is invalid")
        requested_range_items = cast(list[object], requested_range)
        if len(requested_range_items) != 2 or any(
            not isinstance(item, str) for item in requested_range_items
        ):
            raise SnapshotIntegrityError("Choice minute requestedRange is invalid")
        typed_requested_range = cast(list[str], requested_range_items)
        try:
            range_start, range_end = (
                date.fromisoformat(typed_requested_range[0]),
                date.fromisoformat(typed_requested_range[1]),
            )
        except ValueError as exc:
            raise SnapshotIntegrityError("Choice minute requestedRange is invalid") from exc
        if period.start < range_start or period.end > range_end:
            raise MarketDataCapabilityError(
                "requested period is outside Choice minute snapshot coverage"
            )

        capabilities = manifest.get("capabilities")
        if (
            not isinstance(capabilities, Mapping)
            or cast(Mapping[object, object], capabilities).get("technicalMinute")
            != "validated_for_demo"
        ):
            raise SnapshotIntegrityError("Choice minute snapshot lacks validated minute capability")
        coverage = manifest.get("coverage")
        if not isinstance(coverage, Mapping):
            raise SnapshotIntegrityError("Choice minute snapshot coverage is missing")
        typed_coverage = cast(Mapping[object, object], coverage)
        if (
            typed_coverage.get("status") != "complete"
            or typed_coverage.get("querySucceeded") is not True
            or typed_coverage.get("expectedBarsPerFullSession") != 240
            or typed_coverage.get("missingBars") != 0
            or typed_coverage.get("duplicateBars") != 0
        ):
            raise SnapshotIntegrityError("Choice minute snapshot coverage is incomplete")
        session_dates = typed_coverage.get("sessionDates")
        if not isinstance(session_dates, list):
            raise SnapshotIntegrityError("Choice minute session coverage is invalid")
        session_date_items = cast(list[object], session_dates)
        if not session_date_items or any(not isinstance(item, str) for item in session_date_items):
            raise SnapshotIntegrityError("Choice minute session coverage is invalid")
        typed_session_dates = cast(list[str], session_date_items)

        row_counts = manifest.get("rowCounts")
        if not isinstance(row_counts, Mapping):
            raise SnapshotIntegrityError("Choice minute rowCounts is missing")
        typed_counts = cast(Mapping[object, object], row_counts)
        expected_rows = len(typed_session_dates) * 240
        if (
            typed_counts.get("executionMinute") != expected_rows
            or typed_counts.get("signalMinute") != expected_rows
            or typed_counts.get("sessions") != len(typed_session_dates)
        ):
            raise SnapshotIntegrityError("Choice minute row counts are inconsistent")

        raw_files = manifest.get("files")
        if not isinstance(raw_files, Mapping):
            raise SnapshotIntegrityError("Choice minute manifest files must be an object")
        files = cast(Mapping[object, object], raw_files)
        names = {name for name in files if isinstance(name, str)}
        missing = sorted(_CHOICE_MINUTE_REQUIRED_FILES - names)
        if missing:
            raise SnapshotIntegrityError(
                "Choice minute manifest is missing required files: " + ", ".join(missing)
            )
        response_names = sorted(
            name for name in names if name.startswith("raw/responses/") and name.endswith(".json")
        )
        if not response_names or typed_counts.get("sourceResponses") != len(response_names):
            raise SnapshotIntegrityError(
                "Choice minute source response shards are missing or inconsistent"
            )

        known_datasets = {
            _MINUTE_FILENAME: _MINUTE_DATASET,
            _SIGNAL_MINUTE_FILENAME: _SIGNAL_MINUTE_DATASET,
        }
        fingerprints: list[_FileFingerprint] = [
            _FileFingerprint(
                dataset=_PRODUCER_MANIFEST_DATASET,
                path=manifest_path,
                size=len(manifest_bytes),
                sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            )
        ]
        for raw_relative, raw_metadata in sorted(files.items(), key=lambda item: str(item[0])):
            if not isinstance(raw_relative, str) or not raw_relative:
                raise SnapshotIntegrityError(
                    "Choice minute manifest file names must be non-empty strings"
                )
            relative = PurePosixPath(raw_relative)
            if (
                relative.is_absolute()
                or relative.as_posix() != raw_relative
                or any(part in {"", ".", ".."} for part in relative.parts)
            ):
                raise SnapshotIntegrityError(
                    f"Choice minute manifest contains an unsafe path: {raw_relative}"
                )
            if raw_relative == _PRODUCER_MANIFEST_FILENAME:
                raise SnapshotIntegrityError(
                    "Choice minute manifest cannot include its own file hash"
                )
            if not isinstance(raw_metadata, Mapping):
                raise SnapshotIntegrityError(
                    f"Choice minute metadata must be an object: {raw_relative}"
                )
            metadata = cast(Mapping[object, object], raw_metadata)
            expected_size = metadata.get("bytes")
            expected_hash = metadata.get("sha256")
            if (
                not isinstance(expected_size, int)
                or isinstance(expected_size, bool)
                or expected_size < 0
                or not isinstance(expected_hash, str)
                or _SHA256.fullmatch(expected_hash) is None
            ):
                raise SnapshotIntegrityError(
                    f"Choice minute file metadata is invalid: {raw_relative}"
                )
            path = self._data_root.joinpath(*relative.parts).resolve()
            if not path.is_relative_to(self._data_root) or not path.is_file():
                raise SnapshotIntegrityError(
                    f"Choice minute manifest file does not exist: {raw_relative}"
                )
            actual_size = path.stat().st_size
            actual_hash = _sha256_file(path)
            if actual_size != expected_size or actual_hash != expected_hash:
                raise SnapshotIntegrityError(
                    f"Choice minute snapshot file hash mismatch: {raw_relative}"
                )
            fingerprints.append(
                _FileFingerprint(
                    dataset=known_datasets.get(
                        raw_relative,
                        f"choice_minute_manifest_file:{raw_relative}",
                    ),
                    path=path,
                    size=actual_size,
                    sha256=actual_hash,
                )
            )
        try:
            manifest_unchanged = manifest_path.read_bytes() == manifest_bytes
        except OSError as exc:
            raise SnapshotIntegrityError(
                "Choice minute manifest changed while it was being pinned"
            ) from exc
        if not manifest_unchanged:
            raise SnapshotIntegrityError("Choice minute manifest changed while it was being pinned")
        return tuple(fingerprints)

    def _validate_choice_snapshot_manifest(
        self,
        *,
        instruments: tuple[InstrumentId, ...],
        period: DateRange,
    ) -> tuple[_FileFingerprint, ...]:
        """Validate and pin a Choice or technical daily producer manifest."""

        manifest_path = self._data_root / _PRODUCER_MANIFEST_FILENAME
        try:
            manifest_bytes = manifest_path.read_bytes()
        except OSError as exc:
            raise SnapshotIntegrityError(
                f"cannot read Choice snapshot manifest: {manifest_path}"
            ) from exc
        manifest_fingerprint = _FileFingerprint(
            dataset=_PRODUCER_MANIFEST_DATASET,
            path=manifest_path,
            size=len(manifest_bytes),
            sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        )
        try:
            decoded = cast(object, json.loads(manifest_bytes))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SnapshotIntegrityError("Choice snapshot manifest is not valid JSON") from exc
        if not isinstance(decoded, dict):
            raise SnapshotIntegrityError("Choice snapshot manifest must be a JSON object")
        manifest = cast(dict[str, object], decoded)
        technical_snapshot = self._profile == "technical_snapshot"
        snapshot_label = "technical" if technical_snapshot else "Choice"
        expected_schema = (
            _TECHNICAL_SNAPSHOT_SCHEMA_VERSION
            if technical_snapshot
            else _CHOICE_SNAPSHOT_SCHEMA_VERSION
        )
        expected_prefix = "technical" if technical_snapshot else "choice"
        if manifest.get("schemaVersion") != expected_schema:
            raise SnapshotIntegrityError(
                f"{snapshot_label} snapshot manifest has an unsupported schemaVersion"
            )
        raw_symbol = manifest.get("symbol")
        if not isinstance(raw_symbol, str):
            raise SnapshotIntegrityError("Choice snapshot symbol is invalid")
        try:
            manifest_instrument = self._instrument_normalizer(raw_symbol)
        except (DomainValidationError, MarketDataSchemaError) as exc:
            raise SnapshotIntegrityError("Choice snapshot symbol is invalid") from exc
        if instruments != (manifest_instrument,):
            raise SnapshotScopeError("requested instrument is outside Choice snapshot coverage")
        requested_start, requested_end = _choice_manifest_requested_range(manifest)
        if period.start < requested_start or period.end > requested_end:
            raise SnapshotScopeError("requested period is outside Choice snapshot coverage")
        capabilities = manifest.get("capabilities")
        typed_capabilities = (
            cast(Mapping[object, object], capabilities)
            if isinstance(capabilities, Mapping)
            else None
        )
        if (
            typed_capabilities is None
            or typed_capabilities.get("corporateActions") != "validated_for_demo"
        ):
            raise SnapshotIntegrityError(
                "Choice snapshot does not contain validated corporate-action coverage"
            )
        coverage = manifest.get("corporateActionCoverage")
        typed_coverage = (
            cast(Mapping[object, object], coverage) if isinstance(coverage, Mapping) else None
        )
        if typed_coverage is None:
            raise SnapshotIntegrityError("Choice snapshot corporate-action coverage is incomplete")

        snapshot_id = manifest.get("snapshotId")
        body = dict(manifest)
        body.pop("snapshotId", None)
        canonical_body = json.dumps(
            body,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        digest = hashlib.sha256(canonical_body).hexdigest()
        if snapshot_id != f"{expected_prefix}:{digest}":
            raise SnapshotIntegrityError(
                f"{snapshot_label} snapshotId does not match the canonical manifest body"
            )
        if self._data_root.name != digest:
            raise SnapshotIntegrityError(
                "Choice snapshot directory does not match the manifest digest"
            )

        raw_files = manifest.get("files")
        if not isinstance(raw_files, Mapping):
            raise SnapshotIntegrityError("Choice snapshot manifest files must be an object")
        files = cast(Mapping[object, object], raw_files)
        names = {name for name in files if isinstance(name, str)}
        missing = sorted(_CHOICE_REQUIRED_FILES - names)
        if missing:
            raise SnapshotIntegrityError(
                "Choice snapshot manifest is missing required files: " + ", ".join(missing)
            )

        known_datasets = {
            _DAILY_FILENAME: _DAILY_DATASET,
            _SIGNAL_DAILY_FILENAME: _SIGNAL_DAILY_DATASET,
            _CORPORATE_ACTION_FILENAME: _CORPORATE_ACTION_DATASET,
        }
        fingerprints: list[_FileFingerprint] = [manifest_fingerprint]
        for raw_relative, raw_metadata in sorted(files.items(), key=lambda item: str(item[0])):
            if not isinstance(raw_relative, str) or not raw_relative:
                raise SnapshotIntegrityError(
                    "Choice snapshot manifest file names must be non-empty strings"
                )
            relative = PurePosixPath(raw_relative)
            if (
                relative.is_absolute()
                or relative.as_posix() != raw_relative
                or any(part in {"", ".", ".."} for part in relative.parts)
            ):
                raise SnapshotIntegrityError(
                    f"Choice snapshot manifest contains an unsafe file path: {raw_relative}"
                )
            if raw_relative == _PRODUCER_MANIFEST_FILENAME:
                raise SnapshotIntegrityError(
                    "Choice snapshot manifest cannot include its own file hash"
                )
            if not isinstance(raw_metadata, Mapping):
                raise SnapshotIntegrityError(
                    f"Choice snapshot metadata must be an object: {raw_relative}"
                )
            metadata = cast(Mapping[object, object], raw_metadata)
            expected_size = metadata.get("bytes")
            expected_hash = metadata.get("sha256")
            if (
                not isinstance(expected_size, int)
                or isinstance(expected_size, bool)
                or expected_size < 0
            ):
                raise SnapshotIntegrityError(
                    f"Choice snapshot file size is invalid: {raw_relative}"
                )
            if not isinstance(expected_hash, str) or _SHA256.fullmatch(expected_hash) is None:
                raise SnapshotIntegrityError(
                    f"Choice snapshot file sha256 is invalid: {raw_relative}"
                )

            path = self._data_root.joinpath(*relative.parts).resolve()
            if not path.is_relative_to(self._data_root) or not path.is_file():
                raise SnapshotIntegrityError(
                    f"Choice snapshot manifest file does not exist: {raw_relative}"
                )
            actual_size = path.stat().st_size
            actual_hash = _sha256_file(path)
            if actual_size != expected_size or actual_hash != expected_hash:
                raise SnapshotIntegrityError(f"Choice snapshot file hash mismatch: {raw_relative}")
            fingerprints.append(
                _FileFingerprint(
                    dataset=known_datasets.get(
                        raw_relative,
                        f"choice_manifest_file:{raw_relative}",
                    ),
                    path=path,
                    size=actual_size,
                    sha256=actual_hash,
                )
            )

        _validate_persisted_corporate_action_coverage(
            manifest=manifest,
            coverage=typed_coverage,
            start=requested_start,
            end=requested_end,
            path=self._data_root / _CORPORATE_ACTION_FILENAME,
            instrument=manifest_instrument,
        )

        try:
            manifest_unchanged = manifest_path.read_bytes() == manifest_bytes
        except OSError as exc:
            raise SnapshotIntegrityError(
                "Choice snapshot manifest changed while it was being pinned"
            ) from exc
        if not manifest_unchanged:
            raise SnapshotIntegrityError(
                "Choice snapshot manifest changed while it was being pinned"
            )
        return tuple(fingerprints)

    def _fingerprint(self, dataset: str) -> _FileFingerprint:
        filenames = {
            _DAILY_DATASET: _DAILY_FILENAME,
            _SIGNAL_DAILY_DATASET: _SIGNAL_DAILY_FILENAME,
            _MINUTE_DATASET: _MINUTE_FILENAME,
            _SIGNAL_MINUTE_DATASET: _SIGNAL_MINUTE_FILENAME,
            _PRODUCER_MANIFEST_DATASET: _PRODUCER_MANIFEST_FILENAME,
            _EVENT_DATASET: _EVENT_FILENAME,
            _CORPORATE_ACTION_DATASET: _CORPORATE_ACTION_FILENAME,
        }
        filename = filenames.get(dataset)
        if filename is None:
            raise MarketDataCapabilityError(f"unsupported dataset: {dataset}")
        path = self._data_root / filename
        if not path.is_file():
            raise MarketDataCapabilityError(f"required dataset file does not exist: {path}")
        digest = _sha256_file(path)
        return _FileFingerprint(
            dataset=dataset,
            path=path,
            size=path.stat().st_size,
            sha256=digest,
        )

    def _verify_files_unchanged(self, pinned: _PinnedSnapshot) -> None:
        for expected in pinned.files:
            try:
                current_size = expected.path.stat().st_size
            except FileNotFoundError as exc:
                raise SnapshotIntegrityError(
                    f"pinned dataset file was removed: {expected.path}"
                ) from exc
            if current_size != expected.size:
                raise SnapshotIntegrityError(f"pinned dataset file size changed: {expected.path}")
            current_digest = _sha256_file(expected.path)
            if current_digest != expected.sha256:
                raise SnapshotIntegrityError(
                    f"pinned dataset file content changed: {expected.path}"
                )

    def _row_to_daily_bar(
        self,
        row: tuple[object, ...],
        expected_instrument: InstrumentId,
        period: DateRange,
        *,
        price_basis: PriceBasis,
    ) -> DailyBar:
        if len(row) != 8:
            raise MarketDataSchemaError("daily OHLCV projection returned 8-column mismatch")
        (
            raw_code,
            raw_date,
            raw_open,
            raw_high,
            raw_low,
            raw_close,
            raw_volume,
            raw_amount,
        ) = row
        row_instrument = self._instrument_normalizer(_required_text(raw_code, "stock_code"))
        if row_instrument != expected_instrument:
            raise MarketDataSchemaError("daily OHLCV query returned a different instrument")
        session_date = _coerce_date(raw_date)
        if not period.start <= session_date <= period.end:
            raise MarketDataSchemaError(
                "daily OHLCV query returned a row outside the requested range"
            )
        return DailyBar(
            instrument_id=row_instrument,
            session_date=session_date,
            open=Price(_decimal(raw_open, "open")),
            high=Price(_decimal(raw_high, "high")),
            low=Price(_decimal(raw_low, "low")),
            close=Price(_decimal(raw_close, "close")),
            volume=Quantity(_whole_number(raw_volume, "volume")),
            turnover=_decimal(raw_amount, "amount"),
            available_at=datetime.combine(
                session_date,
                time(hour=15),
                tzinfo=_SHANGHAI,
            ),
            price_basis=price_basis,
        )

    def _row_to_minute_bar(
        self,
        row: tuple[object, ...],
        expected_instrument: InstrumentId,
        period: DateRange,
    ) -> MinuteBar:
        if len(row) != 12:
            raise MarketDataSchemaError("minute OHLCV projection returned a 12-column mismatch")
        (
            raw_code,
            raw_start,
            raw_end,
            raw_available,
            raw_open,
            raw_high,
            raw_low,
            raw_close,
            raw_volume,
            raw_amount,
            raw_price_basis,
            raw_interval,
        ) = row
        row_instrument = self._instrument_normalizer(_required_text(raw_code, "stock_code"))
        if row_instrument != expected_instrument:
            raise MarketDataSchemaError("minute OHLCV query returned a different instrument")
        bar_start = _coerce_epoch_us(raw_start, "bar_start_at")
        if not period.start <= bar_start.date() <= period.end:
            raise MarketDataSchemaError("minute OHLCV query returned a row outside the range")
        if _required_text(raw_price_basis, "price_basis") != PriceBasis.UNADJUSTED.value:
            raise MarketDataSchemaError("minute execution bars must be unadjusted")
        if _required_text(raw_interval, "interval") != "1m":
            raise MarketDataSchemaError("minute execution bars must use the 1m interval")
        try:
            return MinuteBar(
                instrument_id=row_instrument,
                bar_start_at=bar_start,
                bar_end_at=_coerce_epoch_us(raw_end, "bar_end_at"),
                available_at=_coerce_epoch_us(raw_available, "available_at"),
                open=Price(_decimal(raw_open, "open")),
                high=Price(_decimal(raw_high, "high")),
                low=Price(_decimal(raw_low, "low")),
                close=Price(_decimal(raw_close, "close")),
                volume=Quantity(_whole_number(raw_volume, "volume")),
                turnover=_decimal(raw_amount, "amount"),
                price_basis=PriceBasis.UNADJUSTED,
            )
        except DomainValidationError as exc:
            raise MarketDataSchemaError(f"invalid minute execution row: {exc}") from exc

    def _row_to_minute_close(
        self,
        row: tuple[object, ...],
        expected_instrument: InstrumentId,
        period: DateRange,
    ) -> MinuteClose:
        if len(row) != 7:
            raise MarketDataSchemaError("minute signal projection returned a 7-column mismatch")
        (
            raw_code,
            raw_start,
            raw_end,
            raw_available,
            raw_close,
            raw_price_basis,
            raw_interval,
        ) = row
        row_instrument = self._instrument_normalizer(_required_text(raw_code, "stock_code"))
        if row_instrument != expected_instrument:
            raise MarketDataSchemaError("minute signal query returned a different instrument")
        bar_start = _coerce_epoch_us(raw_start, "bar_start_at")
        if not period.start <= bar_start.date() <= period.end:
            raise MarketDataSchemaError("minute signal query returned a row outside the range")
        if _required_text(raw_price_basis, "price_basis") != PriceBasis.BACK_ADJUSTED.value:
            raise MarketDataSchemaError("minute signal closes must be back-adjusted")
        if _required_text(raw_interval, "interval") != "1m":
            raise MarketDataSchemaError("minute signal closes must use the 1m interval")
        try:
            return MinuteClose(
                instrument_id=row_instrument,
                bar_start_at=bar_start,
                bar_end_at=_coerce_epoch_us(raw_end, "bar_end_at"),
                available_at=_coerce_epoch_us(raw_available, "available_at"),
                close=Price(_decimal(raw_close, "close")),
                price_basis=PriceBasis.BACK_ADJUSTED,
            )
        except DomainValidationError as exc:
            raise MarketDataSchemaError(f"invalid minute signal row: {exc}") from exc

    def _row_to_event(
        self,
        row: tuple[object, ...],
        expected_instrument: InstrumentId,
    ) -> EventEnvelope:
        if len(row) == 10:
            (
                raw_code,
                raw_event_id,
                raw_event_code,
                raw_occurred_at,
                raw_source_released_at,
                raw_vendor_available_at,
                raw_ingested_at,
                raw_revision_no,
                raw_time_quality,
                raw_attributes,
            ) = row
            raw_source_event_id = None
            raw_replay_available_at = None
            raw_provider = None
            raw_source_url = None
            raw_response_sha256 = None
            raw_validation_status = None
        elif len(row) == 16:
            (
                raw_code,
                raw_event_id,
                raw_source_event_id,
                raw_event_code,
                raw_occurred_at,
                raw_source_released_at,
                raw_vendor_available_at,
                raw_ingested_at,
                raw_replay_available_at,
                raw_revision_no,
                raw_provider,
                raw_source_url,
                raw_response_sha256,
                raw_time_quality,
                raw_validation_status,
                raw_attributes,
            ) = row
        else:
            raise MarketDataSchemaError("event projection returned an unsupported column count")
        row_instrument = self._instrument_normalizer(_required_text(raw_code, "stock_code"))
        if row_instrument != expected_instrument:
            raise MarketDataSchemaError("event query returned a different instrument")
        try:
            time_quality = TimeQuality(_required_text(raw_time_quality, "time_quality"))
        except ValueError as exc:
            raise MarketDataSchemaError(
                f"invalid event time_quality: {raw_time_quality!r}"
            ) from exc
        revision_no = _whole_number(raw_revision_no, "revision_no")
        occurred_at = _event_datetime(
            raw_occurred_at,
            "occurred_at",
            time_quality=time_quality,
            required=False,
        )
        source_released_at = _event_datetime(
            raw_source_released_at,
            "source_released_at",
            time_quality=time_quality,
            required=False,
        )
        vendor_first_available_at = _event_datetime(
            raw_vendor_available_at,
            "vendor_first_available_at",
            time_quality=time_quality,
            required=False,
        )
        ingested_at = _event_datetime(
            raw_ingested_at,
            "ingested_at",
            time_quality=time_quality,
            required=True,
        )
        replay_available_at = _event_datetime(
            raw_replay_available_at,
            "replay_available_at",
            time_quality=time_quality,
            required=False,
        )
        assert ingested_at is not None
        source_event_id = _optional_text(raw_source_event_id, "source_event_id")
        provider = _optional_text(raw_provider, "provider")
        source_url = _optional_text(raw_source_url, "source_url")
        raw_hash = _optional_text(raw_response_sha256, "raw_response_sha256")
        validation_status = _optional_text(raw_validation_status, "validation_status")
        return EventEnvelope(
            event=MarketEvent(
                event_id=StrongId(_required_text(raw_event_id, "event_id")),
                event_code=_required_text(raw_event_code, "event_code"),
                instrument_id=row_instrument,
                attributes=_event_attributes(raw_attributes),
            ),
            occurred_at=occurred_at,
            source_released_at=source_released_at,
            vendor_first_available_at=vendor_first_available_at,
            ingested_at=ingested_at,
            revision_no=revision_no,
            time_quality=time_quality,
            replay_available_at=replay_available_at,
            source_event_id=source_event_id,
            provider=provider,
            source_url=source_url,
            raw_response_sha256=raw_hash,
            validation_status=validation_status,
        )

    def _row_to_corporate_action(
        self,
        row: tuple[object, ...],
        expected_instrument: InstrumentId,
    ) -> CorporateAction:
        if len(row) != 26:
            raise MarketDataSchemaError("corporate-action projection returned a 26-column mismatch")
        (
            raw_code,
            raw_action_id,
            raw_source_action_id,
            raw_action_type,
            raw_record_date,
            raw_ex_date,
            raw_source_released_at,
            raw_vendor_available_at,
            raw_ingested_at,
            raw_replay_available_at,
            raw_revision_no,
            raw_time_quality,
            raw_provider,
            raw_source_url,
            raw_response_sha256,
            raw_validation_status,
            raw_currency,
            raw_cash_per_share,
            raw_cash_pay_date,
            raw_share_multiplier,
            raw_share_credit_date,
            raw_share_sellable_date,
            raw_rights_ratio,
            raw_rights_price,
            raw_rights_deadline,
            raw_rights_listing,
        ) = row
        row_instrument = self._instrument_normalizer(_required_text(raw_code, "stock_code"))
        if row_instrument != expected_instrument:
            raise MarketDataSchemaError("corporate-action query returned a different instrument")
        try:
            action_type = CorporateActionKind(_required_text(raw_action_type, "action_type"))
        except ValueError as exc:
            raise MarketDataSchemaError(
                f"invalid corporate-action type: {raw_action_type!r}"
            ) from exc
        try:
            time_quality = TimeQuality(_required_text(raw_time_quality, "time_quality"))
        except ValueError as exc:
            raise MarketDataSchemaError(
                f"invalid corporate-action time_quality: {raw_time_quality!r}"
            ) from exc
        source_released_at = _event_datetime(
            raw_source_released_at,
            "source_released_at",
            time_quality=time_quality,
            required=False,
        )
        vendor_first_available_at = _event_datetime(
            raw_vendor_available_at,
            "vendor_first_available_at",
            time_quality=time_quality,
            required=False,
        )
        ingested_at = _event_datetime(
            raw_ingested_at,
            "ingested_at",
            time_quality=time_quality,
            required=True,
        )
        replay_available_at = _event_datetime(
            raw_replay_available_at,
            "replay_available_at",
            time_quality=time_quality,
            required=True,
        )
        assert ingested_at is not None and replay_available_at is not None
        try:
            return CorporateAction(
                action_id=StrongId(_required_text(raw_action_id, "action_id")),
                source_action_id=_required_text(
                    raw_source_action_id,
                    "source_action_id",
                ),
                instrument_id=row_instrument,
                action_type=action_type,
                record_date=_coerce_date(raw_record_date),
                ex_date=_coerce_date(raw_ex_date),
                source_released_at=source_released_at,
                vendor_first_available_at=vendor_first_available_at,
                ingested_at=ingested_at,
                replay_available_at=replay_available_at,
                revision_no=_whole_number(raw_revision_no, "revision_no"),
                time_quality=time_quality,
                provider=_required_text(raw_provider, "provider"),
                source_url=_required_text(raw_source_url, "source_url"),
                raw_response_sha256=_required_text(
                    raw_response_sha256,
                    "raw_response_sha256",
                ),
                validation_status=_required_text(
                    raw_validation_status,
                    "validation_status",
                ),
                currency=_required_text(raw_currency, "currency"),
                gross_cash_per_share=_optional_decimal(
                    raw_cash_per_share,
                    "gross_cash_per_share",
                ),
                cash_pay_date=_optional_date(raw_cash_pay_date, "cash_pay_date"),
                share_multiplier=_optional_decimal(
                    raw_share_multiplier,
                    "share_multiplier",
                ),
                share_credit_date=_optional_date(
                    raw_share_credit_date,
                    "share_credit_date",
                ),
                share_sellable_date=_optional_date(
                    raw_share_sellable_date,
                    "share_sellable_date",
                ),
                rights_ratio=_optional_decimal(raw_rights_ratio, "rights_ratio"),
                rights_subscription_price=_optional_decimal(
                    raw_rights_price,
                    "rights_subscription_price",
                ),
                rights_payment_deadline=_optional_date(
                    raw_rights_deadline,
                    "rights_payment_deadline",
                ),
                rights_listing_date=_optional_date(
                    raw_rights_listing,
                    "rights_listing_date",
                ),
            )
        except DomainValidationError as exc:
            raise MarketDataSchemaError(f"invalid corporate-action row: {exc}") from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(_HASH_CHUNK_BYTES):
                digest.update(chunk)
    except OSError as exc:
        raise SnapshotIntegrityError(f"cannot hash dataset file {path}: {exc}") from exc
    return digest.hexdigest()


def _provider_code(instrument_id: InstrumentId) -> str:
    return str(instrument_id).split(".", maxsplit=1)[0]


def _exclusive_day_after(value: date) -> date:
    if value == date.max:
        raise SnapshotScopeError("date.max cannot be represented as an exclusive range end")
    return value + timedelta(days=1)


def _coerce_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError as exc:
            raise MarketDataSchemaError(f"invalid daily bar date: {value!r}") from exc
    raise MarketDataSchemaError(f"invalid daily bar date type: {type(value).__name__}")


def _datetime_epoch_us(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise MarketDataSchemaError("epoch conversion requires an aware datetime")
    delta = value.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def _coerce_epoch_us(value: object, field_name: str) -> datetime:
    if not isinstance(value, int) or isinstance(value, bool):
        raise MarketDataSchemaError(f"{field_name} must be an integer epoch microsecond")
    parsed = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=value)
    return parsed.astimezone(_SHANGHAI)


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise MarketDataSchemaError(f"{field_name} must be a non-empty string")
    return value


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name)


def _decimal(value: object, field_name: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise MarketDataSchemaError(f"{field_name} must be a finite number")
    try:
        converted = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise MarketDataSchemaError(f"{field_name} must be a finite number") from exc
    if not converted.is_finite():
        raise MarketDataSchemaError(f"{field_name} must be a finite number")
    return converted


def _optional_decimal(value: object, field_name: str) -> Decimal | None:
    if value is None:
        return None
    return _decimal(value, field_name)


def _optional_date(value: object, field_name: str) -> date | None:
    if value is None:
        return None
    try:
        return _coerce_date(value)
    except MarketDataSchemaError as exc:
        raise MarketDataSchemaError(f"invalid {field_name}: {value!r}") from exc


def _whole_number(value: object, field_name: str) -> int:
    converted = _decimal(value, field_name)
    integral = converted.to_integral_value()
    if converted != integral or integral < 0:
        raise MarketDataSchemaError(f"{field_name} must be a non-negative whole number")
    return int(integral)


def _event_datetime(
    value: object,
    field_name: str,
    *,
    time_quality: TimeQuality,
    required: bool,
) -> datetime | None:
    if value is None:
        if required:
            raise MarketDataSchemaError(f"{field_name} is required")
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise MarketDataSchemaError(f"{field_name} must include an explicit timezone")
        return value.astimezone(_SHANGHAI)
    if isinstance(value, date):
        if time_quality is not TimeQuality.DATE_ONLY_CONSERVATIVE:
            raise MarketDataSchemaError(
                f"{field_name} DATE requires date_only_conservative time_quality"
            )
        return datetime.combine(value, time(hour=15), tzinfo=_SHANGHAI)
    if isinstance(value, str):
        stripped = value.strip()
        if _DATE_ONLY.fullmatch(stripped):
            if time_quality is not TimeQuality.DATE_ONLY_CONSERVATIVE:
                raise MarketDataSchemaError(
                    f"{field_name} date-only text requires date_only_conservative time_quality"
                )
            return datetime.combine(date.fromisoformat(stripped), time(hour=15), tzinfo=_SHANGHAI)
        try:
            parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
        except ValueError as exc:
            raise MarketDataSchemaError(f"{field_name} is not an ISO datetime") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise MarketDataSchemaError(f"{field_name} must include an explicit timezone")
        return parsed.astimezone(_SHANGHAI)
    raise MarketDataSchemaError(f"{field_name} has unsupported type {type(value).__name__}")


def _event_attributes(value: object) -> Mapping[str, str | int | Decimal | bool | None]:
    if isinstance(value, str):
        try:
            decoded_raw: object = json.loads(value)
        except json.JSONDecodeError as exc:
            raise MarketDataSchemaError("attributes_json must contain valid JSON") from exc
    elif isinstance(value, Mapping):
        decoded_raw = dict(cast(Mapping[object, object], value))
    else:
        raise MarketDataSchemaError("attributes_json must be a JSON object")
    if not isinstance(decoded_raw, dict):
        raise MarketDataSchemaError("attributes_json must decode to an object")
    decoded = cast(dict[object, object], decoded_raw)

    attributes: dict[str, str | int | Decimal | bool | None] = {}
    for raw_name, item in decoded.items():
        if not isinstance(raw_name, str) or not raw_name:
            raise MarketDataSchemaError("event attribute names must be non-empty strings")
        if item is None or isinstance(item, str | bool | int):
            attributes[raw_name] = item
        elif isinstance(item, float):
            converted = Decimal(str(item))
            if not converted.is_finite():
                raise MarketDataSchemaError("event numeric attributes must be finite")
            attributes[raw_name] = converted
        else:
            raise MarketDataSchemaError("event attributes must be scalar JSON values")

    # Normalize aliases used by already published event v2 snapshots at the
    # immutable read boundary instead of rewriting content-addressed files.
    provider = attributes.get("provider")
    if "source" not in attributes and isinstance(provider, str) and provider:
        attributes["source"] = provider
    report_type = attributes.get("report_type")
    if report_type == "earnings_forecast":
        if "forecast_type" not in attributes:
            attributes["forecast_type"] = "earnings_forecast"
        legacy_direction = attributes.get("forecast_direction")
        if "direction" not in attributes and isinstance(legacy_direction, str):
            attributes["direction"] = legacy_direction
    return attributes


def _event_overlaps_period(envelope: EventEnvelope, period: DateRange) -> bool:
    candidate = envelope.available_at
    if candidate is None:
        known_times = tuple(
            item
            for item in (
                envelope.occurred_at,
                envelope.source_released_at,
                envelope.vendor_first_available_at,
                envelope.ingested_at,
            )
            if item is not None
        )
        candidate = max(known_times)
    local_date = candidate.astimezone(_SHANGHAI).date()
    return period.start <= local_date <= period.end


def _select_corporate_action_revisions(
    actions: Sequence[CorporateAction],
) -> tuple[CorporateAction, ...]:
    grouped: dict[str, list[CorporateAction]] = {}
    for action in actions:
        grouped.setdefault(action.action_id.value, []).append(action)

    selected: list[CorporateAction] = []
    for action_id, revisions in grouped.items():
        identities = {
            (item.instrument_id, item.action_type, item.record_date, item.ex_date)
            for item in revisions
        }
        if len(identities) != 1:
            raise MarketDataSchemaError(
                f"corporate-action revisions disagree on identity: {action_id}"
            )
        record_close = datetime.combine(
            revisions[0].record_date,
            time(15),
            tzinfo=_SHANGHAI,
        )
        for item in revisions:
            available_at = item.available_at
            if available_at is None:
                raise MarketDataSchemaError(
                    f"corporate action is not point-in-time validated: {action_id}"
                )
            if available_at > record_close:
                raise MarketDataSchemaError(
                    f"corporate-action revision was unavailable by record-date close: {action_id}"
                )
        selected.append(
            max(
                revisions,
                key=lambda item: (
                    cast(datetime, item.available_at),
                    item.revision_no,
                    item.source_action_id,
                ),
            )
        )
    return tuple(
        sorted(
            selected,
            key=lambda item: (
                item.ex_date,
                _corporate_action_order(item.action_type),
                item.action_id.value,
            ),
        )
    )


def _corporate_action_order(action_type: CorporateActionKind) -> int:
    if action_type is CorporateActionKind.CASH_DIVIDEND:
        return 0
    if action_type is CorporateActionKind.RIGHTS_ISSUE:
        return 2
    return 1


def _choice_manifest_requested_range(manifest: Mapping[str, object]) -> tuple[date, date]:
    raw_range = manifest.get("requestedRange")
    if not isinstance(raw_range, list):
        raise SnapshotIntegrityError("Choice snapshot requestedRange must contain two dates")
    typed_range = cast(list[object], raw_range)
    if len(typed_range) != 2:
        raise SnapshotIntegrityError("Choice snapshot requestedRange must contain two dates")
    start = _iso_date_value(typed_range[0], "Choice snapshot requestedRange start")
    end = _iso_date_value(typed_range[1], "Choice snapshot requestedRange end")
    if start > end:
        raise SnapshotIntegrityError("Choice snapshot requestedRange is invalid")
    return start, end


def _coverage_iso_date(coverage: Mapping[object, object], field_name: str) -> date:
    return _iso_date_value(
        coverage.get(field_name),
        f"corporate-action coverage {field_name}",
    )


def _iso_date_value(value: object, field_name: str) -> date:
    if not isinstance(value, str):
        raise SnapshotIntegrityError(f"{field_name} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SnapshotIntegrityError(f"{field_name} must be an ISO date") from exc


def _validate_persisted_corporate_action_coverage(
    *,
    manifest: Mapping[str, object],
    coverage: Mapping[object, object],
    start: date,
    end: date,
    path: Path,
    instrument: InstrumentId,
) -> None:
    """Reconcile producer coverage with the immutable canonical action rows."""

    counts = {item.value: 0 for item in CorporateActionKind}
    try:
        with duckdb.connect(database=":memory:") as connection:
            described = connection.execute(
                "DESCRIBE SELECT * FROM read_parquet(?)",
                [str(path)],
            ).fetchall()
            columns = {str(row[0]) for row in described}
            required_columns = {
                "stock_code",
                "action_id",
                "source_action_id",
                "action_type",
                "record_date",
                "ex_date",
                "source_released_at",
                "vendor_first_available_at",
                "ingested_at",
                "replay_available_at",
                "revision_no",
                "time_quality",
                "provider",
                "source_url",
                "raw_response_sha256",
                "validation_status",
                "currency",
                "gross_cash_per_share",
                "cash_pay_date",
                "share_multiplier",
                "share_credit_date",
                "share_sellable_date",
                "rights_ratio",
                "rights_subscription_price",
                "rights_payment_deadline",
                "rights_listing_date",
            }
            if not required_columns <= columns:
                raise SnapshotIntegrityError(
                    "corporate-action Parquet is missing canonical columns"
                )
            rows = connection.execute(
                """
                SELECT stock_code, action_type, COUNT(*)
                FROM read_parquet(?)
                GROUP BY stock_code, action_type
                ORDER BY stock_code, action_type
                """,
                [str(path)],
            ).fetchall()
    except duckdb.Error as exc:
        raise SnapshotIntegrityError(
            "cannot reconcile corporate-action coverage with canonical rows"
        ) from exc
    expected_code = _provider_code(instrument)
    for raw_stock_code, raw_action_type, raw_count in rows:
        if _required_text(raw_stock_code, "stock_code") != expected_code:
            raise SnapshotIntegrityError(
                "corporate-action Parquet contains a row for another instrument"
            )
        try:
            action_type = CorporateActionKind(_required_text(raw_action_type, "action_type"))
        except ValueError as exc:
            raise SnapshotIntegrityError(
                f"corporate-action Parquet contains an unsupported category: {raw_action_type!r}"
            ) from exc
        if isinstance(raw_count, bool) or not isinstance(raw_count, int) or raw_count < 0:
            raise SnapshotIntegrityError("corporate-action Parquet returned an invalid row count")
        counts[action_type.value] = raw_count

    row_counts = manifest.get("rowCounts")
    if not isinstance(row_counts, Mapping):
        raise SnapshotIntegrityError("snapshot corporate-action row counts are missing")
    declared = cast(Mapping[object, object], row_counts).get("corporateActions")
    if declared != sum(counts.values()):
        raise SnapshotIntegrityError(
            "snapshot corporate-action row count does not match canonical rows"
        )

    from .choice_snapshot import (
        ChoiceSnapshotError,
        validate_corporate_action_coverage_evidence,
    )

    try:
        validate_corporate_action_coverage_evidence(
            cast(Mapping[str, object], coverage),
            start=start,
            end=end,
            action_type_counts=counts,
        )
    except ChoiceSnapshotError as exc:
        raise SnapshotIntegrityError(
            f"snapshot corporate-action coverage is incomplete: {exc}"
        ) from exc
