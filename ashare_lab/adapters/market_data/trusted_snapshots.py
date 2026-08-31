"""Server-owned, fail-closed loaders for Strategy v2 reference snapshots.

This module is deliberately a thin trust boundary over the existing immutable
market-data registry.  Callers select producer content IDs; paths are always
derived from server-configured roots.  The registry remains responsible for
the physical Parquet/manifest validation and this layer exposes the logical
security-master, calendar and price snapshot identities needed by v2 receipts.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast

from ashare_lab.domain.instruments import (
    InstrumentResolutionError,
    InstrumentResolver,
    SecurityMasterAssetType,
    SecurityMasterRecord,
    SecurityMasterSnapshot,
)
from ashare_lab.domain.shared import InstrumentId
from ashare_lab.ports.market_data import DataRequirements, DateRange
from ashare_lab.ports.trusted_snapshots import (
    TrustedSecurityMasterSnapshot,
    TrustedSnapshotCoverageError,
    TrustedSnapshotError,
    TrustedSnapshotExpiredError,
    TrustedSnapshotIntegrityError,
    TrustedSnapshotMetadata,
    TrustedTechnicalSnapshot,
    TrustedV2SnapshotContracts,
    build_trusted_v2_snapshot_contracts,
    validate_trusted_market_availability,
)

from .local_parquet import (
    MarketDataCapabilityError,
    MarketDataSchemaError,
    SnapshotIntegrityError,
)
from .snapshot_registry import (
    SnapshotRegistryIntegrityError,
    SnapshotRegistryMarketDataRepository,
    SnapshotRegistryNoMatchError,
)

SECURITY_MASTER_SNAPSHOT_SCHEMA_VERSION = "ashare-lab.security-master-snapshot.v1"
CALENDAR_SNAPSHOT_SCHEMA_VERSION = "ashare-lab.instrument-session-snapshot.v1"

_SECURITY_MASTER_ID = re.compile(r"^security_master:(?P<digest>[0-9a-f]{64})$")
_PRODUCER_ID = re.compile(r"^(?P<profile>composite|choice|technical):(?P<digest>[0-9a-f]{64})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SECURITY_MASTER_FILENAME = "security_master.json"
_MANIFEST_FILENAME = "snapshot_manifest.json"
_CHOICE_SOURCE_MANIFEST = "source/choice_snapshot_manifest.json"
_TECHNICAL_SOURCE_MANIFEST = "source/technical_snapshot_manifest.json"


@dataclass(frozen=True, slots=True)
class TrustedSecurityMasterSnapshotBuild:
    metadata: TrustedSnapshotMetadata
    path: Path


class SecurityMasterInstrumentNormalizer:
    """Normalize only exact STOCK/ETF identities proven by one loaded master."""

    def __init__(self, trusted_master: TrustedSecurityMasterSnapshot) -> None:
        by_symbol: dict[str, InstrumentId] = {}
        by_code: dict[str, list[InstrumentId]] = {}
        for record in trusted_master.snapshot.records:
            if record.asset_type not in {
                SecurityMasterAssetType.STOCK,
                SecurityMasterAssetType.ETF,
            }:
                continue
            instrument_id = InstrumentId(record.symbol)
            by_symbol[record.symbol] = instrument_id
            by_code.setdefault(record.symbol.split(".", maxsplit=1)[0], []).append(instrument_id)
        self._by_symbol = by_symbol
        self._by_code = {key: tuple(value) for key, value in by_code.items()}

    def __call__(self, value: InstrumentId | str) -> InstrumentId:
        raw = str(value).strip().upper()
        instrument = self._by_symbol.get(raw)
        if instrument is not None:
            return instrument
        if len(raw) == 6 and raw.isdigit():
            candidates = self._by_code.get(raw, ())
            if len(candidates) == 1:
                return candidates[0]
        raise MarketDataSchemaError(
            "instrument is not an exact executable STOCK/ETF in the trusted security master"
        )


class TrustedSecurityMasterSnapshotLoader:
    """Load one content-addressed master from a server-configured root."""

    def __init__(
        self,
        trusted_root: str | Path,
        *,
        max_age: timedelta = timedelta(days=7),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._root = Path(trusted_root).expanduser().resolve()
        self._max_age = _validated_max_age(max_age)
        self._clock = clock

    def load(self, snapshot_id: object) -> TrustedSecurityMasterSnapshot:
        if not isinstance(snapshot_id, str):
            raise TrustedSnapshotIntegrityError(
                "security-master snapshot id must be security_master:<sha256>"
            )
        match = _SECURITY_MASTER_ID.fullmatch(snapshot_id)
        if match is None:
            raise TrustedSnapshotIntegrityError(
                "security-master snapshot id must be security_master:<sha256>"
            )
        root = _safe_content_child(self._root, match.group("digest"))
        manifest = _load_json(root / _MANIFEST_FILENAME, "security-master manifest")
        if manifest.get("schemaVersion") != SECURITY_MASTER_SNAPSHOT_SCHEMA_VERSION:
            raise TrustedSnapshotIntegrityError("security-master snapshot schema mismatch")
        _validate_content_identity(
            manifest,
            expected_id=snapshot_id,
            expected_prefix="security_master",
            expected_digest=match.group("digest"),
        )
        metadata = _security_master_metadata(manifest)
        _require_fresh(metadata, max_age=self._max_age, clock=self._clock)
        files = _manifest_files(manifest)
        if set(files) != {_SECURITY_MASTER_FILENAME}:
            raise TrustedSnapshotIntegrityError(
                "security-master snapshot must register exactly security_master.json"
            )
        payload_path = root / _SECURITY_MASTER_FILENAME
        _verify_file(payload_path, files[_SECURITY_MASTER_FILENAME])
        payload = _load_json(payload_path, "security-master payload")
        if (
            set(payload) != {"records", "schemaVersion"}
            or payload.get("schemaVersion") != "security-master.v2"
        ):
            raise TrustedSnapshotIntegrityError("security-master payload schema mismatch")
        raw_records = payload.get("records")
        if not isinstance(raw_records, list) or not raw_records:
            raise TrustedSnapshotIntegrityError("security-master records are missing")
        try:
            records = tuple(
                SecurityMasterRecord.model_validate(item)
                for item in cast(list[object], raw_records)
            )
            snapshot = SecurityMasterSnapshot(snapshot_id=snapshot_id, records=records)
        except Exception as exc:
            raise TrustedSnapshotIntegrityError(
                "security-master payload failed domain validation"
            ) from exc
        _verify_file(payload_path, files[_SECURITY_MASTER_FILENAME])
        return TrustedSecurityMasterSnapshot(metadata=metadata, snapshot=snapshot, path=root)


class TrustedTechnicalSnapshotLoader:
    """Pin and read one trusted daily producer without accepting a client path."""

    def __init__(
        self,
        *,
        composite_root: str | Path,
        choice_root: str | Path | None = None,
        technical_root: str | Path | None = None,
        max_age: timedelta = timedelta(days=30),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._composite_root = Path(composite_root).expanduser().resolve()
        self._roots = {
            "composite": self._composite_root,
            "choice": (
                Path(choice_root).expanduser().resolve() if choice_root is not None else None
            ),
            "technical": (
                Path(technical_root).expanduser().resolve() if technical_root is not None else None
            ),
        }
        self._max_age = _validated_max_age(max_age)
        self._clock = clock

    def load(
        self,
        *,
        producer_snapshot_id: object,
        instrument_id: InstrumentId,
        period: DateRange,
        security_master: TrustedSecurityMasterSnapshot,
    ) -> TrustedTechnicalSnapshot:
        if not isinstance(producer_snapshot_id, str):
            raise TrustedSnapshotIntegrityError(
                "producer snapshot id must be composite|choice|technical:<sha256>"
            )
        match = _PRODUCER_ID.fullmatch(producer_snapshot_id)
        if match is None:
            raise TrustedSnapshotIntegrityError(
                "producer snapshot id must be composite|choice|technical:<sha256>"
            )
        profile = match.group("profile")
        selected_root = self._roots[profile]
        if selected_root is None:
            raise TrustedSnapshotCoverageError(
                f"server has no trusted {profile} snapshot root configured"
            )
        producer_path = _safe_content_child(selected_root, match.group("digest"))
        try:
            resolver = InstrumentResolver(security_master.snapshot)
            resolver.resolve(str(instrument_id), as_of=period.start)
            resolver.resolve(str(instrument_id), as_of=period.end)
        except InstrumentResolutionError as exc:
            raise TrustedSnapshotCoverageError(str(exc)) from exc
        instrument_normalizer = SecurityMasterInstrumentNormalizer(security_master)

        registry = SnapshotRegistryMarketDataRepository(
            self._composite_root,
            choice_root=self._roots["choice"],
            technical_root=self._roots["technical"],
            instrument_normalizer=instrument_normalizer,
        )
        requirements = DataRequirements(
            instruments=(instrument_id,),
            datasets=("daily_ohlcv", "corporate_actions"),
        )
        try:
            snapshot_ref = registry.pin_selected_snapshot(
                producer_snapshot_id,
                requirements,
                period,
            )
        except SnapshotRegistryNoMatchError as exc:
            raise TrustedSnapshotCoverageError(str(exc)) from exc
        except (SnapshotRegistryIntegrityError, SnapshotIntegrityError) as exc:
            raise TrustedSnapshotIntegrityError(str(exc)) from exc
        except MarketDataCapabilityError as exc:
            raise TrustedSnapshotCoverageError(str(exc)) from exc

        manifest = _load_json(producer_path / _MANIFEST_FILENAME, "producer manifest")
        source_manifest = _technical_source_manifest(producer_path, manifest)
        market_metadata, calendar_metadata = _technical_metadata(
            producer_snapshot_id=producer_snapshot_id,
            manifest=manifest,
            source_manifest=source_manifest,
        )
        _require_fresh(market_metadata, max_age=self._max_age, clock=self._clock)
        _require_fresh(calendar_metadata, max_age=self._max_age, clock=self._clock)
        if (
            period.start < market_metadata.coverage.start
            or period.end > market_metadata.coverage.end
        ):
            raise TrustedSnapshotCoverageError(
                "trusted market-data snapshot does not cover the requested period"
            )
        if (
            period.start < calendar_metadata.coverage.start
            or period.end > calendar_metadata.coverage.end
        ):
            raise TrustedSnapshotCoverageError(
                "trusted trading-calendar snapshot does not cover the requested period"
            )

        try:
            execution_bars = tuple(registry.load_daily_bars(snapshot_ref, instrument_id, period))
            signal_bars = tuple(registry.load_signal_bars(snapshot_ref, instrument_id, period))
            sessions = tuple(registry.load_sessions(snapshot_ref, instrument_id, period))
            corporate_actions = tuple(
                registry.load_corporate_actions(snapshot_ref, instrument_id, period)
            )
        except SnapshotIntegrityError as exc:
            raise TrustedSnapshotIntegrityError(str(exc)) from exc
        except MarketDataCapabilityError as exc:
            raise TrustedSnapshotCoverageError(str(exc)) from exc
        if not execution_bars or not signal_bars or not sessions:
            raise TrustedSnapshotCoverageError(
                "trusted technical snapshot returned no bars or trading sessions"
            )
        execution_dates = tuple(item.session_date for item in execution_bars)
        signal_dates = tuple(item.session_date for item in signal_bars)
        session_dates = tuple(item.session_date for item in sessions)
        if execution_dates != signal_dates or execution_dates != session_dates:
            raise TrustedSnapshotCoverageError(
                "market, signal and trading-calendar dates are not exactly aligned"
            )
        validate_trusted_market_availability(
            execution_bars=execution_bars,
            signal_bars=signal_bars,
            corporate_actions=corporate_actions,
            market_metadata=market_metadata,
        )
        return TrustedTechnicalSnapshot(
            producer_snapshot_id=producer_snapshot_id,
            snapshot_ref=snapshot_ref,
            market_data_metadata=market_metadata,
            calendar_metadata=calendar_metadata,
            execution_bars=execution_bars,
            signal_bars=signal_bars,
            sessions=sessions,
            corporate_actions=corporate_actions,
        )


def build_trusted_security_master_snapshot(
    *,
    snapshot: SecurityMasterSnapshot,
    provider: str,
    coverage: DateRange,
    generated_at: datetime,
    output_root: str | Path,
) -> TrustedSecurityMasterSnapshotBuild:
    """Publish a content-addressed master after acquisition and validation.

    This builder accepts a domain-validated server object, not a client request.
    Runtime callers still receive snapshots only through the loader above.
    """

    _require_aware(generated_at, "generated_at")
    if not provider.strip():
        raise TrustedSnapshotIntegrityError("security-master provider is missing")
    destination = Path(output_root).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".security-master-", dir=destination))
    try:
        payload = {
            "schemaVersion": "security-master.v2",
            "records": [item.model_dump(mode="json") for item in snapshot.records],
        }
        payload_bytes = _canonical_json_bytes(payload)
        payload_path = temporary / _SECURITY_MASTER_FILENAME
        payload_path.write_bytes(payload_bytes)
        manifest_body: dict[str, object] = {
            "schemaVersion": SECURITY_MASTER_SNAPSHOT_SCHEMA_VERSION,
            "provider": provider.strip(),
            "generatedAt": generated_at.isoformat(),
            "coverage": {
                "start": coverage.start.isoformat(),
                "end": coverage.end.isoformat(),
            },
            "files": {
                _SECURITY_MASTER_FILENAME: {
                    "bytes": len(payload_bytes),
                    "sha256": hashlib.sha256(payload_bytes).hexdigest(),
                }
            },
        }
        digest = hashlib.sha256(_canonical_json_bytes(manifest_body)).hexdigest()
        snapshot_id = f"security_master:{digest}"
        manifest = {"snapshotId": snapshot_id, **manifest_body}
        (temporary / _MANIFEST_FILENAME).write_bytes(_canonical_json_bytes(manifest))
        final_path = destination / digest
        if final_path.exists():
            existing = _load_json(final_path / _MANIFEST_FILENAME, "security-master manifest")
            if existing != manifest:
                raise TrustedSnapshotIntegrityError(
                    "security-master destination contains different content"
                )
            shutil.rmtree(temporary)
        else:
            temporary.replace(final_path)
        metadata = TrustedSnapshotMetadata(
            snapshot_id=snapshot_id,
            provider=provider.strip(),
            schema_version=SECURITY_MASTER_SNAPSHOT_SCHEMA_VERSION,
            content_hash=f"sha256:{digest}",
            coverage=coverage,
            generated_at=generated_at,
        )
        return TrustedSecurityMasterSnapshotBuild(metadata=metadata, path=final_path)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def _security_master_metadata(manifest: Mapping[str, object]) -> TrustedSnapshotMetadata:
    coverage = _coverage(manifest.get("coverage"), "security-master")
    return TrustedSnapshotMetadata(
        snapshot_id=_text(manifest.get("snapshotId"), "security-master snapshot id"),
        provider=_text(manifest.get("provider"), "security-master provider"),
        schema_version=_text(manifest.get("schemaVersion"), "security-master schema"),
        content_hash="sha256:"
        + _text(manifest.get("snapshotId"), "security-master snapshot id").split(":", 1)[1],
        coverage=coverage,
        generated_at=_timestamp(manifest.get("generatedAt"), "security-master generatedAt"),
    )


def _technical_source_manifest(
    producer_path: Path,
    manifest: Mapping[str, object],
) -> Mapping[str, object]:
    schema = manifest.get("schemaVersion")
    if schema == "ashare-lab.composite-research-snapshot.v2":
        files = _manifest_files(manifest)
        if _CHOICE_SOURCE_MANIFEST in files:
            relative = _CHOICE_SOURCE_MANIFEST
        elif _TECHNICAL_SOURCE_MANIFEST in files:
            relative = _TECHNICAL_SOURCE_MANIFEST
        else:
            raise TrustedSnapshotIntegrityError(
                "composite snapshot has no allowlisted technical source manifest"
            )
        return _load_json(producer_path / relative, "technical source manifest")
    if schema in {
        "choice.daily-research-snapshot.v1",
        "ashare-lab.daily-research-snapshot.v1",
    }:
        return manifest
    raise TrustedSnapshotIntegrityError("producer snapshot schema mismatch")


def _technical_metadata(
    *,
    producer_snapshot_id: str,
    manifest: Mapping[str, object],
    source_manifest: Mapping[str, object],
) -> tuple[TrustedSnapshotMetadata, TrustedSnapshotMetadata]:
    requested = source_manifest.get("requestedRange")
    if not isinstance(requested, list):
        raise TrustedSnapshotIntegrityError("technical source requestedRange is invalid")
    typed_requested = cast(list[object], requested)
    if len(typed_requested) != 2 or any(not isinstance(item, str) for item in typed_requested):
        raise TrustedSnapshotIntegrityError("technical source requestedRange is invalid")
    try:
        market_coverage = DateRange(
            date.fromisoformat(cast(str, typed_requested[0])),
            date.fromisoformat(cast(str, typed_requested[1])),
        )
    except ValueError as exc:
        raise TrustedSnapshotIntegrityError("technical source requestedRange is invalid") from exc
    # A composite may be assembled long after its immutable technical source
    # was acquired.  Composition is useful producer audit metadata, but it is
    # not evidence that the underlying prices or sessions were refreshed.
    # Freshness therefore follows the acquisition clock frozen in the source
    # manifest, never the outer wrapper's ``composedAt``.
    source_acquired_at = _timestamp(
        source_manifest.get("capturedAt"),
        "technical source capturedAt",
    )
    provider = _text(source_manifest.get("provider"), "technical source provider")
    source_schema = _text(source_manifest.get("schemaVersion"), "technical source schema")
    files = _manifest_files(manifest)
    market_names = tuple(
        name
        for name in (
            "daily_ohlcv.parquet",
            "signal_daily_ohlcv.parquet",
            "corporate_actions.parquet",
        )
        if name in files
    )
    if len(market_names) != 3 or "instrument_sessions.parquet" not in files:
        raise TrustedSnapshotIntegrityError(
            "producer snapshot does not register complete daily market/calendar files"
        )
    market_digest = hashlib.sha256(
        _canonical_json_bytes({name: files[name] for name in market_names})
    ).hexdigest()
    session_fingerprint = files["instrument_sessions.parquet"]
    typed_session_fingerprint = (
        cast(Mapping[object, object], session_fingerprint)
        if isinstance(session_fingerprint, Mapping)
        else None
    )
    session_digest = _text(
        typed_session_fingerprint.get("sha256") if typed_session_fingerprint is not None else None,
        "instrument-session file hash",
    )
    if _SHA256.fullmatch(session_digest) is None:
        raise TrustedSnapshotIntegrityError("instrument-session file hash is invalid")
    session_reference = source_manifest.get("sessionReference")
    if not isinstance(session_reference, Mapping):
        raise TrustedSnapshotIntegrityError("technical source sessionReference is missing")
    typed_session_reference = cast(Mapping[object, object], session_reference)
    calendar_provider = _text(
        typed_session_reference.get("provider"),
        "trading-calendar provider",
    )
    raw_calendar_coverage = typed_session_reference.get("coverage")
    calendar_coverage = _coverage(raw_calendar_coverage, "trading-calendar")
    calendar_acquired_at = _calendar_acquired_at(
        typed_session_reference,
        fallback=source_acquired_at,
    )
    if calendar_acquired_at > source_acquired_at:
        raise TrustedSnapshotIntegrityError(
            "trading-calendar acquisition follows technical source capturedAt"
        )
    return (
        TrustedSnapshotMetadata(
            snapshot_id=f"market_data:{market_digest}",
            producer_snapshot_id=producer_snapshot_id,
            provider=provider,
            schema_version=source_schema,
            content_hash=f"sha256:{market_digest}",
            coverage=market_coverage,
            generated_at=source_acquired_at,
        ),
        TrustedSnapshotMetadata(
            snapshot_id=f"trading_calendar:{session_digest}",
            producer_snapshot_id=producer_snapshot_id,
            provider=calendar_provider,
            schema_version=CALENDAR_SNAPSHOT_SCHEMA_VERSION,
            content_hash=f"sha256:{session_digest}",
            coverage=calendar_coverage,
            generated_at=calendar_acquired_at,
        ),
    )


def _calendar_acquired_at(
    session_reference: Mapping[object, object],
    *,
    fallback: datetime,
) -> datetime:
    """Return immutable calendar acquisition evidence, or source capture.

    Current Choice snapshots freeze the BaoStock calendar query hashes inside
    ``sessionReference.coverage`` but do not yet carry an independent query
    clock, so their containing source ``capturedAt`` is the conservative
    acquisition bound.  Future schemas may add a direct captured/query time;
    when present it is authoritative and malformed values fail closed.
    """

    containers: list[tuple[Mapping[object, object], str]] = [
        (session_reference, "trading-calendar sessionReference")
    ]
    raw_coverage = session_reference.get("coverage")
    if isinstance(raw_coverage, Mapping):
        containers.append(
            (cast(Mapping[object, object], raw_coverage), "trading-calendar coverage")
        )
    for container, label in containers:
        for key in ("capturedAt", "queriedAt"):
            if key in container:
                return _timestamp(container.get(key), f"{label} {key}")
    return fallback


def _require_fresh(
    metadata: TrustedSnapshotMetadata,
    *,
    max_age: timedelta,
    clock: Callable[[], datetime],
) -> None:
    now = clock()
    _require_aware(now, "snapshot freshness clock")
    if metadata.generated_at > now:
        raise TrustedSnapshotIntegrityError("snapshot generated_at is in the future")
    if now - metadata.generated_at > max_age:
        raise TrustedSnapshotExpiredError(f"trusted snapshot expired: {metadata.snapshot_id}")


def _validated_max_age(value: timedelta) -> timedelta:
    if value <= timedelta(0):
        raise ValueError("max_age must be a positive timedelta")
    return value


def _safe_content_child(root: Path, digest: str) -> Path:
    if _SHA256.fullmatch(digest) is None:
        raise TrustedSnapshotIntegrityError("snapshot id digest is invalid")
    path = root / digest
    if path.is_symlink() or path.resolve().parent != root:
        raise TrustedSnapshotIntegrityError(
            "snapshot path is not a safe direct child of the trusted server root"
        )
    if not path.is_dir():
        raise TrustedSnapshotCoverageError("trusted snapshot is not published")
    return path.resolve()


def _validate_content_identity(
    manifest: Mapping[str, object],
    *,
    expected_id: str,
    expected_prefix: str,
    expected_digest: str,
) -> None:
    declared = manifest.get("snapshotId")
    body = dict(manifest)
    body.pop("snapshotId", None)
    digest = hashlib.sha256(_canonical_json_bytes(body)).hexdigest()
    if (
        declared != expected_id
        or expected_id != f"{expected_prefix}:{digest}"
        or digest != expected_digest
    ):
        raise TrustedSnapshotIntegrityError("snapshot id does not match canonical content")


def _manifest_files(manifest: Mapping[str, object]) -> Mapping[str, object]:
    files = manifest.get("files")
    if not isinstance(files, Mapping):
        raise TrustedSnapshotIntegrityError("snapshot files manifest is missing")
    return cast(Mapping[str, object], files)


def _verify_file(path: Path, metadata: object) -> None:
    if not isinstance(metadata, Mapping):
        raise TrustedSnapshotIntegrityError("snapshot file metadata is invalid")
    typed = cast(Mapping[object, object], metadata)
    expected_bytes = typed.get("bytes")
    expected_hash = typed.get("sha256")
    if (
        not path.is_file()
        or not isinstance(expected_bytes, int)
        or isinstance(expected_bytes, bool)
        or not isinstance(expected_hash, str)
        or _SHA256.fullmatch(expected_hash) is None
        or path.stat().st_size != expected_bytes
        or _sha256_file(path) != expected_hash
    ):
        raise TrustedSnapshotIntegrityError(f"snapshot file hash mismatch: {path.name}")


def _coverage(value: object, label: str) -> DateRange:
    if not isinstance(value, Mapping):
        raise TrustedSnapshotIntegrityError(f"{label} coverage is missing")
    typed = cast(Mapping[object, object], value)
    raw_start = typed.get("start")
    raw_end = typed.get("end")
    if not isinstance(raw_start, str) or not isinstance(raw_end, str):
        raise TrustedSnapshotIntegrityError(f"{label} coverage dates are invalid")
    try:
        return DateRange(date.fromisoformat(raw_start), date.fromisoformat(raw_end))
    except (ValueError, TypeError) as exc:
        raise TrustedSnapshotIntegrityError(f"{label} coverage dates are invalid") from exc


def _timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise TrustedSnapshotIntegrityError(f"{label} is missing")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise TrustedSnapshotIntegrityError(f"{label} is invalid") from exc
    _require_aware(parsed, label)
    return parsed


def _require_aware(value: object, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise TrustedSnapshotIntegrityError(f"{label} must include an explicit timezone")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TrustedSnapshotIntegrityError(f"{label} is missing")
    return value.strip()


def _load_json(path: Path, label: str) -> Mapping[str, object]:
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TrustedSnapshotIntegrityError(f"cannot read {label}") from exc
    if not isinstance(value, Mapping):
        raise TrustedSnapshotIntegrityError(f"{label} must be an object")
    return cast(Mapping[str, object], value)


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "CALENDAR_SNAPSHOT_SCHEMA_VERSION",
    "SECURITY_MASTER_SNAPSHOT_SCHEMA_VERSION",
    "SecurityMasterInstrumentNormalizer",
    "TrustedSecurityMasterSnapshot",
    "TrustedSecurityMasterSnapshotBuild",
    "TrustedSecurityMasterSnapshotLoader",
    "TrustedSnapshotCoverageError",
    "TrustedSnapshotError",
    "TrustedSnapshotExpiredError",
    "TrustedSnapshotIntegrityError",
    "TrustedSnapshotMetadata",
    "TrustedTechnicalSnapshot",
    "TrustedTechnicalSnapshotLoader",
    "TrustedV2SnapshotContracts",
    "build_trusted_security_master_snapshot",
    "build_trusted_v2_snapshot_contracts",
]
