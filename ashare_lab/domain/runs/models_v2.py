"""Immutable, restart-safe audit artifacts for Strategy DSL v2.

These models are additive.  They intentionally do not alter the v1
``RunManifest`` or its hash semantics.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import date, datetime
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ashare_lab.domain.provenance import SignalRecord
from ashare_lab.domain.shared import require_aware
from ashare_lab.domain.strategy.canonical import canonical_hash, canonical_json
from ashare_lab.domain.strategy.models_v2 import StrategySpecV2

_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_CONTENT_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}:[0-9a-f]{64}")
_DRAFT_ID = re.compile(r"draft:[A-Za-z0-9_.-]{1,128}")
_RECEIPT_ID = re.compile(r"receipt:[0-9a-f]{64}")
_GIT_SHA = re.compile(r"[0-9a-f]{40}")
_SENSITIVE_TEXT = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{16,}\b|authorization\s*:\s*bearer\s+\S+)",
    re.IGNORECASE,
)
_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "candidate_provider_api_key",
        "model_raw",
        "raw_model_output",
        "secret",
        "token",
    }
)


class _FrozenV2Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)


class SnapshotBindingV2(_FrozenV2Artifact):
    """Content identity of one server-loaded trusted snapshot."""

    kind: Literal["security_master", "trading_calendar", "market_data"]
    snapshot_id: str = Field(min_length=1, max_length=160)
    provider: str = Field(min_length=1, max_length=128)
    schema_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    content_hash: str
    coverage_start: date
    coverage_end: date
    generated_at: datetime

    @field_validator("provider")
    @classmethod
    def provider_is_exact_nonblank_text(cls, value: str) -> str:
        has_control = any(ord(character) < 32 or ord(character) == 127 for character in value)
        if not value.strip() or has_control:
            raise ValueError("provider must be nonblank text without control characters")
        return value

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if _CONTENT_ID.fullmatch(self.snapshot_id) is None:
            raise ValueError("snapshot_id must be a content-addressed identity")
        _require_hash(self.content_hash, "content_hash")
        if self.coverage_start > self.coverage_end:
            raise ValueError("snapshot coverage_start cannot follow coverage_end")
        require_aware(self.generated_at, "generated_at")
        return self


class DraftRevisionV2(_FrozenV2Artifact):
    """Append-only server record of one exact user input revision."""

    schema_version: Literal["strategy-draft-revision.v2"] = "strategy-draft-revision.v2"
    draft_id: str
    revision: int = Field(ge=1, le=1_000_000)
    original_input: str = Field(min_length=1, max_length=20_000)
    provider: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    created_at: datetime

    @model_validator(mode="after")
    def validate_revision(self) -> Self:
        if _DRAFT_ID.fullmatch(self.draft_id) is None:
            raise ValueError("draft_id is invalid")
        if not self.original_input.strip():
            raise ValueError("original_input cannot be blank")
        _reject_sensitive_text(self.original_input, "original_input")
        require_aware(self.created_at, "created_at")
        return self


class ExecutableStrategyPlanRecordV2(_FrozenV2Artifact):
    """Canonical durable form of a validator-issued in-memory plan.

    This record is not itself executable.  The persistent receipt service must
    verify its signature, expiry and current snapshot bindings, then rerun the
    v2 validator to recover an in-memory ``ExecutableStrategyPlan``.
    """

    schema_version: Literal["executable-strategy-plan-record.v2"] = (
        "executable-strategy-plan-record.v2"
    )
    plan_id: str
    draft_id: str
    revision: int = Field(ge=1, le=1_000_000)
    strategy_json: str = Field(min_length=2, max_length=1_000_000)
    strategy_hash: str
    catalog_hash: str
    security_master: SnapshotBindingV2
    trading_calendar: SnapshotBindingV2
    market_data: SnapshotBindingV2
    provider: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    validator_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    code_revision: str
    created_at: datetime

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        _require_hash(self.plan_id, "plan_id")
        _require_hash(self.strategy_hash, "strategy_hash")
        _require_hash(self.catalog_hash, "catalog_hash")
        if _DRAFT_ID.fullmatch(self.draft_id) is None:
            raise ValueError("draft_id is invalid")
        if _GIT_SHA.fullmatch(self.code_revision) is None:
            raise ValueError("code_revision must be a complete 40-character Git SHA")
        require_aware(self.created_at, "created_at")
        if self.security_master.kind != "security_master":
            raise ValueError("security_master binding has the wrong kind")
        if self.trading_calendar.kind != "trading_calendar":
            raise ValueError("trading_calendar binding has the wrong kind")
        if self.market_data.kind != "market_data":
            raise ValueError("market_data binding has the wrong kind")
        strategy = _parse_strategy(self.strategy_json)
        if canonical_json(strategy) != self.strategy_json:
            raise ValueError("strategy_json must be canonical JSON")
        if canonical_hash(strategy) != self.strategy_hash:
            raise ValueError("strategy_hash does not match strategy_json")
        return self

    @property
    def strategy(self) -> StrategySpecV2:
        return _parse_strategy(self.strategy_json)

    @property
    def snapshot_bindings(self) -> tuple[SnapshotBindingV2, ...]:
        return (self.security_master, self.trading_calendar, self.market_data)


class ValidationReceiptClaimsV2(_FrozenV2Artifact):
    """Claims authenticated by the configured server-side signing key."""

    schema_version: Literal["validation-receipt-claims.v2"] = "validation-receipt-claims.v2"
    plan_id: str
    draft_id: str
    revision: int = Field(ge=1, le=1_000_000)
    strategy_hash: str
    catalog_hash: str
    security_master: SnapshotBindingV2
    trading_calendar: SnapshotBindingV2
    market_data: SnapshotBindingV2
    provider: str
    validator_version: str
    code_revision: str
    issued_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def validate_claims(self) -> Self:
        _require_hash(self.plan_id, "plan_id")
        _require_hash(self.strategy_hash, "strategy_hash")
        _require_hash(self.catalog_hash, "catalog_hash")
        if _DRAFT_ID.fullmatch(self.draft_id) is None:
            raise ValueError("draft_id is invalid")
        if _GIT_SHA.fullmatch(self.code_revision) is None:
            raise ValueError("code_revision must be a complete 40-character Git SHA")
        require_aware(self.issued_at, "issued_at")
        require_aware(self.expires_at, "expires_at")
        if self.expires_at <= self.issued_at:
            raise ValueError("receipt expires_at must follow issued_at")
        return self

    @classmethod
    def from_plan(
        cls,
        plan: ExecutableStrategyPlanRecordV2,
        *,
        issued_at: datetime,
        expires_at: datetime,
    ) -> ValidationReceiptClaimsV2:
        return cls(
            plan_id=plan.plan_id,
            draft_id=plan.draft_id,
            revision=plan.revision,
            strategy_hash=plan.strategy_hash,
            catalog_hash=plan.catalog_hash,
            security_master=plan.security_master,
            trading_calendar=plan.trading_calendar,
            market_data=plan.market_data,
            provider=plan.provider,
            validator_version=plan.validator_version,
            code_revision=plan.code_revision,
            issued_at=issued_at,
            expires_at=expires_at,
        )

    @property
    def snapshot_bindings(self) -> tuple[SnapshotBindingV2, ...]:
        return (self.security_master, self.trading_calendar, self.market_data)


class StoredValidationReceiptV2(_FrozenV2Artifact):
    """Signed validation receipt persisted alongside the immutable plan."""

    schema_version: Literal["stored-validation-receipt.v2"] = "stored-validation-receipt.v2"
    receipt_id: str
    plan_id: str
    claims_json: str = Field(min_length=2, max_length=1_000_000)
    signed_token: str = Field(min_length=16, max_length=2_000_000)
    token_sha256: str
    issued_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def validate_receipt(self) -> Self:
        if _RECEIPT_ID.fullmatch(self.receipt_id) is None:
            raise ValueError("receipt_id is invalid")
        _require_hash(self.plan_id, "plan_id")
        _require_hash(self.token_sha256, "token_sha256")
        claims = _parse_claims(self.claims_json)
        if canonical_json(claims) != self.claims_json:
            raise ValueError("claims_json must be canonical JSON")
        if claims.plan_id != self.plan_id:
            raise ValueError("receipt plan_id does not match claims")
        if claims.issued_at != self.issued_at or claims.expires_at != self.expires_at:
            raise ValueError("receipt timestamps do not match claims")
        require_aware(self.issued_at, "issued_at")
        require_aware(self.expires_at, "expires_at")
        return self

    @property
    def claims(self) -> ValidationReceiptClaimsV2:
        return _parse_claims(self.claims_json)


class RunManifestV2(_FrozenV2Artifact):
    """Immutable audit join from run identity back to server-owned evidence."""

    schema_version: Literal["run-manifest.v2"] = "run-manifest.v2"
    run_id: str = Field(min_length=1, max_length=128)
    plan_id: str
    draft_id: str
    revision: int = Field(ge=1, le=1_000_000)
    original_input: str = Field(min_length=1, max_length=20_000)
    final_strategy_json: str = Field(min_length=2, max_length=1_000_000)
    strategy_hash: str
    provider: str
    validation_receipt_id: str
    validation_receipt_sha256: str
    catalog_hash: str
    security_master: SnapshotBindingV2
    trading_calendar: SnapshotBindingV2
    market_data: SnapshotBindingV2
    code_revision: str
    engine_run_key: str = Field(min_length=1, max_length=256)
    backtest_config_json: str = Field(min_length=2, max_length=1_000_000)
    backtest_config_hash: str
    fee_policy_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    fee_policy_hash: str
    engine_result_hash: str
    signal_records_json: str = "[]"
    created_at: datetime

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        if _DRAFT_ID.fullmatch(self.draft_id) is None:
            raise ValueError("draft_id is invalid")
        if _RECEIPT_ID.fullmatch(self.validation_receipt_id) is None:
            raise ValueError("validation_receipt_id is invalid")
        for value, field_name in (
            (self.plan_id, "plan_id"),
            (self.strategy_hash, "strategy_hash"),
            (self.validation_receipt_sha256, "validation_receipt_sha256"),
            (self.catalog_hash, "catalog_hash"),
            (self.backtest_config_hash, "backtest_config_hash"),
            (self.fee_policy_hash, "fee_policy_hash"),
            (self.engine_result_hash, "engine_result_hash"),
        ):
            _require_hash(value, field_name)
        if _GIT_SHA.fullmatch(self.code_revision) is None:
            raise ValueError("code_revision must be a complete 40-character Git SHA")
        _reject_sensitive_text(self.original_input, "original_input")
        require_aware(self.created_at, "created_at")
        strategy = _parse_strategy(self.final_strategy_json)
        if canonical_json(strategy) != self.final_strategy_json:
            raise ValueError("final_strategy_json must be canonical JSON")
        if canonical_hash(strategy) != self.strategy_hash:
            raise ValueError("strategy_hash does not match final_strategy_json")
        backtest_config = _parse_canonical_object_json(
            self.backtest_config_json,
            "backtest_config_json",
        )
        if canonical_hash(backtest_config) != self.backtest_config_hash:
            raise ValueError("backtest_config_hash does not match backtest_config_json")
        _reject_sensitive_text(self.engine_run_key, "engine_run_key")
        records = _validate_signal_records_json(self.signal_records_json)
        for record in records:
            if record.plan_id != self.plan_id:
                raise ValueError("signal record plan_id does not match manifest")
            if record.strategy_hash != self.strategy_hash:
                raise ValueError("signal record strategy_hash does not match manifest")
            if record.code_revision != self.code_revision:
                raise ValueError("signal record code_revision does not match manifest")
            if any(
                source.snapshot_id != self.market_data.snapshot_id for source in record.source_refs
            ):
                raise ValueError("signal record source snapshot does not match manifest")
        _reject_sensitive_payload(self.model_dump(mode="json"), "manifest")
        return self

    @classmethod
    def from_server_records(
        cls,
        *,
        run_id: str,
        draft: DraftRevisionV2,
        plan: ExecutableStrategyPlanRecordV2,
        receipt_id: str,
        receipt_sha256: str,
        engine_run_key: str,
        backtest_config_json: str,
        backtest_config_hash: str,
        fee_policy_version: str,
        fee_policy_hash: str,
        engine_result_hash: str,
        created_at: datetime,
        signal_records_json: str = "[]",
    ) -> RunManifestV2:
        if (draft.draft_id, draft.revision, draft.provider) != (
            plan.draft_id,
            plan.revision,
            plan.provider,
        ):
            raise ValueError("plan does not belong to the server draft revision")
        return cls(
            run_id=run_id,
            plan_id=plan.plan_id,
            draft_id=draft.draft_id,
            revision=draft.revision,
            original_input=draft.original_input,
            final_strategy_json=plan.strategy_json,
            strategy_hash=plan.strategy_hash,
            provider=plan.provider,
            validation_receipt_id=receipt_id,
            validation_receipt_sha256=receipt_sha256,
            catalog_hash=plan.catalog_hash,
            security_master=plan.security_master,
            trading_calendar=plan.trading_calendar,
            market_data=plan.market_data,
            code_revision=plan.code_revision,
            engine_run_key=engine_run_key,
            backtest_config_json=backtest_config_json,
            backtest_config_hash=backtest_config_hash,
            fee_policy_version=fee_policy_version,
            fee_policy_hash=fee_policy_hash,
            engine_result_hash=engine_result_hash,
            signal_records_json=signal_records_json,
            created_at=created_at,
        )

    def canonical_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json")

    @property
    def manifest_hash(self) -> str:
        return canonical_hash(self.canonical_payload())

    @property
    def signal_records(self) -> tuple[SignalRecord, ...]:
        raw = cast(list[object], json.loads(self.signal_records_json))
        return tuple(SignalRecord.from_dict(item) for item in raw)


def canonical_signal_records_json(records: tuple[SignalRecord, ...]) -> str:
    """Persist only true signals; false evaluations are reproducible inputs, not run facts."""

    if type(records) is not tuple or any(type(item) is not SignalRecord for item in records):
        raise ValueError("signal records must be a tuple of SignalRecord values")
    if any(not item.triggered for item in records):
        raise ValueError("run manifest may persist only triggered SignalRecord values")
    values = [item.to_dict() for item in sorted(records, key=lambda item: item.fingerprint)]
    return canonical_json(values)


def _parse_strategy(payload: str) -> StrategySpecV2:
    try:
        decoded = json.loads(payload)
        return StrategySpecV2.model_validate(decoded)
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError("strategy_json is not a valid StrategySpecV2") from error


def _parse_claims(payload: str) -> ValidationReceiptClaimsV2:
    try:
        decoded = json.loads(payload)
        return ValidationReceiptClaimsV2.model_validate(decoded)
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError("claims_json is invalid") from error


def _validate_signal_records_json(payload: str) -> tuple[SignalRecord, ...]:
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ValueError("signal_records_json is invalid") from error
    if type(decoded) is not list:
        raise ValueError("signal_records_json must contain an array")
    records = tuple(SignalRecord.from_dict(item) for item in cast(list[object], decoded))
    if any(not item.triggered for item in records):
        raise ValueError("signal_records_json may contain only triggered signals")
    if canonical_signal_records_json(records) != payload:
        raise ValueError("signal_records_json must be canonical and fingerprint ordered")
    return records


def _parse_canonical_object_json(payload: str, field_name: str) -> dict[str, object]:
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ValueError(f"{field_name} is invalid") from error
    if type(decoded) is not dict:
        raise ValueError(f"{field_name} must contain an object")
    typed = cast(dict[str, object], decoded)
    if canonical_json(typed) != payload:
        raise ValueError(f"{field_name} must be canonical JSON")
    _reject_sensitive_payload(typed, field_name)
    return typed


def _reject_sensitive_text(value: str, field_name: str) -> None:
    if _SENSITIVE_TEXT.search(value):
        raise ValueError(f"{field_name} contains a sensitive credential")


def _reject_sensitive_payload(value: object, path: str) -> None:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        for raw_key, item in mapping.items():
            if type(raw_key) is not str:
                raise ValueError(f"{path} contains a non-string field name")
            key = raw_key
            if key.lower() in _SENSITIVE_KEYS:
                raise ValueError(f"{path}.{key} contains a forbidden sensitive field")
            _reject_sensitive_payload(item, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(cast(list[object], value)):
            _reject_sensitive_payload(item, f"{path}[{index}]")
        return
    if isinstance(value, str):
        _reject_sensitive_text(value, path)


def _require_hash(value: object, field_name: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a sha256 content hash")


__all__ = [
    "DraftRevisionV2",
    "ExecutableStrategyPlanRecordV2",
    "RunManifestV2",
    "SnapshotBindingV2",
    "StoredValidationReceiptV2",
    "ValidationReceiptClaimsV2",
    "canonical_signal_records_json",
]
