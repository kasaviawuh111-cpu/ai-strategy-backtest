"""Immutable and deterministic provenance records for future strategy v2.

These value objects deliberately do not depend on the v1 signal runtime, API,
storage, or backtest engine.  They describe provider facts and deterministic
condition evaluations; a language model is not a permitted fact or signal
producer.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import cast

from ashare_lab.domain.shared import DomainValidationError, require_aware
from ashare_lab.domain.time import PointInTimeAvailability

_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_GIT_SHA = re.compile(r"[0-9a-f]{40}")
_INSTRUMENT_ID = re.compile(r"[0-9]{6}\.(SH|SZ|BJ)")
_CONDITION_REF = re.compile(r"\$\.(entry|exit)(?:\.children\[[0-9]+\]|\.child)*")


class SourceKind(StrEnum):
    """Permitted origins for data facts.

    Model output is intentionally absent.  A derived fact must identify a
    deterministic transform as its provider and still belong to a snapshot.
    """

    PROVIDER_RECORD = "provider_record"
    DETERMINISTIC_TRANSFORM = "deterministic_transform"


class SignalProducer(StrEnum):
    """The only component allowed to produce a replayable signal record."""

    DETERMINISTIC_RUNTIME = "deterministic_runtime"


type DataValue = str | int | bool | Decimal


@dataclass(frozen=True, slots=True)
class SourceRef:
    """Content-addressed identity of one source fact."""

    provider: str
    source_id: str
    snapshot_id: str
    schema_version: str
    content_sha256: str
    source_kind: SourceKind = SourceKind.PROVIDER_RECORD

    def __post_init__(self) -> None:
        for field_name in ("provider", "source_id", "snapshot_id", "schema_version"):
            object.__setattr__(
                self,
                field_name,
                _required_text(getattr(self, field_name), field_name),
            )
        _require_sha256(self.content_sha256, "content_sha256")
        if type(self.source_kind) is not SourceKind:
            raise DomainValidationError("source_kind is invalid")

    @property
    def sort_key(self) -> tuple[str, str, str, str, str, str]:
        return (
            self.provider,
            self.snapshot_id,
            self.source_id,
            self.schema_version,
            self.content_sha256,
            self.source_kind.value,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "content_sha256": self.content_sha256,
            "provider": self.provider,
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "source_id": self.source_id,
            "source_kind": self.source_kind.value,
        }

    @classmethod
    def from_dict(cls, payload: object) -> SourceRef:
        values = _strict_mapping(
            payload,
            fields={
                "content_sha256",
                "provider",
                "schema_version",
                "snapshot_id",
                "source_id",
                "source_kind",
            },
            label="source ref",
        )
        try:
            source_kind = SourceKind(_payload_text(values["source_kind"], "source_kind"))
        except ValueError as error:
            raise DomainValidationError("source_kind is invalid") from error
        return cls(
            provider=_payload_text(values["provider"], "provider"),
            source_id=_payload_text(values["source_id"], "source_id"),
            snapshot_id=_payload_text(values["snapshot_id"], "snapshot_id"),
            schema_version=_payload_text(values["schema_version"], "schema_version"),
            content_sha256=_payload_text(values["content_sha256"], "content_sha256"),
            source_kind=source_kind,
        )


@dataclass(frozen=True, slots=True)
class DataEnvelope:
    """One typed scalar fact with point-in-time and source provenance."""

    data_id: str
    value: DataValue | None
    unit: str | None
    availability: PointInTimeAvailability
    source_refs: tuple[SourceRef, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "data_id", _required_text(self.data_id, "data_id"))
        object.__setattr__(self, "unit", _optional_text(self.unit, "unit"))
        _validate_value(self.value)
        if type(self.availability) is not PointInTimeAvailability:
            raise DomainValidationError("availability must be a PointInTimeAvailability")
        canonical_sources = _canonical_source_refs(self.source_refs)
        if self.availability.source not in {item.provider for item in canonical_sources}:
            raise DomainValidationError("availability source must match a source_refs provider")
        object.__setattr__(self, "source_refs", canonical_sources)

    @property
    def fingerprint(self) -> str:
        return _canonical_hash(self._fingerprint_payload())

    def value_as_of(self, as_of: datetime) -> DataValue | None:
        """Expose the fact only after its historical first-availability gate."""

        return self.availability.value_as_of(self.value, as_of)

    def _fingerprint_payload(self) -> dict[str, object]:
        return {
            "availability": self.availability.to_dict(),
            "data_id": self.data_id,
            "schema_version": "data-envelope.v2",
            "source_refs": [item.to_dict() for item in self.source_refs],
            "unit": self.unit,
            "value": _serialize_value(self.value),
        }

    def to_dict(self) -> dict[str, object]:
        return {**self._fingerprint_payload(), "fingerprint": self.fingerprint}

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, payload: object) -> DataEnvelope:
        values = _strict_mapping(
            payload,
            fields={
                "availability",
                "data_id",
                "fingerprint",
                "schema_version",
                "source_refs",
                "unit",
                "value",
            },
            label="data envelope",
        )
        if values["schema_version"] != "data-envelope.v2":
            raise DomainValidationError("data envelope schema_version is invalid")
        item = cls(
            data_id=_payload_text(values["data_id"], "data_id"),
            value=_parse_value(values["value"]),
            unit=_payload_optional_text(values["unit"], "unit"),
            availability=PointInTimeAvailability.from_dict(values["availability"]),
            source_refs=_parse_source_refs(values["source_refs"]),
        )
        _require_expected_fingerprint(values["fingerprint"], item.fingerprint)
        return item

    @classmethod
    def from_json(cls, payload: str) -> DataEnvelope:
        return cls.from_dict(_parse_json_object(payload, "data envelope"))


@dataclass(frozen=True, slots=True)
class SignalRecord:
    """Tamper-evident result of one deterministic DSL condition evaluation."""

    instrument_id: str
    condition_ref: str
    triggered: bool
    signal_at: datetime
    plan_id: str
    strategy_hash: str
    dsl_schema_version: str
    code_revision: str
    input_envelopes: tuple[DataEnvelope, ...]
    source_refs: tuple[SourceRef, ...]
    producer: SignalProducer = SignalProducer.DETERMINISTIC_RUNTIME

    def __post_init__(self) -> None:
        for field_name in ("instrument_id", "condition_ref", "dsl_schema_version"):
            object.__setattr__(
                self,
                field_name,
                _required_text(getattr(self, field_name), field_name),
            )
        if _INSTRUMENT_ID.fullmatch(self.instrument_id) is None:
            raise DomainValidationError("instrument_id must be a canonical A-share symbol")
        if _CONDITION_REF.fullmatch(self.condition_ref) is None:
            raise DomainValidationError("condition_ref must be a Strategy v2 path")
        if self.dsl_schema_version != "strategy.v2":
            raise DomainValidationError("dsl_schema_version must be strategy.v2")
        if type(self.triggered) is not bool:
            raise DomainValidationError("triggered must be a boolean")
        require_aware(self.signal_at, "signal_at")
        _require_sha256(self.plan_id, "plan_id")
        _require_sha256(self.strategy_hash, "strategy_hash")
        if type(self.code_revision) is not str or _GIT_SHA.fullmatch(self.code_revision) is None:
            raise DomainValidationError("code_revision must be a 40-character Git SHA")
        if self.producer is not SignalProducer.DETERMINISTIC_RUNTIME:
            raise DomainValidationError("producer must be deterministic_runtime")

        canonical_inputs = _canonical_envelopes(self.input_envelopes)
        canonical_sources = _canonical_source_refs(self.source_refs)
        input_sources = {
            source_ref
            for input_envelope in canonical_inputs
            for source_ref in input_envelope.source_refs
        }
        if set(canonical_sources) != input_sources:
            raise DomainValidationError("source_refs must exactly match input sources")
        for item in canonical_inputs:
            first_available = item.availability.first_available_at
            if first_available is None:
                raise DomainValidationError("input requires first_available_at")
            if first_available > self.signal_at:
                raise DomainValidationError("input first_available_at cannot follow signal_at")
            input_signal_at = item.availability.signal_at
            if input_signal_at is not None and input_signal_at != self.signal_at:
                raise DomainValidationError("input signal_at must match signal record signal_at")
        object.__setattr__(self, "input_envelopes", canonical_inputs)
        object.__setattr__(self, "source_refs", canonical_sources)

    @property
    def fingerprint(self) -> str:
        return _canonical_hash(self._fingerprint_payload())

    @property
    def data_snapshot_ids(self) -> tuple[str, ...]:
        """Canonical set of physical snapshot identities behind the signal."""

        return tuple(sorted({item.snapshot_id for item in self.source_refs}))

    def _fingerprint_payload(self) -> dict[str, object]:
        return {
            "code_revision": self.code_revision,
            "condition_ref": self.condition_ref,
            "dsl_schema_version": self.dsl_schema_version,
            "input_envelopes": [item.to_dict() for item in self.input_envelopes],
            "instrument_id": self.instrument_id,
            "plan_id": self.plan_id,
            "producer": self.producer.value,
            "schema_version": "signal-record.v2",
            "signal_at": self.signal_at.isoformat(),
            "source_refs": [item.to_dict() for item in self.source_refs],
            "strategy_hash": self.strategy_hash,
            "triggered": self.triggered,
        }

    def to_dict(self) -> dict[str, object]:
        return {**self._fingerprint_payload(), "fingerprint": self.fingerprint}

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, payload: object) -> SignalRecord:
        values = _strict_mapping(
            payload,
            fields={
                "code_revision",
                "condition_ref",
                "dsl_schema_version",
                "fingerprint",
                "input_envelopes",
                "instrument_id",
                "plan_id",
                "producer",
                "schema_version",
                "signal_at",
                "source_refs",
                "strategy_hash",
                "triggered",
            },
            label="signal record",
        )
        if values["schema_version"] != "signal-record.v2":
            raise DomainValidationError("signal record schema_version is invalid")
        producer_value = _payload_text(values["producer"], "producer")
        try:
            producer = SignalProducer(producer_value)
        except ValueError as error:
            raise DomainValidationError("producer must be deterministic_runtime") from error
        item = cls(
            instrument_id=_payload_text(values["instrument_id"], "instrument_id"),
            condition_ref=_payload_text(values["condition_ref"], "condition_ref"),
            triggered=_payload_bool(values["triggered"], "triggered"),
            signal_at=_payload_datetime(values["signal_at"], "signal_at"),
            plan_id=_payload_text(values["plan_id"], "plan_id"),
            strategy_hash=_payload_text(values["strategy_hash"], "strategy_hash"),
            dsl_schema_version=_payload_text(values["dsl_schema_version"], "dsl_schema_version"),
            code_revision=_payload_text(values["code_revision"], "code_revision"),
            input_envelopes=_parse_envelopes(values["input_envelopes"]),
            source_refs=_parse_source_refs(values["source_refs"]),
            producer=producer,
        )
        _require_expected_fingerprint(values["fingerprint"], item.fingerprint)
        return item

    @classmethod
    def from_json(cls, payload: str) -> SignalRecord:
        return cls.from_dict(_parse_json_object(payload, "signal record"))


def _canonical_source_refs(values: object) -> tuple[SourceRef, ...]:
    if type(values) is not tuple or not values:
        raise DomainValidationError("source_refs must be non-empty")
    raw_values = cast(tuple[object, ...], values)
    if any(type(item) is not SourceRef for item in raw_values):
        raise DomainValidationError("source_refs must contain SourceRef values")
    typed_values = cast(tuple[SourceRef, ...], raw_values)
    if len(typed_values) != len(set(typed_values)):
        raise DomainValidationError("source_refs must be unique")
    return tuple(sorted(typed_values, key=lambda item: item.sort_key))


def _canonical_envelopes(values: object) -> tuple[DataEnvelope, ...]:
    if type(values) is not tuple or not values:
        raise DomainValidationError("input_envelopes must be non-empty")
    raw_values = cast(tuple[object, ...], values)
    if any(type(item) is not DataEnvelope for item in raw_values):
        raise DomainValidationError("input_envelopes must contain DataEnvelope values")
    typed_values = cast(tuple[DataEnvelope, ...], raw_values)
    fingerprints = tuple(item.fingerprint for item in typed_values)
    if len(fingerprints) != len(set(fingerprints)):
        raise DomainValidationError("input_envelopes must be unique")
    return tuple(sorted(typed_values, key=lambda item: item.fingerprint))


def _validate_value(value: object) -> None:
    if value is None or type(value) in {str, int, bool}:
        return
    if type(value) is Decimal:
        if not value.is_finite():
            raise DomainValidationError("value must be finite")
        return
    raise DomainValidationError("value type is not supported")


def _serialize_value(value: DataValue | None) -> dict[str, object] | None:
    if value is None:
        return None
    if type(value) is bool:
        return {"type": "boolean", "value": value}
    if type(value) is int:
        return {"type": "integer", "value": value}
    if type(value) is Decimal:
        return {"type": "decimal", "value": _canonical_decimal(value)}
    return {"type": "string", "value": value}


def _parse_value(payload: object) -> DataValue | None:
    if payload is None:
        return None
    values = _strict_mapping(payload, fields={"type", "value"}, label="data value")
    value_type = _payload_text(values["type"], "value.type")
    value = values["value"]
    if value_type == "boolean" and type(value) is bool:
        return value
    if value_type == "integer" and type(value) is int:
        return value
    if value_type == "string" and type(value) is str:
        return value
    if value_type == "decimal" and type(value) is str:
        try:
            parsed = Decimal(value)
        except Exception as error:
            raise DomainValidationError("decimal value is invalid") from error
        _validate_value(parsed)
        return parsed
    raise DomainValidationError("serialized data value is invalid")


def _parse_source_refs(payload: object) -> tuple[SourceRef, ...]:
    if type(payload) is not list:
        raise DomainValidationError("source_refs must be an array")
    return tuple(SourceRef.from_dict(item) for item in cast(list[object], payload))


def _parse_envelopes(payload: object) -> tuple[DataEnvelope, ...]:
    if type(payload) is not list:
        raise DomainValidationError("input_envelopes must be an array")
    return tuple(DataEnvelope.from_dict(item) for item in cast(list[object], payload))


def _require_sha256(value: object, field_name: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise DomainValidationError(f"{field_name} must be a sha256 content hash")


def _required_text(value: object, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise DomainValidationError(f"{field_name} must be a non-empty string")
    return unicodedata.normalize("NFC", value.strip())


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name)


def _payload_text(value: object, field_name: str) -> str:
    return _required_text(value, field_name)


def _payload_optional_text(value: object, field_name: str) -> str | None:
    return _optional_text(value, field_name)


def _payload_bool(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise DomainValidationError(f"{field_name} must be a boolean")
    return value


def _payload_datetime(value: object, field_name: str) -> datetime:
    if type(value) is not str:
        raise DomainValidationError(f"{field_name} must be an ISO 8601 datetime")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise DomainValidationError(f"{field_name} must be a valid ISO 8601 datetime") from error
    require_aware(parsed, field_name)
    return parsed


def _strict_mapping(
    payload: object,
    *,
    fields: set[str],
    label: str,
) -> Mapping[str, object]:
    if not isinstance(payload, Mapping):
        raise DomainValidationError(f"{label} payload must be an object")
    raw = cast(Mapping[object, object], payload)
    if any(type(key) is not str for key in raw):
        raise DomainValidationError(f"{label} field names must be strings")
    actual = cast(set[str], set(raw))
    extras = actual - fields
    missing = fields - actual
    if extras:
        raise DomainValidationError(
            f"{label} payload has unexpected fields: {', '.join(sorted(extras))}"
        )
    if missing:
        raise DomainValidationError(
            f"{label} payload is missing fields: {', '.join(sorted(missing))}"
        )
    return cast(Mapping[str, object], payload)


def _canonical_decimal(value: Decimal) -> str:
    if value.is_zero():
        return "0"
    return format(value.normalize(), "f")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _canonical_hash(value: object) -> str:
    payload = _canonical_json(value).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _parse_json_object(payload: object, label: str) -> Mapping[str, object]:
    if type(payload) is not str:
        raise DomainValidationError(f"{label} JSON must be a string")
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise DomainValidationError(f"{label} JSON is invalid") from error
    if type(decoded) is not dict:
        raise DomainValidationError(f"{label} JSON must contain an object")
    return cast(dict[str, object], decoded)


def _require_expected_fingerprint(value: object, expected: str) -> None:
    _require_sha256(value, "fingerprint")
    if value != expected:
        raise DomainValidationError("fingerprint does not match payload")
