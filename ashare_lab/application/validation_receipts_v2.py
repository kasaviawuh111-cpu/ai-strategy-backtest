"""Persistent, server-signed recovery gate for executable Strategy v2 plans."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from ashare_lab.domain.runs.models_v2 import (
    DraftRevisionV2,
    ExecutableStrategyPlanRecordV2,
    SnapshotBindingV2,
    StoredValidationReceiptV2,
    ValidationReceiptClaimsV2,
)
from ashare_lab.domain.strategy.canonical import canonical_hash, canonical_json
from ashare_lab.domain.strategy.validation_v2 import (
    ExecutableStrategyPlan,
    StrategyCandidateV2,
    StrategyV2ValidationContext,
    is_validator_issued_plan,
    validate_strategy_candidate_v2,
)
from ashare_lab.ports.strategy_v2_artifacts import StrategyV2ArtifactStore

_TOKEN_PREFIX = "p0c-v2"
_VALIDATOR_VERSION = "strategy-v2-persistent-gate.1"


class PersistentPlanRecoveryError(ValueError):
    """A durable plan cannot be authenticated against current server state."""


class ValidationReceiptServiceV2:
    """Append, authenticate and recover v2 plans without trusting client claims."""

    def __init__(
        self,
        *,
        store: StrategyV2ArtifactStore,
        signing_key: bytes,
        receipt_ttl: timedelta,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if type(signing_key) is not bytes or len(signing_key) < 32:
            raise ValueError("validation receipt signing key must contain at least 32 bytes")
        if type(receipt_ttl) is not timedelta or receipt_ttl <= timedelta(0):
            raise ValueError("validation receipt ttl must be positive")
        self._store = store
        self._signing_key = signing_key
        self._receipt_ttl = receipt_ttl
        self._clock = clock

    @property
    def store(self) -> StrategyV2ArtifactStore:
        return self._store

    def save_draft(self, draft: DraftRevisionV2) -> None:
        self._store.append_draft_revision(draft)

    def persist_validated_plan(
        self,
        plan: ExecutableStrategyPlan,
        *,
        snapshot_bindings: tuple[SnapshotBindingV2, ...],
    ) -> StoredValidationReceiptV2:
        """Persist only an in-process plan already issued by the ordered validator."""

        if not is_validator_issued_plan(plan):
            raise PersistentPlanRecoveryError(
                "persist requires a validator-issued ExecutableStrategyPlan"
            )
        draft = self._store.get_draft_revision(plan.draft_id, plan.revision)
        if draft is None:
            raise PersistentPlanRecoveryError("server draft revision is missing")
        if draft.original_input != plan.original_input or draft.provider != plan.provider:
            raise PersistentPlanRecoveryError("plan differs from the server draft revision")
        security, calendar, market, composite, producer_children = _snapshot_bindings_by_kind(
            snapshot_bindings
        )
        if security.snapshot_id != plan.security_master_snapshot_id:
            raise PersistentPlanRecoveryError("security-master snapshot binding differs from plan")
        if market.snapshot_id not in plan.data_snapshot_ids:
            raise PersistentPlanRecoveryError("market-data snapshot binding differs from plan")
        if calendar.snapshot_id != plan.trading_calendar_snapshot_id:
            raise PersistentPlanRecoveryError("trading-calendar snapshot binding differs from plan")
        if composite.snapshot_id != plan.composite_snapshot_id:
            raise PersistentPlanRecoveryError("composite snapshot binding differs from plan")
        if snapshot_bindings_hash(snapshot_bindings) != plan.snapshot_bindings_hash:
            raise PersistentPlanRecoveryError("trusted snapshot binding digest differs from plan")
        if (
            market.coverage_start > plan.strategy.backtest.start
            or market.coverage_end < plan.strategy.backtest.end
            or calendar.coverage_start > plan.strategy.backtest.start
            or calendar.coverage_end < plan.strategy.backtest.end
        ):
            raise PersistentPlanRecoveryError("trusted snapshot coverage is incomplete")

        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("receipt clock must return a timezone-aware datetime")
        record = ExecutableStrategyPlanRecordV2(
            plan_id=plan.plan_id,
            draft_id=plan.draft_id,
            revision=plan.revision,
            strategy_json=canonical_json(plan.strategy),
            strategy_hash=plan.strategy_hash,
            catalog_hash=plan.catalog_hash,
            security_master=security,
            trading_calendar=calendar,
            market_data=market,
            composite_snapshot=composite,
            producer_children=producer_children,
            provider=plan.provider,
            validator_version=_VALIDATOR_VERSION,
            code_revision=plan.code_revision,
            created_at=now,
        )
        claims = ValidationReceiptClaimsV2.from_plan(
            record,
            issued_at=now,
            expires_at=now + self._receipt_ttl,
        )
        claims_json = canonical_json(claims)
        token = self._sign(claims_json)
        token_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        receipt = StoredValidationReceiptV2(
            receipt_id=f"receipt:{token_digest}",
            plan_id=record.plan_id,
            claims_json=claims_json,
            signed_token=token,
            token_sha256=f"sha256:{token_digest}",
            issued_at=claims.issued_at,
            expires_at=claims.expires_at,
        )
        self._store.append_validated_plan(record, receipt)
        return receipt

    def restore_executable_plan(
        self,
        receipt_id: str,
        *,
        current_context: StrategyV2ValidationContext,
        current_snapshot_bindings: tuple[SnapshotBindingV2, ...],
    ) -> ExecutableStrategyPlan:
        """Recover a fresh in-process authority only after all server checks pass."""

        receipt = self._store.get_validation_receipt(receipt_id)
        if receipt is None:
            raise PersistentPlanRecoveryError("validation receipt was not found")
        record = self._store.get_plan(receipt.plan_id)
        if record is None:
            raise PersistentPlanRecoveryError("persisted executable plan was not found")
        draft = self._store.get_draft_revision(record.draft_id, record.revision)
        if draft is None:
            raise PersistentPlanRecoveryError("server draft revision was not found")

        token_digest = f"sha256:{hashlib.sha256(receipt.signed_token.encode()).hexdigest()}"
        if token_digest != receipt.token_sha256 or receipt.receipt_id != (
            "receipt:" + token_digest.removeprefix("sha256:")
        ):
            raise PersistentPlanRecoveryError("validation receipt token hash is invalid")
        authenticated_claims = self._verify(receipt.signed_token)
        if canonical_json(authenticated_claims) != receipt.claims_json:
            raise PersistentPlanRecoveryError("validation receipt claims differ from storage")
        expected_claims = ValidationReceiptClaimsV2.from_plan(
            record,
            issued_at=receipt.issued_at,
            expires_at=receipt.expires_at,
        )
        if authenticated_claims != expected_claims:
            raise PersistentPlanRecoveryError("validation receipt does not bind the stored plan")
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("receipt clock must return a timezone-aware datetime")
        if now >= authenticated_claims.expires_at:
            raise PersistentPlanRecoveryError("validation receipt has expired")
        if now < authenticated_claims.issued_at:
            raise PersistentPlanRecoveryError("validation receipt is not valid yet")

        current_bindings = _ordered_snapshot_bindings(current_snapshot_bindings)
        if current_bindings != authenticated_claims.snapshot_bindings:
            raise PersistentPlanRecoveryError("trusted snapshot binding changed after validation")
        if (
            current_context.original_input != draft.original_input
            or current_context.draft_id != draft.draft_id
            or current_context.revision != draft.revision
            or current_context.provider != draft.provider
        ):
            raise PersistentPlanRecoveryError("current context differs from server draft revision")
        if (
            current_context.catalog.content_hash != record.catalog_hash
            or current_context.security_master.snapshot_id != record.security_master.snapshot_id
            or current_context.code_revision != record.code_revision
        ):
            raise PersistentPlanRecoveryError("current trusted validation context changed")

        candidate = StrategyCandidateV2(
            original_input=draft.original_input,
            draft_id=draft.draft_id,
            revision=draft.revision,
            provider=draft.provider,
            strategy=record.strategy,
        )
        recovered = validate_strategy_candidate_v2(candidate, current_context)
        if (
            recovered.plan_id != record.plan_id
            or recovered.strategy_hash != record.strategy_hash
            or recovered.data_snapshot_ids != plan_snapshot_ids(record)
        ):
            raise PersistentPlanRecoveryError(
                "revalidated plan differs from the persisted executable plan"
            )
        return recovered

    def _sign(self, claims_json: str) -> str:
        payload = _urlsafe_encode(claims_json.encode("utf-8"))
        signing_input = f"{_TOKEN_PREFIX}.{payload}".encode("ascii")
        signature = hmac.new(self._signing_key, signing_input, hashlib.sha256).digest()
        return f"{_TOKEN_PREFIX}.{payload}.{_urlsafe_encode(signature)}"

    def _verify(self, token: str) -> ValidationReceiptClaimsV2:
        try:
            prefix, payload, signature = token.split(".")
        except ValueError as error:
            raise PersistentPlanRecoveryError("validation receipt signature is invalid") from error
        if prefix != _TOKEN_PREFIX:
            raise PersistentPlanRecoveryError("validation receipt signature is invalid")
        signing_input = f"{prefix}.{payload}".encode("ascii")
        expected = hmac.new(self._signing_key, signing_input, hashlib.sha256).digest()
        try:
            supplied = _urlsafe_decode(signature)
        except ValueError as error:
            raise PersistentPlanRecoveryError("validation receipt signature is invalid") from error
        if not hmac.compare_digest(supplied, expected):
            raise PersistentPlanRecoveryError("validation receipt signature is invalid")
        try:
            decoded = json.loads(_urlsafe_decode(payload))
            return ValidationReceiptClaimsV2.model_validate(decoded)
        except (ValueError, json.JSONDecodeError) as error:
            raise PersistentPlanRecoveryError("validation receipt payload is invalid") from error


def plan_snapshot_ids(record: ExecutableStrategyPlanRecordV2) -> tuple[str, ...]:
    return (record.market_data.snapshot_id,)


def snapshot_bindings_hash(values: tuple[SnapshotBindingV2, ...]) -> str:
    """Canonical digest of outer, derived and producer-child authority."""

    ordered = _ordered_snapshot_bindings(values)
    return canonical_hash([item.model_dump(mode="json") for item in ordered])


def _snapshot_bindings_by_kind(
    values: tuple[SnapshotBindingV2, ...],
) -> tuple[
    SnapshotBindingV2,
    SnapshotBindingV2,
    SnapshotBindingV2,
    SnapshotBindingV2,
    tuple[SnapshotBindingV2, ...],
]:
    if type(values) is not tuple or any(type(item) is not SnapshotBindingV2 for item in values):
        raise PersistentPlanRecoveryError("snapshot bindings must be trusted typed values")
    core_values = tuple(item for item in values if item.kind != "producer_child")
    by_kind = {item.kind: item for item in core_values}
    producer_children = tuple(
        sorted(
            (item for item in values if item.kind == "producer_child"),
            key=lambda item: item.snapshot_id,
        )
    )
    if len(core_values) != 4 or set(by_kind) != {
        "security_master",
        "trading_calendar",
        "market_data",
        "composite_snapshot",
    }:
        raise PersistentPlanRecoveryError(
            "exactly one composite, security-master, calendar and market-data snapshot is required"
        )
    if len({item.snapshot_id for item in producer_children}) != len(producer_children):
        raise PersistentPlanRecoveryError("producer child snapshot identities must be unique")
    return (
        by_kind["security_master"],
        by_kind["trading_calendar"],
        by_kind["market_data"],
        by_kind["composite_snapshot"],
        producer_children,
    )


def _ordered_snapshot_bindings(
    values: tuple[SnapshotBindingV2, ...],
) -> tuple[SnapshotBindingV2, ...]:
    security, calendar, market, composite, producer_children = _snapshot_bindings_by_kind(values)
    return security, calendar, market, *producer_children, composite


def _urlsafe_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _urlsafe_decode(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as error:
        raise ValueError("invalid base64url") from error


__all__ = [
    "PersistentPlanRecoveryError",
    "ValidationReceiptServiceV2",
    "snapshot_bindings_hash",
]
