"""Point-in-time-safe normalization for web-derived event evidence.

Search results are discovery aids only.  They are represented by ``SearchHit``
and intentionally have no conversion path to ``EventObservation``.  Evidence
may cross the domain boundary only after it has a stable external fact id,
content hashes, an exact security mapping, versioned extraction, and an
auditable timing basis.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from numbers import Integral, Real
from typing import cast
from urllib.parse import SplitResult, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from ashare_lab.domain.events.observations import EventAttribute, EventObservation
from ashare_lab.domain.market_data import TimeQuality
from ashare_lab.domain.shared import InstrumentId

SHANGHAI = ZoneInfo("Asia/Shanghai")

MAJOR_CONTRACT_WON = "event.contracts_orders.major_contract_won"
LICENSE_APPROVAL = "event.macro_policy_industry.license_approval"
SUPPORTED_WEB_EVENT_CODES = frozenset({MAJOR_CONTRACT_WON, LICENSE_APPROVAL})
SUPPORTED_EVIDENCE_PROVIDERS = frozenset({"eastmoney", "ifind", "rqdata", "tushare", "web_archive"})

TIMING_LICENSED_VENDOR = "licensed_vendor_first_available"
TIMING_PROSPECTIVE_ARCHIVE = "prospective_web_archive_capture"
TIMING_PAGE_METADATA = "page_metadata_only"
TIMING_SEARCH_INDEX = "search_index"

_SUPPORTED_TIMING_BASES = frozenset(
    {
        TIMING_LICENSED_VENDOR,
        TIMING_PROSPECTIVE_ARCHIVE,
        TIMING_PAGE_METADATA,
        TIMING_SEARCH_INDEX,
    }
)
_SUPPORTED_PUBLISHER_TYPES = frozenset(
    {
        "exchange",
        "government",
        "issuer",
        "licensed_vendor",
        "news_media",
        "public_procurement_platform",
        "regulator",
    }
)
_AUTHORITATIVE_PUBLISHERS = {
    MAJOR_CONTRACT_WON: frozenset(
        {"exchange", "government", "issuer", "public_procurement_platform", "regulator"}
    ),
    LICENSE_APPROVAL: frozenset({"exchange", "government", "regulator"}),
}
_SUPPORTED_REVISION_TYPES = frozenset({"initial", "amendment", "correction", "cancellation"})
_REVISION_TYPE_ALIASES = {
    "delete": "cancellation",
    "original": "initial",
    "update": "amendment",
}
_SUPPORTED_REVIEW_STATUSES = frozenset({"accepted", "pending", "rejected"})
_SUPPORTED_MATERIALITY_STATUSES = frozenset({"material", "not_material", "unassessed"})
_SUPPORTED_MATERIALITY_BASES = frozenset(
    {
        "exchange_materiality_review",
        "issuer_major_contract_disclosure",
        "regulatory_materiality_rule",
    }
)
_TIMING_BASIS_ALIASES = {
    "licensed_vendor": TIMING_LICENSED_VENDOR,
    "prospective_collector": TIMING_PROSPECTIVE_ARCHIVE,
}
_DATE_ONLY = re.compile(r"^\d{4}[-/]\d{2}[-/]\d{2}$")
_COMPACT_DATE = re.compile(r"^\d{8}$")
_SECONDS = re.compile(r"(?:^|[ T])\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?")
_SHA256 = re.compile(r"^(?:sha256:)?([0-9a-fA-F]{64})$")


class WebEvidenceError(ValueError):
    """Raised when evidence cannot be retained without inventing facts."""


class DiscoveryOnlyEvidenceError(WebEvidenceError):
    """Raised when a search result is passed to the evidence normalizer."""


@dataclass(frozen=True, slots=True)
class SearchHit:
    """A search-engine lead that can only schedule subsequent collection.

    ``indexed_at_label`` is deliberately text.  Search-index timestamps are not
    source release times and must never be promoted into a market clock.
    """

    search_provider: str
    query: str
    title: str
    snippet: str
    url: str
    retrieved_at: datetime
    raw_response_sha256: str
    indexed_at_label: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("search_provider", "query", "title", "snippet"):
            object.__setattr__(
                self,
                field_name,
                _non_empty_text(getattr(self, field_name), field_name),
            )
        object.__setattr__(self, "url", _canonical_url(self.url))
        object.__setattr__(
            self,
            "retrieved_at",
            _aware_retrieved_at(self.retrieved_at, "retrieved_at"),
        )
        object.__setattr__(
            self,
            "raw_response_sha256",
            _required_sha256(self.raw_response_sha256, "raw_response_sha256"),
        )
        if self.indexed_at_label is not None:
            object.__setattr__(
                self,
                "indexed_at_label",
                _non_empty_text(self.indexed_at_label, "indexed_at_label"),
            )


@dataclass(frozen=True, slots=True)
class _ParsedTimestamp:
    value: datetime
    precision: str

    @property
    def has_exact_seconds(self) -> bool:
        return self.precision == "second"


def normalize_web_evidence_row(
    row: Mapping[str, object] | SearchHit,
    *,
    retrieved_at: datetime | None = None,
) -> EventObservation:
    """Normalize one fully collected web fact, retaining quarantined evidence.

    Validation does not improve timestamps.  In particular, page metadata and
    search-index labels are never copied into ``vendor_first_available_at``.
    Only a licensed vendor's exact first-seen timestamp or an exact prospective
    archive capture can receive ``validation_status='validated'``.
    """

    if isinstance(row, SearchHit):
        raise DiscoveryOnlyEvidenceError(
            "SearchHit is discovery-only; collect and hash the canonical page before normalization"
        )

    provider = _required_choice(
        row,
        "provider",
        SUPPORTED_EVIDENCE_PROVIDERS,
    )
    event_code = _required_choice(row, "event_code", SUPPORTED_WEB_EVENT_CODES)
    external_fact_id = _required_text(row, "external_fact_id")
    source_event_id = _source_event_id(row, external_fact_id)
    title = _required_text(row, "title")
    canonical_url = _canonical_url(_required_text(row, "canonical_url"))
    raw_response_sha256 = _required_sha256(
        _required_text(row, "raw_response_sha256"),
        "raw_response_sha256",
    )
    content_sha256 = _required_sha256(
        _required_text(row, "content_sha256"),
        "content_sha256",
    )
    instrument_id = _exact_instrument_mapping(row)
    entity_match_method = _required_text(row, "entity_match_method")
    mapping_confidence = _exact_mapping_confidence(row)
    publisher_name = _required_text(row, "publisher_name")
    extractor_id = _required_text(row, "extractor_id")
    extractor_version = _required_text(row, "extractor_version")
    classifier_id = _required_text(row, "classifier_id")
    classifier_version = _required_text(row, "classifier_version")
    revision_type = _revision_type(row)
    revision_no = _required_revision_no(row, revision_type)
    revision_id = _required_text(row, "revision_id")
    publisher_type = _required_choice(
        row,
        "publisher_type",
        _SUPPORTED_PUBLISHER_TYPES,
    )
    timing_basis = _timing_basis(row)
    review_status = _required_choice(
        row,
        "review_status",
        _SUPPORTED_REVIEW_STATUSES,
    )
    captured_at = _row_retrieved_at(row, retrieved_at)
    event_attributes = _event_attributes(row, event_code, publisher_type=publisher_type)
    semantic_status = _semantic_validation_status(event_code, event_attributes)
    evidence_attributes = _evidence_attributes(row)

    occurred_at = _optional_timestamp_value(row.get("occurred_at"), "occurred_at")
    source_released = _optional_timestamp(
        _first_present(
            row,
            (
                "source_released_at",
                "source_published_at",
                "page_published_at",
                "published_at",
            ),
        ),
        "source_released_at",
    )
    timing = _safe_timing(
        row,
        provider=provider,
        timing_basis=timing_basis,
        source_released=source_released,
    )

    validation_status = timing.validation_status
    if semantic_status == "rejected" or review_status == "rejected":
        validation_status = "rejected"
    elif validation_status == "validated" and (
        review_status != "accepted" or publisher_type not in _AUTHORITATIVE_PUBLISHERS[event_code]
    ):
        validation_status = "unverified"

    attributes: dict[str, EventAttribute] = dict(event_attributes)
    attributes.update(evidence_attributes)
    attributes.update(
        {
            "canonical_url": canonical_url,
            "classifier_id": classifier_id,
            "classifier_version": classifier_version,
            "entity_mapping_key": instrument_id.value,
            "entity_mapping_status": "exact",
            "entity_match_method": entity_match_method,
            "external_fact_id": external_fact_id,
            "extractor_id": extractor_id,
            "extractor_version": extractor_version,
            "ingested_at": captured_at.isoformat(),
            "mapping_confidence": mapping_confidence,
            "provider": provider,
            "publisher_authority": (
                "authoritative"
                if publisher_type in _AUTHORITATIVE_PUBLISHERS[event_code]
                else "secondary"
            ),
            "publisher_name": publisher_name,
            "publisher_type": publisher_type,
            "review_status": review_status,
            "revision_id": revision_id,
            "revision_type": revision_type,
            "source_entity_id": _required_text(row, "source_entity_id"),
            "source_event_id": source_event_id,
            "source_url": canonical_url,
            "timestamp_precision": timing.timestamp_precision,
            "time_quality": timing.time_quality.value,
            "timing_basis": timing_basis,
            "validation_status": validation_status,
        }
    )
    if source_released is not None:
        attributes["source_timestamp_precision"] = source_released.precision
    for optional_name in ("capture_mode", "provider_license_status", "search_indexed_at"):
        optional_value = row.get(optional_name)
        if optional_value is not None:
            attributes[optional_name] = _scalar(optional_value, optional_name)

    return EventObservation(
        provider=provider,
        provider_event_id=source_event_id,
        instrument_id=instrument_id,
        event_code=event_code,
        title=title,
        occurred_at=occurred_at,
        source_released_at=source_released.value if source_released is not None else None,
        vendor_first_available_at=timing.vendor_first_available_at,
        retrieved_at=captured_at,
        time_quality=timing.time_quality,
        document_url=canonical_url,
        document_sha256=content_sha256,
        raw_response_sha256=raw_response_sha256,
        external_fact_id=external_fact_id,
        validation_status=validation_status,
        attributes=attributes,
        revision_no=revision_no,
    )


@dataclass(frozen=True, slots=True)
class _TimingDecision:
    vendor_first_available_at: datetime | None
    time_quality: TimeQuality
    validation_status: str
    timestamp_precision: str


def _safe_timing(
    row: Mapping[str, object],
    *,
    provider: str,
    timing_basis: str,
    source_released: _ParsedTimestamp | None,
) -> _TimingDecision:
    if timing_basis == TIMING_LICENSED_VENDOR:
        if provider == "web_archive":
            raise WebEvidenceError(
                "web_archive cannot claim licensed_vendor_first_available timing"
            )
        license_status = _required_text(row, "provider_license_status").lower()
        vendor_time = _required_timestamp(
            row,
            ("vendor_first_available_at", "vendor_available_at"),
            "vendor_first_available_at",
        )
        if not vendor_time.has_exact_seconds:
            return _TimingDecision(
                vendor_time.value,
                _quality_for_precision(vendor_time.precision, vendor_observed=True),
                "blocked_time_quality",
                vendor_time.precision,
            )
        return _TimingDecision(
            vendor_time.value,
            TimeQuality.VENDOR_OBSERVED,
            "validated" if license_status == "licensed" else "unverified",
            vendor_time.precision,
        )

    if timing_basis == TIMING_PROSPECTIVE_ARCHIVE:
        if provider != "web_archive":
            raise WebEvidenceError(
                "prospective_web_archive_capture requires provider='web_archive'"
            )
        capture_mode = _required_text(row, "capture_mode").lower()
        archive_time = _required_timestamp(
            row,
            ("collector_observed_at", "web_archive_captured_at"),
            "collector_observed_at",
        )
        if not archive_time.has_exact_seconds:
            return _TimingDecision(
                archive_time.value,
                _quality_for_precision(archive_time.precision, vendor_observed=True),
                "blocked_time_quality",
                archive_time.precision,
            )
        return _TimingDecision(
            archive_time.value,
            TimeQuality.VENDOR_OBSERVED,
            "validated" if capture_mode == "prospective" else "unverified",
            archive_time.precision,
        )

    if timing_basis == TIMING_PAGE_METADATA:
        if source_released is None:
            raise WebEvidenceError(
                "page_metadata_only requires source_released_at/page_published_at"
            )
        quality = _quality_for_precision(source_released.precision, vendor_observed=False)
        status = "unverified" if source_released.has_exact_seconds else "blocked_time_quality"
        return _TimingDecision(None, quality, status, source_released.precision)

    # Search-index time remains an audit attribute, never a market clock.
    if row.get("search_indexed_at") is None:
        raise WebEvidenceError("search_index timing requires search_indexed_at")
    return _TimingDecision(
        None,
        TimeQuality.ESTIMATED_RESEARCH_ONLY,
        "unverified",
        "untrusted_search_index",
    )


def _quality_for_precision(precision: str, *, vendor_observed: bool) -> TimeQuality:
    if precision in {"date", "placeholder"}:
        return TimeQuality.DATE_ONLY_CONSERVATIVE
    if precision == "minute" or vendor_observed:
        return TimeQuality.VENDOR_OBSERVED
    return TimeQuality.EXACT


def _event_attributes(
    row: Mapping[str, object],
    event_code: str,
    *,
    publisher_type: str,
) -> Mapping[str, EventAttribute]:
    raw = row.get("event_attributes", row.get("attributes"))
    if not isinstance(raw, Mapping):
        raise WebEvidenceError("event_attributes must be a mapping")
    raw_attributes = cast(Mapping[object, object], raw)
    attributes: dict[str, EventAttribute] = {}
    for raw_name, value in raw_attributes.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise WebEvidenceError("event attribute names must be non-empty strings")
        attributes[raw_name.strip()] = _scalar(value, f"event_attributes.{raw_name}")

    if event_code == MAJOR_CONTRACT_WON:
        _canonicalize_alias(attributes, "award_stage", "award_status")
        _canonicalize_alias(attributes, "counterparty", "awarding_entity")
        required_names = (
            "award_stage",
            "project_name",
            "counterparty",
            "materiality_status",
            "materiality_basis",
        )
        status_name = "award_stage"
    else:
        _canonicalize_alias(attributes, "approval_status", "license_status")
        _canonicalize_alias(attributes, "license_type", "license_name")
        _canonicalize_alias(attributes, "regulator", "approving_authority")
        required_names = ("approval_status", "license_type", "regulator")
        status_name = "approval_status"
    for name in required_names:
        value = attributes.get(name)
        if not isinstance(value, str) or not value.strip():
            raise WebEvidenceError(f"{event_code} requires non-empty event attribute {name!r}")
    status = cast(str, attributes[status_name]).strip().lower()
    attributes[status_name] = status
    if event_code == MAJOR_CONTRACT_WON:
        materiality_status = cast(str, attributes["materiality_status"]).strip().lower()
        materiality_basis = cast(str, attributes["materiality_basis"]).strip().lower()
        if materiality_status not in _SUPPORTED_MATERIALITY_STATUSES:
            raise WebEvidenceError(
                "event_attributes.materiality_status must be material, not_material, or unassessed"
            )
        if materiality_basis not in _SUPPORTED_MATERIALITY_BASES:
            allowed = ", ".join(sorted(_SUPPORTED_MATERIALITY_BASES))
            raise WebEvidenceError("event_attributes.materiality_basis must be one of: " + allowed)
        attributes["materiality_status"] = materiality_status
        attributes["materiality_basis"] = materiality_basis
    attributes.setdefault("source_kind", publisher_type)
    return attributes


def _semantic_validation_status(
    event_code: str,
    attributes: Mapping[str, EventAttribute],
) -> str:
    if event_code == MAJOR_CONTRACT_WON:
        return (
            "validated"
            if attributes["award_stage"] == "formal_winner"
            and attributes["materiality_status"] == "material"
            else "rejected"
        )
    return "validated" if attributes["approval_status"] == "approved" else "rejected"


def _canonicalize_alias(
    attributes: dict[str, EventAttribute],
    canonical_name: str,
    alias_name: str,
) -> None:
    canonical = attributes.get(canonical_name)
    alias = attributes.get(alias_name)
    if canonical is not None and alias is not None and canonical != alias:
        raise WebEvidenceError(f"event_attributes.{canonical_name} conflicts with {alias_name}")
    if canonical is None and alias is not None:
        attributes[canonical_name] = alias
    attributes.pop(alias_name, None)


def _evidence_attributes(row: Mapping[str, object]) -> Mapping[str, EventAttribute]:
    attributes: dict[str, EventAttribute] = {}
    evidence_span = row.get("evidence_span")
    evidence_sha256 = row.get("evidence_sha256")
    if evidence_span is None and evidence_sha256 is None:
        raise WebEvidenceError("evidence_span or evidence_sha256 is required")
    if evidence_span is not None:
        attributes["evidence_span"] = _non_empty_text(evidence_span, "evidence_span")
    if evidence_sha256 is not None:
        attributes["evidence_sha256"] = _required_sha256(
            _non_empty_text(evidence_sha256, "evidence_sha256"),
            "evidence_sha256",
        )
    return attributes


def _exact_instrument_mapping(row: Mapping[str, object]) -> InstrumentId:
    mapping_status = row.get("entity_mapping_status")
    if (
        mapping_status is not None
        and _non_empty_text(
            mapping_status,
            "entity_mapping_status",
        ).lower()
        != "exact"
    ):
        raise WebEvidenceError("entity_mapping_status must be 'exact'")
    instrument = _instrument_id(_required_text(row, "instrument_id"), "instrument_id")
    mapping_key = _instrument_id(
        _required_text(row, "entity_mapping_key"),
        "entity_mapping_key",
    )
    if mapping_key != instrument:
        raise WebEvidenceError("entity_mapping_key must exactly equal instrument_id")
    _required_text(row, "source_entity_id")
    return instrument


def _exact_mapping_confidence(row: Mapping[str, object]) -> Decimal:
    value = row.get("mapping_confidence")
    if isinstance(value, bool) or value is None:
        raise WebEvidenceError("mapping_confidence must equal 1 for an exact mapping")
    try:
        confidence = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise WebEvidenceError("mapping_confidence must equal 1 for an exact mapping") from exc
    if not confidence.is_finite() or confidence != Decimal(1):
        raise WebEvidenceError("mapping_confidence must equal 1 for an exact mapping")
    return confidence


def _source_event_id(row: Mapping[str, object], external_fact_id: str) -> str:
    value = _first_present(row, ("source_event_id", "provider_event_id"))
    return external_fact_id if value is None else _non_empty_text(value, "source_event_id")


def _revision_type(row: Mapping[str, object]) -> str:
    raw = _required_text(row, "revision_type").lower()
    normalized = _REVISION_TYPE_ALIASES.get(raw, raw)
    if normalized not in _SUPPORTED_REVISION_TYPES:
        allowed = ", ".join(sorted({*_SUPPORTED_REVISION_TYPES, *_REVISION_TYPE_ALIASES}))
        raise WebEvidenceError(f"revision_type must be one of: {allowed}")
    return normalized


def _timing_basis(row: Mapping[str, object]) -> str:
    raw_value = _first_present(row, ("timing_basis", "first_seen_basis"))
    raw = _non_empty_text(raw_value, "timing_basis").lower()
    normalized = _TIMING_BASIS_ALIASES.get(raw, raw)
    if normalized not in _SUPPORTED_TIMING_BASES:
        allowed = ", ".join(sorted({*_SUPPORTED_TIMING_BASES, *_TIMING_BASIS_ALIASES}))
        raise WebEvidenceError(f"timing_basis must be one of: {allowed}")
    return normalized


def _instrument_id(value: str, field_name: str) -> InstrumentId:
    normalized = value.strip().upper()
    if normalized.endswith(".XSHG"):
        normalized = f"{normalized.removesuffix('.XSHG')}.SH"
    elif normalized.endswith(".XSHE"):
        normalized = f"{normalized.removesuffix('.XSHE')}.SZ"
    elif normalized.endswith(".XBSE"):
        normalized = f"{normalized.removesuffix('.XBSE')}.BJ"
    if not re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", normalized):
        raise WebEvidenceError(f"{field_name} must have an explicit SH, SZ, or BJ suffix")
    return InstrumentId(normalized)


def _required_revision_no(row: Mapping[str, object], revision_type: str) -> int:
    value = row.get("revision_no")
    if isinstance(value, bool) or not isinstance(value, Integral) or int(value) < 0:
        raise WebEvidenceError("revision_no must be a non-negative integer")
    revision_no = int(value)
    if revision_type == "initial" and revision_no != 0:
        raise WebEvidenceError("initial evidence must use revision_no=0")
    if revision_type != "initial" and revision_no == 0:
        raise WebEvidenceError("non-initial evidence must use revision_no>0")
    return revision_no


def _row_retrieved_at(
    row: Mapping[str, object],
    supplied: datetime | None,
) -> datetime:
    value: object = supplied if supplied is not None else row.get("retrieved_at")
    if not isinstance(value, datetime):
        raise WebEvidenceError("retrieved_at must be supplied as an aware datetime")
    return _aware_retrieved_at(value, "retrieved_at")


def _aware_retrieved_at(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise WebEvidenceError(f"{field_name} must include an explicit timezone")
    return value.astimezone(SHANGHAI).replace(microsecond=0)


def _required_timestamp(
    row: Mapping[str, object],
    names: tuple[str, ...],
    field_name: str,
) -> _ParsedTimestamp:
    value = _first_present(row, names)
    if value is None:
        joined = ", ".join(names)
        raise WebEvidenceError(f"{field_name} requires one of: {joined}")
    parsed = _optional_timestamp(value, field_name)
    assert parsed is not None
    return parsed


def _optional_timestamp_value(value: object, field_name: str) -> datetime | None:
    parsed = _optional_timestamp(value, field_name)
    return parsed.value if parsed is not None else None


def _optional_timestamp(value: object, field_name: str) -> _ParsedTimestamp | None:
    if value is None:
        return None
    precision = "second"
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
        precision = "date"
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            raise WebEvidenceError(f"{field_name} must not be empty")
        if _DATE_ONLY.fullmatch(raw):
            parsed = datetime.combine(date.fromisoformat(raw.replace("/", "-")), time.min)
            precision = "date"
        elif _COMPACT_DATE.fullmatch(raw):
            parsed = datetime.strptime(raw, "%Y%m%d")
            precision = "date"
        else:
            precision = "second" if _SECONDS.search(raw) else "minute"
            parsed = _parse_datetime_text(raw, field_name)
    else:
        raise WebEvidenceError(f"{field_name} must be a date, datetime, or timestamp string")

    local = (
        parsed.replace(tzinfo=SHANGHAI)
        if parsed.tzinfo is None or parsed.utcoffset() is None
        else parsed.astimezone(SHANGHAI)
    )
    if (
        precision != "date"
        and local.hour in {0, 12}
        and local.minute == 0
        and local.second == 0
        and local.microsecond == 0
    ):
        precision = "placeholder"
    if precision in {"date", "placeholder"}:
        local = datetime.combine(local.date(), time(hour=15), tzinfo=SHANGHAI)
    else:
        local = local.replace(microsecond=0)
    return _ParsedTimestamp(local, precision)


def _parse_datetime_text(raw: str, field_name: str) -> datetime:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        pass
    for pattern in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(raw, pattern)
        except ValueError:
            continue
    raise WebEvidenceError(f"{field_name} is not a supported timestamp: {raw!r}")


def _canonical_url(value: str) -> str:
    parsed = urlsplit(_non_empty_text(value, "canonical_url"))
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise WebEvidenceError("canonical_url must be an absolute HTTPS URL")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise WebEvidenceError("canonical_url cannot contain credentials or a fragment")
    host = parsed.hostname.lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise WebEvidenceError("canonical_url contains an invalid port") from exc
    if port not in {None, 443}:
        host = f"{host}:{port}"
    canonical = SplitResult("https", host, parsed.path or "/", parsed.query, "")
    return urlunsplit(canonical)


def _required_sha256(value: str, field_name: str) -> str:
    match = _SHA256.fullmatch(value.strip())
    if match is None:
        raise WebEvidenceError(f"{field_name} must contain 64 hexadecimal characters")
    return match.group(1).lower()


def _required_text(row: Mapping[str, object], field_name: str) -> str:
    value = row.get(field_name)
    return _non_empty_text(value, field_name)


def _non_empty_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WebEvidenceError(f"{field_name} must be a non-empty string")
    return value.strip()


def _required_choice(
    row: Mapping[str, object],
    field_name: str,
    choices: frozenset[str],
) -> str:
    value = _required_text(row, field_name).lower()
    if value not in choices:
        allowed = ", ".join(sorted(choices))
        raise WebEvidenceError(f"{field_name} must be one of: {allowed}")
    return value


def _first_present(row: Mapping[str, object], names: tuple[str, ...]) -> object | None:
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return None


def _scalar(value: object, field_name: str) -> EventAttribute:
    if value is None or isinstance(value, str | bool):
        return value
    if isinstance(value, Integral):
        return int(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise WebEvidenceError(f"{field_name} must be finite")
        return value
    if isinstance(value, Real):
        try:
            converted = Decimal(str(value))
        except InvalidOperation as exc:
            raise WebEvidenceError(f"{field_name} must be finite") from exc
        if not converted.is_finite():
            raise WebEvidenceError(f"{field_name} must be finite")
        return converted
    raise WebEvidenceError(f"{field_name} must be a scalar event attribute")


__all__ = [
    "LICENSE_APPROVAL",
    "MAJOR_CONTRACT_WON",
    "SUPPORTED_EVIDENCE_PROVIDERS",
    "SUPPORTED_WEB_EVENT_CODES",
    "TIMING_LICENSED_VENDOR",
    "TIMING_PAGE_METADATA",
    "TIMING_PROSPECTIVE_ARCHIVE",
    "TIMING_SEARCH_INDEX",
    "DiscoveryOnlyEvidenceError",
    "SearchHit",
    "WebEvidenceError",
    "normalize_web_evidence_row",
]
